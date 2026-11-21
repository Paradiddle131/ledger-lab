"""Score the own agent on the golden set (`agent/evals/golden.yaml`) by running the queries its
answers cite.

For each case, `score` asks `agent.ask.ask` the case's question as the case's persona, then reads
from the answer only each citation's query id and SQL, runs that SQL again on a connection
`as_persona(persona)` returns, through `agent.ask.run_metric_query` (a cursor with the search path
set to the persona's own schemas), and hands the results to `classify`, a pure function.

What `classify` compares, for a case with an expected result:
- the answer must cite exactly one query, and that query's SQL must run again without an error;
- the columns, by name and in order, equal the expected columns;
- the rows, as a multiset: row order is not compared, a row returned twice must be expected twice;
- each value after `canonical`: a number (int, float or Decimal, not a bool) as a double, equal
  with no tolerance; a date or time as its ISO text, apart from any text; a text exactly; a null
  only to a null; a bool only to a bool.
For a case expecting `abstains`, the answer is right when it cites no query.

The verdicts: right, as above; wrong, anything else, among them no citation where a result was
expected, two citations, a re-run that fails, and a citation where `abstains` was expected;
wrong-and-sure, a wrong answer that cites at least one query, so it asserted a figure rather than
abstaining. Every wrong-and-sure answer is also counted as wrong.

`python -m agent.evals.score [--warehouse <sample build>]` prints the counts and each wrong case's
id and reason, and exits 1 when any case is wrong; without `--warehouse` it builds the committed
samples on the local engine in a temporary folder first (`tests/models/dbt_build.py`). It runs the
model `LEDGER_LAB_AGENT_MODEL` names, which today is only the scripted one.

Stated limits:
- the answer's text and the rows its citations report are not read; `ask` fills a citation's rows
  from the same run, and a text that misstates them is not caught here;
- the cited query's id is not compared with the case's query: a citation whose SQL returns the
  expected result is right whatever query it names;
- the re-run uses the agent's own tool function, so a fault in how that function runs SQL is shared
  by the answer and its score;
- integers beyond a double's precision, and decimals that differ only beyond it, compare equal;
- the expected results were frozen on the committed sample build; on another build (the landed
  files) the cases are expected to score wrong;
- drift shows only where a changed definition changes a frozen result: a change that leaves every
  case's rows on the sample build as they were does not move the score.
"""

import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import yaml
from jsonschema import Draft202012Validator

from agent.ask import Answer, AsPersona, ask, run_metric_query
from agent.model import Citation, Model, VerifiedQuery

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "agent" / "evals" / "golden.yaml"
SCHEMA = REPO / "agent" / "evals" / "golden.schema.json"

Value = tuple[str, Any]
Row = tuple[Value, ...]


@dataclass(frozen=True)
class Result:
    """What a query returned: its column names in order and its rows."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class Case:
    """A golden case; `expected` is None when the right answer abstains."""

    id: str
    question: str
    persona: str
    query: str
    expected: Result | None


@dataclass(frozen=True)
class Verdict:
    right: bool
    sure: bool
    reason: str


@dataclass(frozen=True)
class Scored:
    case: Case
    verdict: Verdict


def load_cases(path: Path = GOLDEN, schema: Path = SCHEMA) -> list[Case]:
    """The golden cases, after the file is checked against its schema."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")))
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(doc)
    ]
    if errors:
        raise ValueError(f"{path.name} breaks its schema: {'; '.join(errors)}")
    cases = []
    for case in doc["cases"]:
        expected = case["expected"]
        result = (
            None
            if expected.get("abstains")
            else Result(tuple(expected["columns"]), tuple(tuple(r) for r in expected["rows"]))
        )
        cases.append(Case(case["id"], case["question"], case["persona"], case["query"], result))
    return cases


def canonical(value: Any) -> Value:
    """A value as `classify` compares it, tagged with its kind."""
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int | float | Decimal):
        return ("number", float(value))
    if isinstance(value, datetime | date | time):
        return ("time", value.isoformat())
    if isinstance(value, str):
        return ("text", value)
    return ("other", repr(value))


def _rows(result: Result) -> Counter[Row]:
    return Counter(tuple(canonical(v) for v in row) for row in result.rows)


def classify(expected: Result | None, executed: Sequence[Result | None]) -> Verdict:
    """The verdict on one answer: `expected` is the case's frozen result (None: abstains), and
    `executed` holds, per citation of the answer, what its SQL returned when run again (None: the
    re-run failed)."""
    sure = len(executed) > 0
    if expected is None:
        if sure:
            return Verdict(False, True, "cited a query where the right answer abstains")
        return Verdict(True, False, "abstained")
    if not sure:
        return Verdict(False, False, "cited no query")
    if len(executed) > 1:
        return Verdict(False, True, f"cited {len(executed)} queries where one answers")
    (result,) = executed
    if result is None:
        return Verdict(False, True, "the cited query failed when run again")
    if result.columns != expected.columns:
        return Verdict(False, True, f"columns {list(result.columns)}, not {list(expected.columns)}")
    if _rows(result) != _rows(expected):
        return Verdict(False, True, "the rows differ from the expected rows")
    return Verdict(True, True, "the cited query returned the expected result")


def cited(answer: Answer) -> tuple[tuple[str, str], ...]:
    """The only part of an answer the score reads: each citation's query id and SQL."""
    return tuple((citation.query, citation.sql) for citation in answer.citations)


def rerun(connection: duckdb.DuckDBPyConnection, query: str, sql: str) -> Result | None:
    """Run a cited SQL again on the persona's connection; None when it fails."""
    ran = run_metric_query(connection, VerifiedQuery(query, "", "", 0, sql))
    if not isinstance(ran, Citation):
        return None
    return Result(ran.columns, ran.rows)


def score(
    cases: Sequence[Case],
    *,
    as_persona: AsPersona,
    model: Model | None = None,
    queries: Mapping[str, VerifiedQuery] | None = None,
) -> list[Scored]:
    """Ask each case's question and score the answer by running what it cites again."""
    scored = []
    for case in cases:
        answer = ask(
            case.persona, case.question, as_persona=as_persona, model=model, queries=queries
        )
        citations = cited(answer)
        executed: list[Result | None] = []
        if citations:
            connection = as_persona(case.persona)
            executed = [rerun(connection, query, sql) for query, sql in citations]
        scored.append(Scored(case, classify(case.expected, executed)))
    return scored


def report(scored: Sequence[Scored]) -> tuple[str, int]:
    """The counts and each wrong case, and the exit status: 1 when any case is wrong."""
    right = sum(s.verdict.right for s in scored)
    wrong = [s for s in scored if not s.verdict.right]
    sure = sum(s.verdict.sure for s in wrong)
    lines = [f"right {right}", f"wrong {len(wrong)}", f"wrong-and-sure {sure}"]
    lines += [
        f"wrong{' and sure' if s.verdict.sure else ''}: {s.case.id}: {s.verdict.reason}"
        for s in wrong
    ]
    return "\n".join(lines), 1 if wrong else 0


@contextmanager
def personas_over(warehouse: Path) -> Iterator[Callable[[str], duckdb.DuckDBPyConnection]]:
    """A connection-maker over `warehouse`: each persona's database compiled once
    (`access/compile.py`) into a temporary folder, deleted with its connections at the end."""
    from access.compile import open_persona

    def load(name: str) -> dict[str, Any]:
        return yaml.safe_load((REPO / "access" / name).read_text(encoding="utf-8"))

    catalogue, columns = load("personas.yaml"), load("columns.yaml")
    aggregates, purposes = load("aggregates.yaml"), load("purposes.yaml")
    folder = Path(tempfile.mkdtemp(prefix="golden-personas-"))
    opened: dict[str, duckdb.DuckDBPyConnection] = {}

    def act(persona: str) -> duckdb.DuckDBPyConnection:
        if persona not in opened:
            directory = folder / persona
            directory.mkdir()
            opened[persona] = open_persona(
                catalogue, columns, aggregates, purposes, persona, warehouse, directory
            )
        return opened[persona]

    try:
        yield act
    finally:
        for connection in opened.values():
            connection.close()
        shutil.rmtree(folder, ignore_errors=True)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--warehouse", type=Path, help="a local build over the committed samples")
    args = parser.parse_args(argv)
    cases = load_cases()
    with tempfile.TemporaryDirectory(prefix="golden-build-") as scratch:
        warehouse = args.warehouse
        if warehouse is None:
            from tests.models.dbt_build import built

            warehouse = built(Path(scratch), "sample").database
        with personas_over(warehouse) as as_persona:
            text, code = report(score(cases, as_persona=as_persona))
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
