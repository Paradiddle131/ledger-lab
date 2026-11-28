"""Answer integrity (`agent/integrity.py`): the model writes no figure, `ask` renders each one from
a cell of a run the turn cites, the logged query is re-run as the asking persona, and a reply that
cannot be shown that way is withheld and recorded as withheld.

The asks run on the scripted model (`agent/model.py`) or a hand-written one, through the
`as_persona` fixture (`tests/conftest.py`), so every query runs on a database compiled for the
asking persona over the sample build; a verified query of invented figures (`INVENTED`) returns
chosen cells there. Nothing here calls a model or GCP. The pure checks run on hand-made citations
and on a small database of invented tables.
"""

import json
import re
import shutil
import sys
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

import agent.integrity
from access.compile import quote
from agent.ask import CAPPED, MAX_TOOL_CALLS, Answer, Audit, ask, load_context
from agent.audit import FileLog, bind, cell_text, new_salt, read_log, seal, sealed_result
from agent.integrity import WITHHELD, Logged, logged
from agent.model import Citation, Model, Reply, ToolCall, Turn, VerifiedQuery, model_from_config

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
CHICAGO = "accountant-chicago"
AUDITOR = "auditor"
UK = "accountant-uk-transport"
QUESTION = "What share of the City of Chicago's served payment rows repeat another served row?"
BY_YEAR = "What was spent on each basis in each fiscal year, in euros at constant rates?"
BY_BASIS = "What was spent on each basis, in euros at constant rates?"
BY_ENTITY = (
    "What did each entity spend, in euros at constant rates, where all its figures share one basis?"
)
BY_MONTH = (
    "By month of payment date, what share of the City of Chicago's served payment rows repeat"
    " another served row?"
)
QUERY_OF = {
    QUESTION: "duplicate-payment-rate",
    BY_YEAR: "constant-currency-spend-by-basis-and-fiscal-year",
    BY_BASIS: "constant-currency-spend-by-basis",
    BY_ENTITY: "constant-currency-spend-by-entity",
    BY_MONTH: "duplicate-payment-rate-by-month",
}
SHARE = "{duplicate-payment-rate:1:duplicate_payment_rate}"
OUTSIDE = "Which vendor was paid the most?"
# A question whose verified query reads a table no persona's database holds, so its run fails.
UNREADABLE = "What share of the City of Chicago's fixed contract awards has been paid?"
WRITTEN = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
# A verified query of invented figures, put to the tool policy as a metric Chicago's accountant
# may read, so a test chooses the cells a run returns on the persona's own database.
INVENTED_QUESTION = "What are the invented figures?"
INVENTED_SQL = (
    "select cast('12.25' as double) as a, 1 as b, 0 as c, 12250 as d,"
    " cast('52.21' as double) as e, cast('-12.25' as double) as f"
)
INVENTED = {
    "invented-figures": VerifiedQuery(
        "invented-figures", INVENTED_QUESTION, "duplicate_payment_rate", 1, INVENTED_SQL
    )
}


def cell(column: str) -> str:
    """A reference to a cell of the invented figures' one row."""
    return f"{{invented-figures:1:{column}}}"


def records(audit: Audit) -> list[dict[str, Any]]:
    assert isinstance(audit.log, FileLog)
    return read_log(audit.log.path.read_bytes())


def kept(audit: Audit, seq: int) -> dict[str, Any]:
    assert isinstance(audit.log, FileLog)
    return json.loads((audit.log.kept / f"{seq}.json").read_text())


def last_logged(audit: Audit) -> tuple[Logged, ...]:
    record = records(audit)[-1]
    return logged(record, kept(audit, record["seq"]))


def recorded_answer(audit: Audit) -> Any:
    """The answer text the turn's record seals, as the kept value opens it."""
    record = records(audit)[-1]
    salt, value = kept(audit, record["seq"])["answer"]
    assert seal(salt, value) == record["answer"]
    return value


class Saying:
    """Runs the query it is given (none for None), then replies with a fixed text."""

    def __init__(self, query: str | None, text: Any) -> None:
        self._query, self._text = query, text

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return () if self._query is None else (ToolCall(self._query),)

    def step(self, turn: Turn) -> ToolCall | Reply:
        if self._query is None or turn.results:
            return Reply(self._text)
        return ToolCall(self._query)


class Recording:
    """A model's steps unchanged, with the text of its reply kept (`said`) and, when `change` is
    given, that text changed by it before `ask` sees it."""

    def __init__(self, model: Model, change: Callable[[str], str] | None = None) -> None:
        self._model, self._change = model, change
        self.said = ""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return self._model.plan(turn)

    def step(self, turn: Turn) -> ToolCall | Reply:
        step = self._model.step(turn)
        if isinstance(step, ToolCall):
            return step
        self.said = step.text if self._change is None else self._change(step.text)
        return Reply(self.said)


@pytest.fixture
def scripted() -> Model:
    return model_from_config({}, load_context())


def shown(text: str, as_persona: AsPersona, audit: Audit) -> str:
    """What `ask` shows for a reply of `text` to the invented figures' question, the recorded
    answer checked to be that same text."""
    model = Saying("invented-figures", text)
    answer = ask(
        CHICAGO,
        INVENTED_QUESTION,
        as_persona=as_persona,
        audit=audit,
        model=model,
        queries=INVENTED,
    )
    assert len(answer.citations) == 1
    assert recorded_answer(audit) == answer.text
    expected = "abstained" if answer.text == WITHHELD else "answered"
    assert records(audit)[-1]["outcome"] == expected
    return answer.text


@pytest.mark.parametrize(
    "text",
    [
        "The total is ‮52.21‬.",
        "paid ‮1234‬",
        f"The total is ‮{cell('e')}‬.",
        f"The total is ⁧{cell('e')}⁩.",
        f"The total is ‏{cell('e')}.",
        f"The total is {cell('e')}؜.",
    ],
)
def test_a_bidi_control_around_a_figure_is_withheld(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    """A bidi override or isolate, a direction mark or the Arabic letter mark beside a figure is
    withheld: the text shown is the text recorded, so no shown text differs from a checked one."""
    assert shown(text, as_persona, audit) == WITHHELD


@pytest.mark.parametrize(
    "text",
    [
        "The share is О.ОƼ.",
        f"paid ⲻ {cell('a')}",
        f"paid −{cell('a')}",
        f"paid － {cell('a')}",
        "paid １２.２５",
        "paid \U0001d7cf\U0001d7d0",
        f"The share is {cell('a')}⁰.",
    ],
)
def test_a_look_alike_of_a_digit_or_a_minus_is_withheld(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    """Cyrillic O and Latin tone five for 0.05, a Coptic letter or a Unicode minus before a
    positive figure, fullwidth and mathematical digits, a superscript zero: each is withheld."""
    assert shown(text, as_persona, audit) == WITHHELD


@pytest.mark.parametrize(
    "text",
    [
        "The change is mіnus 12.25",
        "a lоss of 12.25",
        "a net-loss of 12.25",
        "down about 12.25",
    ],
)
def test_a_sign_word_beside_a_figure_the_model_writes_is_withheld(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    assert shown(text, as_persona, audit) == WITHHELD


@pytest.mark.parametrize(
    "text",
    ["spend was 12.25 in millions", "spend was 12.25 USD million", "in thousands: 12250"],
)
def test_a_scale_word_beside_a_figure_the_model_writes_is_withheld(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    assert shown(text, as_persona, audit) == WITHHELD


@pytest.mark.parametrize(
    "text",
    [
        "The spend was 1'000.",
        "The spend was 1 000.",
        "The spend was 1’000.",
        "The spend was 1\xa0000.",
        f"The spend was {cell('b')} {cell('c')}.",
        f"The spend was {cell('b')}'{cell('c')}.",
        f"The spend was {cell('b')}, {cell('d')}.",
        f"The spend was {cell('b')},{cell('d')}.",
        f"The spend was {cell('b')}.{cell('c')}.",
        f"The spend was {cell('b')}{cell('c')}.",
        f"The spend was {cell('b')}\n{cell('c')}.",
        f"The spend was {cell('b')} = {cell('c')}.",
    ],
)
def test_two_figures_with_no_letter_between_are_withheld(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    """A thousands group written by the model, or two references parted by nothing but space or
    punctuation, could read as one figure: withheld."""
    assert shown(text, as_persona, audit) == WITHHELD


