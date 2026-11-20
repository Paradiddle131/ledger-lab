"""The verified queries (`semantics/verified_queries.yaml`): each a question, its SQL, the metric it
answers with that metric's version and owner, and the grain and filter the SQL answers at.

What is checked:

- the file against its schema, and its ids listed once;
- each query's metric, version and owner against the metric's own `config.meta` in the semantic
  manifest dbt's parse wrote, not against the file; the metric must be of role `headline`;
- every `headline` metric of that manifest has at least one verified query; an `input` metric
  has none, since it is not for direct use: a query on one is refused;
- each query's SQL, run on a build's database, against `mf query` for the same metric, grain
  (`group_by`) and filter (`where`), run offline through `semantics/offline/offline_mf.py`, over
  the sample build (the committed samples built on the local engine) and, for the queries reading
  the contract reconciliation, also over the invented contracts of
  `tests/semantics/test_metrics.py` (a contract paid over its award, one held to an award other
  than its award after revisions, a negative award, none of which the samples hold):
  - the SQL's columns are exactly the grain's and the metric's names;
  - neither side holds two rows for one grain value, and both hold the same grain values;
  - grain values are compared as text, after the SQL's are converted as MetricFlow converts the
    rows it reads (a date as midnight, a decimal as a double) and written as its CSV writes them;
  - metric values are compared as doubles, exactly, with no tolerance, and an empty value (null)
    equals only an empty value; row order is not compared;
  - over the samples, `mf` must answer with at least one row holding a figure, and a stated
    filter must change its answer, so neither side passes by being empty or unfiltered;
- wrong SQL planted for each query (a row left out, a row twice, every figure a cent off, a grain
  value rewritten) and for several queries (a filter or the Chicago condition on one side only,
  payments with no date left out, a month for a year, a null made 0, the amount rate counted by
  rows, unlike bases added; over the invented contracts: the unpaid part netted, awards not above
  0 kept, the award after revisions for the award held to) is refused;
- a version or owner changed in the metric's YAML and read back through dbt's parse is refused.

Stated limits:

- `mf` writes every decimal through a double, so two decimal figures that differ only beyond a
  double's precision (about 15 significant digits) compare equal;
- `mf`'s CSV writes a null and an empty text alike, so a grain value of empty text and a null one
  compare equal;
- the SQL is checked only over the committed samples and the invented contracts; the landed
  build is not run here, so a difference only real data shows (such as a basis of whole years,
  which the samples lack) is not caught, nor a filter, condition or grain whose wrong form gives
  the same result over those rows;
- the SQL is written for the local DuckDB build's table names and dialect, not BigQuery's.
"""

import datetime
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import load_checked

from tests.models.dbt_build import Build
from tests.semantics.metric_cli import manifest, mf_query
from tests.semantics.test_metrics import PLANTED, invented_contracts, parsed_with

SEMANTICS = Path(__file__).resolve().parents[2] / "semantics"
Query = dict[str, Any]
Rows = list[dict[str, str]]


def load() -> list[Query]:
    return load_checked(
        SEMANTICS / "verified_queries.yaml", SEMANTICS / "verified_queries.schema.json"
    )["verified_queries"]


QUERIES = load()
BY_ID = {q["id"]: q for q in QUERIES}


def catalogue_problems(queries: list[Query], metrics: list[dict[str, Any]]) -> list[str]:
    """Every query whose metric the manifest lacks or holds as other than a headline, or whose
    version or owner is not the metric's `config.meta`; every headline metric with no query; every
    id listed twice."""
    metas = {m["name"]: (m.get("config") or {}).get("meta") or {} for m in metrics}
    found = [
        f"{i}: listed {n} times" for i, n in Counter(q["id"] for q in queries).items() if n > 1
    ]
    for query in queries:
        qid, name = query["id"], query["metric"]
        if name not in metas:
            found.append(f"{qid}: metric {name} is not in the semantic manifest")
            continue
        meta = metas[name]
        if meta.get("role") != "headline":
            found.append(f"{qid}: metric {name} is of role {meta.get('role')}, not headline")
        version = meta.get("version")
        if type(version) is not int or query["version"] != version:
            found.append(f"{qid}: version {query['version']}, but {name} is at version {version}")
        if query["owner"] != meta.get("owner"):
            found.append(
                f"{qid}: owner {query['owner']}, but {name} is owned by {meta.get('owner')}"
            )
    covered = {q["metric"] for q in queries}
    found += [
        f"{name}: a headline metric with no verified query"
        for name, meta in metas.items()
        if meta.get("role") == "headline" and name not in covered
    ]
    return found


def as_mf_writes(value: object) -> str:
    """A cell of the SQL's result as `mf query --csv` writes a cell it read: MetricFlow turns a
    decimal into a double and a date into a timestamp at midnight (its data table), and Python's
    CSV writer writes None as empty and anything else as `str`."""
    if value is None:
        return ""
    if isinstance(value, bool | int | float | str):
        return str(value)
    if isinstance(value, Decimal):
        return str(float(value))
    if isinstance(value, datetime.datetime):
        return str(value.replace(tzinfo=None))
    if isinstance(value, datetime.date):
        return str(datetime.datetime.combine(value, datetime.time.min))
    raise TypeError(f"a cell of type {type(value).__name__} has no form `mf` writes")


def same_figure(sql: str, mf: str) -> bool:
    """Two metric cells as written: both empty, or both figures equal as doubles, exactly."""
    if sql == "" or mf == "":
        return sql == mf
    return float(sql) == float(mf)


def result_problems(
    columns: list[str],
    sql_rows: list[tuple[Any, ...]],
    mf_rows: Rows,
    grain: list[str],
    metric: str,
) -> list[str]:
    """Every way the SQL's result differs from `mf`'s, as the module docstring lists."""
    expected = [*grain, metric]
    if Counter(columns) != Counter(expected):
        return [f"the SQL's columns are {columns}, not {expected}"]
    if mf_rows and Counter(list(mf_rows[0])) != Counter(expected):
        return [f"mf's columns are {list(mf_rows[0])}, not {expected}"]
    found: list[str] = []
    sql_written = [
        {c: as_mf_writes(v) for c, v in zip(columns, row, strict=True)} for row in sql_rows
    ]

    def by_grain(rows: Rows, side: str) -> dict[tuple[str, ...], str]:
        out: dict[tuple[str, ...], str] = {}
        for row in rows:
            key = tuple(row[g] for g in grain)
            if key in out:
                found.append(f"{side} holds two rows for {key}")
            out[key] = row[metric]
        return out

    sql, mf = by_grain(sql_written, "the SQL"), by_grain(mf_rows, "mf")
    found += [f"mf has {k}, the SQL does not" for k in sorted(mf.keys() - sql.keys())]
    found += [f"the SQL has {k}, mf does not" for k in sorted(sql.keys() - mf.keys())]
    found += [
        f"{k}: the SQL gives {sql[k]!r}, mf {mf[k]!r}"
        for k in sorted(sql.keys() & mf.keys())
        if not same_figure(sql[k], mf[k])
    ]
    return found


def run_sql(build: Build, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    with duckdb.connect(str(build.database), read_only=True) as db:
        cursor = db.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        return columns, cursor.fetchall()


def ask_mf(build: Build, query: Query, *, filtered: bool = True) -> Rows:
    where = query.get("where") if filtered else None
    return mf_query(build, [query["metric"]], query["group_by"] or None, where)


@pytest.fixture(scope="module")
def answers(build: Build) -> dict[str, Rows]:
    """`mf query`'s answer to each verified query over the sample build, asked once."""
    return {q["id"]: ask_mf(build, q) for q in QUERIES}


# The queries that read the contract reconciliation, also run over the invented contracts.
CONTRACT_QUERIES = [q["id"] for q in QUERIES if "chicago_contract_reconciliation" in q["sql"]]


@pytest.fixture(scope="module")
def invented(build: Build, tmp_path_factory: pytest.TempPathFactory) -> Build:
    """A build holding only the invented contracts of `tests/semantics/test_metrics.py` as the
    contract reconciliation: a contract paid over its award, one held to an award other than its
    award after revisions, a negative award, which the samples lack."""
    return invented_contracts(build, tmp_path_factory.mktemp("invented"))


@pytest.fixture(scope="module")
def invented_answers(invented: Build) -> dict[str, Rows]:
    """`mf query`'s answer to each contract query over the invented contracts, asked once."""
    return {qid: ask_mf(invented, BY_ID[qid]) for qid in CONTRACT_QUERIES}


def verify(build: Build, query: Query, mf_rows: Rows, sql: str | None = None) -> list[str]:
    columns, rows = run_sql(build, query["sql"] if sql is None else sql)
    return result_problems(columns, rows, mf_rows, query["group_by"], query["metric"])


def test_the_verified_queries_name_their_metrics_as_dbt_parses_them(build: Build) -> None:
    assert catalogue_problems(QUERIES, manifest(build)["metrics"]) == []


def test_every_headline_metric_has_a_verified_query_and_no_input_has_one(build: Build) -> None:
    roles = {m["name"]: m["config"]["meta"]["role"] for m in manifest(build)["metrics"]}
    covered = {q["metric"] for q in QUERIES}
    assert covered == {name for name, role in roles.items() if role == "headline"}


def changed(**edits: Any) -> Callable[[list[Query]], list[Query]]:
    def edit(queries: list[Query]) -> list[Query]:
        return [q | edits if q["id"] == "spend-against-award" else q for q in queries]

    return edit


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            changed(version=2),
            ["spend-against-award: version 2, but spend_against_award is at version 1"],
        ),
        (
            changed(owner="group-controller"),
            [
                "spend-against-award: owner group-controller, but spend_against_award is owned by"
                " payables-clerk"
            ],
        ),
        (
            changed(metric="made_up"),
            ["spend-against-award: metric made_up is not in the semantic manifest"],
        ),
        (
            changed(metric="fixed_award"),
            ["spend-against-award: metric fixed_award is of role input, not headline"],
        ),
        (
            lambda qs: [q for q in qs if q["metric"] != "paid_over_award"],
            ["paid_over_award: a headline metric with no verified query"],
        ),
        (lambda qs: [*qs, qs[0]], ["spend-against-award: listed 2 times"]),
    ],
    ids=["version", "owner", "unknown-metric", "input-metric", "headline-uncovered", "id-twice"],
)
def test_a_query_that_disagrees_with_the_manifest_is_refused(
    build: Build, edit: Callable[[list[Query]], list[Query]], expected: list[str]
) -> None:
    assert catalogue_problems(edit(QUERIES), manifest(build)["metrics"]) == expected


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (
            "{owner: payables-clerk, term: spend-against-award, role: headline, version: 2}",
            "version 1, but spend_against_award is at version 2",
        ),
        (
            "{owner: group-controller, term: spend-against-award, role: headline, version: 1}",
            "owner payables-clerk, but spend_against_award is owned by group-controller",
        ),
    ],
    ids=["version", "owner"],
)
def test_a_metric_changed_in_its_yaml_refuses_the_queries_that_still_state_the_old(
    tmp_path: Path, meta: str, expected: str
) -> None:
    """The metric's meta is changed in a copy of its YAML and read back through dbt's parse; the
    verified queries, unchanged, now disagree with it."""
    assert "version: 1}" in PLANTED, "spend_against_award's meta moved: update the cases"
    metrics = parsed_with(tmp_path, meta)["metrics"]
    on_it = [q["id"] for q in QUERIES if q["metric"] == "spend_against_award"]
    assert catalogue_problems(QUERIES, metrics) == [f"{i}: {expected}" for i in on_it]


@pytest.mark.parametrize("qid", list(BY_ID))
def test_the_verified_query_equals_mf_query(
    build: Build, answers: dict[str, Rows], qid: str
) -> None:
    query = BY_ID[qid]
    mf_rows = answers[qid]
    # Not vacuous: mf answers with a figure.
    assert any(r[query["metric"]] for r in mf_rows), mf_rows
    assert verify(build, query, mf_rows) == []


@pytest.mark.parametrize("qid", CONTRACT_QUERIES)
def test_the_contract_query_equals_mf_query_over_invented_contracts(
    invented: Build, invented_answers: dict[str, Rows], qid: str
) -> None:
    assert verify(invented, BY_ID[qid], invented_answers[qid]) == []


@pytest.mark.parametrize("qid", [q["id"] for q in QUERIES if "where" in q])
def test_a_stated_filter_changes_the_answer_over_the_samples(
    build: Build, answers: dict[str, Rows], qid: str
) -> None:
    assert ask_mf(build, BY_ID[qid], filtered=False) != answers[qid]


def test_row_order_is_not_compared(build: Build, answers: dict[str, Rows]) -> None:
    query = BY_ID["duplicate-payment-rate-by-month"]
    for order in ("asc", "desc"):
        sql = f"select * from ({query['sql']}) order by 1 {order} nulls first"  # noqa: S608
        assert verify(build, query, answers[query["id"]], sql) == []


def a_row_left_out(sql: str, _: Query) -> str:
    return f"select * from ({sql}) offset 1"  # noqa: S608


def a_row_twice(sql: str, _: Query) -> str:
    return f"select * from ({sql}) union all (select * from ({sql}) limit 1)"  # noqa: S608


def a_cent_off(sql: str, query: Query) -> str:
    metric = query["metric"]
    return f"select * replace ({metric} + 0.01 as {metric}) from ({sql})"  # noqa: S608


def a_grain_value_rewritten(sql: str, query: Query) -> str:
    dim = query["group_by"][0]
    return f"select * replace (cast({dim} as varchar) || '-' as {dim}) from ({sql})"  # noqa: S608


GENERIC = {
    "a-row-left-out": a_row_left_out,
    "a-row-twice": a_row_twice,
    "a-cent-off": a_cent_off,
    "a-grain-value-rewritten": a_grain_value_rewritten,
}


@pytest.mark.parametrize(
    ("qid", "plant"),
    [
        (q["id"], p)
        for q in QUERIES
        for p in GENERIC
        if q["group_by"] or p != "a-grain-value-rewritten"
    ],
)
def test_a_wrong_result_is_refused(
    build: Build, answers: dict[str, Rows], qid: str, plant: str
) -> None:
    query = BY_ID[qid]
    assert verify(build, query, answers[qid], GENERIC[plant](query["sql"], query)) != []


# Wrong SQL for a query, as replacements in its text, and the build it is run over, the samples or
# the invented contracts: (query, [(text, replacement), ...], build).
PLANTED_SQL: dict[str, tuple[str, list[tuple[str, str]], str]] = {
    "the-chicago-condition-left-out": (
        "duplicate-payment-rate-by-month",
        [("where entity_id = 'chicago'\n", "")],
        "samples",
    ),
    "payments-with-no-date-left-out": (
        "duplicate-payment-rate-by-month",
        [
            (
                "where entity_id = 'chicago'\n",
                "where entity_id = 'chicago' and payment_date is not null\n",
            )
        ],
        "samples",
    ),
    "the-stated-filter-left-out": (
        "remaining-commitment-on-running-contracts",
        [("where last_ends_on >= date '2026-10-31'\n", "")],
        "samples",
    ),
    "a-month-for-a-year": (
        "spend-against-award-by-approval-year",
        [("date_trunc('year', first_approved_on)", "date_trunc('month', first_approved_on)")],
        "samples",
    ),
    "the-unpaid-part-netted": (
        "remaining-commitment",
        [
            (
                "case when paid < award_held_to then award_held_to - paid else 0 end",
                "award_held_to - paid",
            )
        ],
        "invented",
    ),
    "awards-not-above-0-kept": (
        "paid-over-award-by-break-class",
        [("case when award_held_to > 0 then\n", "case when true then\n")],
        "invented",
    ),
    "the-award-after-revisions-for-the-award-held-to": (
        "spend-against-award",
        [("award_held_to", "award_after_revisions")],
        "invented",
    ),
    "a-null-made-0": (
        "paid-over-award-by-break-class",
        [("sum(\n", "coalesce(sum(\n"), (") as paid_over_award", "), 0) as paid_over_award")],
        "samples",
    ),
    "the-amount-rate-by-rows": (
        "duplicate-payment-amount-rate",
        [("case when is_repeat then amount else 0 end", "case when is_repeat then 1 else 0 end")],
        "samples",
    ),
    "unlike-bases-added": (
        "constant-currency-spend-by-entity",
        [("= 1\n", ">= 1\n")],
        "samples",
    ),
}


@pytest.mark.parametrize("plant", list(PLANTED_SQL))
def test_a_planted_wrong_sql_is_refused(
    build: Build,
    answers: dict[str, Rows],
    invented: Build,
    invented_answers: dict[str, Rows],
    plant: str,
) -> None:
    qid, replacements, over = PLANTED_SQL[plant]
    query = BY_ID[qid]
    wrong = query["sql"]
    for text, replacement in replacements:
        assert text in wrong, f"{qid}'s SQL moved: update {plant}"
        wrong = wrong.replace(text, replacement)
    assert verify(build, query, answers[qid], query["sql"]) == []
    if over == "invented":
        assert verify(invented, query, invented_answers[qid], wrong) != []
    else:
        assert verify(build, query, answers[qid], wrong) != []
