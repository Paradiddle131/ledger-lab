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

from access.compile import quote
from agent.ask import Answer, Audit, ask, load_context
from agent.audit import FileLog, bind, cell_text, new_salt, read_log, seal, sealed_result
from agent.integrity import WITHHELD, Logged, logged, numbers, reproduce, trace
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

    def step(self, turn: Turn) -> ToolCall | Reply:
        if self._query is None or turn.results:
            return Reply(self._text)
        return ToolCall(self._query)


class Perturbing:
    """The scripted model, with a digit added to the last number of its reply."""

    def __init__(self, model: Model) -> None:
        self._model = model
        self.said = ""

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


def test_every_number_of_a_many_row_answer_is_caught_when_changed(
    as_persona: AsPersona, scripted: Model, audit: Audit
) -> None:
    """Each number of the auditor's answer by basis and fiscal year, changed in its last digit on
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
    """The scripted reply with one digit of its figure changed: `trace` names the changed number,
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


def test_a_question_outside_the_metrics_gets_an_abstention(
    as_persona: AsPersona, scripted: Model, audit: Audit
) -> None:
    answer = ask(CHICAGO, OUTSIDE, as_persona=as_persona, audit=audit)
    assert answer.citations == ()
    assert not re.search(r"\d", answer.text)
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
