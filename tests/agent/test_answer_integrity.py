"""Answer integrity (`agent/integrity.py`): each number of an answer's text traced to a cell of a
run it cites, the logged query re-run as the asking persona, and no number shown where none is
traced.

The asks run on the scripted model (`agent/model.py`) or a hand-written one, through the
`as_persona` fixture (`tests/conftest.py`), so every query runs on a database compiled for the
asking persona over the sample build; nothing here calls a model or GCP. The pure checks run on
hand-made citations and on a small database of invented tables.
"""

import json
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

import agent.integrity
from access.compile import quote
from agent.ask import CAPPED, MAX_TOOL_CALLS, Answer, Audit, ask, load_context
from agent.audit import FileLog, bind, cell_text, new_salt, read_log, seal, sealed_result
from agent.integrity import WITHHELD, Logged, logged, numbers, reproduce, same, trace
from agent.model import Citation, Model, Reply, ToolCall, Turn, model_from_config

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
CHICAGO = "accountant-chicago"
AUDITOR = "auditor"
UK = "accountant-uk-transport"
QUESTION = "What share of the City of Chicago's served payment rows repeat another served row?"
BY_YEAR = "What was spent on each basis in each fiscal year, in euros at constant rates?"
BY_MONTH = (
    "By month of payment date, what share of the City of Chicago's served payment rows repeat"
    " another served row?"
)
QUERY_OF = {
    QUESTION: "duplicate-payment-rate",
    BY_YEAR: "constant-currency-spend-by-basis-and-fiscal-year",
    BY_MONTH: "duplicate-payment-rate-by-month",
}
OUTSIDE = "Which vendor was paid the most?"
# A question whose verified query reads a table no persona's database holds, so its run fails.
UNREADABLE = "What share of the City of Chicago's fixed contract awards has been paid?"
WRITTEN = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)


def records(audit: Audit) -> list[dict[str, Any]]:
    assert isinstance(audit.log, FileLog)
    return read_log(audit.log.path.read_bytes())


def kept(audit: Audit, seq: int) -> dict[str, Any]:
    assert isinstance(audit.log, FileLog)
    return json.loads((audit.log.kept / f"{seq}.json").read_text())


def last_logged(audit: Audit) -> tuple[Logged, ...]:
    record = records(audit)[-1]
    return logged(record, kept(audit, record["seq"]))


class Saying:
    """Runs the query it is given (none for None), then replies with a fixed text."""

    def __init__(self, query: str | None, text: str) -> None:
        self._query, self._text = query, text

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return () if self._query is None else (ToolCall(self._query),)

    def step(self, turn: Turn) -> ToolCall | Reply:
        if self._query is None or turn.results:
            return Reply(self._text)
        return ToolCall(self._query)


class Perturbing:
    """The scripted model, with a digit added to the last number of its reply."""

    def __init__(self, model: Model) -> None:
        self._model = model
        self.said = ""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return self._model.plan(turn)

    def step(self, turn: Turn) -> ToolCall | Reply:
        step = self._model.step(turn)
        if isinstance(step, ToolCall):
            return step
        self.said = perturbed(step.text, -1)
        return Reply(self.said)


def perturbed(text: str, index: int) -> str:
    """`text` with a 7 written after its `index`-th number, as `numbers` reads them: the same
    number with one more digit, so it names another value."""
    _, end = numbers(text)[index]
    return text[:end] + "7" + text[end:]


@pytest.fixture
def scripted() -> Model:
    return model_from_config({}, load_context())


# The logged query reproduces the answer


@pytest.mark.parametrize(
    ("asker", "question"),
    [(CHICAGO, QUESTION), (AUDITOR, BY_YEAR), (AUDITOR, BY_MONTH)],
    ids=["a-share", "decimals-years-and-coded-text", "months-and-zeros"],
)
def test_logged_query_reproduces_the_answer(
    as_persona: AsPersona, scripted: Model, audit: Audit, asker: str, question: str
) -> None:
    """An answer of the scripted model: each number of its text is a cell of the run it cites,
    and the query its record logged, re-run as the record's persona, returns that run's rows and
    matches the record's sealed result."""
    answer = ask(asker, question, as_persona=as_persona, audit=audit)
    assert records(audit)[-1]["outcome"] == "answered"
    (citation,) = answer.citations
    assert citation.rows, "a run with no rows proves nothing here"
    report = trace(answer.text, answer.citations)
    assert report.failures == ()
    # Each number cell of the run is written in the text and traced to the run's citation.
    traced = {
        Decimal(t.number.replace(",", "")) for t in report.traced if t.query == QUERY_OF[question]
    }
    cells = {
        Decimal(repr(v) if isinstance(v, float) else v)
        for row in citation.rows
        for v in row
        if isinstance(v, int | float | Decimal) and not isinstance(v, bool)
    }
    assert cells and cells <= traced
    rerun = reproduce(answer, last_logged(audit), as_persona)
    assert rerun.failures == ()
    assert rerun.reproduced == (citation.query,)


def test_each_number_of_a_many_row_answer_is_caught_with_a_digit_appended(
    as_persona: AsPersona, scripted: Model, audit: Audit
) -> None:
    """Each number of the auditor's answer by basis and fiscal year, with a 7 appended to it on
    its own, is reported by that changed text; the unchanged text passes."""
    answer = ask(AUDITOR, BY_YEAR, as_persona=as_persona, audit=audit)
    assert trace(answer.text, answer.citations).failures == ()
    found = numbers(answer.text)
    assert len(found) >= 6
    for index in range(len(found)):
        text = perturbed(answer.text, index)
        start, end = numbers(text)[index]
        failures = trace(text, answer.citations).failures
        assert [f.number for f in failures] == [text[start:end]], text


def test_a_perturbed_number_in_a_scripted_answer_is_caught(
    as_persona: AsPersona, scripted: Model, audit: Audit
) -> None:
    """The scripted reply with a 7 appended to its figure: `trace` names the changed number,
    and `ask` shows no number, recording the turn as abstained with the run it made."""
    model = Perturbing(scripted)
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=model)
    (citation,) = answer.citations
    ((share,),) = citation.rows
    start, end = numbers(model.said)[-1]
    changed = model.said[start:end]
    assert changed == f"{share!r}7"
    (failure,) = trace(model.said, answer.citations).failures
    assert (failure.number, failure.query) == (changed, None)
    assert answer.text == WITHHELD and not re.search(r"\d", answer.text)
    record = records(audit)[-1]
    assert (record["outcome"], len(record["runs"])) == ("abstained", 1)
    assert kept(audit, record["seq"])["answer"][1] == WITHHELD


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


# No number where no verified query covers the question


def share_of_chicago(as_persona: AsPersona, audit: Audit) -> float:
    """The figure the verified duplicate-payment query returns for Chicago's accountant."""
    ((share,),) = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit).citations[0].rows
    return share


@pytest.mark.parametrize("model", ["scripted", "quotes-another-query"])
def test_a_question_outside_the_metrics_gets_an_abstention(
    as_persona: AsPersona, audit: Audit, model: str
) -> None:
    """A question no verified query is worded as: the scripted model calls no tool; a model that
    runs an unrelated verified query and quotes its cell gets the reply withheld. Either way no
    digit is shown and the turn is abstained."""
    share = share_of_chicago(as_persona, audit)
    chosen = (
        model_from_config({}, load_context())
        if model == "scripted"
        else Saying("duplicate-payment-rate", f"The top vendor was paid {share!r}.")
    )
    answer = ask(CHICAGO, OUTSIDE, as_persona=as_persona, audit=audit, model=chosen)
    assert answer.text == WITHHELD and not re.search(r"\d", answer.text)
    assert records(audit)[-1]["outcome"] == "abstained"


def test_only_a_verified_question_as_worded_is_answered(
    as_persona: AsPersona, audit: Audit
) -> None:
    """The verified question with its case, white space and compatibility forms changed is
    answered; a paraphrase of it, with the same run and figure, is withheld."""
    share = share_of_chicago(as_persona, audit)
    # Upper case, doubled spaces and a fullwidth letter, which NFKC makes the plain one.
    reworded = "  " + QUESTION.upper().replace(" ", "  ").replace("ROWS", "\uff32OWS")
    assert same(reworded, QUESTION)
    paraphrase = "How often does a served Chicago payment row repeat another?"
    for question, shown in [(reworded, True), (paraphrase, False)]:
        model = Saying("duplicate-payment-rate", f"The share is {share!r}.")
        answer = ask(CHICAGO, question, as_persona=as_persona, audit=audit, model=model)
        assert (answer.text != WITHHELD, records(audit)[-1]["outcome"]) == (
            shown,
            "answered" if shown else "abstained",
        )


def test_a_reply_with_no_cited_run_is_withheld_whatever_it_says(
    as_persona: AsPersona, audit: Audit
) -> None:
    """Figures in words or in numerals that are no decimal digit, with no run: not shown."""
    model = Saying(
        None, "The top vendor was paid about one point two million, \u00b9\u00b2 dollars."
    )
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


@pytest.mark.parametrize("cause", ["an-unreadable-exponent", "a-check-that-raises"])
def test_a_reply_the_check_cannot_read_is_withheld_and_recorded(
    as_persona: AsPersona, audit: Audit, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    """A number whose value cannot be read is reported; a check that raises withholds the reply;
    either way the turn's record is written."""
    text = "It was 1e999999999999999999999."
    (failure,) = trace(text, (cited(((0.5,),)),)).failures
    assert (failure.number, failure.reason) == (
        "1e999999999999999999999",
        "its value cannot be read",
    )
    if cause == "a-check-that-raises":

        def raising(*_: Any) -> Any:
            raise RuntimeError("the check failed")

        monkeypatch.setattr(agent.integrity, "trace", raising)
        text = "The share is 0.5."
    model = Saying("duplicate-payment-rate", text)
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=model)
    assert answer.text == WITHHELD
    record = records(audit)[-1]
    assert (record["outcome"], len(record["runs"])) == ("abstained", 1)


@pytest.mark.parametrize("form", ["\u2212{}", "-${}", "(${})", "\u2212\u20ac{}", "- {}", "USD-{}"])
def test_a_sign_flipped_figure_is_withheld(as_persona: AsPersona, audit: Audit, form: str) -> None:
    """The cited positive share written as a negative, the minus touching the digits or parted
    from them by a currency sign, a space or a code: withheld, not answered."""
    share = share_of_chicago(as_persona, audit)
    model = Saying("duplicate-payment-rate", f"The share fell to {form.format(repr(share))}.")
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=model)
    assert answer.text == WITHHELD
    assert records(audit)[-1]["outcome"] == "abstained"


class TwoQueries:
    """Plans the verified query of the question and the by-month one, then replies with a figure
    the by-month run returned and the covering run did not."""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return (ToolCall("duplicate-payment-rate"), ToolCall("duplicate-payment-rate-by-month"))

    def step(self, turn: Turn) -> ToolCall | Reply:
        calls = self.plan(turn)
        if len(turn.results) < len(calls):
            return calls[len(turn.results)]
        covering, other = turn.results
        assert isinstance(covering, Citation) and isinstance(other, Citation)
        figures = [v for (_, v) in other.rows if isinstance(v, float) and (v,) not in covering.rows]
        assert figures, "the by-month run returns no figure the covering run lacks"
        return Reply(f"The share is {figures[0]!r}.")


def test_a_figure_from_a_run_that_does_not_cover_the_question_is_withheld(
    as_persona: AsPersona, audit: Audit
) -> None:
    """Both runs are cited, one covering the question; a figure only the other returned is
    withheld."""
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
    """A model that replies with figures where no run returned (it called no tool, or its query
    failed on the persona's database) gets no number shown, and the turn is abstained."""
    model = Saying(query, "The top vendor was paid 1,234,567.89 in 2025, 83% of the total.")
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


def test_a_number_beside_a_cited_run_but_not_in_it_is_withheld(
    as_persona: AsPersona, audit: Audit
) -> None:
    model = Saying("duplicate-payment-rate", "About 0.5 of the rows repeat.")
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=model)
    assert len(answer.citations) == 1
    assert answer.text == WITHHELD
    assert records(audit)[-1]["outcome"] == "abstained"


# The matching rule, on hand-made citations


def cited(
    rows: tuple[tuple[Any, ...], ...], *, version: int = 3, fresh: datetime | None = WRITTEN
) -> Citation:
    columns = tuple(f"c{i}" for i in range(len(rows[0]))) if rows else ()
    return Citation("invented", "invented_metric", version, "select 1", columns, rows, fresh)


def failed_numbers(text: str, *citations: Citation) -> list[str | None]:
    return [f.number for f in trace(text, citations).failures]


def test_each_written_form_of_a_number_is_compared_by_value() -> None:
    citation = cited(((1234567.5, Decimal("17947.690000"), 2026, -12.25),))
    text = "1,234,567.5 and 1234567.50 and 17947.69 and 2026 and -12.25 and 1.23456750e6"
    assert failed_numbers(text, citation) == []
    assert len(trace(text, (citation,)).traced) == 6


@pytest.mark.parametrize(
    "text",
    [
        "about 1.2 million",  # rounded
        "12.25%",  # a percent of a share
        "1234568",  # rounded to a whole number
        "12.25",  # the sign dropped
        "1,23",  # a comma that is no thousands separator
        "42M",  # digits glued to a letter
        "Q3",
        "in 2024",  # a year in prose
        "version 2",  # not a cited version
    ],
)
def test_a_number_that_is_not_a_cell_as_written_is_reported(text: str) -> None:
    citation = cited(((1234567.5, -12.25, 2026, "x"),))
    assert failed_numbers(text, citation) != []


def test_a_version_counts_only_after_the_word_and_only_when_cited() -> None:
    citation = cited(((0.5,),), version=3)
    assert failed_numbers("metric (version 3): 0.5", citation) == []
    assert failed_numbers("metric (version 4): 0.5", citation) == ["4"]
    assert failed_numbers("metric 3: 0.5", citation) == ["3"]
    # The word must come right before the number, and be a word of its own.
    assert failed_numbers("version 3, then 3", citation) == ["3"]
    assert failed_numbers("subversion 3", citation) == ["3"]


DASHES = ["-", "\u2212", "\u2010", "\u2012", "\u2013", "\u2014", "\ufe63", "\uff0d"]


@pytest.mark.parametrize("dash", DASHES)
def test_a_dash_before_a_number_is_its_minus(dash: str) -> None:
    assert failed_numbers(f"it fell to {dash}12.25", cited(((12.25,),))) == [f"{dash}12.25"]
    assert failed_numbers(f"it fell to {dash}12.25", cited(((-12.25,),))) == []


@pytest.mark.parametrize(
    "text",
    [
        "(12.25)",
        "12.25-",
        "12.25\u2212",
        "(\u221212.25)",
        "-$12.25",
        "($12.25)",
        "\u2212\u20ac12.25",
        "- 12.25",
        "( 12.25 )",
        "($ 12.25)",
        "-\xa012.25",
        "USD-12.25",
        "(USD 12.25)",
        "12.25 USD-",
        "( 12.25",
    ],
)
def test_a_number_with_an_ambiguous_sign_is_reported(text: str) -> None:
    """A dash or parenthesis parted from the digits by white space, a currency sign or code, or
    a dash with a letter before it, is not read as a sign: the number is reported whichever sign
    its cell has."""
    for cell in (12.25, -12.25):
        (failure,) = trace(text, (cited(((cell,),)),)).failures
        assert failure.number in ("12.25", "\u221212.25")
        assert "sign" in failure.reason


def test_a_range_is_reported_not_matched() -> None:
    assert failed_numbers("10-12", cited(((10, 12),))) == ["10", "12"]


def test_a_tuple_of_figures_is_no_parenthesis_sign() -> None:
    assert failed_numbers("[(12.25,), (2026, 3)]", cited(((12.25, 2026, 3),))) == []


def test_thousands_are_read_only_in_groups_of_three() -> None:
    assert failed_numbers("1,2345", cited(((1234, 5),))) == ["1", "2345"]


@pytest.mark.parametrize("numeral", ["\u2075\u2070", "\u00bd", "\u2460", "\u216b"])
def test_a_numeral_that_is_no_decimal_digit_is_reported(numeral: str) -> None:
    assert failed_numbers(f"paid {numeral}", cited(((5.0,),))) == list(numeral)


def test_a_bool_cell_is_no_number() -> None:
    assert failed_numbers("1 vendor", cited(((True,),))) == ["1"]


def test_a_text_cell_is_set_aside_only_where_it_is_written_whole() -> None:
    assert failed_numbers("Q1 and Q10", cited((("Q1",),))) == ["10"]
    # Not as the whole or the fraction of a decimal number.
    assert failed_numbers("the total was 12.25", cited((("12", 25),))) == ["12.25"]
    assert failed_numbers("the total was 0.12", cited((("12", 0),))) == ["0.12"]


def test_a_cell_that_is_not_a_number_covers_only_its_own_text() -> None:
    when = datetime(2026, 10, 1, 0, 0)  # noqa: DTZ001 - the engine's naive timestamp
    citation = cited((("payments-over-25k", when, 0.0),))
    assert failed_numbers(f"payments-over-25k {when} {when!r} 0", citation) == []
    assert failed_numbers("payments-over-26k", citation) == ["26"]
    # The zeros of the time are the cell 0.0; the date is no cell's text.
    assert failed_numbers("2026-10-02 00:00:00", citation) == ["2026", "10", "02"]


def test_a_run_without_freshness_traces_no_number() -> None:
    stale = cited(((0.5,),), fresh=None)
    (failure,) = trace("0.5", (stale,)).failures
    assert (failure.number, failure.query) == ("0.5", "invented")
    assert "freshness" in failure.reason
    assert failed_numbers("payments-over-25k", cited((("payments-over-25k",),), fresh=None)) == [
        "25"
    ]
    assert failed_numbers("(version 3)", cited(((0.5,),), fresh=None)) == ["3"]


def test_no_citation_traces_no_number() -> None:
    assert failed_numbers("No verified query answers this question.") == []
    assert failed_numbers("version 1, 0") == ["1", "0"]


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
    from dataclasses import replace

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
    from dataclasses import replace

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
    from dataclasses import replace

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
    """Plans nothing, calls the verified query anyway, then replies with a figure."""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return ()

    def step(self, turn: Turn) -> ToolCall | Reply:
        return Reply("The share is 0.5.") if turn.results else ToolCall("duplicate-payment-rate")


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
    model: Model = Unplanned() if unplanned else Saying("duplicate-payment-rate", "It is 0.5.")
    answer = ask(asker, QUESTION, as_persona=as_persona, audit=audit, model=model)
    assert answer.text == WITHHELD
    record = records(audit)[-1]
    assert (record["outcome"], record["reason"]) == outcome
