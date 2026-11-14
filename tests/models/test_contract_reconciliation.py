"""Chicago's payments tied to their contracts' awards: one row per contract number, each with the
one class that says why its paid total and award differ.

The classes are worked out again here, by this file's own arithmetic over the rows the models
start from (each contract's revisions and types, each payment that is not a repeat), and held to
the model's on the committed cut and, in the landed test, on every contract of the real landing.
The reconciliation is a control: it passes while every contract number has one known class and
the unexplained breaks stay within the bound the singular test states, and it fails once a break
none of the classes explains appears.
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ingest.fetch import landing_path
from tests.models.dbt_build import Build, built, dbt, query
from tests.models.test_payments_mart import CUT, land

BREAKS = {
    "contract_not_found",
    "negative_award",
    "paid_under_two_names",
    "reduced_after_payment",
    "paid_before_fixed_award",
    "term_agreement",
    "rolled_up_before_2003",
    "unexplained",
}
NOT_BREAKS = {"no_fixed_award", "unpaid", "paid_in_full", "unspent"}
DIME = Decimal("0.05")


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
    revisions, the contract types and the payments that are not repeats."""
    revisions: defaultdict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for number, approved_on, amount in query(
        build,
        "select contract_number, approved_on, award_amount from int_chicago__contract_revisions",
    ):
        revisions[number].append((approved_on, amount))
    term = {
        number
        for number, typed in query(
            build,
            "select contract_number, bool_or(contract_type = 'Term Agreement')"
            " from stg_chicago__contracts group by 1",
        )
        if typed or re.fullmatch(r"T[0-9]{5}", number)
    }
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
        award = sum((a for _, a in revisions[number]), Decimal(0))
        allowance = DIME * len(revisions[number])
        # A payment served again under another vendor name: every name after the first.
        names: defaultdict[tuple[object, ...], set[str]] = defaultdict(set)
        for p in payments:
            names[(p.document, p.amount, p.day, p.year)].add(p.vendor)
        firsts = [
            p for p in payments if p.vendor == min(names[(p.document, p.amount, p.day, p.year)])
        ]
        once = sum((p.amount for p in firsts), Decimal(0))
        last = max((p.paid_by for p in payments), default=None)
        peaks = [in_force(number, d) for d, _ in revisions[number] if last and d <= last]
        peak = max((a for a in peaks if a is not None), default=None)
        blanket = sum(
            (p.amount for p in firsts if (in_force(number, p.paid_by) or 0) <= 0), Decimal(0)
        )
        in_2002 = sum((p.amount for p in firsts if p.year == 2002), Decimal(0))
        within = award + allowance
        if award < 0:
            kind = "negative_award"
        elif award == 0:
            kind = "no_fixed_award"
        elif not payments:
            kind = "unpaid"
        elif abs(total - award) <= allowance:
            kind = "paid_in_full"
        elif total < award:
            kind = "unspent"
        elif once <= within:
            kind = "paid_under_two_names"
        elif peak is not None and once <= peak + allowance:
            kind = "reduced_after_payment"
        elif once - blanket <= within:
            kind = "paid_before_fixed_award"
        elif number in term:
            kind = "term_agreement"
        elif once - in_2002 <= within:
            kind = "rolled_up_before_2003"
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


@pytest.mark.parametrize(
    "rule",
    [
        "chicago_contract_reconciliation::each_contract_takes_the_first_class_that_holds",
        "unique_chicago_contract_reconciliation_contract_number",
        "not_null_chicago_contract_reconciliation_break_class",
        "no_break_hides_in_a_class_that_is_not_one",
        "unexplained_breaks_stay_within_bound",
    ],
)
def test_each_reconciliation_rule_ran_and_passed(build: Build, rule: str) -> None:
    if "::" in rule:
        assert any("PASS" in line and rule in line for line in build.stdout.splitlines())
    else:
        assert build.results.get(rule) == "pass"


def test_every_contract_number_of_the_cut_has_its_class_and_paid_total(build: Build) -> None:
    """Every contract of the cut and every number its payments carry that the contracts lack is
    one row, with the class and paid total this file works out; direct vouchers are on none."""
    found = reconciled(build)
    assert found == expected_classes(build)
    numbers = {c["purchase_order_contract_number"] for c in CUT["contracts"]}
    numbers |= {p["contract_number"] for p in CUT["payments"] if p["contract_number"] != "DV"}
    assert found.keys() == numbers
    assert found["T26417"][0] == found["T26263"][0] == "contract_not_found"
    assert {k for k, _ in found.values()} >= {"paid_in_full", "unspent", "unpaid"}
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
    assert rows == [
        (
            sum(
                Decimal(a)
                for (a,) in query(
                    build,
                    "select amount from int_chicago__payments"
                    " where contract_number is not null and not is_repeat",
                )
            ),
            sum(
                Decimal(a)
                for (a,) in query(
                    build,
                    "select amount from int_chicago__payments"
                    " where contract_number is not null and is_repeat",
                )
            ),
            len(served),
        )
    ]
    assert rows[0][1] > 0, "the cut holds a repeat on a contract"


def test_the_reconciliation_control_fails_on_a_break_no_class_explains(
    build: Build, tmp_path: Path
) -> None:
    """The cut landed with one invented payment (labelled so, never data) that takes a contract
    awarded $1,333,731 to $5m paid, in a year and on a contract no class explains: the control's
    bound test fails, on that contract alone, while every other rule still passes."""
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
        "LEDGER_LAB_DUCKDB_PATH": str(tmp_path / "invented.duckdb"),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
    }
    vars_ = json.dumps({"source_rows": "landed"})
    result = dbt(
        env,
        tmp_path,
        "build",
        "--select",
        "+chicago_contract_reconciliation",
        "--indirect-selection",
        "cautious",
        "--vars",
        vars_,
    )
    assert result.returncode != 0
    run = json.loads((tmp_path / "target" / "run_results.json").read_text())
    failed = {
        r["unique_id"].split(".")[2]
        for r in run["results"]
        if r["status"] not in ("pass", "success")
    }
    assert failed == {"unexplained_breaks_stay_within_bound"}
    invented_build = Build(result.stdout, tmp_path / "invented.duckdb", {}, env, tmp_path)
    classes = reconciled(invented_build)
    assert [n for n, (k, _) in classes.items() if k == "unexplained"] == ["18419"]
    assert classes == expected_classes(invented_build)


@pytest.mark.landed
def test_the_real_landing_reconciles_every_contract(tmp_path: Path) -> None:
    """Over the real landing: every contract number classed as this file works it out, the class
    table pinned, the reconciliation with repeats counted beside it, and the largest unexplained
    breaks printed."""
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
    found = reconciled(build)
    assert found == expected_classes(build)
    table = {
        kind: (n, total)
        for kind, n, total in query(
            build,
            "select break_class, count(*), sum(case when difference > 0 then difference"
            " when break_class = 'contract_not_found' then paid else 0 end)"
            " from chicago_contract_reconciliation group by 1",
        )
    }
    print("contracts and dollars over (paid where not found) by class:", table)
    assert table == {
        "unspent": (31_579, Decimal("0")),
        "paid_in_full": (25_106, Decimal("51.65")),
        "unpaid": (25_057, Decimal("0")),
        "no_fixed_award": (5_328, Decimal("2718564474.67")),
        "paid_under_two_names": (1_149, Decimal("2895551971.25")),
        "rolled_up_before_2003": (1_111, Decimal("469718131.19")),
        "term_agreement": (636, Decimal("1745528021.71")),
        "contract_not_found": (205, Decimal("192143987.81")),
        "paid_before_fixed_award": (120, Decimal("358729696.42")),
        "reduced_after_payment": (105, Decimal("13170882.52")),
        "unexplained": (100, Decimal("133316706.24")),
        "negative_award": (28, Decimal("80723676.66")),
    }
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
    # The repeats counted: contracts over their award only because of them, and contracts paid
    # in full either way.
    both_ways = query(
        build,
        "select count(*) filter (where paid + paid_in_repeats - award_after_revisions"
        " > rounding_allowance and difference <= rounding_allowance),"
        " count(*) filter (where abs(paid + paid_in_repeats - award_after_revisions)"
        " <= rounding_allowance),"
        " count(*) filter (where break_class = 'paid_in_full'),"
        " count(*) filter (where break_class = 'paid_in_full' and difference <> 0)"
        " from chicago_contract_reconciliation"
        " where award_after_revisions > 0 and payment_rows > 0",
    )
    print(
        "over only with repeats, in full with repeats, in full, in full only to the dime:",
        both_ways,
    )
    assert both_ways == [(1_501, 24_390, 25_106, 3_146)]
    unexplained = query(
        build,
        "select contract_number, award_after_revisions, paid, difference,"
        " paid_again_under_another_name from chicago_contract_reconciliation"
        " where break_class = 'unexplained' order by difference desc limit 10",
    )
    print("largest unexplained breaks:", unexplained)
    term = query(
        build,
        "select count(*), count(*) filter (where break_class = 'paid_in_full')"
        " from chicago_contract_reconciliation"
        " where is_term_agreement and award_after_revisions > 0 and payment_rows > 0",
    )
    assert term == [(1_156, 7)]
