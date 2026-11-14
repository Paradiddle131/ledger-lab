"""Chicago's payments tied to their contracts' awards: one row per contract number, each with the
one class that says why its paid total and award differ.

The reconciliation is a control. It passes while every contract number has exactly one class and
the count and dollars of every class equal a reviewed baseline exactly, so any new or grown break
fails until a person re-pins the baseline in a reviewed change. `make check` proves the mechanism
on the committed cut (a planted break moves the cut's pinned table); the control itself is the
landed run over the full files (`make reconcile-landed`), whose baseline is pinned here: each
class's count and dollars, and for each break class a digest of its contract numbers and their
excess (`reconciliation_state.py`), so a break that grows on one contract while another shrinks
fails too. Where an accepted run (`make reconcile-accept`) left its per-contract list beside the
landing, a failure names the contract numbers that entered, left or changed excess.

The classes are also worked out a second time here, in Python, from the rows the model starts from
(each contract's revisions and types, each payment the repeat rule does not flag). That second
implementation follows the same definitions as the SQL, so it guards the implementation against
drift and slips, not the definitions themselves.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ingest.fetch import landing_path
from tests.models.dbt_build import Build, built, dbt, query
from tests.models.reconciliation_state import (
    accepted_path,
    break_rows,
    changes,
    digests,
    read_accepted,
)
from tests.models.test_payments_mart import CUT, land

BREAKS = {
    "contract_not_found",
    "negative_award",
    "paid_under_two_names",
    "reduced_after_payment",
    "paid_before_fixed_award",
    "unexplained",
}
NOT_BREAKS = {"no_fixed_award", "unpaid", "paid_in_full", "unspent"}
DIME = Decimal("0.05")
# The cut's reviewed baseline: contracts and dollars per class (dollars over the award, or paid
# where the contracts table lacks the number).
CUT_BASELINE = {
    "unpaid": (30, Decimal(0)),
    "unspent": (7, Decimal(0)),
    "paid_in_full": (4, Decimal(0)),
    "contract_not_found": (2, Decimal("388319.79")),
    "no_fixed_award": (1, Decimal(0)),
}
# The real landing's reviewed baseline, the control's pass condition over the full files.
LANDED_BASELINE = {
    "unspent": (32_250, Decimal(0)),
    "unpaid": (26_500, Decimal(0)),
    "paid_in_full": (25_288, Decimal("53.43")),
    "no_fixed_award": (3_852, Decimal("2573863451.53")),
    "unexplained": (1_940, Decimal("2377977241.84")),
    "paid_under_two_names": (301, Decimal("720778722.70")),
    "contract_not_found": (205, Decimal("192143987.81")),
    "paid_before_fixed_award": (120, Decimal("355512087.25")),
    "reduced_after_payment": (40, Decimal("2327908.21")),
    "negative_award": (28, Decimal("80723676.66")),
}
# The same run's digest per break class: SHA-256 of its contract numbers and their excess.
LANDED_DIGESTS = {
    "contract_not_found": "bfda103994d3bbab223840e963b65b52778af48db901ec7c8d3cd8cd564948a1",  # noqa: E501  # pragma: allowlist secret
    "negative_award": "ca0b49c3a55f9ca9b7fc489811a9cc9c2422801139570f8338b6aaaa8ddc059c",  # noqa: E501  # pragma: allowlist secret
    "paid_under_two_names": "ada984e4bc209b629b2a75fdc51945839f666395ae8381ff25c70426a790c1f2",  # noqa: E501  # pragma: allowlist secret
    "reduced_after_payment": "ff9537ae11bd260418620e9d30429b59de6b7d19a3a15b328d2e3ea7c2807b8a",  # noqa: E501  # pragma: allowlist secret
    "paid_before_fixed_award": "e50dab2dab193673cd6b6843f1330b6530408f9ac8bcc902323c4407ccf137ae",  # noqa: E501  # pragma: allowlist secret
    "unexplained": "c206b699b6024b9127fe29a09e64d32fcd642403b6e5488d3edacfa4d518d6bf",  # noqa: E501  # pragma: allowlist secret
}


@dataclass(frozen=True)
class Payment:
    contract: str
    document: str | None
    amount: Decimal
    day: date | None
    year: int | None
    vendor: str

    @property
    def paid_by(self) -> date:
        """The end of the period the payment covers: its day, or 31 December of its year."""
        if self.day is not None:
            return self.day
        assert self.year is not None
        return date(self.year, 12, 31)


def expected_classes(build: Build) -> dict[str, tuple[str, Decimal]]:
    """Each contract number's class and paid total, by the classes' definitions, from the
    revisions, the contract types and the payments the repeat rule does not flag."""
    revisions: defaultdict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for number, approved_on, amount in query(
        build,
        "select contract_number, approved_on, award_amount from int_chicago__contract_revisions",
    ):
        revisions[number].append((approved_on, amount))
    paid: defaultdict[str, list[Payment]] = defaultdict(list)
    for row in query(
        build,
        "select contract_number, document_number, amount, payment_date, rolled_up_year,"
        " vendor_name from int_chicago__payments"
        " where contract_number is not null and not is_repeat",
    ):
        paid[row[0]].append(Payment(*row))

    def in_force(number: str, day: date) -> Decimal | None:
        amounts = [a for d, a in revisions[number] if d <= day]
        return sum(amounts, Decimal(0)) if amounts else None

    classes: dict[str, tuple[str, Decimal]] = {}
    for number in revisions.keys() | paid.keys():
        payments = paid.get(number, [])
        total = sum((p.amount for p in payments), Decimal(0))
        if number not in revisions:
            classes[number] = ("contract_not_found", total)
            continue
        days = sorted({d for d, _ in revisions[number]})
        running = [(d, in_force(number, d) or Decimal(0)) for d in days]
        award = sum((a for _, a in revisions[number]), Decimal(0))
        nonzero = any(a != 0 for _, a in revisions[number])
        held = max(a for _, a in running) if award == 0 and nonzero else award
        allowance = DIME * len(revisions[number])
        within = held + allowance
        names: defaultdict[tuple[object, ...], set[str]] = defaultdict(set)
        for p in payments:
            names[(p.document, p.amount, p.day, p.year)].add(p.vendor)
        again = sum(
            (
                p.amount
                for p in payments
                if p.vendor != min(names[(p.document, p.amount, p.day, p.year)])
            ),
            Decimal(0),
        )
        last = max((p.paid_by for p in payments), default=None)
        at_last = in_force(number, last) if last else None
        fixed_on = min((d for d, a in running if a > 0), default=None)
        before_fixed = sum(
            (p.amount for p in payments if fixed_on is None or p.paid_by < fixed_on), Decimal(0)
        )
        if held < 0:
            kind = "negative_award"
        elif not nonzero:
            kind = "no_fixed_award"
        elif not payments:
            kind = "unpaid"
        elif abs(total - held) <= allowance:
            kind = "paid_in_full"
        elif total < held:
            kind = "unspent"
        elif total - again <= within:
            kind = "paid_under_two_names"
        elif at_last is not None and total <= at_last + allowance:
            kind = "reduced_after_payment"
        elif fixed_on is not None and total - before_fixed <= within:
            kind = "paid_before_fixed_award"
        else:
            kind = "unexplained"
        classes[number] = (kind, total)
    return classes


def reconciled(build: Build) -> dict[str, tuple[str, Decimal]]:
    return {
        number: (kind, paid)
        for number, kind, paid in query(
            build, "select contract_number, break_class, paid from chicago_contract_reconciliation"
        )
    }


def class_table(build: Build) -> dict[str, tuple[int, Decimal]]:
    """Contracts and dollars per class: over the award held to, or paid where not found."""
    return {
        kind: (n, Decimal(total))
        for kind, n, total in query(
            build,
            "select break_class, count(*), sum(case when difference > 0 then difference"
            " when break_class = 'contract_not_found' then paid else 0 end)"
            " from chicago_contract_reconciliation group by 1",
        )
    }


@pytest.mark.parametrize(
    "rule",
    [
        "chicago_contract_reconciliation::each_contract_takes_the_first_class_that_holds",
        "int_chicago__payments::a_payment_under_a_second_name_without_a_department_is_a_repeat",
        "unique_chicago_contract_reconciliation_contract_number",
        "not_null_chicago_contract_reconciliation_break_class",
        "no_break_hides_in_a_class_that_is_not_one",
    ],
)
def test_each_reconciliation_rule_ran_and_passed(build: Build, rule: str) -> None:
    if "::" in rule:
        assert any("PASS" in line and rule in line for line in build.stdout.splitlines())
    else:
        assert build.results.get(rule) == "pass"


def test_every_contract_number_of_the_cut_has_its_class_and_paid_total(build: Build) -> None:
    """Every contract of the cut and every number its payments carry that the contracts lack is
    one row, with the class and paid total the second implementation finds; direct vouchers are
    on none; and the cut's table of classes is its reviewed baseline."""
    found = reconciled(build)
    assert found == expected_classes(build)
    numbers = {c["purchase_order_contract_number"] for c in CUT["contracts"]}
    numbers |= {p["contract_number"] for p in CUT["payments"] if p["contract_number"] != "DV"}
    assert found.keys() == numbers
    assert class_table(build) == CUT_BASELINE
    is_break = dict(
        query(
            build,
            "select break_class, bool_and(is_break) from chicago_contract_reconciliation"
            " group by 1",
        )
    )
    assert {k for k, b in is_break.items() if b} <= BREAKS
    assert {k for k, b in is_break.items() if not b} <= NOT_BREAKS


def test_the_paid_totals_leave_out_repeats_and_add_up_to_every_contract_payment(
    build: Build,
) -> None:
    rows = query(
        build,
        "select sum(paid), sum(paid_in_repeats), sum(payment_rows + repeat_rows)"
        " from chicago_contract_reconciliation",
    )
    served = [p for p in CUT["payments"] if p["contract_number"] != "DV"]
    firsts, repeats = query(
        build,
        "select sum(case when not is_repeat then amount end), sum(case when is_repeat then amount"
        " end) from int_chicago__payments where contract_number is not null",
    )[0]
    assert rows == [(firsts, repeats, len(served))]
    assert repeats > 0, "the cut holds a repeat on a contract"


def test_the_reconciliation_control_fails_on_a_planted_break(build: Build, tmp_path: Path) -> None:
    """The cut landed with one invented payment (labelled so, never data) that takes a contract
    awarded $1,333,731 to $5m paid: the cut's table of classes no longer equals its baseline, by
    exactly that contract and its excess, so the control fails until the baseline is re-pinned."""
    invented = {
        "voucher_number": "INVENTED1",
        "amount": "5000000",
        "check_date": "05/01/2025",
        "contract_number": "18419",
        "vendor_name": "LEVEL 3 COMMUNICATIONS INC",
    }
    landing = tmp_path / "landing"
    landing.mkdir()
    land({"contracts": CUT["contracts"], "payments": [*CUT["payments"], invented]}, landing)
    env = build.env | {
        "LEDGER_LAB_LANDING": str(landing),
        "LEDGER_LAB_DUCKDB_PATH": str(tmp_path / "planted.duckdb"),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
    }
    result = dbt(
        env,
        tmp_path,
        "build",
        "--select",
        "+chicago_contract_reconciliation",
        "--indirect-selection",
        "cautious",
        "--vars",
        json.dumps({"source_rows": "landed"}),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    planted = Build(result.stdout, tmp_path / "planted.duckdb", {}, env, tmp_path)
    table = class_table(planted)
    assert table != CUT_BASELINE
    assert {k: v for k, v in table.items() if v != CUT_BASELINE.get(k)} == {
        "unpaid": (29, Decimal(0)),
        "unexplained": (1, Decimal(5_000_000) - Decimal(1_333_731)),
    }
    assert reconciled(planted) == expected_classes(planted)


@pytest.mark.landed
def test_the_real_landing_reconciles_every_contract(tmp_path: Path) -> None:
    """The control over the real landing: every contract number has one class, the second
    implementation agrees on each, and the count and dollars of every class are the reviewed
    baseline; the reconciliation with repeats counted is beside it, and the largest unexplained
    breaks are printed."""
    landing = landing_path().resolve()
    for name in (
        "chicago-contracts",
        "chicago-payments",
        "gleif-legal-entities",
        "uk-transport-spend-2025",
    ):
        if not (landing / name).exists():
            pytest.skip(f"{name} is not landed")
    build = built(tmp_path, "landed", landing)
    table = class_table(build)
    print("contracts and dollars over (paid where not found) by class:", table)
    rows = break_rows(build)
    accepted = accepted_path(landing)
    if accepted.exists():
        report = changes(read_accepted(accepted), rows) or [
            "no break differs from the accepted run"
        ]
    else:
        report = [f"no accepted run at {accepted}: `make reconcile-accept` writes one"]
    print("against the accepted run:", *report, sep="\n  ")
    assert (table, digests(rows)) == (LANDED_BASELINE, LANDED_DIGESTS), "\n".join(report)
    assert reconciled(build) == expected_classes(build)
    # Every payment on a contract number is in the reconciliation once, repeats beside it, and
    # the 452 rows on numbers the contracts table lacks are all there.
    totals = query(
        build,
        "select sum(paid + paid_in_repeats), sum(payment_rows + repeat_rows),"
        " sum(case when not in_contracts_table then payment_rows + repeat_rows end),"
        " sum(case when not in_contracts_table then paid + paid_in_repeats end)"
        " from chicago_contract_reconciliation",
    )
    served = query(
        build,
        "select sum(amount), count(*) from int_chicago__payments where contract_number is not null",
    )
    assert totals == [(*served[0], 452, Decimal("193626608.54"))]
    # The repeats counted: contracts over their award only because of them.
    both_ways = query(
        build,
        "select count(*) filter (where paid + paid_in_repeats - award_held_to"
        " > rounding_allowance and difference <= rounding_allowance),"
        " sum(paid_in_repeats) filter (where paid + paid_in_repeats - award_held_to"
        " > rounding_allowance and difference <= rounding_allowance),"
        " count(*) filter (where abs(paid + paid_in_repeats - award_held_to)"
        " <= rounding_allowance and payment_rows > 0)"
        " from chicago_contract_reconciliation where break_class <> 'no_fixed_award'",
    )
    print("over only with repeats (contracts, repeats), in full with repeats:", both_ways)
    assert both_ways == [(2_349, Decimal("4474400193.37"), 24_393)]
    tags = query(
        build,
        "select count(*) filter (where is_term_agreement), count(*) filter (where"
        " excess_in_2002_row), count(*) filter (where paid_again_under_another_name > 0)"
        " from chicago_contract_reconciliation where break_class = 'unexplained'",
    )
    print(
        "unexplained that are term agreements, all excess in the 2002 row, with second names:", tags
    )
    unexplained = query(
        build,
        "select contract_number, award_held_to, paid, difference, is_term_agreement,"
        " excess_in_2002_row from chicago_contract_reconciliation"
        " where break_class = 'unexplained' order by difference desc limit 10",
    )
    print("largest unexplained breaks:", unexplained)
