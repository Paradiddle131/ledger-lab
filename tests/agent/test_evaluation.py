"""The golden set (`agent/evals/golden.yaml`) and its scorer (`agent/evals/score.py`).

The scorer is played on the scripted model through the `as_persona` fixture (`tests/conftest.py`),
so each answer and each re-run of its cited SQL run on the database compiled for the asking persona
over the sample build; nothing here calls a model or GCP. What is checked:
- the file against its schema, its personas, queries and metrics against the catalogues and the
  metric context, its abstaining cases against the tool policy, and its texts against the values
  of the sample build's tagged columns;
- that a headline metric with a verified query is either covered by a case or listed as uncovered,
  and that an uncovered one's queries fail on each persona's database the policy lets run them;
- `classify` on hand-made results: row order, repeated rows, number types, nulls, kinds of value,
  column order, citation counts and abstaining;
- that the score comes from re-running the cited SQL: an answer whose text and reported rows hold
  the expected figure while its SQL returns another scores wrong, and one whose reported rows are
  wrong while its SQL returns the expected result scores right;
- the drift: with the committed metric context every case scores right; with two metrics' SQL
  changed in a copy of the context, exactly the cases those changes move score wrong-and-sure,
  and the report names them and exits 1.
"""

import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
from conftest import REPO, load_checked

from access.compile import quote
from agent.ask import Answer, Audit, load_context, run_metric_query
from agent.evals import score as scorer
from agent.evals.score import Case, Result, classify, load_cases, report, score
from agent.model import Citation, Refusal, Reply, ToolCall, Turn
from agent.policy.tools import Entity, authorize

if TYPE_CHECKING:
    from tests.models.dbt_build import Build

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
GOLDEN = REPO / "agent/evals/golden.yaml"
SCHEMA = REPO / "agent/evals/golden.schema.json"
CONTEXT = REPO / "semantics/context.json"


def context() -> dict[str, Any]:
    return json.loads(CONTEXT.read_text(encoding="utf-8"))


def allowed(persona: str, metric: str) -> bool:
    return bool(
        authorize(
            Entity("Persona", persona),
            "run_metric_query",
            Entity("Metric", metric),
            {"session": "golden"},
        )
    )


def test_the_golden_set_holds_its_schema_and_its_references(access: dict[str, Any]) -> None:
    doc = load_checked(GOLDEN, SCHEMA)
    ids = [case["id"] for case in doc["cases"]]
    assert len(ids) == len(set(ids)), "a case id is used twice"
    personas = {p["id"] for p in access["personas"]}
    queries = load_context()
    for case in doc["cases"]:
        assert case["persona"] in personas, case["id"]
        assert case["query"] in queries, case["id"]
    assert [c.id for c in load_cases()] == ids


def test_each_headline_metric_with_a_query_is_covered_or_listed_as_uncovered() -> None:
    doc = load_checked(GOLDEN, SCHEMA)
    queries = load_context()
    headline = {
        name
        for name, metric in context()["metrics"].items()
        if metric["role"] == "headline" and metric.get("verified_queries")
    }
    covered = {
        queries[case["query"]].metric for case in doc["cases"] if "columns" in case["expected"]
    }
    uncovered = [entry["metric"] for entry in doc["uncovered"]]
    assert len(uncovered) == len(set(uncovered))
    assert covered & set(uncovered) == set(), "a metric is both covered and listed as uncovered"
    assert covered | set(uncovered) == headline


def test_an_uncovered_metric_is_answered_on_no_persona_database(
    access: dict[str, Any], as_persona: AsPersona
) -> None:
    """Each verified query of an uncovered metric fails on each persona's database the tool policy
    lets run it, so a metric that becomes answerable fails here until a case covers it."""
    doc = load_checked(GOLDEN, SCHEMA)
    uncovered = {entry["metric"] for entry in doc["uncovered"]}
    queries = [q for q in load_context().values() if q.metric in uncovered]
    assert {q.metric for q in queries} == uncovered
    tried = 0
    for persona in (p["id"] for p in access["personas"]):
        runnable = [q for q in queries if allowed(persona, q.metric)]
        if not runnable:
            continue
        connection = as_persona(persona)
        for query in runnable:
            assert isinstance(run_metric_query(connection, query), Refusal), (persona, query.id)
            tried += 1
    assert tried > 0


def test_an_abstaining_case_is_one_the_tool_policy_refuses() -> None:
    queries = load_context()
    abstaining = [c for c in load_cases() if c.expected is None]
    assert abstaining, "no case expects an abstention"
    for case in abstaining:
        assert not allowed(case.persona, queries[case.query].metric), case.id
    for case in load_cases():
        if case.expected is not None:
            assert allowed(case.persona, queries[case.query].metric), case.id


def test_the_expected_results_hold_no_personal_value(
    build: "Build", columns: dict[str, Any]
) -> None:
    """An expected text is only a grain value (a column the verified query groups by), and no
    expected text equals a value of a tagged column of the sample build (`access/columns.yaml`)."""
    by_id = {
        query["id"]: (name, query.get("group_by", []))
        for name, metric in context()["metrics"].items()
        for query in metric.get("verified_queries", [])
    }
    tagged = [
        column for product in columns["products"].values() for column in product.get("columns", {})
    ]
    assert tagged, "no tagged column to check against"
    with duckdb.connect(str(build.database), read_only=True) as db:
        held: set[str] = set()
        for column in tagged:
            table, name = column.split(".")
            held |= {
                value
                for (value,) in db.execute(
                    f"select distinct cast({quote(name)} as varchar) from main.{quote(table)}"  # noqa: S608
                ).fetchall()
                if value is not None
            }
    assert held
    for case in load_cases():
        if case.expected is None:
            continue
        metric, grain = by_id[case.query]
        assert set(case.expected.columns) <= {metric, *grain}, case.id
        for row in case.expected.rows:
            for column, value in zip(case.expected.columns, row, strict=True):
                if isinstance(value, str):
                    assert column in grain, (case.id, column)
                    assert value not in held, (case.id, column)


SHARE = Result(("share",), ((0.25,),))
GROUPED = Result(("basis", "spend"), (("a", Decimal("1.50")), ("b", None), ("a", Decimal("1.50"))))


@pytest.mark.parametrize(
    ("expected", "executed", "verdict"),
    [
        (SHARE, [Result(("share",), ((0.25,),))], (True, True)),
        (SHARE, [Result(("share",), ((Decimal("0.25"),),))], (True, True)),
        (SHARE, [Result(("share",), ((0.2500001,),))], (False, True)),
        (SHARE, [Result(("rate",), ((0.25,),))], (False, True)),
        (SHARE, [Result(("share",), ((0.25,), (0.25,)))], (False, True)),
        (SHARE, [Result(("share",), ((None,),))], (False, True)),
        (SHARE, [Result(("share",), ())], (False, True)),
        (SHARE, [], (False, False)),
        (SHARE, [None], (False, True)),
        (SHARE, [SHARE, SHARE], (False, True)),
        (
            GROUPED,
            [Result(("basis", "spend"), (("b", None), ("a", 1.5), ("a", 1.5)))],
            (True, True),
        ),
        (GROUPED, [Result(("basis", "spend"), (("b", None), ("a", 1.5)))], (False, True)),
        (
            GROUPED,
            [Result(("spend", "basis"), ((1.5, "a"), (None, "b"), (1.5, "a")))],
            (False, True),
        ),
        (GROUPED, [Result(("basis", "spend"), (("b", 0), ("a", 1.5), ("a", 1.5)))], (False, True)),
        (Result(("n",), ((2026,),)), [Result(("n",), ((2026.0,),))], (True, True)),
        (Result(("n",), ((1,),)), [Result(("n",), ((True,),))], (False, True)),
        (
            Result(("d",), (("2026-07-01",),)),
            [Result(("d",), ((date(2026, 7, 1),),))],
            (False, True),
        ),
        (
            Result(("d",), ((datetime(2026, 7, 1),),)),
            [Result(("d",), ((datetime(2026, 7, 1),),))],
            (True, True),
        ),
        (None, [], (True, False)),
        (None, [SHARE], (False, True)),
        (None, [None], (False, True)),
    ],
)
def test_classify_compares_executed_results_with_the_expected_one(
    expected: Result | None, executed: list[Result | None], verdict: tuple[bool, bool]
) -> None:
    got = classify(expected, executed)
    assert (got.right, got.sure) == verdict, got.reason


def case_named(case_id: str) -> Case:
    (case,) = [c for c in load_cases() if c.id == case_id]
    return case


def forged(case: Case, rows: tuple[tuple[Any, ...], ...], sql: str) -> Answer:
    """An answer whose text and citation report `rows`, citing `sql`."""
    query = load_context()[case.query]
    assert case.expected is not None
    citation = Citation(
        query.id, query.metric, query.version, sql, case.expected.columns, rows, None
    )
    return Answer(case.persona, case.question, f"{list(rows)}", (citation,), ())


def test_the_score_comes_from_running_the_cited_sql(
    as_persona: AsPersona, audit: Audit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answers here come from a stand-in for `ask`, so only the cited SQL can tell them apart:
    the expected figure in the text and the rows with SQL that returns another is wrong, and wrong
    rows with the verified SQL is right."""
    case = case_named("duplicate-dollars-as-the-payables-clerk")
    assert case.expected is not None
    verified = load_context()[case.query].sql
    other = verified.replace("then amount else 0", "then 1 else 0").replace(
        "sum(amount)", "count(amount)"
    )
    assert other != verified
    answers = {
        "claims-the-figure": forged(case, case.expected.rows, other),
        "misreports-the-figure": forged(case, ((0.5,),), verified),
    }
    verdicts = {}
    for name, answer in answers.items():
        monkeypatch.setattr(scorer, "ask", lambda *_, answer=answer, **__: answer)
        (scored,) = score([case], as_persona=as_persona, audit=audit)
        verdicts[name] = (scored.verdict.right, scored.verdict.sure)
    assert verdicts == {"claims-the-figure": (False, True), "misreports-the-figure": (True, True)}


class Boasting:
    """A model that runs the query it is scripted to, then replies with a fixed text."""

    def __init__(self, query: str, text: str) -> None:
        self._query, self._text = query, text

    def step(self, turn: Turn) -> ToolCall | Reply:
        return Reply(self._text) if turn.results else ToolCall(self._query)

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return (ToolCall(self._query),)


def test_a_reply_claiming_the_expected_figure_scores_wrong_when_its_query_returns_another(
    as_persona: AsPersona, audit: Audit
) -> None:
    case = case_named("duplicate-dollars-as-the-payables-clerk")
    assert case.expected is not None
    ((figure,),) = case.expected.rows
    queries = load_context()
    query = queries[case.query]
    changed = {
        **queries,
        query.id: replace(query, sql=query.sql.replace("amount else 0", "1 else 0")),
    }
    model = Boasting(case.query, f"duplicate_payment_amount_rate = {figure!r}")
    (scored,) = score([case], as_persona=as_persona, audit=audit, model=model, queries=changed)
    assert (scored.verdict.right, scored.verdict.sure) == (False, True)
    (honest,) = score([case], as_persona=as_persona, audit=audit, model=model, queries=queries)
    assert (honest.verdict.right, honest.verdict.sure) == (True, True)


def changed_context(path: Path) -> Path:
    """A copy of the metric context with two definitions changed: the duplicate amount rate taken
    by rows, and constant-currency spend kept for a group of more than one basis."""
    doc = context()
    edits = {
        "duplicate_payment_amount_rate": [
            ("then amount else 0", "then 1 else 0"),
            ("sum(amount)", "count(amount)"),
        ],
        "constant_currency_spend": [(") = 1\n", ") >= 1\n")],
    }
    for name, pairs in edits.items():
        for query in doc["metrics"][name]["verified_queries"]:
            for old, new in pairs:
                assert old in query["sql"], (query["id"], old)
                query["sql"] = query["sql"].replace(old, new)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return path


# The cases the changed definitions move on the sample build: the amount rate by rows is the row
# rate, and Chicago's figures sit on two bases, so its constant-currency spend stops being null.
# The by-basis cases group one basis each, so the changed rule leaves them as they were.
MOVED = {
    "duplicate-dollars-as-the-payables-clerk",
    "constant-currency-by-entity-as-the-group-controller",
}


def test_metric_change_shows_as_drift(as_persona: AsPersona, audit: Audit, tmp_path: Path) -> None:
    cases = load_cases()
    assert {c.id for c in cases} > MOVED

    control = score(cases, as_persona=as_persona, audit=audit)
    assert [(s.case.id, s.verdict.right) for s in control] == [(c.id, True) for c in cases]
    text, code = report(control)
    assert code == 0
    assert text.splitlines() == [f"right {len(cases)}", "wrong 0", "wrong-and-sure 0"]

    changed = load_context(changed_context(tmp_path / "context.json"))
    drifted = score(cases, as_persona=as_persona, audit=audit, queries=changed)
    verdicts = {s.case.id: (s.verdict.right, s.verdict.sure) for s in drifted}
    assert verdicts == {
        c.id: (False, True) if c.id in MOVED else (True, c.expected is not None) for c in cases
    }
    text, code = report(drifted)
    assert code == 1
    lines = text.splitlines()
    assert lines[:3] == [
        f"right {len(cases) - len(MOVED)}",
        f"wrong {len(MOVED)}",
        f"wrong-and-sure {len(MOVED)}",
    ]
    named = {re.match(r"wrong and sure: ([a-z0-9-]+): ", line) for line in lines[3:]}
    assert {m.group(1) for m in named if m} == MOVED and len(lines[3:]) == len(MOVED)


def test_the_command_prints_the_counts(build: "Build", capsys: pytest.CaptureFixture[str]) -> None:
    assert scorer.main(["--warehouse", str(build.database)]) == 0
    cases = load_cases()
    assert capsys.readouterr().out.splitlines() == [
        f"right {len(cases)}",
        "wrong 0",
        "wrong-and-sure 0",
    ]
