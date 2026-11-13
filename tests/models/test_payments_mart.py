"""Chicago's contracts, their revisions and the payments made against them become the payments
mart: one row per payment, its contract and that contract's award after revisions.

The fixture build lands the cut of real rows in `chicago_sample.yaml` through the landing's own step
with a test key (`dbt_build.py`); each expectation here is worked out from the cut's served rows by
this file's own arithmetic, not read back from the models. The landed test runs the build over the
real landing and pins its counts.
"""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from ingest.fetch import landing_path
from ingest.individuals import person_hasher
from tests.models.dbt_build import (
    CHICAGO,
    TEST_KEY,
    Build,
    built,
    dbt,
    land_chicago,
    query,
    sample,
)

CUT = yaml.safe_load(CHICAGO.read_text())
COMMON = [
    "payment_key",
    "entity_id",
    "currency",
    "payment_date",
    "fiscal_year",
    "fiscal_period",
    "amount",
    "vendor_name",
    "vendor_is_hashed",
    "document_number",
    "contract_number",
    "award_after_revisions",
    "source_table",
]
HASHED_FIRM = "ERNST & YOUNG LLP"


def award_after_revisions(rows: list[dict[str, str]]) -> dict[str, Decimal]:
    """Each contract's award: the sum of its revisions' amounts, a revision served twice once."""
    revisions = {
        (r["purchase_order_contract_number"], r["revision_number"]): Decimal(r["award_amount"])
        for r in rows
    }
    awards: dict[str, Decimal] = {}
    for (contract, _), amount in revisions.items():
        awards[contract] = awards.get(contract, Decimal(0)) + amount
    return awards


def payments(build: Build, where: str = "true", *params: object) -> list[dict[str, Any]]:
    sql = f"select * from int_chicago__payments where {where}"  # noqa: S608
    with duckdb.connect(str(build.database), read_only=True) as db:
        rel = db.execute(sql, list(params))
        names = [d[0] for d in rel.description]
        return [dict(zip(names, row, strict=True)) for row in rel.fetchall()]


def test_the_cuts_vendors_are_the_vendor_samples_companies_and_one_hashed_firm() -> None:
    """Every Chicago name of the labelled vendor sample is on a row of the cut, so the vendor
    matching sees it, and no vendor outside them and the one firm the test key hashes is."""
    companies = {v["name"] for v in sample()["vendors"] if v["entity"] == "chicago"}
    vendors = {r["vendor_name"] for r in CUT["contracts"] + CUT["payments"]}
    assert vendors == companies | {HASHED_FIRM}
    cells = [v for r in CUT["contracts"] + CUT["payments"] for v in r.values()]
    assert not any(v.startswith(("person:", "id:")) for v in cells), "no hash is committed"


@pytest.mark.parametrize(
    "rule",
    [
        "unique_stg_chicago__payments_payment_key",
        "unique_int_chicago__contract_revisions_contract_number_revision_number",
        "unique_int_chicago__contracts_contract_number",
        "unique_payments_payment_key",
        "accepted_values_payments_currency__USD__GBP__EUR",
        "a_payment_is_dated_by_a_day_or_a_year",
        "every_served_payment_is_one_row",
        "a_vendor_is_flagged_hashed_exactly_when_it_is",
    ],
)
def test_each_payments_rule_ran_and_passed(build: Build, rule: str) -> None:
    assert build.results.get(rule) == "pass"


def test_the_mart_has_the_shared_columns_in_order_with_decimal_amounts(build: Build) -> None:
    columns = query(
        build,
        "select column_name, data_type from information_schema.columns"
        " where table_name = 'payments' order by ordinal_position",
    )
    assert [c for c, _ in columns] == COMMON
    types = dict(columns)
    assert types["amount"].startswith("DECIMAL")
    assert types["award_after_revisions"].startswith("DECIMAL")
    assert types["payment_date"] == "DATE"
    intermediate = query(
        build,
        "select column_name from information_schema.columns"
        " where table_name = 'int_chicago__payments' order by ordinal_position",
    )
    assert [c for (c,) in intermediate][: len(COMMON)] == COMMON


def test_every_served_payment_is_one_mart_row_with_its_amount_as_served(build: Build) -> None:
    rows = query(build, "select amount from payments where entity_id = 'chicago'")
    assert Counter(Decimal(a) for (a,) in rows) == Counter(
        Decimal(p["amount"]) for p in CUT["payments"]
    )
    assert any(Decimal(p["amount"]) < 0 for p in CUT["payments"]), "the cut holds refunds"
    served_twice = Counter(tuple(sorted(p.items())) for p in CUT["payments"])
    assert max(served_twice.values()) == 2, "the cut holds a payment served twice"


def test_the_award_after_revisions_sums_every_revision_once(build: Build) -> None:
    expected = award_after_revisions(CUT["contracts"])
    found = dict(
        query(build, "select contract_number, award_after_revisions from int_chicago__contracts")
    )
    assert found == expected
    # A contract with a negative revision and two revisions the legacy system numbered.
    maximus = [r for r in CUT["contracts"] if r["purchase_order_contract_number"] == "D0952390332"]
    assert sorted(r["revision_number"] for r in maximus)[:2] == ["0", "1"]
    assert any(Decimal(r["award_amount"]) < 0 for r in maximus)
    assert sum(not r["revision_number"].isdigit() for r in maximus) == 2
    assert found["D0952390332"] == Decimal("537719.70")


@pytest.mark.parametrize("contract", ["18942", "17466"])
def test_a_fully_paid_contract_was_paid_exactly_its_award_after_revisions(
    build: Build, contract: str
) -> None:
    """Each row of these contracts adds to the award: the paid total is the sum of all rows, not
    the latest row's amount (a revision of zero that changed only the dates)."""
    rows = [r for r in CUT["contracts"] if r["purchase_order_contract_number"] == contract]
    paid = sum(Decimal(p["amount"]) for p in CUT["payments"] if p["contract_number"] == contract)
    assert len(rows) > 1
    assert paid == award_after_revisions(rows)[contract]
    assert paid != Decimal(max(rows, key=lambda r: r["approval_date"])["award_amount"])
    awards = {
        a
        for (a,) in query(
            build,
            "select distinct award_after_revisions from payments where contract_number = ?",
            contract,
        )
    }
    assert awards == {paid}


def test_a_payment_joins_its_contract_or_none(build: Build) -> None:
    """A direct voucher has no contract; a number the contracts lack keeps its number and no
    award; any other payment carries its contract's award."""
    awards = award_after_revisions(CUT["contracts"])
    expected: Counter[tuple[str | None, Decimal | None, Decimal]] = Counter()
    for p in CUT["payments"]:
        number = None if p["contract_number"] == "DV" else p["contract_number"]
        expected[(number, awards.get(number or ""), Decimal(p["amount"]))] += 1
    found = Counter(
        query(build, "select contract_number, award_after_revisions, amount from payments")
    )
    assert found == expected
    kinds = Counter(
        "direct voucher" if n is None else "found" if a is not None else "not found"
        for n, a, _ in expected
    )
    assert min(kinds.values()) > 0 and len(kinds) == 3, kinds


def test_a_rolled_up_payment_keeps_its_year_and_gets_no_day(build: Build) -> None:
    rolled = payments(build, "is_rolled_up")
    years = Counter(p["check_date"] for p in CUT["payments"] if len(p["check_date"]) == 4)
    assert Counter(str(r["rolled_up_year"]) for r in rolled) == years
    assert "2002" in years and {"2023", "2024"} & years.keys()
    for row in rolled:
        assert row["payment_date"] is None
        assert row["fiscal_period"] is None
        assert row["document_number"] is None
        # A calendar year the calendars cover is that fiscal year; the 2002 row (1996 to 2002)
        # and an uncovered year get none.
        assert row["fiscal_year"] == (
            row["rolled_up_year"] if row["rolled_up_year"] in (2023, 2024) else None
        )


def test_a_dated_payment_takes_its_days_fiscal_period(build: Build) -> None:
    for row in payments(build, "not is_rolled_up"):
        day = row["payment_date"]
        assert row["document_number"] is not None
        if 2023 <= day.year <= 2026:
            assert (row["fiscal_year"], row["fiscal_period"]) == (day.year, day.month)
        else:
            assert (row["fiscal_year"], row["fiscal_period"]) == (None, None)


def test_a_hashed_payee_is_the_landing_hash_and_never_in_clear(build: Build) -> None:
    hashed = person_hasher(TEST_KEY)(HASHED_FIRM)
    served = sum(p["vendor_name"] == HASHED_FIRM for p in CUT["payments"])
    rows = query(
        build,
        "select vendor_name, vendor_is_hashed, contract_number from payments"
        " where vendor_is_hashed",
    )
    assert served > 0
    assert {(v, h) for v, h, _ in rows} == {(hashed, True)}
    assert len(rows) == served
    assert any(c is not None for _, _, c in rows), "a hashed payee's contract still joins"
    assert query(build, "select count(*) from payments where vendor_name like '%ERNST%'") == [(0,)]


def test_the_mart_currency_is_the_entitys_own(build: Build, access: dict[str, Any]) -> None:
    currencies = {e["id"]: e["currency"] for e in access["entities"]}
    rows = query(build, "select distinct entity_id, currency from payments")
    assert rows
    assert all(currencies[entity] == currency for entity, currency in rows)


def test_the_payment_key_is_the_same_in_another_landing_of_the_same_rows(
    build: Build, tmp_path: Path
) -> None:
    """Landed again in the reverse order, the same payments get the same keys."""
    reversed_cut = tmp_path / "chicago_sample.yaml"
    reversed_cut.write_text(
        yaml.safe_dump({"contracts": CUT["contracts"], "payments": CUT["payments"][::-1]})
    )
    landing = tmp_path / "landing"
    landing.mkdir()
    land_chicago(landing, reversed_cut)
    env = build.env | {"LEDGER_LAB_LANDING": str(landing)}
    result = dbt(
        env,
        build.cwd,
        "compile",
        "--select",
        "stg_chicago__payments",
        "--output",
        "json",
        "--quiet",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    sql = json.loads(result.stdout)["compiled"]
    keys = f"select payment_key from ({sql})"  # noqa: S608
    again = Counter(k for (k,) in duckdb.connect().execute(keys).fetchall())
    first = Counter(k for (k,) in query(build, "select payment_key from stg_chicago__payments"))
    assert again == first


@pytest.mark.landed
def test_the_real_landing_builds_the_payments_mart(tmp_path: Path) -> None:
    """Over the real landing: every payment once, the join measured, and the award rule against
    what the City paid."""
    landing = landing_path().resolve()
    for name in (
        "chicago-contracts",
        "chicago-payments",
        "gleif-legal-entities",
        "uk-transport-spend-2025",
    ):
        if not (landing / name).exists():
            pytest.skip(f"{name} is not landed")
    build = built(tmp_path, landing)
    counts = dict(
        query(
            build,
            "select 'contract rows', count(*) from stg_chicago__contracts"
            " union all select 'revisions', count(*) from int_chicago__contract_revisions"
            " union all select 'contracts', count(*) from int_chicago__contracts"
            " union all select 'payment rows', count(*) from stg_chicago__payments"
            " union all select 'mart rows', count(*) from payments where entity_id = 'chicago'",
        )
    )
    print("rows by model:", counts)
    assert counts == {
        "contract rows": 188_665,
        "revisions": 188_624,
        "contracts": 90_319,
        "payment rows": 475_609,
        "mart rows": 475_609,
    }
    join = dict(
        query(
            build,
            "select case when contract_number is null then 'direct voucher'"
            " when award_after_revisions is null then 'not found' else 'found' end, count(*)"
            " from payments where entity_id = 'chicago' group by 1",
        )
    )
    print("payments by contract join:", join)
    assert join == {"found": 242_395, "not found": 452, "direct voucher": 232_762}
    dating = dict(
        query(
            build,
            "select case when rolled_up_year = 2002 then '2002 row'"
            " when is_rolled_up then 'rolled up' else 'dated' end"
            " || case when fiscal_year is null then ', no fiscal year' else '' end,"
            " count(*) from int_chicago__payments group by 1",
        )
    )
    print("payments by dating:", dating)
    assert dating == {
        "dated": 307_408,
        "dated, no fiscal year": 51_666,
        "rolled up": 9_233,
        "rolled up, no fiscal year": 77_739,
        "2002 row, no fiscal year": 29_563,
    }
    # Contracts with several revisions whose paid total equals, to the cent, the award by the
    # sum of every revision, and by the latest revision's amount alone.
    exact = query(
        build,
        "with paid as (select contract_number, sum(amount) as paid from payments"
        " where contract_number is not null group by 1),"
        " latest as (select contract_number, arg_max(award_amount, (approved_on, revision_number))"
        " as latest from int_chicago__contract_revisions group by 1)"
        " select count(*) filter (where paid = award_after_revisions),"
        " count(*) filter (where paid = latest) from int_chicago__contracts"
        " join paid using (contract_number) join latest using (contract_number)"
        " where revision_count > 1",
    )
    print("several-revision contracts paid exactly (sum, latest):", exact)
    by_sum, by_latest = exact[0]
    assert by_sum > 5 * by_latest
    awards = dict(
        query(
            build,
            "select contract_number, award_after_revisions from int_chicago__contracts"
            " where contract_number in ('P011551', '7116')",
        )
    )
    # Six revisions, two negative and three legacy-numbered, paid in one 2002 row to the cent;
    # and a contract whose second revision is served twice, paid exactly its award counted once.
    assert awards == {"P011551": Decimal("1560856.10"), "7116": Decimal("147938.00")}
    assert query(
        build,
        "select sum(amount) from payments where contract_number in ('P011551', '7116')"
        " group by contract_number order by contract_number",
    ) == [(Decimal("147938.00"),), (Decimal("1560856.10"),)]
