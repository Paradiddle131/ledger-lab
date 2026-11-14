"""The governed finance metrics: defined once in MetricFlow, each owned and named in the glossary.

`mf validate-configs` and `mf query` run over the sample build with the command's own arguments,
through `semantics/offline/offline_mf.py`, which refuses every network call. Each metric's value is
worked out again: the Chicago metrics from the served rows themselves (the committed seeds, or the
landed files) by this file's own arithmetic, the contract metrics also over invented contracts
labelled as such, and constant-currency spend from the group totals' own worked-out figures
(`tests/models/test_group_totals.py`). The landed test pins each metric's real value.
"""

import csv
import json
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import load_checked

from ingest.fetch import landing_path
from semantics.offline.refusal import REFUSED
from tests.models.dbt_build import DBT, PROJECT, Build, built, query
from tests.models.test_group_totals import worked_out
from tests.semantics.metric_cli import MF, manifest, mf, mf_env, mf_query

SEEDS = PROJECT / "seeds" / "staging" / "chicago"
SEMANTICS = PROJECT.parent / "semantics"
# Every metric, headline and input alike, each carrying an owner, a term and a role.
METRICS = {
    "spend_against_award",
    "remaining_commitment",
    "paid_over_award",
    "fixed_award",
    "paid_against_fixed_award",
    "duplicate_payment_rate",
    "duplicate_payment_amount_rate",
    "chicago_payment_rows",
    "chicago_repeat_rows",
    "chicago_payment_amount",
    "chicago_repeat_amount",
    "constant_currency_spend",
    "spend_in_periods",
    "spend_in_years",
    "spend_bases_in_periods",
    "spend_bases_in_years",
}
CHICAGO_METRICS = [
    "spend_against_award",
    "remaining_commitment",
    "paid_over_award",
    "fixed_award",
    "paid_against_fixed_award",
    "duplicate_payment_rate",
    "duplicate_payment_amount_rate",
    "chicago_payment_rows",
    "chicago_repeat_rows",
    "chicago_payment_amount",
    "chicago_repeat_amount",
]
CONTRACT_METRICS = [
    "spend_against_award",
    "remaining_commitment",
    "paid_over_award",
    "fixed_award",
    "paid_against_fixed_award",
]
ROLES = {"headline", "input"}
# The five finance metrics the platform promises, by id in `semantics/finance_metrics.yaml`.
PROMISED = {
    "spend-against-award",
    "remaining-commitment",
    "days-to-pay",
    "duplicate-payment-rate",
    "constant-currency-spend",
}


def ownership_problems(
    metrics: list[dict[str, Any]], access: dict[str, Any], glossary: dict[str, Any]
) -> list[str]:
    """Every metric whose MetricFlow meta lacks an owner who is a human persona and the owner of
    its glossary term, a term the glossary holds, or a role (headline or input)."""
    kinds = {p["id"]: p["kind"] for p in access["personas"]}
    term_owners = {t["id"]: t.get("owner") for t in glossary["terms"]}
    found = []
    for metric in metrics:
        name = metric["name"]
        meta = (metric.get("config") or {}).get("meta") or {}
        owner, term, role = meta.get("owner"), meta.get("term"), meta.get("role")
        if owner is None:
            found.append(f"{name}: no owner")
        elif owner not in kinds:
            found.append(f"{name}: owner {owner} is no persona")
        elif kinds[owner] != "human":
            found.append(f"{name}: owner {owner} is not a human persona")
        if term is None:
            found.append(f"{name}: no glossary term")
        elif term not in term_owners:
            found.append(f"{name}: term {term} is not in the glossary")
        elif kinds.get(owner) == "human" and owner != term_owners[term]:
            found.append(f"{name}: owner {owner} does not own term {term}")
        if role not in ROLES:
            found.append(f"{name}: no role")
    return found


def test_mf_validate_configs_passes(build: Build) -> None:
    result = mf(build, "validate-configs")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    # The manifest's own semantics, then the semantic models, dimensions, entities, measures and
    # metrics against the built warehouse.
    assert out.count("(ERRORS: 0, FUTURE_ERRORS: 0, WARNINGS: 0)") == 6, out


def test_the_metrics_are_defined_once(build: Build) -> None:
    names = [m["name"] for m in manifest(build)["metrics"]]
    assert Counter(names) == Counter(METRICS)


def test_every_metric_has_a_human_owner_and_a_glossary_term(
    build: Build, access: dict[str, Any], glossary: dict[str, Any]
) -> None:
    assert ownership_problems(manifest(build)["metrics"], access, glossary) == []


def test_dbt_parses_only_the_metricflow_folder_of_semantics(build: Build) -> None:
    files = {m["original_file_path"] for m in manifest_nodes(build)}
    names = ("contracts", "payments", "group_totals")
    assert files == {f"../semantics/metricflow/{n}.yml" for n in names}


def manifest_nodes(build: Build) -> list[dict[str, Any]]:
    """Every node dbt parsed from outside `models/`."""
    nodes = json.loads((build.cwd / "target" / "manifest.json").read_text())
    found = []
    for kind in ("nodes", "semantic_models", "metrics", "sources", "exposures", "saved_queries"):
        found += [n for n in nodes.get(kind, {}).values() if "semantics" in n["original_file_path"]]
    return found


def test_the_promised_metrics_map_to_headline_metrics(
    build: Build, glossary: dict[str, Any]
) -> None:
    promised = load_checked(
        SEMANTICS / "finance_metrics.yaml", SEMANTICS / "finance_metrics.schema.json"
    )["finance_metrics"]
    assert {p["id"] for p in promised} == PROMISED
    roles = {m["name"]: m["config"]["meta"]["role"] for m in manifest(build)["metrics"]}
    listed = [name for p in promised for name in p.get("metrics", [])]
    assert len(listed) == len(set(listed))
    assert set(listed) == {name for name, role in roles.items() if role == "headline"}
    assert {p["id"] for p in promised if "not_definable" in p} == {"days-to-pay"}
    terms = {t["id"] for t in glossary["terms"]}
    assert {p["term"] for p in promised} <= terms


def test_every_input_says_it_is_not_for_direct_use(build: Build) -> None:
    for metric in manifest(build)["metrics"]:
        says = metric["description"].strip().startswith("Not for direct use")
        assert says == (metric["config"]["meta"]["role"] == "input"), metric["name"]


PLANTED = "      meta: {owner: payables-clerk, term: spend-against-award, role: headline}\n"


def parsed_with(tmp_path: Path, meta: str | None) -> dict[str, Any]:
    """The semantic manifest `dbt parse` writes for a copy of the project whose
    `spend_against_award` carries `meta` in place of its own (none at all when `meta` is None)."""
    copied = tmp_path / "project"
    ignore = shutil.ignore_patterns("target", "logs", "dbt_packages", "__pycache__")
    shutil.copytree(PROJECT, copied / PROJECT.name, ignore=ignore)
    shutil.copytree(SEMANTICS, copied / SEMANTICS.name, ignore=ignore)
    contracts = copied / SEMANTICS.name / "metricflow" / "contracts.yml"
    source = contracts.read_text()
    assert source.count(PLANTED) == 1, "spend_against_award's meta moved: update PLANTED"
    planted = "    config:\n" + PLANTED
    replacement = "" if meta is None else f"    config:\n      meta: {meta}\n"
    contracts.write_text(source.replace(planted, replacement))
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_LAB_GCP_PROJECT"}
    env |= {
        "LEDGER_LAB_DUCKDB_PATH": str(tmp_path / "unused.duckdb"),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    result = subprocess.run(
        [str(DBT), "parse", "--project-dir", str(copied / PROJECT.name)]
        + ["--profiles-dir", str(copied / PROJECT.name), "--target", "local"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((tmp_path / "target" / "semantic_manifest.json").read_text())


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ("{term: award, role: input}", ["no owner"]),
        ("{owner: proposer, term: award, role: input}", ["owner proposer is not a human persona"]),
        (
            "{owner: someone-else, term: award, role: input}",
            ["owner someone-else is no persona"],
        ),
        ("{owner: payables-clerk, role: input}", ["no glossary term"]),
        (
            "{owner: payables-clerk, term: made-up, role: input}",
            ["term made-up is not in the glossary"],
        ),
        (
            "{owner: metric-owner, term: spend-against-award, role: headline}",
            ["owner metric-owner does not own term spend-against-award"],
        ),
        ("{owner: payables-clerk, term: spend-against-award}", ["no role"]),
        (
            "{owner: payables-clerk, term: spend-against-award, role: other}",
            ["no role"],
        ),
        (None, ["no owner", "no glossary term", "no role"]),
    ],
)
def test_a_metric_without_a_human_owner_or_a_known_term_is_refused(
    tmp_path: Path,
    access: dict[str, Any],
    glossary: dict[str, Any],
    meta: str | None,
    expected: list[str],
) -> None:
    """Each planted case is written into the metric's own YAML and read back through dbt's parse,
    as `make check` reads the real definitions."""
    metrics = parsed_with(tmp_path, meta)["metrics"]
    assert ownership_problems(metrics, access, glossary) == [
        f"spend_against_award: {e}" for e in expected
    ]


def text(value: object) -> str | None:
    """A served cell as text, blank as missing."""
    return None if value is None or value == "" else str(value)


@dataclass
class Chicago:
    """The Chicago metrics worked out from the served rows."""

    payment_rows: int
    repeat_rows: int
    payment_amount: Decimal
    repeat_amount: Decimal
    fixed_award: Decimal
    paid_against_fixed_award: Decimal
    unpaid_award: Decimal
    paid_over_award: Decimal
    fixed_contracts: int

    def values(self) -> dict[str, Decimal]:
        return {
            "spend_against_award": self.paid_against_fixed_award / self.fixed_award,
            "remaining_commitment": self.unpaid_award,
            "paid_over_award": self.paid_over_award,
            "fixed_award": self.fixed_award,
            "paid_against_fixed_award": self.paid_against_fixed_award,
            "duplicate_payment_rate": Decimal(self.repeat_rows) / self.payment_rows,
            "duplicate_payment_amount_rate": self.repeat_amount / self.payment_amount,
            "chicago_payment_rows": Decimal(self.payment_rows),
            "chicago_repeat_rows": Decimal(self.repeat_rows),
            "chicago_payment_amount": self.payment_amount,
            "chicago_repeat_amount": self.repeat_amount,
        }


def held_to(revisions: list[tuple[str | None, Decimal]]) -> Decimal:
    """A contract's award held to, from its revisions (approval day, amount), each once: the
    award after revisions, or, where revisions that are not all 0 net to 0, the highest award in
    force after any approval day."""
    after = sum((amount for _, amount in revisions), Decimal(0))
    if after != 0 or all(amount == 0 for _, amount in revisions):
        return after
    by_day: dict[str | None, Decimal] = defaultdict(Decimal)
    for day, amount in revisions:
        by_day[day] += amount
    in_force, peak = Decimal(0), None
    for day in sorted(by_day, key=lambda d: (d is None, d or "")):
        in_force += by_day[day]
        peak = in_force if peak is None else max(peak, in_force)
    assert peak is not None
    return peak


def worked_out_chicago(
    payments: list[tuple[Any, ...]], contracts: list[tuple[Any, ...]]
) -> Chicago:
    """Payments as served (voucher, amount, check date, department, contract, vendor) and contract
    rows (contract, revision, award, approval date), as text."""
    revisions: dict[str, list[tuple[str | None, Decimal]]] = defaultdict(list)
    # A revision counts once, however many rows serve it.
    for contract, _, award, day in {(c, r, a, text(d) and d[:10]) for c, r, a, d in contracts}:
        revisions[contract].append((day, Decimal(award) if award else Decimal(0)))
    awards = {contract: held_to(rows) for contract, rows in revisions.items()}

    rows = [tuple(text(cell) for cell in row) for row in payments]
    seen: Counter[tuple[str | None, ...]] = Counter()

    def same_payment(row: tuple[str | None, ...], *, any_name: bool) -> tuple[object, ...]:
        voucher, amount, day, _, contract, vendor = row
        return (voucher, Decimal(amount or 0), day, contract, None if any_name else vendor)

    with_department = {
        same_payment(row, any_name=any_name)
        for row in rows
        if row[3] is not None
        for any_name in (False, True)
    }
    repeat = []
    for row in rows:
        seen[row] += 1
        # A copy, or a row without a department beside one with it, the vendor name the same or
        # not.
        repeat.append(
            seen[row] > 1
            or (row[3] is None and same_payment(row, any_name=True) in with_department)
        )
    dated: dict[tuple[str | None, str | None, str], Decimal] = defaultdict(Decimal)
    for row, is_repeat in zip(rows, repeat, strict=True):
        _, amount, day, _, contract, vendor = row
        if not is_repeat and day is not None and len(day) == 10:
            dated[vendor, contract, day[6:]] += Decimal(amount or 0)
    for at, (_, amount, day, _, contract, vendor) in enumerate(rows):
        key = (vendor, contract, day or "")
        if not repeat[at] and day is not None and len(day) == 4 and key in dated:
            repeat[at] = dated[key] == Decimal(amount or 0)

    paid: dict[str, Decimal] = defaultdict(Decimal)
    for (_, amount, _, _, contract, _), is_repeat in zip(rows, repeat, strict=True):
        if not is_repeat and contract in awards:
            paid[contract] += Decimal(amount or 0)
    fixed = {c: award for c, award in awards.items() if award > 0}
    return Chicago(
        payment_rows=len(rows),
        repeat_rows=sum(repeat),
        payment_amount=sum((Decimal(r[1] or 0) for r in rows), Decimal(0)),
        repeat_amount=sum(
            (Decimal(r[1] or 0) for r, flag in zip(rows, repeat, strict=True) if flag), Decimal(0)
        ),
        fixed_contracts=len(fixed),
        fixed_award=sum(fixed.values(), Decimal(0)),
        paid_against_fixed_award=sum((paid[c] for c in fixed), Decimal(0)),
        unpaid_award=sum((max(a - paid[c], Decimal(0)) for c, a in fixed.items()), Decimal(0)),
        paid_over_award=sum((max(paid[c] - a, Decimal(0)) for c, a in fixed.items()), Decimal(0)),
    )


PAYMENT_COLUMNS = (
    "voucher_number, amount, check_date, department_name, contract_number, vendor_name"
)
CONTRACT_COLUMNS = "purchase_order_contract_number, revision_number, award_amount, approval_date"


def sample_chicago() -> Chicago:
    def read(name: str, columns: str) -> list[tuple[Any, ...]]:
        with (SEEDS / name).open(newline="") as rows:
            names = [c.strip() for c in columns.split(",")]
            return [tuple(row[c] for c in names) for row in csv.DictReader(rows)]

    return worked_out_chicago(
        read("chicago_payments_sample.csv", PAYMENT_COLUMNS),
        read("chicago_contracts_sample.csv", CONTRACT_COLUMNS),
    )


def assert_the_chicago_metrics_are_worked_out(build: Build, expected: Chicago) -> dict[str, str]:
    (row,) = mf_query(build, CHICAGO_METRICS)
    for name, value in expected.values().items():
        got = Decimal(row[name])
        if name.endswith("rate") or name == "spend_against_award":
            assert abs(got - value) < Decimal("1e-12"), name
        else:
            assert got == value, name
    return row


def assert_the_contract_metrics_agree_with_the_reconciliation(
    build: Build, expected: Chicago
) -> dict[str, tuple[Decimal, Decimal]]:
    """The contract metrics read the reconciliation's own figures for the contracts it holds to an
    award above 0: their count, and over that population its positive differences are
    `paid_over_award`; by break class, the metrics add up to their totals. Returns each class's
    paid over award and remaining commitment."""
    ((contracts, over),) = query(
        build,
        "select count(*), coalesce(sum(case when difference > 0 then difference else 0 end), 0)"
        " from chicago_contract_reconciliation where award_held_to > 0",
    )
    assert contracts == expected.fixed_contracts
    assert over == expected.paid_over_award
    rows = mf_query(build, ["paid_over_award", "remaining_commitment"], ["contract__break_class"])
    by_class = {
        r["contract__break_class"]: (
            Decimal(r["paid_over_award"] or 0),
            Decimal(r["remaining_commitment"] or 0),
        )
        for r in rows
    }
    assert sum(o for o, _ in by_class.values()) == expected.paid_over_award
    assert sum(u for _, u in by_class.values()) == expected.unpaid_award
    # A number only payments carry and a blanket have no award to be paid over or to remain.
    for kind in ("contract_not_found", "no_fixed_award", "negative_award"):
        assert by_class.get(kind, (0, 0)) == (0, 0), kind
    return by_class


def test_the_chicago_metrics_over_the_samples(build: Build) -> None:
    expected = sample_chicago()
    # The samples hold repeats, contracts paid under their award and payments on no contract.
    assert expected.repeat_rows > 0
    assert expected.unpaid_award > 0
    assert_the_chicago_metrics_are_worked_out(build, expected)
    assert_the_contract_metrics_agree_with_the_reconciliation(build, expected)


def amounts(rows: list[dict[str, str]], key: str, metric: str) -> dict[str, Decimal | None]:
    return {r[key]: Decimal(r[metric]) if r[metric] else None for r in rows}


def assert_constant_currency_spend_is_worked_out(build: Build) -> dict[str, set[str]]:
    """`constant_currency_spend` against the group totals worked out again: by basis, the sum of
    each basis's figures at constant rates; by basis and calendar year of the span's start, the
    same split by year; by entity, an entity's sum where it has one basis and no figure where it
    has several; over the whole group, no figure. A query by month is refused, and by the period
    start's month a whole year's figure has no month. Returns each entity's bases."""
    totals, _ = worked_out(build)
    starts = {
        (entity, year, period, basis): start
        for entity, year, period, basis, start in query(
            build,
            "select entity_id, fiscal_year, fiscal_period, basis, period_start from group_totals",
        )
    }
    by_basis: dict[str, Decimal] = defaultdict(Decimal)
    by_year: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    by_entity: dict[str, Decimal] = defaultdict(Decimal)
    in_years = Decimal(0)
    bases: dict[str, set[str]] = defaultdict(set)
    for key, total in totals.items():
        entity, _, period, basis = key
        by_basis[basis] += total.constant
        by_year[basis, f"{starts[key].year}-01-01 00:00:00"] += total.constant
        by_entity[entity] += total.constant
        bases[entity].add(basis)
        if period is None:
            in_years += total.constant

    rows = mf_query(build, ["constant_currency_spend"], ["group_total__basis"])
    assert amounts(rows, "group_total__basis", "constant_currency_spend") == by_basis
    rows = mf_query(build, ["constant_currency_spend"], ["group_total__basis", "metric_time__year"])
    assert {
        (r["group_total__basis"], r["metric_time__year"]): Decimal(r["constant_currency_spend"])
        for r in rows
    } == by_year
    rows = mf_query(build, ["constant_currency_spend"], ["group_total__entity_id"])
    assert amounts(rows, "group_total__entity_id", "constant_currency_spend") == {
        e: (total if len(bases[e]) == 1 else None) for e, total in by_entity.items()
    }
    (whole,) = mf_query(build, ["constant_currency_spend"])
    assert whole["constant_currency_spend"] == ""

    refused = mf(
        build, "query", "--metrics", "constant_currency_spend", "--group-by", "metric_time__month"
    )
    assert refused.returncode != 0
    assert "does not match any of the available group-by-items" in refused.stdout + refused.stderr
    rows = mf_query(
        build,
        ["constant_currency_spend"],
        ["group_total__basis", "group_total__period_start__month"],
    )
    no_month = [r for r in rows if not r["group_total__period_start__month"]]
    assert sum((Decimal(r["constant_currency_spend"]) for r in no_month), Decimal(0)) == in_years
    return dict(bases)


def test_constant_currency_spend_over_the_samples(build: Build) -> None:
    # Over the samples Chicago has two bases in March 2026 and the UK one, so the entity query
    # holds a figure and a refusal; no whole year has rates.
    bases = assert_constant_currency_spend_is_worked_out(build)
    assert {e: len(b) for e, b in bases.items()} == {"chicago": 2, "uk-transport": 1}


# Invented contracts, labelled as invented, in the reconciliation's columns: the award held to,
# the award after revisions, what was paid, the difference and the class. The samples hold no
# contract paid over a fixed award, none held to an award other than its award after revisions
# and no negative award, so these are what prove the contract metrics' rules.
INVENTED = [
    ("UNDER", 1000, 1000, 300.25, "unspent"),
    ("OVER", 500, 500, 650, "unexplained"),
    ("NETZERO", 800, 0, 900, "unexplained"),
    ("NETZEROUNDER", 400, 0, 100, "unspent"),
    ("BLANKET", 0, 0, 40, "no_fixed_award"),
    ("NEVERFIXED", 0, 0, 10, "unexplained"),
    ("NEGATIVE", -20, -20, 10, "negative_award"),
    ("NOTFOUND", None, None, 99, "contract_not_found"),
]


def test_the_contract_metrics_over_invented_contracts(build: Build, tmp_path: Path) -> None:
    """Over the invented contracts: only an award held to above 0 is fixed (held to, not the award
    after revisions), the unpaid part and the part paid over are each floored at 0 per contract
    and never netted, and a blanket, a negative award, an award never above 0 and a number the
    contracts table lacks stay out."""
    (tmp_path / "target").mkdir()
    shutil.copy(build.cwd / "target" / "semantic_manifest.json", tmp_path / "target")
    database = tmp_path / "ledger_lab.duckdb"
    with duckdb.connect(str(database)) as db:
        db.execute(
            "create table chicago_contract_reconciliation (contract_number varchar,"
            " award_held_to decimal(18, 2), award_after_revisions decimal(18, 2),"
            " paid decimal(18, 2), difference decimal(18, 2), break_class varchar,"
            " is_break boolean, is_term_agreement boolean, in_contracts_table boolean,"
            " first_approved_on date, last_ends_on date)"
        )
        db.executemany(
            "insert into chicago_contract_reconciliation values"
            " (?, ?, ?, ?, ? - ?, ?, false, false, ? is not null, date '2020-01-02', null)",
            [(c, h, a, p, p, h, k, h) for c, h, a, p, k in INVENTED],
        )
    invented = Build(
        "", database, {}, build.env | {"LEDGER_LAB_DUCKDB_PATH": str(database)}, tmp_path
    )
    (row,) = mf_query(invented, CONTRACT_METRICS)
    got = {k: Decimal(v) for k, v in row.items()}
    assert got["fixed_award"] == Decimal("2700")
    assert got["paid_against_fixed_award"] == Decimal("1950.25")
    assert got["remaining_commitment"] == Decimal("999.75")
    assert got["paid_over_award"] == Decimal("250")
    assert abs(got["spend_against_award"] - Decimal("1950.25") / 2700) < Decimal("1e-12")


def test_a_process_on_the_mf_path_refuses_a_network_call_before_it_is_made(
    build: Build,
) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import socket; socket.create_connection(('pypi.org', 443), 1)"],
        env=mf_env(build),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == REFUSED, result.stderr
    assert "refused a network call: socket.getaddrinfo" in result.stderr


def test_mf_without_the_runner_is_stopped_at_its_update_check(build: Build) -> None:
    """The plain `mf` command, with the update check on, is ended at its request to PyPI, so a
    test that bypassed the runner would fail rather than reach the network."""
    result = subprocess.run(
        [str(MF), "validate-configs"],
        env=mf_env(build),
        capture_output=True,
        text=True,
        check=False,
        cwd=build.cwd,
    )
    assert result.returncode == REFUSED, result.stdout + result.stderr
    assert "refused a network call: socket.getaddrinfo ('pypi.org'" in result.stderr


# Each metric's value over the landed files, as `mf query` gives it.
LANDED = {
    "spend_against_award": "0.570307",
    "remaining_commitment": "71379226730.41",
    "paid_over_award": "3454795094.89",
    "fixed_award": "158076545794.10",
    "paid_against_fixed_award": "90152114158.58",
    "duplicate_payment_rate": "0.029106",
    "duplicate_payment_amount_rate": "0.058861",
    "chicago_payment_rows": "475609",
    "chicago_repeat_rows": "13843",
    "chicago_payment_amount": "119497897723.29",
    "chicago_repeat_amount": "7033758721.22",
}
# Contracts held to an award above 0.
LANDED_FIXED_CONTRACTS = 86_218
# Paid over award and remaining commitment by the reconciliation's break class, where either is
# above 0. The reconciliation's own `unexplained` dollars over the award are $1,800,918.54 more:
# 24 contracts whose revisions net to 0 and whose award in force never rose above 0, which hold no
# fixed award here.
LANDED_BY_CLASS = {
    "paid_before_fixed_award": ("355512087.25", "0.00"),
    "paid_in_full": ("53.43", "70.40"),
    "paid_under_two_names": ("720778722.70", "0.00"),
    "reduced_after_payment": ("2327908.21", "0.00"),
    "unexplained": ("2376176323.30", "0.00"),
    "unpaid": ("0.00", "19780804494.40"),
    "unspent": ("0.00", "51598422165.61"),
}
# Remaining commitment by the contract's latest end date against the files' cut-off.
LANDED_LIVE = "28601363714.34"
LANDED_NO_END = "10980642673.62"
# Constant-currency spend in euros at the rates of 29 December 2023, by basis and by entity
# (Chicago, split by basis, gives none).
LANDED_BY_BASIS = {
    "cash-outturn": "1157995830000.0",
    "contract-payments": "9117744901.34",
    "direct-vouchers": "14642299522.77",
    "payments-over-25k": "51450165212.22",
    "payments-year-rollup": "9200083450.73",
}
LANDED_BY_ENTITY = {
    "chicago": None,
    "nl-government": "1157995830000.0",
    "uk-transport": "51450165212.22",
}


@pytest.mark.landed
def test_the_metrics_over_the_landed_files(tmp_path: Path) -> None:
    landing = landing_path().resolve()
    for name in (
        "chicago-contracts",
        "chicago-payments",
        "gleif-legal-entities",
        "uk-transport-spend-2025",
        "ecb-rates",
        "nl-government-budget",
    ):
        if not (landing / name).exists():
            pytest.skip(f"{name} is not landed")
    build = built(tmp_path, "landed", landing)
    assert mf(build, "validate-configs").returncode == 0

    with duckdb.connect() as db:
        served = db.execute(
            f"select {PAYMENT_COLUMNS} from read_parquet(?)",  # noqa: S608
            [str(landing / "chicago-payments")],
        ).fetchall()
        contracts = db.execute(
            f"select {CONTRACT_COLUMNS} from read_parquet(?)",  # noqa: S608
            [str(landing / "chicago-contracts")],
        ).fetchall()
    expected = worked_out_chicago(served, contracts)
    row = assert_the_chicago_metrics_are_worked_out(build, expected)
    places = {k: len(v.split(".")[1]) if "." in v else 0 for k, v in LANDED.items()}
    got = {k: f"{Decimal(row[k]):.{places[k]}f}" for k in LANDED}
    print("landed metrics:", got)
    assert got == LANDED

    assert expected.fixed_contracts == LANDED_FIXED_CONTRACTS
    by_class = assert_the_contract_metrics_agree_with_the_reconciliation(build, expected)
    classes = {c: (f"{o:.2f}", f"{u:.2f}") for c, (o, u) in by_class.items() if o or u}
    print("paid over award and remaining commitment by break class:", classes)
    assert classes == LANDED_BY_CLASS
    ((held_to_zero, excess),) = query(
        build,
        "select count(*), sum(difference) from chicago_contract_reconciliation"
        " where break_class = 'unexplained' and award_held_to <= 0",
    )
    assert (held_to_zero, f"{excess:.2f}") == (24, "1800918.54")
    ends = "{{ TimeDimension('contract__last_ends_on', 'day') }}"
    (live,) = mf_query(build, ["remaining_commitment"], where=f"{ends} >= '2026-10-31'")
    (no_end,) = mf_query(build, ["remaining_commitment"], where=f"{ends} is null")
    assert (live["remaining_commitment"], no_end["remaining_commitment"]) == (
        LANDED_LIVE,
        LANDED_NO_END,
    )

    assert_constant_currency_spend_is_worked_out(build)
    by_basis = mf_query(build, ["constant_currency_spend"], ["group_total__basis"])
    by_entity = mf_query(build, ["constant_currency_spend"], ["group_total__entity_id"])
    basis = {r["group_total__basis"]: r["constant_currency_spend"] for r in by_basis}
    entity = {r["group_total__entity_id"]: r["constant_currency_spend"] or None for r in by_entity}
    print("constant-currency spend by basis:", basis, "by entity:", entity)
    assert basis == LANDED_BY_BASIS
    assert entity == LANDED_BY_ENTITY
