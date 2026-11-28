"""Answer integrity (`agent/integrity.py`): the model writes no digit, `ask` renders each figure
from a cell of a run the turn cites, the logged query is re-run as the asking persona, and a reply
that cannot be shown that way is withheld and recorded as withheld.

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
from agent.integrity import (
    REFERENCE,
    WITHHELD,
    Logged,
    Unrendered,
    check,
    logged,
    render,
    rendered,
    reproduce,
    same,
)
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


# The logged query reproduces the answer


@pytest.mark.parametrize(
    ("asker", "question"),
    [
        (CHICAGO, QUESTION),
        (AUDITOR, BY_YEAR),
        (AUDITOR, BY_BASIS),
        (AUDITOR, BY_ENTITY),
        (AUDITOR, BY_MONTH),
    ],
    ids=[
        "a-share",
        "decimals-years-and-coded-text",
        "decimals-and-text",
        "text-and-a-null",
        "months-and-zeros",
    ],
)
def test_logged_query_reproduces_the_answer(
    as_persona: AsPersona, scripted: Model, audit: Audit, asker: str, question: str
) -> None:
    """An answer of the scripted model: its reply names every cell of the run it cites by a
    reference and writes no digit; the text shown and recorded is that reply with each reference
    rendered from its cell; and the query its record logged, re-run as the record's persona,
    returns that run's rows and matches the record's sealed result."""
    model = Recording(scripted)
    answer = ask(asker, question, as_persona=as_persona, audit=audit, model=model)
    assert records(audit)[-1]["outcome"] == "answered"
    (citation,) = answer.citations
    assert citation.rows, "a run with no rows proves nothing here"
    assert not re.search(r"[0-9]", REFERENCE.sub("", model.said))
    result = check(question, model.said, answer.citations, load_context())
    assert result.failures == ()
    assert answer.text == result.text == recorded_answer(audit)
    named = {(r.row, r.column): r.rendered for r in result.references}
    assert named == {
        (number, column): rendered(value)
        for number, row in enumerate(citation.rows, start=1)
        for column, value in zip(citation.columns, row, strict=True)
    }
    rerun = reproduce(answer, last_logged(audit), as_persona)
    assert rerun.failures == ()
    assert rerun.reproduced == (citation.query,)


def test_a_perturbed_number_in_a_scripted_answer_is_caught(
    as_persona: AsPersona, scripted: Model, audit: Audit
) -> None:
    """The scripted reply with its reference replaced by the figure written out and a 7 appended,
    or by the exact figure written out, or with the reference's row or column changed: each is
    withheld, the turn recorded abstained with the run it made."""
    share = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit).citations[0].rows[0][0]
    changes = [
        lambda text: text.replace(SHARE, f"{share!r}7"),
        lambda text: text.replace(SHARE, repr(share)),
        lambda text: text.replace(SHARE, SHARE.replace(":1:", ":2:")),
        lambda text: text.replace(SHARE, SHARE.replace("_rate}", "_rates}")),
    ]
    for change in changes:
        model = Recording(scripted, change)
        answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=model)
        assert model.said and model.said != answer.text
        assert answer.text == WITHHELD and not re.search(r"\d", answer.text)
        record = records(audit)[-1]
        assert (record["outcome"], len(record["runs"])) == ("abstained", 1)
        assert recorded_answer(audit) == WITHHELD


def test_each_figure_of_a_many_row_answer_written_by_the_model_is_withheld(
    as_persona: AsPersona, scripted: Model, audit: Audit
) -> None:
    """Each reference of the auditor's answer by basis and fiscal year, replaced on its own by its
    own rendering or by that rendering with a 7 appended, gets the reply withheld; the reply as
    the model wrote it renders."""
    model = Recording(scripted)
    answer = ask(AUDITOR, BY_YEAR, as_persona=as_persona, audit=audit, model=model)
    queries = load_context()
    references = check(BY_YEAR, model.said, answer.citations, queries).references
    assert len(references) >= 9
    figures = [r for r in references if re.search(r"[0-9]", r.rendered)]
    assert len(figures) >= 6
    for reference in figures:
        for written in (reference.rendered, reference.rendered + "7"):
            text = model.said.replace(reference.written, written, 1)
            assert check(BY_YEAR, text, answer.citations, queries).text is None, text


def test_a_changed_warehouse_is_caught_on_re_run(
    as_persona: AsPersona, scripted: Model, audit: Audit, tmp_path: Path
) -> None:
    """The persona's database with every repeat flag flipped after the answer: the re-run of the
    logged query returns other rows than the cited run and than the record's seal."""
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    runs = last_logged(audit)

    def changed(persona: str) -> duckdb.DuckDBPyConnection:
        connection = as_persona(persona)
        row = connection.execute(
            "select path from duckdb_databases() where database_name = current_database()"
        ).fetchone()
        assert row is not None
        connection.close()
        copy = tmp_path / "changed.duckdb"
        shutil.copy(row[0], copy)
        with duckdb.connect(str(copy)) as write:
            (schema,) = write.execute(
                "select table_schema from information_schema.tables where table_name = 'payments'"
            ).fetchone() or ("",)
            write.execute(f"update {quote(schema)}.payments set is_repeat = not is_repeat")  # noqa: S608
        return duckdb.connect(str(copy), read_only=True)

    report = reproduce(answer, runs, changed)
    assert [(f.query, f.reason) for f in report.failures] == [
        ("duplicate-payment-rate", "the re-run returned other rows than the cited run"),
        ("duplicate-payment-rate", "the re-run does not match the record's sealed result"),
    ]
    assert report.reproduced == ()


# No figure where no verified query covers the question


@pytest.mark.parametrize("model", ["scripted", "quotes-another-query"])
def test_a_question_outside_the_metrics_gets_an_abstention(
    as_persona: AsPersona, audit: Audit, model: str
) -> None:
    """A question no verified query is worded as: the scripted model calls no tool; a model that
    runs an unrelated verified query and references its cell gets the reply withheld. Either way
    no digit is shown and the turn is abstained."""
    chosen = (
        model_from_config({}, load_context())
        if model == "scripted"
        else Saying("duplicate-payment-rate", f"The top vendor was paid {SHARE}.")
    )
    answer = ask(CHICAGO, OUTSIDE, as_persona=as_persona, audit=audit, model=chosen)
    assert answer.text == WITHHELD and not re.search(r"\d", answer.text)
    assert records(audit)[-1]["outcome"] == "abstained"


def test_only_a_verified_question_as_worded_is_answered(
    as_persona: AsPersona, audit: Audit
) -> None:
    """The verified question with its case, white space and compatibility forms changed is
    answered; a paraphrase of it, with the same run and reference, is withheld."""
    # Upper case, doubled spaces and a fullwidth letter, which NFKC makes the plain one.
    reworded = "  " + QUESTION.upper().replace(" ", "  ").replace("ROWS", "ＲOWS")
    assert same(reworded, QUESTION)
    paraphrase = "How often does a served Chicago payment row repeat another?"
    for question, answered in [(reworded, True), (paraphrase, False)]:
        model = Saying("duplicate-payment-rate", f"The share is {SHARE}.")
        answer = ask(CHICAGO, question, as_persona=as_persona, audit=audit, model=model)
        assert (answer.text != WITHHELD, records(audit)[-1]["outcome"]) == (
            answered,
            "answered" if answered else "abstained",
        )


@pytest.mark.parametrize(
    "text",
    [
        "The top vendor was paid about one point two million, ¹² dollars.",
        f"The top vendor was paid {SHARE}.",
        "The top vendor was paid a lot.",
    ],
    ids=["figures-in-words-and-numerals", "a-reference", "prose"],
)
def test_a_reply_with_no_cited_run_is_withheld_whatever_it_says(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    model = Saying(None, text)
    answer = ask(CHICAGO, OUTSIDE, as_persona=as_persona, audit=audit, model=model)
    assert answer.text == WITHHELD
    assert records(audit)[-1]["outcome"] == "abstained"


@pytest.mark.parametrize("cap", ["tool-call-cap", "refusal-cap"])
def test_a_turn_stopped_at_the_cap_shows_its_own_message(
    as_persona: AsPersona, audit: Audit, cap: str
) -> None:
    """At the tool-call cap (every call planned) and at the refusal cap (no call planned), the
    turn shows `ask`'s own fixed message, which names the cap with digits, unchecked."""

    class Looping:
        def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
            if cap == "refusal-cap":
                return ()
            return (ToolCall("duplicate-payment-rate"),) * (MAX_TOOL_CALLS + 1)

        def step(self, turn: Turn) -> ToolCall | Reply:
            return ToolCall("duplicate-payment-rate")

    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Looping())
    record = records(audit)[-1]
    if cap == "tool-call-cap":
        assert answer.text == f"the model made no reply within {MAX_TOOL_CALLS} tool calls"
        assert (record["outcome"], record["reason"]) == ("stopped", "model-call-cap")
    else:
        assert answer.text == CAPPED and re.search(r"\d", CAPPED)
        assert (record["outcome"], record["reason"]) == ("stopped", "model-refusal-cap")


@pytest.mark.parametrize(
    "cause", ["an-unrendered-cell", "a-check-that-raises", "a-reply-that-is-no-string"]
)
def test_a_reply_the_check_cannot_read_is_withheld_and_recorded(
    as_persona: AsPersona, audit: Audit, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    """A cell the renderer does not write, a check that raises and a reply that is not a string
    each withhold the reply, and the turn's record is written, abstained with its run."""
    text: Any = f"The share is {SHARE}."
    if cause == "an-unrendered-cell":

        def unrendered(_: Any) -> str:
            raise Unrendered("a cell the renderer does not write")

        monkeypatch.setattr(agent.integrity, "rendered", unrendered)
    elif cause == "a-check-that-raises":

        def raising(*_: Any) -> Any:
            raise RuntimeError("the check failed")

        monkeypatch.setattr(agent.integrity, "render", raising)
    else:
        text = 0.04054054054054054
    model = Saying("duplicate-payment-rate", text)
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=model)
    assert answer.text == WITHHELD
    record = records(audit)[-1]
    assert (record["outcome"], len(record["runs"])) == ("abstained", 1)
    assert recorded_answer(audit) == WITHHELD


@pytest.mark.parametrize("text", ["", "   \n "])
def test_an_empty_reply_is_withheld(as_persona: AsPersona, audit: Audit, text: str) -> None:
    assert shown(text, as_persona, audit) == WITHHELD


def test_a_reply_of_prose_alone_is_shown(as_persona: AsPersona, audit: Audit) -> None:
    """A stated limit: prose with no reference states no figure and is shown on a covering run."""
    assert shown("The share is high.", as_persona, audit) == "The share is high."


# The five forms a review of reading the model's own figures found: each either shows the cell's
# own rendering or is withheld. The first of each pair is the review's own reproduction, where the
# model writes the figure; the second puts a reference in the figure's place.


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


def test_the_text_shown_is_the_text_recorded(as_persona: AsPersona, audit: Audit) -> None:
    """The reply's references rendered, with no brace left: what `ask` returns and what the
    record seals are that same text."""
    text = shown(f"The total is {cell('e')}, and the loss is {cell('f')}.", as_persona, audit)
    assert text == "The total is 52.21, and the loss is -12.25."


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
def test_a_non_ascii_look_alike_of_a_digit_or_a_minus_is_withheld(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    """Cyrillic O and Latin tone five for 0.05, a Coptic letter or a Unicode minus before a
    positive figure, fullwidth and mathematical digits, a superscript zero: each is withheld."""
    assert shown(text, as_persona, audit) == WITHHELD


@pytest.mark.parametrize(
    "text",
    [
        "The share is O.OS.",
        "The share is lO,ZS now.",
        f"The spend was {cell('b')} OOO OOO.",
        f"The spend was {cell('a')} and before that lO.ZS",
        f"The spend was {cell('b')}. OOO",
        f"It was SOS {cell('a')}.",
    ],
)
def test_a_word_of_digit_shaped_letters_with_a_point_or_beside_a_reference_is_withheld(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    """A word made only of O, o, l, I, S, Z or B is withheld when it holds a `.` or `,` between two
    of its letters, or is the word right before or after a reference."""
    assert shown(text, as_persona, audit) == WITHHELD


@pytest.mark.parametrize(
    "text",
    [
        "The spend was lOO thousand.",
        "It is OOO.",
        "The spend was forty-two thousand.",
        f"The share is {cell('a')}, and the spend was OO-OO.",
    ],
)
def test_a_number_word_or_a_look_alike_not_beside_a_reference_and_with_no_point_is_shown(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    """Stated limits: number words are not read, and a word of digit-shaped letters holding no `.`
    or `,` between its letters and not beside a reference is prose, so each of these is shown."""
    assert shown(text, as_persona, audit) == text.replace(cell("a"), "12.25")


def test_an_honest_word_of_digit_shaped_letters_beside_a_reference_is_withheld(
    as_persona: AsPersona, audit: Audit
) -> None:
    """A stated limit on the safe side: the pronoun I beside a reference is withheld."""
    assert shown(f"It is {cell('a')}, I think.", as_persona, audit) == WITHHELD
    assert shown(f"I think it is {cell('a')}.", as_persona, audit) == "I think it is 12.25."


def test_no_character_outside_ascii_passes_in_the_prose() -> None:
    """Every code point outside ASCII, each Unicode digit, numeral and letter that folds to a digit
    under NFKC among them, is reported where the prose holds it."""
    citation = cited(((Decimal("12.25"),),))
    chars = [chr(code) for code in range(0x80, sys.maxunicode + 1) if not 0xD800 <= code <= 0xDFFF]
    digits = [
        c
        for c in chars
        if unicodedata.category(c) in ("Nd", "No", "Nl")
        or any(d.isdigit() for d in unicodedata.normalize("NFKC", c))
    ]
    assert len(digits) > 1000
    for start in range(0, len(chars), 4096):
        chunk = chars[start : start + 4096]
        result = render(f"It is {{invented:1:c0}} {''.join(chunk)}", (citation,))
        assert result.text is None
        assert [f.number for f in result.failures] == chunk
    assert rendering(f"It is {{invented:1:c0}} {''.join(digits)}", citation) is None


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


@pytest.mark.parametrize("words", ["a net-loss of {}", "down about {}", "minus {}"])
def test_a_sign_word_beside_a_reference_shows_the_cells_own_sign(
    as_persona: AsPersona, audit: Audit, words: str
) -> None:
    """A stated limit: the sign word is prose and is shown as written; the figure beside it is the
    cell's own, with no sign for the positive cell and a minus for the negative one."""
    text = f"The change is {words.format(cell('a'))}, then {words.format(cell('f'))}."
    expected = f"The change is {words.format('12.25')}, then {words.format('-12.25')}."
    assert shown(text, as_persona, audit) == expected


@pytest.mark.parametrize(
    "text",
    ["spend was 12.25 in millions", "spend was 12.25 USD million", "in thousands: 12250"],
)
def test_a_scale_word_beside_a_figure_the_model_writes_is_withheld(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    assert shown(text, as_persona, audit) == WITHHELD


def test_a_scale_word_beside_a_reference_shows_the_cells_own_figure(
    as_persona: AsPersona, audit: Audit
) -> None:
    """A stated limit: the scale word is prose and shown as written, beside the cell's figure."""
    text = f"spend was {cell('a')} in millions, in thousands: {cell('d')}"
    assert shown(text, as_persona, audit) == "spend was 12.25 in millions, in thousands: 12250"


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


def test_two_references_parted_by_a_word_are_shown(as_persona: AsPersona, audit: Audit) -> None:
    text = f"The spend was {cell('b')} and {cell('d')}, or {cell('c')}."
    assert shown(text, as_persona, audit) == "The spend was 1 and 12250, or 0."


# Signs written around a figure: every form the earlier reviews of the sign rule found.
UNREAD = [
    "−{}",
    "‐{}",
    "–{}",
    "—{}",
    "˗{}",
    "➖{}",
    "⸺{}",
    "−⁠{}",
    "{}-",
    "{}−",
    "({})",
    "( {} )",
    "( {}",
    "(​{})",
    "(−{})",
    "﹙{}﹚",
    "（{}）",
    "-${}",
    "(${})",
    "($ {})",
    "−€{}",
    "- {}",
    "-\xa0{}",
    "- USD {}",
    "USD-{}",
    "(USD {})",
    "(USDEUR {})",
    "{} USD-",
    "-US${}",
    "-A${}",
    "-HK${}",
    "-R$ {}",
    "-Rs {}",
    "-usd {}",
    "-ABCDEF {}",
    "-'{}'",
    "-**{}**",
    "(**{}**)",
    "({} million)",
    "({}%)",
    "({}k)",
    "minus {}",
    "negative {}",
    "Negative: {}",
    "down {}",
    "down by {}",
    "a loss of {}",
    "a deficit of {}",
    "{} CR",
    "{} DR",
    "{} negative",
    "{} million",
    "{} bn",
    "{}k",
    "{} percent",
    "{} per cent",
]
FOLDED_MINUS = ["﹣{}", "－{}", "-​{}", "-\xad{}", "-⁠{}", "-﻿{}"]
# The forms whose characters are all prose: a reference in their place is shown, the words read
# as the stated limit says.
IN_WORDS = {
    "minus {}",
    "negative {}",
    "Negative: {}",
    "down {}",
    "down by {}",
    "a loss of {}",
    "a deficit of {}",
    "{} CR",
    "{} DR",
    "{} negative",
    "{} million",
    "{} bn",
    "{} percent",
    "{} per cent",
}


@pytest.mark.parametrize("form", UNREAD + FOLDED_MINUS)
def test_a_sign_form_around_a_figure_the_model_writes_is_withheld(
    as_persona: AsPersona, audit: Audit, form: str
) -> None:
    assert shown(f"The figure is {form.format('12.25')}.", as_persona, audit) == WITHHELD


@pytest.mark.parametrize("form", UNREAD + FOLDED_MINUS)
def test_a_sign_form_around_a_reference_is_withheld_or_shows_the_cell(form: str) -> None:
    """Each form with a reference in the figure's place: withheld when it holds a symbol, a
    character outside ASCII or a character touching the reference; shown, with the cell's own
    figure, only when it is words."""
    citation = cited(((Decimal("12.25"),),))
    result = render(f"The figure is {form.format('{invented:1:c0}')}.", (citation,))
    if form in IN_WORDS:
        assert result.text == f"The figure is {form.format('12.25')}."
    else:
        assert result.text is None


# The rule, on hand-made citations


def cited(
    rows: tuple[tuple[Any, ...], ...],
    *,
    query: str = "invented",
    columns: tuple[str, ...] | None = None,
    fresh: datetime | None = WRITTEN,
) -> Citation:
    if columns is None:
        columns = tuple(f"c{i}" for i in range(len(rows[0]))) if rows else ("c0",)
    return Citation(query, "invented_metric", 3, "select 1", columns, rows, fresh)


def rendering(text: str, *citations: Citation) -> str | None:
    return render(text, citations).text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.04054054054054054, "0.04054054054054054"),
        (-12.25, "-12.25"),
        (1e16, "10000000000000000"),
        (1e-7, "0.0000001"),
        (-0.0, "0.0"),
        (0.0, "0.0"),
        (Decimal("17947.690000"), "17947.690000"),
        (Decimal("-0.00"), "0.00"),
        (Decimal("1E+2"), "100"),
        (Decimal("-12.25"), "-12.25"),
        (2026, "2026"),
        (-5, "-5"),
        (0, "0"),
        (None, "null"),
        (True, "true"),
        (False, "false"),
        (date(2026, 10, 1), "2026-10-01"),
        (datetime(2026, 10, 1), "2026-10-01T00:00:00"),
        (time(3, 4, 5), "03:04:05"),
        ("payments-over-25k", "payments-over-25k"),
        ("12.25", "12.25"),
    ],
)
def test_a_cell_is_rendered_in_one_fixed_format(value: Any, text: str) -> None:
    """A number in positional notation with every digit it carries, a minus below zero and no sign
    at zero, none rounded; a null, a bool, a date or time as a fixed word or its ISO text; a text
    cell as its own text."""
    assert rendered(value) == text
    assert rendering("It is {invented:1:c0}.", cited(((value,),))) == f"It is {text}."


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("1E+1000000"),
        Decimal("1E-1000000"),
        5e-324,
        10**63,
        -(10**63),
        "",
        "‮12.25",
        "-12·25",
        "a {brace}",
        "a\nline",
        "x" * 201,
        uuid.UUID(int=1),
        b"12",
        [1, 2],
    ],
)
def test_a_cell_the_renderer_does_not_write_withholds_the_reply(value: Any) -> None:
    with pytest.raises(Unrendered):
        rendered(value)
    result = render("It is {invented:1:c0}.", (cited(((value,),)),))
    assert result.text is None
    assert "not rendered" in result.failures[0].reason


def test_the_longest_numbers_rendered() -> None:
    assert rendered(10**63 - 1) == "9" * 63
    assert rendered(-(10**63 - 1)) == "-" + "9" * 63
    assert len(rendered(Decimal("1E+61"))) == 62


@pytest.mark.parametrize(
    "text",
    [
        "{invented:1:c0",
        "invented:1:c0}",
        "{invented:01:c0}",
        "{invented:0:c0}",
        "{invented:-1:c0}",
        "{invented:+1:c0}",
        "{invented:1.0:c0}",
        "{Invented:1:c0}",
        "{invented:1:C0}",
        "{invented: 1:c0}",
        "{ invented:1:c0}",
        "{invented:1:c0 }",
        "{invented::c0}",
        "{invented:1}",
        "{invented:1:c0:c0}",
        "{{invented:1:c0}}",
        "{invented:1:c0}}",
        "{{invented:1:c0}",
        "{invented:{invented:1:c0}:c0}",
        "{invented:１:c0}",
        "{invented:1:cа}",
        "{invented:1234567890:c0}",
    ],
)
def test_a_malformed_reference_is_withheld_and_nothing_of_it_rendered(text: str) -> None:
    citation = cited(((Decimal("12.25"),),))
    result = render(f"It is {text} now.", (citation,))
    assert result.text is None
    assert result.failures


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("-", ""),
        ("(", ")"),
        ("$", ""),
        ("+", ""),
        ("", "%"),
        ('"', '"'),
        ("'", "'"),
        ("_", "_"),
        ("*", "*"),
        ("a", ""),
        ("", "a"),
        ("", "k"),
        ("=", ""),
        (":", ""),
        ("", ".a"),
        ("", ".."),
        ("", ".-"),
        ("", "-"),
        ("", "'"),
        ("\t", ""),
        ("\r", ""),
        ("", "\x08\x08"),
        ("\x1b[7m", ""),
        ("#", ""),
        ("~", ""),
        ("", "/"),
        ("", "e"),
    ],
)
def test_a_character_touching_a_reference_is_withheld(before: str, after: str) -> None:
    citation = cited(((Decimal("12.25"),),))
    assert rendering(f"It is {before}{{invented:1:c0}}{after} now", citation) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{invented:1:c0}", "12.25"),
        ("It is {invented:1:c0}", "It is 12.25"),
        ("It is {invented:1:c0}.", "It is 12.25."),
        ("It is {invented:1:c0}, it is.", "It is 12.25, it is."),
        ("It is {invented:1:c0}!\nYes.", "It is 12.25!\nYes."),
        ("It is:\n{invented:1:c0} now", "It is:\n12.25 now"),
        ("c_zero = {invented:1:c0}; well-known", "c_zero = 12.25; well-known"),
        ('"It is" {invented:1:c0}?', '"It is" 12.25?'),
    ],
)
def test_a_reference_set_off_by_space_or_closing_punctuation_is_rendered(
    text: str, expected: str
) -> None:
    assert rendering(text, cited(((Decimal("12.25"),),))) == expected


@pytest.mark.parametrize(
    "text",
    ["a - b", "a -b", "a- b", "-a", "a-", "a--b", "a-б", "1", "a\tb", "a#b", "a(b)", "a%"],
)
def test_prose_holds_a_hyphen_only_between_two_letters(text: str) -> None:
    assert rendering(text, cited(((1,),))) is None
    assert rendering("well-known and so-called", cited(((1,),))) == "well-known and so-called"


def test_each_character_the_prose_may_not_hold_is_named_with_its_reason() -> None:
    result = render("It is 7 ١ { now", (cited(((1,),)),))
    assert [(f.number, f.reason) for f in result.failures] == [
        ("7", "a digit outside a reference: the model writes no figure"),
        ("١", "a character that is not ASCII (ARABIC-INDIC DIGIT ONE) outside a reference"),
        ("{", "a brace that is not part of a whole reference"),
    ]
    (failure,) = render(0.5, (cited(((0.5,),)),)).failures
    assert failure.reason == "the reply is not a string"


def test_a_reference_names_one_run_of_this_turn_one_row_and_one_column() -> None:
    """A query no run of the turn cites (another turn's, an uncited one), a query cited twice, a
    row past the run's last, a column the run lacks or returns twice: each withheld."""
    one = cited(((Decimal("12.25"), 1), (Decimal("3.5"), 2)))
    other = cited(((Decimal("7"),),), query="other")
    assert rendering("It is {invented:2:c1} and {other:1:c0}.", one, other) == "It is 2 and 7."
    assert rendering("It is {another:1:c0}.", one, other) is None
    assert rendering("It is {invented:1:c0}.", one, one) is None
    assert rendering("It is {invented:3:c0}.", one) is None
    assert rendering("It is {invented:1:c2}.", one) is None
    twice = cited(((1, 2),), columns=("c0", "c0"))
    assert rendering("It is {invented:1:c0}.", twice) is None
    assert rendering("It is {invented:1:c0}.", cited((), columns=("c0",))) is None
    assert rendering("It is {invented:1:c0}.") is None


def test_check_needs_a_covering_run_with_a_freshness_for_each_reference() -> None:
    """A reference to a run that does not cover the question, or to one whose citation carries no
    freshness, is withheld; so is any reply on a turn with no covering run."""
    queries = {
        "invented": VerifiedQuery("invented", "Asked?", "m", 3, "select 1"),
        "other": VerifiedQuery("other", "Another?", "m", 3, "select 1"),
    }
    covering = cited(((Decimal("12.25"),),))
    other = cited(((Decimal("7"),),), query="other")
    assert check("Asked?", "It is {invented:1:c0}.", (covering, other), queries).text == (
        "It is 12.25."
    )
    result = check("Asked?", "It is {other:1:c0}.", (covering, other), queries)
    assert (result.text, [f.reason for f in result.failures]) == (
        None,
        ["the run does not cover the question"],
    )
    stale = replace(covering, freshness=None)
    result = check("Asked?", "It is {invented:1:c0}.", (stale,), queries)
    assert [f.reason for f in result.failures] == ["the run's citation carries no freshness"]
    assert check("Asked?", "It is high.", (other,), queries).text is None
    assert check("Asked?", "It is high.", (), queries).text is None


class TwoQueries:
    """Plans the verified query of the question and the by-month one, then replies with a
    reference to a cell of the by-month run."""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return (ToolCall("duplicate-payment-rate"), ToolCall("duplicate-payment-rate-by-month"))

    def step(self, turn: Turn) -> ToolCall | Reply:
        calls = self.plan(turn)
        if len(turn.results) < len(calls):
            return calls[len(turn.results)]
        return Reply("The share is {duplicate-payment-rate-by-month:2:duplicate_payment_rate}.")


def test_a_figure_from_a_run_that_does_not_cover_the_question_is_withheld(
    as_persona: AsPersona, audit: Audit
) -> None:
    """Both runs are cited, one covering the question; a reference to the other is withheld."""
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=TwoQueries())
    assert [c.query for c in answer.citations] == [
        "duplicate-payment-rate",
        "duplicate-payment-rate-by-month",
    ]
    assert answer.text == WITHHELD
    assert records(audit)[-1]["outcome"] == "abstained"


@pytest.mark.parametrize(
    ("question", "query"),
    [(OUTSIDE, None), (UNREADABLE, "spend-against-award")],
    ids=["no-tool-call", "the-run-failed"],
)
def test_a_number_with_no_cited_run_is_withheld(
    as_persona: AsPersona, audit: Audit, question: str, query: str | None
) -> None:
    """A model that replies with figures, or a reference, where no run returned (it called no
    tool, or its query failed on the persona's database) gets no number shown, and the turn is
    abstained."""
    for text in [
        "The top vendor was paid 1,234,567.89 in 2025, 83% of the total.",
        "The top vendor was paid {spend-against-award:1:spend}.",
    ]:
        model = Saying(query, text)
        answer = ask(CHICAGO, question, as_persona=as_persona, audit=audit, model=model)
        assert answer.citations == ()
        assert answer.text == WITHHELD and not re.search(r"\d", answer.text)
        record = records(audit)[-1]
        assert (record["outcome"], record["runs"]) == ("abstained", [])


def test_the_scripted_reply_to_a_failed_run_is_withheld(
    as_persona: AsPersona, scripted: Model, audit: Audit
) -> None:
    """The scripted model repeats the engine's error, which names a line number; no run returned,
    so no number is shown."""
    answer = ask(CHICAGO, UNREADABLE, as_persona=as_persona, audit=audit)
    (refusal,) = answer.refusals
    assert re.search(r"\d", refusal.reason)
    assert answer.text == WITHHELD


@pytest.mark.parametrize("text", ["About 0.5 of the rows repeat.", "It is 0.04054054054054054."])
def test_a_figure_the_model_writes_itself_is_withheld(
    as_persona: AsPersona, audit: Audit, text: str
) -> None:
    """A figure no cell holds, and the covering cell's own figure, written by the model: the model
    writes no figure, so each is withheld."""
    model = Saying("duplicate-payment-rate", text)
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=model)
    assert len(answer.citations) == 1
    assert answer.text == WITHHELD
    assert records(audit)[-1]["outcome"] == "abstained"


# The re-run, on a database of invented tables


def crafted(path: Path, *statements: str) -> Path:
    with duckdb.connect(str(path)) as build:
        for statement in statements:
            build.execute(statement)
    return path


@pytest.fixture
def invented(tmp_path: Path) -> tuple[Answer, tuple[Logged, ...], AsPersona]:
    """An answer citing `select x from t where x > 1` run as Chicago's accountant on an invented
    database, the run as its record would log it, and a connection-maker opening that database."""
    database = crafted(tmp_path / "t.duckdb", "create table t as select * from range(4) r(x)")

    def connect(persona: str) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(database), read_only=True)

    sql = "select x from t where x > 1"
    text, parameters = bind(sql, "duckdb")
    rows = ((2,), (3,))
    citation = Citation("invented", "invented_metric", 1, sql, ("x",), rows, WRITTEN)
    answer = Answer(CHICAGO, "?", "2 and 3", (citation,), ())
    salt = new_salt()
    result = seal(salt, sealed_result(("x",), ([cell_text(v) for v in r] for r in rows)))
    return answer, (Logged(text, parameters, CHICAGO, result, salt),), connect


def test_the_invented_answer_reproduces(
    invented: tuple[Answer, tuple[Logged, ...], AsPersona],
) -> None:
    answer, runs, connect = invented
    report = reproduce(answer, runs, connect)
    assert (report.failures, report.reproduced) == ((), ("invented",))


def change_run(runs: tuple[Logged, ...], **fields: Any) -> tuple[Logged, ...]:
    return (replace(runs[0], **fields),)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("another-persona", "the run was logged as another persona than the answer's"),
        ("another-query", "the logged query is not the cited query bound"),
        ("other-values", "the logged values are not the cited query's"),
        ("other-cited-rows", "the re-run returned other rows than the cited run"),
        ("other-cited-columns", "the re-run returned other rows than the cited run"),
        ("another-seal", "the re-run does not match the record's sealed result"),
        ("a-failing-query", "the re-run failed"),
        ("one-run-too-many", "the record logs 2 runs where the answer cites 1"),
    ],
)
def test_a_re_run_that_does_not_reproduce_is_reported(
    invented: tuple[Answer, tuple[Logged, ...], AsPersona], case: str, reason: str
) -> None:
    answer, runs, connect = invented
    (citation,) = answer.citations
    if case == "another-persona":
        runs = change_run(runs, persona=UK)
    elif case == "another-query":
        runs = change_run(runs, text="select x from t where x > 2")
    elif case == "other-values":
        runs = change_run(runs, parameters={"p0": 2})
    elif case == "other-cited-rows":
        answer = replace(answer, citations=(replace(citation, rows=((2,),)),))
    elif case == "other-cited-columns":
        answer = replace(answer, citations=(replace(citation, columns=("y",)),))
    elif case == "another-seal":
        runs = change_run(runs, salt=new_salt())
    elif case == "a-failing-query":
        missing = "select x from missing_table"
        answer = replace(answer, citations=(replace(citation, sql=missing),))
        runs = change_run(runs, text=bind(missing, "duckdb")[0])
    else:
        runs = runs + runs
    report = reproduce(answer, runs, connect)
    assert [f.reason.split(":")[0] for f in report.failures][:1] == [reason]
    assert report.reproduced == ()


@pytest.mark.parametrize("path", ["runs/0/result", "runs/0/query/parameters"])
def test_kept_values_that_do_not_open_their_seals_are_refused(
    as_persona: AsPersona, scripted: Model, audit: Audit, path: str
) -> None:
    ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    record = records(audit)[-1]
    values = kept(audit, record["seq"])
    ((run,),) = (logged(record, values),)
    assert run.parameters, "the query binds a value"
    values[path] = [new_salt(), values[path][1]]
    with pytest.raises(ValueError, match="do(es)? not open"):
        logged(record, values)


def test_a_schema_name_the_tool_does_not_quote_fails_the_re_run(
    invented: tuple[Answer, tuple[Logged, ...], AsPersona], tmp_path: Path
) -> None:
    answer, runs, _ = invented
    database = crafted(
        tmp_path / "odd.duckdb",
        'create schema "Odd\'Name"',
        'create table "Odd\'Name".t as select * from range(4) r(x)',
    )
    report = reproduce(answer, runs, lambda _: duckdb.connect(str(database), read_only=True))
    (failure,) = report.failures
    assert failure.reason.startswith("the re-run failed: a schema name the tool does not quote")


def test_a_run_logged_unlike_its_citation_is_not_re_run(
    invented: tuple[Answer, tuple[Logged, ...], AsPersona],
) -> None:
    answer, runs, _ = invented

    def refusing(persona: str) -> duckdb.DuckDBPyConnection:
        raise AssertionError(f"a connection was opened for {persona}")

    report = reproduce(answer, change_run(runs, persona=UK, parameters={"p0": 2}), refusing)
    assert [(f.query, f.reason) for f in report.failures] == [
        ("invented", "the run was logged as another persona than the answer's"),
        ("invented", "the logged values are not the cited query's"),
    ]


def test_the_re_run_is_made_as_the_logged_persona(
    invented: tuple[Answer, tuple[Logged, ...], AsPersona], tmp_path: Path
) -> None:
    """Each persona gets its own database, with other rows of `t`: the re-run reproduces only on
    the database of the persona the run was logged as."""
    answer, runs, connect = invented
    other = crafted(tmp_path / "other.duckdb", "create table t as select * from range(9) r(x)")

    def by_persona(persona: str) -> duckdb.DuckDBPyConnection:
        if persona == CHICAGO:
            return connect(persona)
        return duckdb.connect(str(other), read_only=True)

    assert reproduce(answer, runs, by_persona).failures == ()


def test_runs_are_compared_with_citations_in_order(
    invented: tuple[Answer, tuple[Logged, ...], AsPersona],
) -> None:
    answer, runs, connect = invented
    (citation,) = answer.citations
    sql = "select x from t where x > 2"
    rows = ((3,),)
    second = replace(citation, query="second", sql=sql, rows=rows)
    salt = new_salt()
    result = seal(salt, sealed_result(("x",), ([cell_text(v) for v in r] for r in rows)))
    text, parameters = bind(sql, "duckdb")
    logged_second = Logged(text, parameters, CHICAGO, result, salt)
    both = replace(answer, citations=(citation, second))
    assert reproduce(both, (*runs, logged_second), connect).reproduced == ("invented", "second")
    swapped = reproduce(both, (logged_second, *runs), connect)
    assert swapped.reproduced == ()
    assert "the logged query is not the cited query bound" in {f.reason for f in swapped.failures}


class Unplanned:
    """Plans nothing, calls the verified query anyway, then replies with a reference."""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return ()

    def step(self, turn: Turn) -> ToolCall | Reply:
        if turn.results:
            return Reply(f"The share is {SHARE}.")
        return ToolCall("duplicate-payment-rate")


@pytest.mark.parametrize(
    ("asker", "unplanned", "outcome"),
    [
        ("agent-operator", False, ("refused", "policy")),
        (CHICAGO, True, ("refused", "unplanned")),
    ],
    ids=["denied", "unplanned"],
)
def test_a_withheld_reply_keeps_the_refusal_the_turn_records(
    as_persona: AsPersona, audit: Audit, asker: str, unplanned: bool, outcome: tuple[str, str]
) -> None:
    """A withheld reply whose call the policy denied is recorded refused for the policy; one whose
    call was unplanned, refused as unplanned."""
    model: Model = Unplanned() if unplanned else Saying("duplicate-payment-rate", f"It is {SHARE}.")
    answer = ask(asker, QUESTION, as_persona=as_persona, audit=audit, model=model)
    assert answer.text == WITHHELD
    record = records(audit)[-1]
    assert (record["outcome"], record["reason"]) == outcome
