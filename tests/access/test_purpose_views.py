"""BigQuery's purposes (`access/purpose_views.py`): a dataset of authorised views per purpose of
`access/purposes.yaml`, each view the purpose's columns of a table as a persona read it before.

Offline: one dataset per purpose, its id the purpose's with `_` for `-`, and no other declared
dataset's; one view per table the purpose lists, and for a governed copy with a tagged column one
more over its masked copy; each view exactly the purpose's columns, in its order, from the product's
dataset (never the marts), with no filter; the datasets and sources the ones `platform/gcp`
declares; refusals of what the module cannot write; the command refusing a session with no lab
project. The live checks below also run here over a simulated warehouse that applies the row access
policies and dataset grants the catalogues give, and over one changed per kind of fault, each of
which they report in one line.

Live, on the owner's machine (`bigquery_persona`, `make bigquery-purposes`), as each human persona:
through each view of its purposes it reads exactly the rows, per entity, by count and a digest of
the purpose's columns, that the same query returns from the view's source copy for its entities as
an identity that reads every row (the publisher, for a governed copy; the login running the test,
the project's owner, for a group product, which no persona or the publisher reads once the product
grants go), of the copy its tag treatment gives, and none of the other copy; it is refused every
other purpose's dataset; and, once the product grants are removed, every source dataset directly.
That run is the proof that a row access policy acts as the persona querying through an authorised
view. With `LEDGER_LAB_PRODUCT_GRANTS=kept`, while the product grants are still applied, the last
case skips.
"""

import copy
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import pytest

from access import purpose_views
from access.bigquery import PersonaQueryError, PersonaSession, chain, mint, service_account
from access.identities import identity
from access.purpose_views import (
    Found,
    PurposeError,
    View,
    definition,
    drift,
    main,
    plan,
    run_plan,
    same_query,
    survey,
    views,
)
from access.row_policies import PUBLISHER, RowPolicyError, clear_readers, query, rows, run_tool
from tests.conftest import load_aggregates, load_purposes
from tests.platform.test_access_rules import (
    expected_datasets,
    expected_purpose_datasets,
    expected_purpose_sources,
)

PROJECT = "the-lab"
Doc = dict[str, Any]


@pytest.fixture
def aggregates() -> Doc:
    return load_aggregates()


@pytest.fixture
def purposes() -> Doc:
    return load_purposes()


def products_of(access: Doc) -> dict[str, Doc]:
    """Each product table's product, read independently of the module."""
    return {t: p for p in access["data_products"] for t in p.get("tables", [])}


def tagged(columns: Doc, product: str, table: str) -> set[str]:
    listed = columns["products"].get(product, {}).get("columns", {}) or {}
    return {c.split(".", 1)[1] for c in listed if c.split(".", 1)[0] == table}


def test_each_purpose_is_one_dataset_of_one_view_per_table_and_copy(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc
) -> None:
    found = views(access, columns, purposes, aggregates)
    products = products_of(access)
    expected = set()
    for purpose in purposes["purposes"]:
        dataset = purpose["id"].replace("-", "_")
        for table in purpose["tables"]:
            product = products[table]
            expected.add((dataset, table, product["id"].replace("-", "_"), table))
            if product["scope"] == "entity" and tagged(columns, product["id"], table):
                masked = f"{table}_masked"
                expected.add((dataset, masked, product["id"].replace("-", "_"), masked))
    assert {(v.dataset, v.name, v.source_dataset, v.source) for v in found} == expected
    assert len(found) == len(expected)
    assert len(found) == 10


def test_the_datasets_and_sources_are_the_ones_platform_gcp_declares(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc
) -> None:
    found = views(access, columns, purposes, aggregates)
    assert {v.dataset for v in found} == set(expected_purpose_datasets().values())
    assert {(v.dataset, v.source_dataset) for v in found} == expected_purpose_sources()
    assert {v.dataset for v in found} <= expected_datasets()
    # The governed copy and a group product's own table, never the marts dbt replaces.
    assert "intermediate" not in {v.source_dataset for v in found}


def test_each_view_selects_exactly_its_purposes_columns_in_order_and_nothing_else(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc
) -> None:
    listed = {
        (p["id"].replace("-", "_"), table): cols
        for p in purposes["purposes"]
        for table, cols in p["tables"].items()
    }
    statements = plan(access, columns, purposes, aggregates, PROJECT)
    found = views(access, columns, purposes, aggregates)
    assert len(statements) == len(found)
    for view, sql in zip(found, statements, strict=True):
        cols = listed[(view.dataset, view.table)]
        assert sql == (
            f"create or replace view `{PROJECT}.{view.dataset}.{view.name}`"  # noqa: S608
            f" as select {', '.join(cols)} from `{PROJECT}.{view.source_dataset}.{view.source}`"
        )
        for word in (" where ", " join ", " group by ", " union ", " limit ", "session_user"):
            assert word not in sql.lower(), (view.name, word)


def test_the_masked_copys_view_is_for_the_tags_masked_readers_and_the_tables_for_the_clear(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc
) -> None:
    by_name = {(v.dataset, v.name): v for v in views(access, columns, purposes, aggregates)}
    assert by_name[("payment_review", "payments")].audience == "clear"
    assert by_name[("payment_review", "payments_masked")].audience == "masked"
    assert by_name[("group_close", "group_totals")].audience == "all"
    # Group close lists no tagged column, and its persona is a tag's masked reader: it reads the
    # payments rows through the masked copy's view.
    group_close = by_name[("group_close", "payments_masked")]
    assert not set(group_close.columns) & tagged(columns, "payments", "payments")
    assert "group-controller" not in (clear_readers(columns, "payments", "payments") or set())


def planted(purposes: Doc, **changes: Any) -> Doc:
    changed = copy.deepcopy(purposes)
    changed["purposes"][0].update(changes)
    return changed


@pytest.mark.parametrize(
    ("purpose", "words"),
    [
        ("landed", "another declared dataset"),
        ("intermediate", "another declared dataset"),
        ("payments", "another declared dataset"),
        ("group-totals", "another declared dataset"),
        ("payments-aggregates", "another declared dataset"),
        ("audit-log", "another declared dataset"),
        ("audit", "another purpose's dataset"),
    ],
)
def test_a_purpose_whose_dataset_another_dataset_holds_is_refused(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc, purpose: str, words: str
) -> None:
    with pytest.raises(PurposeError, match=words):
        views(access, columns, planted(purposes, id=purpose), aggregates)


def test_every_dataset_a_purpose_cannot_take_is_one_platform_gcp_declares(
    access: Doc, aggregates: Doc
) -> None:
    assert purpose_views.declared_datasets(access, aggregates) == expected_datasets() - set(
        expected_purpose_datasets().values()
    )


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"tables": {"forecasts": ["entity_id"]}}, "no product's table"),
        ({"personas": ["metric-owner"]}, "no row grant on payments"),
        ({"personas": ["nobody"]}, "no persona nobody"),
        ({"tables": {"payments": ["entity_id; drop table x"]}}, "not a column name"),
    ],
    ids=["no table", "no row grant", "no persona", "no column name"],
)
def test_a_purpose_the_module_cannot_write_is_refused(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc, changes: Doc, words: str
) -> None:
    with pytest.raises(PurposeError, match=words):
        plan(access, columns, planted(purposes, **changes), aggregates, PROJECT)


def test_two_views_of_one_name_are_refused(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc
) -> None:
    changed = copy.deepcopy(access)
    changed["data_products"][0]["tables"] = ["payments", "payments_masked"]
    with pytest.raises(PurposeError, match="two views of one name"):
        views(changed, columns, purposes, aggregates)


def test_a_project_outside_its_shape_is_refused(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc
) -> None:
    with pytest.raises(RowPolicyError, match="not a project id"):
        plan(access, columns, purposes, aggregates, "the lab`; drop")


@pytest.mark.parametrize(
    "env",
    [{}, {"LEDGER_LAB_GCP_PROJECT": PROJECT, "GCP_PROJECT": "another-lab"}],
    ids=["no lab project", "another lab's session"],
)
def test_the_command_refuses_a_session_without_the_lab_project(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_gcp(_: object) -> str:
        raise AssertionError("no GCP call outside the lab's environment")

    monkeypatch.setattr(purpose_views, "run_tool", no_gcp)
    for command in ("plan", "apply"):
        assert main([command], env) == 1
    err = capsys.readouterr().err
    assert "purpose-views:" in err and "another-lab" not in err
    assert main(["nothing"], env) == 2


WHERE = "us-central1"
LAB = {"LEDGER_LAB_GCP_PROJECT": PROJECT, "LEDGER_LAB_BQ_LOCATION": WHERE}


class Bq:
    """A stand-in for `bq`: what each purpose dataset holds, as `bq ls` lists its tables and its
    routines, and each statement it was given, dropping or creating the views they name."""

    def __init__(self, held: dict[str, list[tuple[str, str]]]) -> None:
        # Each dataset's entries as (name, kind), a routine's kind `ROUTINE`.
        self.held = {d: list(entries) for d, entries in held.items()}
        self.commands: list[list[str]] = []
        self.statements: list[str] = []

    def __call__(self, command: Sequence[str]) -> str:
        command = list(command)
        self.commands.append(command)
        assert command[:3] == ["bq", f"--project_id={PROJECT}", f"--location={WHERE}"]
        if "ls" in command:
            assert f"--max_results={purpose_views.LISTED}" in command
            dataset = command[-1].removeprefix(f"{PROJECT}:")
            entries = self.held.get(dataset, [])
            if "--routines" in command:
                # BigQuery refuses a routine listing of more than 1000.
                assert purpose_views.LISTED <= 1000, "the routine listing asks for more than 1000"
                return json.dumps(
                    [{"routineReference": {"routineId": n}} for n, k in entries if k == "ROUTINE"]
                )
            return json.dumps(
                [
                    {"tableReference": {"tableId": n}, "type": k}
                    for n, k in entries
                    if k != "ROUTINE"
                ]
            )
        assert "query" in command and any(a.startswith("--maximum_bytes_billed=") for a in command)
        sql = command[-1]
        self.statements.append(sql)
        named = re.search(rf"`{PROJECT}\.([a-z_]+)\.([a-z_]+)`", sql)
        assert named, sql
        dataset, name = named[1], named[2]
        if sql.startswith("drop view if exists "):
            self.held[dataset] = [e for e in self.held.get(dataset, []) if e != (name, "VIEW")]
        elif sql.startswith("create or replace view "):
            entries = self.held.setdefault(dataset, [])
            assert (name, "VIEW") in entries or all(n != name for n, _ in entries), sql
            if (name, "VIEW") not in entries:
                entries.append((name, "VIEW"))
        else:
            raise AssertionError(sql)
        return ""


def generated_held(found: Sequence[View]) -> dict[str, list[tuple[str, str]]]:
    held: dict[str, list[tuple[str, str]]] = {}
    for v in found:
        held.setdefault(v.dataset, []).append((v.name, "VIEW"))
    return held


def catalogues() -> tuple[Doc, Doc, Doc, Doc]:
    load = purpose_views.load
    return (
        load(purpose_views.PERSONAS),
        load(purpose_views.COLUMNS),
        load(purpose_views.PURPOSES),
        load(purpose_views.AGGREGATES),
    )


def test_apply_lists_each_purpose_dataset_then_writes_its_views_capped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    access, columns, purposes, aggregates = catalogues()
    stub = Bq({})
    monkeypatch.setattr(purpose_views, "run_tool", stub)
    assert main(["apply"], LAB) == 0
    datasets = sorted({v.dataset for v in views(access, columns, purposes, aggregates)})
    listed = [c[-1] for c in stub.commands if "ls" in c]
    assert listed == [f"{PROJECT}:{d}" for d in datasets for _ in ("tables", "routines")]
    assert stub.statements == plan(access, columns, purposes, aggregates, PROJECT)
    assert "10 views written, 0 not generated dropped" in capsys.readouterr().out
    # A second apply over what the first wrote finds nothing to drop.
    stub.commands.clear()
    stub.statements.clear()
    assert main(["apply"], LAB) == 0
    assert not [s for s in stub.statements if s.startswith("drop ")]


def narrowed(purposes: Doc, purpose: str, *tables: str) -> Doc:
    changed = copy.deepcopy(purposes)
    (entry,) = [p for p in changed["purposes"] if p["id"] == purpose]
    for table in tables:
        del entry["tables"][table]
    return changed


@pytest.mark.parametrize(
    ("purpose", "tables", "dropped"),
    [
        ("group-close", ["group_totals_left_out"], [("group_close", "group_totals_left_out")]),
        (
            "group-close",
            ["payments"],
            [("group_close", "payments"), ("group_close", "payments_masked")],
        ),
        (
            "audit",
            ["group_totals", "group_totals_left_out"],
            [("audit", "group_totals"), ("audit", "group_totals_left_out")],
        ),
    ],
    ids=["one table of a source", "every view of one table", "every table of a source"],
)
def test_a_view_a_purpose_no_longer_lists_is_dropped_before_any_view_is_written(
    purpose: str, tables: list[str], dropped: list[tuple[str, str]]
) -> None:
    """The views written before stay in the dataset, under its authorization on the source, until
    a run drops them: `plan` prints the drops and runs nothing, `apply` runs them first."""
    access, columns, purposes, aggregates = catalogues()
    before = views(access, columns, purposes, aggregates)
    after = views(access, columns, narrowed(purposes, purpose, *tables), aggregates)
    drops = [f"drop view if exists `{PROJECT}.{d}.{n}`" for d, n in dropped]
    stub = Bq(generated_held(before))
    assert run_plan(after, PROJECT, WHERE, stub, write=False) == (
        drops,
        [v.sql(PROJECT) for v in after],
    )
    assert stub.statements == []
    run_plan(after, PROJECT, WHERE, stub, write=True)
    assert stub.statements == [*drops, *(v.sql(PROJECT) for v in after)]
    assert stub.held == generated_held(after)
    assert survey(after, PROJECT, WHERE, stub).stale == ()


def test_plan_prints_the_drops_first_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    access, columns, purposes, aggregates = catalogues()
    generated = views(access, columns, purposes, aggregates)
    held = generated_held(generated)
    held["group_close"].append(("gone", "VIEW"))
    stub = Bq(held)
    monkeypatch.setattr(purpose_views, "run_tool", stub)
    assert main(["plan"], LAB) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"drop view if exists `{PROJECT}.group_close.gone`",
        *(v.sql(PROJECT) for v in generated),
    ]
    assert stub.statements == []


@pytest.mark.parametrize(
    ("entry", "said"),
    [
        (("copy_of_payments", "TABLE"), "group_close.copy_of_payments (table)"),
        (("kept", "MATERIALIZED_VIEW"), "group_close.kept (materialized_view)"),
        (("snap", "SNAPSHOT"), "group_close.snap (snapshot)"),
        (("reader", "ROUTINE"), "group_close.reader (routine)"),
        (("group_totals", "TABLE"), "group_close.group_totals (table)"),
        (("Odd-Name`", "VIEW"), "1 more whose names it cannot write"),
    ],
    ids=["a table", "a materialized view", "a snapshot", "a routine", "a generated name", "a name"],
)
def test_anything_else_in_a_purpose_dataset_is_refused_never_dropped_and_stops_the_writes(
    entry: tuple[str, str], said: str
) -> None:
    access, columns, purposes, aggregates = catalogues()
    generated = views(access, columns, purposes, aggregates)
    held = generated_held(generated)
    held["group_close"] = [e for e in held["group_close"] if e[0] != entry[0]]
    # Beside it, a stale view, which is still dropped.
    held["group_close"] += [entry, ("gone", "VIEW")]
    stub = Bq(held)
    with pytest.raises(PurposeError) as refused:
        run_plan(generated, PROJECT, WHERE, stub, write=True)
    assert said in str(refused.value) and "\n" not in str(refused.value)
    assert stub.statements == [f"drop view if exists `{PROJECT}.group_close.gone`"]
    assert entry in stub.held["group_close"]


def test_the_command_reports_a_refusal_or_a_bq_failure_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    access, columns, purposes, aggregates = catalogues()
    held = generated_held(views(access, columns, purposes, aggregates))
    held["audit"].append(("copy", "TABLE"))
    monkeypatch.setattr(purpose_views, "run_tool", Bq(held))
    for command in ("plan", "apply"):
        assert main([command], LAB) == 1
        (line,) = capsys.readouterr().err.splitlines()
        assert line.startswith("purpose-views: the purpose datasets hold") and "audit.copy" in line

    def failing(command: Sequence[str]) -> str:
        raise subprocess.CalledProcessError(2, list(command), "", f"denied on {PROJECT}")

    monkeypatch.setattr(purpose_views, "run_tool", failing)
    assert main(["apply"], LAB) == 1
    assert capsys.readouterr().err == "purpose-views: bq failed with exit status 2\n"


@pytest.mark.parametrize(
    "answer",
    ["not json", json.dumps([{"type": "VIEW"}]), json.dumps({"tableReference": 1})],
    ids=["not json", "no reference", "not a list"],
)
def test_a_listing_of_another_shape_is_refused(answer: str) -> None:
    with pytest.raises(PurposeError, match="unexpected shape"):
        purpose_views.contents(PROJECT, WHERE, "audit", lambda _: answer)


def test_a_listing_that_may_leave_entries_out_is_refused() -> None:
    full = json.dumps(
        [
            {"tableReference": {"tableId": f"v{i}"}, "type": "VIEW"}
            for i in range(purpose_views.LISTED)
        ]
    )
    with pytest.raises(PurposeError, match="may leave entries out"):
        purpose_views.contents(PROJECT, WHERE, "audit", lambda _: full)


def test_the_survey_names_the_views_missing_and_a_stored_query_is_compared_as_written() -> None:
    access, columns, purposes, aggregates = catalogues()
    generated = views(access, columns, purposes, aggregates)
    found = [Found(v.dataset, v.name, "VIEW") for v in generated[1:]]
    assert drift(generated, found).missing == (generated[0],)
    query_text = generated[0].select(PROJECT)
    assert same_query(f"  {query_text.upper()} ;\n", query_text)
    assert not same_query(query_text + " where entity_id = 'chicago'", query_text)
    shown = json.dumps({"type": "VIEW", "view": {"query": query_text, "useLegacySql": False}})
    assert definition(PROJECT, WHERE, "payment_review", "payments", lambda _: shown) == query_text
    with pytest.raises(PurposeError, match="no view definition"):
        definition(PROJECT, WHERE, "payment_review", "payments", lambda _: "{}")


# The live checks, written over a warehouse that answers the summaries they compare, so they run
# live as each persona and offline over a simulated warehouse alike.

ENTITY = "entity_id"
SAFE = re.compile(r"^[a-z][a-z0-9_]*$")
ENTITY_ID = re.compile(r"^[a-z][a-z0-9-]*$")


class Warehouse(Protocol):
    def summary(
        self, dataset: str, table: str, columns: Sequence[str], entities: frozenset[str] | None
    ) -> list[tuple[Any, ...]]:
        """Per entity (when the columns hold `entity_id`), the rows' count and a digest of the
        columns, of the entities given or every row."""
        ...

    def count(self, dataset: str, table: str) -> int: ...


def digested(view: View, columns: Doc) -> tuple[str, ...]:
    """The view's columns its source's every-row reader may read: a tagged column of the clear copy
    is left out, since its policy tag refuses it to any account without the tag's clear role."""
    if view.audience != "clear":
        return view.columns
    tags = tagged(columns, view.product, view.table)
    return tuple(c for c in view.columns if c not in tags)


def ours(view: View, columns: Doc, persona: str) -> bool:
    """Whether the persona's tag treatment gives it the view's source copy."""
    if view.audience == "all":
        return True
    clear = clear_readers(columns, view.product, view.table) or frozenset()
    return (view.audience == "clear") == (persona in clear)


def reads_through_purposes(
    persona: str,
    access: Doc,
    columns: Doc,
    found: Sequence[View],
    purposes: Doc,
    as_persona: Warehouse,
    reference: Callable[[View], Warehouse],
) -> list[str]:
    """Each view of the persona's purposes against its source as an every-row reader reads it:
    the persona's copy holds exactly its entities' rows, the other copy none."""
    mine = {p["id"] for p in purposes["purposes"] if persona in p["personas"]}
    reads = identity(access, persona).reads
    failures = []
    for view in (v for v in found if v.purpose in mine):
        where = f"{persona}: {view.dataset}.{view.name}"
        digest = digested(view, columns)
        source = reference(view)
        if source.count(view.source_dataset, view.source) == 0:
            failures.append(f"{where}: its source holds no rows, so the view proves nothing")
            continue
        try:
            seen = as_persona.summary(view.dataset, view.name, digest, None)
        except PersonaQueryError as error:
            failures.append(f"{where}: {error}")
            continue
        if not ours(view, columns, persona):
            if seen:
                failures.append(f"{where}: returned rows of the copy its tags do not give it")
            continue
        want = source.summary(view.source_dataset, view.source, digest, reads[view.product])
        if seen != want:
            failures.append(f"{where}: returned other rows than its source holds for it")
    return failures


def refused(as_persona: Warehouse, places: Sequence[tuple[str, str]], persona: str) -> list[str]:
    failures = []
    for dataset, table in places:
        try:
            as_persona.count(dataset, table)
        except PersonaQueryError as error:
            if error.status == 403:
                continue
            failures.append(f"{persona}: {dataset}.{table}: {error}")
            continue
        failures.append(f"{persona}: {dataset}.{table} was not refused")
    return failures


def other_purposes_views(persona: str, found: Sequence[View], purposes: Doc) -> list[View]:
    mine = {p["id"] for p in purposes["purposes"] if persona in p["personas"]}
    return [v for v in found if v.purpose not in mine]


def sources(found: Sequence[View]) -> list[tuple[str, str]]:
    return sorted({(v.source_dataset, v.source) for v in found})


# The simulated warehouse: the governed copies under one row access policy per entity granted as
# access/row_policies.py grants them, a group product's table for all its readers, each view
# reading its source as its querying persona, and dataset grants as platform/gcp gives them.


@dataclass
class Simulated:
    """BigQuery as the catalogues set it up, with a fault to plant: `leak` lets the views read
    every row of their source, `granted` adds a dataset a persona reads, `same_copy` gives the
    masked readers rows of the clear copy too."""

    access: Doc
    columns: Doc
    found: Sequence[View]
    purposes: Doc
    leak: bool = False
    granted: dict[str, set[str]] = field(default_factory=dict)
    same_copy: bool = False
    tables: dict[tuple[str, str], list[dict[str, str]]] = field(init=False)

    def __post_init__(self) -> None:
        payments = [
            {"entity_id": e, "amount": str(n), "currency": "X"}
            for e, n in (("chicago", 1), ("chicago", 2), ("uk-transport", 3))
        ]
        totals = [
            {"entity_id": "chicago", "amount": "9"},
            {"entity_id": "uk-transport", "amount": "8"},
        ]
        self.tables = {
            ("payments", "payments"): payments,
            ("payments", "payments_masked"): payments,
            ("group_totals", "group_totals"): totals,
            ("group_totals", "group_totals_left_out"): totals[:1],
        }

    def datasets(self, persona: str | None) -> set[str]:
        if persona is None:
            return {d for d, _ in self.tables}
        held = {
            v.dataset
            for v in self.found
            for p in self.purposes["purposes"]
            if p["id"] == v.purpose and persona in p["personas"]
        }
        return held | self.granted.get(persona, set())

    def visible(self, persona: str | None, dataset: str, table: str) -> list[dict[str, str]]:
        """The source's rows the persona gets, by the row access policies or a group table."""
        rows = self.tables[(dataset, table)]
        if persona is None or dataset != "payments" or self.leak:
            return rows
        product = "payments"
        entities = identity(self.access, persona).reads.get(product) or frozenset()
        clear = clear_readers(self.columns, product, "payments") or frozenset()
        mine = table.endswith("_masked") != (persona in clear) or self.same_copy
        return [r for r in rows if r["entity_id"] in entities] if mine else []

    def as_persona(self, persona: str | None) -> Warehouse:
        simulated = self

        class Session:
            def read(self, dataset: str, table: str) -> list[dict[str, str]]:
                if dataset not in simulated.datasets(persona):
                    raise PersonaQueryError("refused", 403, "accessDenied")
                view = next(
                    (v for v in simulated.found if (v.dataset, v.name) == (dataset, table)), None
                )
                if view is None:
                    return simulated.visible(persona, dataset, table)
                return simulated.visible(persona, view.source_dataset, view.source)

            def summary(
                self,
                dataset: str,
                table: str,
                columns: Sequence[str],
                entities: frozenset[str] | None,
            ) -> list[tuple[Any, ...]]:
                rows = [
                    r
                    for r in self.read(dataset, table)
                    if entities is None or r["entity_id"] in entities
                ]
                keys = sorted({r["entity_id"] for r in rows})
                return [
                    (k, str(sum(1 for r in rows if r["entity_id"] == k)), "digest") for k in keys
                ]

            def count(self, dataset: str, table: str) -> int:
                return len(self.read(dataset, table))

        return Session()


def offline_run(simulated: Simulated, access: Doc, columns: Doc, purposes: Doc) -> list[str]:
    """Every live check, for every human persona, over the simulated warehouse."""
    failures: list[str] = []
    for persona in (p["id"] for p in access["personas"] if p["kind"] == "human"):
        session = simulated.as_persona(persona)
        failures += reads_through_purposes(
            persona,
            access,
            columns,
            simulated.found,
            purposes,
            session,
            lambda _: simulated.as_persona(None),
        )
        others = other_purposes_views(persona, simulated.found, purposes)
        failures += refused(session, [(v.dataset, v.name) for v in others], persona)
        failures += refused(session, sources(simulated.found), persona)
    return failures


def test_the_live_checks_pass_over_a_warehouse_set_up_as_the_catalogues_say(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc
) -> None:
    found = views(access, columns, purposes, aggregates)
    assert offline_run(Simulated(access, columns, found, purposes), access, columns, purposes) == []


def test_the_live_checks_report_each_kind_of_fault_in_one_line(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc
) -> None:
    found = views(access, columns, purposes, aggregates)

    def run(**fault: Any) -> list[str]:
        return offline_run(
            Simulated(access, columns, found, purposes, **fault), access, columns, purposes
        )

    other_rows = "returned other rows than its source holds for it"
    not_its_copy = "returned rows of the copy its tags do not give it"
    leaked = run(leak=True)
    assert f"accountant-chicago: payment_review.payments_masked: {other_rows}" in leaked
    assert f"accountant-chicago: payment_review.payments: {not_its_copy}" in leaked
    both = run(same_copy=True)
    assert f"group-controller: group_close.payments: {not_its_copy}" in both
    assert not [f for f in both if "other rows" in f]
    widened = run(granted={"approver": {"group_close", "payments"}})
    assert set(widened) == {
        "approver: group_close.payments was not refused",
        "approver: group_close.payments_masked was not refused",
        "approver: group_close.group_totals was not refused",
        "approver: group_close.group_totals_left_out was not refused",
        "approver: payments.payments was not refused",
        "approver: payments.payments_masked was not refused",
    }
    for failure in [*leaked, *both, *widened]:
        assert "\n" not in failure and PROJECT not in failure


# Live, as each persona, on the owner's machine.

PRODUCT_GRANTS = "LEDGER_LAB_PRODUCT_GRANTS"


def summary_sql(
    project: str,
    dataset: str,
    table: str,
    columns: Sequence[str],
    entities: frozenset[str] | None,
) -> str:
    """Per entity, the rows' count and an order-free digest of the columns, as text, so every
    reader's answer compares as given; names checked against their shapes, as the text is SQL."""
    for name in (dataset, table, *columns):
        if not SAFE.fullmatch(name):
            raise PurposeError(f"{name!r} is not a dataset, table or column name")
    for entity in entities or ():
        if not ENTITY_ID.fullmatch(entity):
            raise PurposeError(f"{entity!r} is not an entity id")
    digest = (
        "CAST(BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(STRUCT("
        + ", ".join(columns)
        + ")))) AS STRING) AS digest"
    )
    where = ""
    if entities is not None:
        listed = ", ".join(f"'{e}'" for e in sorted(entities)) or "NULL"
        where = f" WHERE {ENTITY} IN ({listed})"
    keyed = ENTITY in columns
    keys = f"{ENTITY}, " if keyed else ""
    tail = f" GROUP BY {ENTITY} ORDER BY {ENTITY}" if keyed else ""
    return (
        f"SELECT {keys}CAST(COUNT(*) AS STRING) AS n, {digest}"  # noqa: S608
        f" FROM `{project}.{dataset}.{table}`{where}{tail}"
    )


def count_sql(project: str, dataset: str, table: str) -> str:
    for name in (dataset, table):
        if not SAFE.fullmatch(name):
            raise PurposeError(f"{name!r} is not a dataset or table name")
    return f"SELECT CAST(COUNT(*) AS STRING) AS n FROM `{project}.{dataset}.{table}`"  # noqa: S608


def test_the_summary_compares_counts_and_a_digest_per_entity_of_the_entities_given() -> None:
    sql = summary_sql(
        PROJECT, "payments", "payments", ["entity_id", "amount"], frozenset({"chicago"})
    )
    assert sql == (
        "SELECT entity_id, CAST(COUNT(*) AS STRING) AS n, CAST(BIT_XOR(FARM_FINGERPRINT("  # noqa: S608
        "TO_JSON_STRING(STRUCT(entity_id, amount)))) AS STRING) AS digest"
        f" FROM `{PROJECT}.payments.payments` WHERE entity_id IN ('chicago')"
        " GROUP BY entity_id ORDER BY entity_id"
    )
    assert " WHERE " not in summary_sql(PROJECT, "audit", "payments", ["entity_id"], None)
    with pytest.raises(PurposeError):
        summary_sql(PROJECT, "audit", "payments", ["entity_id"], frozenset({"chi'cago"}))
    with pytest.raises(PurposeError):
        count_sql(PROJECT, "audit", "pay`ments")


@dataclass
class Live:
    """A warehouse read by one identity: a persona's session, the publisher or the login."""

    project: str
    run: Callable[[str], list[tuple[Any, ...]]]

    def summary(
        self, dataset: str, table: str, columns: Sequence[str], entities: frozenset[str] | None
    ) -> list[tuple[Any, ...]]:
        return self.run(summary_sql(self.project, dataset, table, columns, entities))

    def count(self, dataset: str, table: str) -> int:
        ((n,),) = self.run(count_sql(self.project, dataset, table))
        return int(n)


READ: dict[tuple[str, str], list[tuple[Any, ...]]] = {}


def as_publisher(project: str) -> Callable[[str], list[tuple[Any, ...]]]:
    """The publisher reads every row of a governed copy (its row access policy is `TRUE`), and
    no tagged column of the clear copy, which the summaries leave out; each answer read once."""

    def run(sql: str) -> list[tuple[Any, ...]]:
        if ("publisher", sql) not in READ:
            accounts = chain(service_account(PUBLISHER, project), project, os.environ)
            with httpx.Client(timeout=60) as client:
                READ[("publisher", sql)] = (
                    PersonaSession(project, mint(accounts), client).query(sql).rows
                )
        return READ[("publisher", sql)]

    return run


def as_login(project: str, where: str) -> Callable[[str], list[tuple[Any, ...]]]:
    """The login running the test, the project's owner, reads a group product's table, which no
    persona or the publisher reads once the product grants go; by `bq`, capped as the module's
    statements are, each answer read once."""

    def run(sql: str) -> list[tuple[Any, ...]]:
        if ("login", sql) not in READ:
            answer = rows(run_tool(query(project, where, sql)))
            READ[("login", sql)] = [
                tuple(row[k] for k in (ENTITY, "n", "digest") if k in row) for row in answer
            ]
        return READ[("login", sql)]

    return run


def reference_for(project: str, where: str) -> Callable[[View], Warehouse]:
    def reference(view: View) -> Warehouse:
        governed = view.source_dataset == "payments"
        return Live(project, as_publisher(project) if governed else as_login(project, where))

    return reference


def persona_reads(session: PersonaSession, project: str) -> Live:
    return Live(project, lambda sql: session.query(sql).rows)


def lab_catalogues() -> tuple[Doc, Doc, Doc, list[View]]:
    from tests.conftest import load_access, load_columns

    access, columns, purposes = load_access(), load_columns(), load_purposes()
    return access, columns, purposes, views(access, columns, purposes, load_aggregates())


@pytest.mark.bigquery_persona
def test_each_persona_reads_through_its_purposes_exactly_its_rows_of_its_copy(
    as_persona_on_bigquery: Callable[[str], PersonaSession], human: str
) -> None:
    from ingest.load import lab

    project, where = lab(os.environ)
    access, columns, purposes, found = lab_catalogues()
    session = persona_reads(as_persona_on_bigquery(human), project)
    failures = reads_through_purposes(
        human, access, columns, found, purposes, session, reference_for(project, where)
    )
    if failures:
        pytest.fail("; ".join(failures))


@pytest.mark.bigquery_persona
def test_each_persona_is_refused_every_other_purposes_views(
    as_persona_on_bigquery: Callable[[str], PersonaSession], human: str
) -> None:
    from gcp_project import lab_project

    project = lab_project()
    _, _, purposes, found = lab_catalogues()
    others = [(v.dataset, v.name) for v in other_purposes_views(human, found, purposes)]
    failures = refused(persona_reads(as_persona_on_bigquery(human), project), others, human)
    if failures:
        pytest.fail("; ".join(failures))


@pytest.mark.bigquery_persona
def test_each_persona_is_refused_the_source_datasets_the_purpose_views_read(
    as_persona_on_bigquery: Callable[[str], PersonaSession], human: str
) -> None:
    """Holds once the product grants are removed; skipped while `LEDGER_LAB_PRODUCT_GRANTS=kept`
    says they are still applied."""
    from gcp_project import lab_project

    kept = os.environ.get(PRODUCT_GRANTS, "").strip()
    if kept == "kept":
        pytest.skip("the product grants are still applied: run again after the full apply")
    if kept:
        pytest.fail(f"{PRODUCT_GRANTS} is neither unset nor kept")
    project = lab_project()
    _, _, _, found = lab_catalogues()
    failures = refused(persona_reads(as_persona_on_bigquery(human), project), sources(found), human)
    if failures:
        pytest.fail("; ".join(failures))


@pytest.mark.bigquery_persona
def test_each_purpose_dataset_holds_exactly_its_generated_views(
    as_persona_on_bigquery: Callable[[str], PersonaSession],
) -> None:
    """Read as the login running the test, by `bq ls` and `bq show`, free metadata reads; the
    persona fixture is taken, unused, so a failure reports as a persona test's does, in one
    line."""
    from ingest.load import lab

    project, where = lab(os.environ)
    _, _, _, generated = lab_catalogues()
    try:
        found = survey(generated, project, where, run_tool)
        failures = [
            f"{e.dataset}.{e.name} ({e.kind.lower()}) is not generated" for e in found.stale
        ]
        failures += [found.refusal()] if found.refused else []
        failures += [f"{v.dataset}.{v.name} is missing" for v in found.missing]
        missing = set(found.missing)
        for view in (v for v in generated if v not in missing):
            stored = definition(project, where, view.dataset, view.name, run_tool)
            if not same_query(stored, view.select(project)):
                failures.append(
                    f"{view.dataset}.{view.name} holds another query than its generated one"
                )
    except subprocess.CalledProcessError as error:
        pytest.fail(f"bq failed with exit status {error.returncode}")
    except PurposeError as error:
        pytest.fail(str(error))
    if failures:
        pytest.fail("; ".join(failures))


def test_the_live_tests_carry_their_marker() -> None:
    for test in (
        test_each_purpose_dataset_holds_exactly_its_generated_views,
        test_each_persona_reads_through_its_purposes_exactly_its_rows_of_its_copy,
        test_each_persona_is_refused_every_other_purposes_views,
        test_each_persona_is_refused_the_source_datasets_the_purpose_views_read,
    ):
        assert "bigquery_persona" in {m.name for m in test.pytestmark}  # type: ignore[attr-defined]


def test_the_failures_name_no_project_or_account(
    access: Doc, columns: Doc, purposes: Doc, aggregates: Doc
) -> None:
    """A failure line names the persona and the view; the project, accounts and token stay out,
    as `tests/conftest.py`'s report hook expects of a `pytest.fail`."""
    found = views(access, columns, purposes, aggregates)
    broken = Simulated(access, columns, found, purposes, leak=True)
    for line in offline_run(broken, access, columns, purposes):
        assert "@" not in line and "iam.gserviceaccount.com" not in line
        assert not set(line) & {"`", "\n"}
