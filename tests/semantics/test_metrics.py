"""The governed finance metrics: defined once in MetricFlow, each owned and named in the glossary.

`mf validate-configs` and `mf query` run over the sample build with the command's own arguments,
through `offline_mf.py`, which refuses every network call. Each metric's value is worked out again:
the Chicago metrics from the served rows themselves (the committed seeds, or the landed files) by
this file's own arithmetic, and constant-currency spend from the group totals' own worked-out
figures (`tests/models/test_group_totals.py`). The landed test pins each metric's real value.
"""

import copy
import csv
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from ingest.fetch import landing_path
from tests.models.dbt_build import PROJECT, Build, built
from tests.models.test_group_totals import worked_out
from tests.semantics.metric_cli import RUNNER, manifest, mf, mf_query
from tests.semantics.offline_mf import REFUSED

SEEDS = PROJECT / "seeds" / "staging" / "chicago"
# The metrics a consumer reads, and the inputs they are built from, which carry an owner and a
# term like any other. Days to pay is not among them: every public file serves one date a payment.
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
    "spend_at_constant_rates",
    "spend_bases",
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
# The landed files' cut-off: a contract ending on or after it is still running.
CUT_OFF = "2026-10-31"
RUNNING = f"{{{{ TimeDimension('contract__ends_on', 'day') }}}} >= '{CUT_OFF}'"


def ownership_problems(
    metrics: list[dict[str, Any]], access: dict[str, Any], glossary: dict[str, Any]
) -> list[str]:
    """Every metric whose MetricFlow meta lacks an owner who is a human persona, or a term the
    glossary holds."""
    kinds = {p["id"]: p["kind"] for p in access["personas"]}
    terms = {t["id"] for t in glossary["terms"]}
    found = []
    for metric in metrics:
        name = metric["name"]
        meta = (metric.get("config") or {}).get("meta") or {}
        owner, term = meta.get("owner"), meta.get("term")
        if owner is None:
            found.append(f"{name}: no owner")
        elif owner not in kinds:
            found.append(f"{name}: owner {owner} is no persona")
        elif kinds[owner] != "human":
            found.append(f"{name}: owner {owner} is not a human persona")
        if term is None:
            found.append(f"{name}: no glossary term")
        elif term not in terms:
            found.append(f"{name}: term {term} is not in the glossary")
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


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"term": "award"}, ["no owner"]),
        ({"owner": "proposer", "term": "award"}, ["owner proposer is not a human persona"]),
        ({"owner": "someone-else", "term": "award"}, ["owner someone-else is no persona"]),
        ({"owner": "metric-owner"}, ["no glossary term"]),
        ({"owner": "metric-owner", "term": "made-up"}, ["term made-up is not in the glossary"]),
        (None, ["no owner", "no glossary term"]),
    ],
)
def test_a_metric_without_a_human_owner_or_a_known_term_is_refused(
    build: Build,
    access: dict[str, Any],
    glossary: dict[str, Any],
    meta: dict[str, str] | None,
    expected: list[str],
) -> None:
    metrics = copy.deepcopy(manifest(build)["metrics"])
    metrics[0]["config"] = None if meta is None else {"meta": meta}
    name = metrics[0]["name"]
    assert ownership_problems(metrics, access, glossary) == [f"{name}: {e}" for e in expected]


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
    running_unpaid_award: Decimal
    running_fixed_award: Decimal
    running_paid: Decimal

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


def worked_out_chicago(
    payments: list[tuple[Any, ...]], contracts: list[tuple[Any, ...]]
) -> Chicago:
    """Payments as served (voucher, amount, check date, department, contract, vendor) and contract
    rows (contract, revision, award, approval date, end date), as text."""
    awards: dict[str, Decimal] = defaultdict(Decimal)
    ends: dict[str, str] = {}
    for contract, *_, end in contracts:
        if end:
            ends[contract] = max(ends.get(contract, end[:10]), end[:10])
    # A revision counts once, however many rows serve it.
    for contract, _, award, _ in {(c, r, a, (d or "")[:10]) for c, r, a, d, _ in contracts}:
        awards[contract] += Decimal(award) if award else Decimal(0)

    rows = [tuple(text(cell) for cell in row) for row in payments]
    seen: Counter[tuple[str | None, ...]] = Counter()

    def same_payment(row: tuple[str | None, ...]) -> tuple[object, ...]:
        voucher, amount, day, _, contract, vendor = row
        return (voucher, Decimal(amount or 0), day, contract, vendor)

    with_department = {same_payment(row) for row in rows if row[3] is not None}
    repeat = []
    for row in rows:
        seen[row] += 1
        repeat.append(seen[row] > 1 or (row[3] is None and same_payment(row) in with_department))
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
    running = {c for c in fixed if ends.get(c, "") >= CUT_OFF}
    return Chicago(
        payment_rows=len(rows),
        repeat_rows=sum(repeat),
        payment_amount=sum((Decimal(r[1] or 0) for r in rows), Decimal(0)),
        repeat_amount=sum(
            (Decimal(r[1] or 0) for r, flag in zip(rows, repeat, strict=True) if flag), Decimal(0)
        ),
        fixed_award=sum(fixed.values(), Decimal(0)),
        paid_against_fixed_award=sum((paid[c] for c in fixed), Decimal(0)),
        unpaid_award=sum((max(a - paid[c], Decimal(0)) for c, a in fixed.items()), Decimal(0)),
        paid_over_award=sum((max(paid[c] - a, Decimal(0)) for c, a in fixed.items()), Decimal(0)),
        running_unpaid_award=sum(
            (max(fixed[c] - paid[c], Decimal(0)) for c in running), Decimal(0)
        ),
        running_fixed_award=sum((fixed[c] for c in running), Decimal(0)),
        running_paid=sum((paid[c] for c in running), Decimal(0)),
    )


PAYMENT_COLUMNS = (
    "voucher_number, amount, check_date, department_name, contract_number, vendor_name"
)
CONTRACT_COLUMNS = (
    "purchase_order_contract_number, revision_number, award_amount, approval_date, end_date"
)


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


def test_the_chicago_metrics_over_the_samples(build: Build) -> None:
    expected = sample_chicago()
    # The samples hold repeats, contracts paid under their award and payments on no contract.
    assert expected.repeat_rows > 0
    assert expected.unpaid_award > 0
    assert_the_chicago_metrics_are_worked_out(build, expected)


def amounts(rows: list[dict[str, str]], key: str, metric: str) -> dict[str, Decimal | None]:
    return {r[key]: Decimal(r[metric]) if r[metric] else None for r in rows}


def assert_constant_currency_spend_is_worked_out(build: Build) -> dict[str, set[str]]:
    """`constant_currency_spend` against the group totals worked out again: by basis, the sum of
    each basis's figures at constant rates; by entity, an entity's sum where it has one basis and
    no figure where it has several; over the whole group, no figure. Returns each entity's bases."""
    totals, _ = worked_out(build)
    by_basis: dict[str, Decimal] = defaultdict(Decimal)
    by_entity: dict[str, Decimal] = defaultdict(Decimal)
    bases: dict[str, set[str]] = defaultdict(set)
    for (entity, _, _, basis), total in totals.items():
        by_basis[basis] += total.constant
        by_entity[entity] += total.constant
        bases[entity].add(basis)

    rows = mf_query(build, ["constant_currency_spend"], ["group_total__basis"])
    assert amounts(rows, "group_total__basis", "constant_currency_spend") == by_basis
    rows = mf_query(
        build, ["constant_currency_spend", "spend_at_constant_rates"], ["group_total__entity_id"]
    )
    assert amounts(rows, "group_total__entity_id", "constant_currency_spend") == {
        e: (total if len(bases[e]) == 1 else None) for e, total in by_entity.items()
    }
    assert amounts(rows, "group_total__entity_id", "spend_at_constant_rates") == by_entity
    (whole,) = mf_query(build, ["constant_currency_spend", "spend_at_constant_rates"])
    assert whole["constant_currency_spend"] == ""
    assert Decimal(whole["spend_at_constant_rates"]) == sum(by_entity.values())
    return dict(bases)


def test_constant_currency_spend_over_the_samples(build: Build) -> None:
    # Over the samples Chicago has two bases in March 2026 and the UK one, so the entity query
    # holds a figure and a refusal.
    bases = assert_constant_currency_spend_is_worked_out(build)
    assert {e: len(b) for e, b in bases.items()} == {"chicago": 2, "uk-transport": 1}


def test_the_runner_refuses_a_network_call_before_it_is_made() -> None:
    probe = (
        "import runpy, socket, sys;"
        f"hook = runpy.run_path({str(RUNNER)!r})['refuse_the_network'];"
        "sys.addaudithook(hook); socket.create_connection(('pypi.org', 443), timeout=1)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == REFUSED, result.stderr
    assert "refused a network call: socket.getaddrinfo" in result.stderr


# Each metric's value over the landed files, as `mf query` gives it.
LANDED = {
    "spend_against_award": "0.590429",
    "remaining_commitment": "70158984044.16",
    "paid_over_award": "5616015460.98",
    "fixed_award": "157586711909.40",
    "paid_against_fixed_award": "93043743326.22",
    "duplicate_payment_rate": "0.020740",
    "duplicate_payment_amount_rate": "0.033467",
    "chicago_payment_rows": "475609",
    "chicago_repeat_rows": "9864",
    "chicago_payment_amount": "119497897723.29",
    "chicago_repeat_amount": "3999229448.98",
}
# Constant-currency spend in euros at the rates of 29 December 2023, by basis and by entity
# (Chicago, split by basis, gives none).
LANDED_BY_BASIS = {
    "cash-outturn": "1157995830000.0",
    "contract-payments": "9478683548.67",
    "direct-vouchers": "14642299522.77",
    "payments-over-25k": "51450165212.22",
    "payments-year-rollup": "9633768642.41",
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

    (running,) = mf_query(build, ["remaining_commitment", "spend_against_award"], where=RUNNING)
    assert Decimal(running["remaining_commitment"]) == expected.running_unpaid_award
    assert Decimal(running["remaining_commitment"]) == Decimal("28443738969.50")
    ratio = expected.running_paid / expected.running_fixed_award
    assert abs(Decimal(running["spend_against_award"]) - ratio) < Decimal("1e-12")

    assert_constant_currency_spend_is_worked_out(build)
    by_basis = mf_query(build, ["constant_currency_spend"], ["group_total__basis"])
    by_entity = mf_query(build, ["constant_currency_spend"], ["group_total__entity_id"])
    basis = {r["group_total__basis"]: r["constant_currency_spend"] for r in by_basis}
    entity = {r["group_total__entity_id"]: r["constant_currency_spend"] or None for r in by_entity}
    print("constant-currency spend by basis:", basis, "by entity:", entity)
    assert basis == LANDED_BY_BASIS
    assert entity == LANDED_BY_ENTITY
