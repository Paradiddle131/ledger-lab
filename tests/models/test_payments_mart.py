"""Chicago's contracts, their revisions and the payments made against them become the payments
mart: one row per payment, its contract and that contract's award after revisions.

The sample build reads the Chicago seeds, which are the cut of real rows in `chicago_sample.yaml`
landed by the landing's own step with a test key and the vendor seeds' register records; a test
lands the cut again and holds the seeds to it. Each expectation here is worked out from the cut's
served rows by this file's own arithmetic, not read back from the models. The landed test runs the
build over the real landing and pins its counts.
"""

import csv
import io
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from ingest.chicago import contracts, payments
from ingest.chicago.table import land_all
from ingest.entities import Record, Register
from ingest.fetch import landing_path
from ingest.individuals import KEY_VARIABLE, person_hasher
from tests.models.dbt_build import PROJECT, SAMPLE, Build, built, dbt, query

CHICAGO = Path(__file__).with_name("chicago_sample.yaml")
CUT = yaml.safe_load(CHICAGO.read_text())
SEEDS = PROJECT / "seeds" / "staging" / "chicago"
REGISTER = PROJECT / "seeds" / "intermediate" / "vendors" / "vendor_legal_entities_sample.csv"
# The key the cut is landed with: a test key, so no committed row depends on the real one.
TEST_KEY = {KEY_VARIABLE: "test-key-" + "k" * 32}
TABLES = {"chicago-contracts": contracts, "chicago-payments": payments}
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
    "is_repeat",
]
# The mart's own columns after the shared ones: the published payee split by hash, and the
# document numbers a clear column may show.
SPLIT = ["vendor_organisation_name", "vendor_person_key", "document_reference"]
# The vendors of contract 8878, private companies, whose second revision is served twice.
REPEATED_REVISION_COMPANIES = {"V3 COMPANIES OF ILLINOIS, LIMITED", "V3 COMPANIES, LIMITED"}
# Companies of the cut with no legal form the classifier accepts as one party (a `&`, a joined
# name, `Company` alone) that the vendor seeds' register does not vouch for: the landing hashes
# them with the test key, as a real landing hashes any payee without evidence.
HASHED_COMPANIES = {
    "3M COMPANY",
    "AT&T CORP",
    "CANON U.S.A., INC",
    "COMPUTERSHARE , INC.",
    "DOW JONES & COMPANY INC",
    "DUN & BRADSTREET INC",
    "ERNST & YOUNG LLP",
    "GODADDY.COM LLC",
    "HOME DEPOT U.S.A., INC.",
    "SS & C TECHNOLOGIES INC",
    "TRANE U.S. INC",
    "W.W. GRAINGER, INC",
}


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


def served(columns: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    """Rows as the City serves them: CSV in the table's columns, blanks for cells not given."""
    out = io.StringIO()
    writer = csv.DictWriter(out, columns)
    writer.writeheader()
    writer.writerows({c: row.get(c, "") for c in columns} for row in rows)
    return out.getvalue().encode()


def land(cut: dict[str, list[dict[str, str]]], landing: Path) -> None:
    """The cut landed by the set's own step, with the test key and the vendor seeds' register."""
    with REGISTER.open() as f:
        register = Register(
            Record(
                r["Entity.LegalName"],
                r["Entity.EntityCategory"],
                r["Entity.LegalForm.EntityLegalFormCode"],
            )
            for r in csv.DictReader(f)
        )
    landed = land_all(
        {
            name: (served(module.COLUMNS, cut[name.removeprefix("chicago-")]), module.TABLE)
            for name, module in TABLES.items()
        },
        TEST_KEY,
        register,
    )
    for name, data in landed.items():
        (landing / name).write_bytes(data)


def seed_name(table: str) -> str:
    return table.replace("-", "_") + "_sample"


def payments_where(build: Build, where: str = "true", *params: object) -> list[dict[str, Any]]:
    sql = f"select * from int_chicago__payments where {where}"  # noqa: S608
    with duckdb.connect(str(build.database), read_only=True) as db:
        rel = db.execute(sql, list(params))
        names = [d[0] for d in rel.description]
        return [dict(zip(names, row, strict=True)) for row in rel.fetchall()]


def test_the_cuts_vendors_are_companies_named_in_the_tree() -> None:
    """Every vendor of the cut is a public company of the labelled vendor sample, one of the
    companies the landing hashes for want of a vouching register record, or a company of the
    repeated revision, so a person's row cannot slip in unseen."""
    companies = {
        v["name"] for v in yaml.safe_load(SAMPLE.read_text())["vendors"] if v["entity"] == "chicago"
    }
    vendors = {r["vendor_name"] for r in CUT["contracts"] + CUT["payments"]}
    assert vendors <= companies | HASHED_COMPANIES | REPEATED_REVISION_COMPANIES
    assert vendors >= HASHED_COMPANIES
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
        "a_repeat_leaves_its_first_row_unflagged",
        "accepted_values_int_chicago__payments_repeat_kind__exact__without_department"
        "__without_department_other_name__rolled_up_itemised",
    ],
)
def test_each_payments_rule_ran_and_passed(build: Build, rule: str) -> None:
    assert build.results.get(rule) == "pass"


def test_the_2002_unit_test_ran_and_passed(build: Build) -> None:
    name = "int_chicago__payments::the_2002_row_takes_no_fiscal_year_whatever_the_calendars_cover"
    assert any("PASS" in line and name in line for line in build.stdout.splitlines())


def test_the_withholding_unit_test_ran_and_passed(build: Build) -> None:
    name = "payments::a_reference_is_withheld_beside_a_hashed_payee_and_on_placeholders_and_uk_rows"
    assert any("PASS" in line and name in line for line in build.stdout.splitlines())


def test_the_mart_has_the_shared_columns_in_order_with_decimal_amounts(build: Build) -> None:
    columns = query(
        build,
        "select column_name, data_type from information_schema.columns"
        " where table_name = 'payments' order by ordinal_position",
    )
    assert [c for c, _ in columns] == COMMON + SPLIT
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
    # Revision 2 of contract 8878 is served twice, under two vendor names: it counts once.
    v3 = [r for r in CUT["contracts"] if r["purchase_order_contract_number"] == "8878"]
    assert Counter(r["revision_number"] for r in v3) == {"0": 1, "2": 2}
    assert found["8878"] == Decimal("1251062")
    assert found["8878"] != sum(Decimal(r["award_amount"]) for r in v3)


def repeats_of_the_cut() -> Counter[str]:
    """The repeats among the cut's payments by kind, by the rule's own definition."""
    rows = CUT["payments"]
    with_department = Counter(
        tuple(sorted((k, v) for k, v in p.items() if k != "department_name"))
        for p in rows
        if p.get("department_name")
    )
    kinds: Counter[str] = Counter()
    for row, n in Counter(tuple(sorted(p.items())) for p in rows).items():
        kinds["exact"] += n - 1
        rest = tuple((k, v) for k, v in row if k != "department_name")
        if "department_name" not in dict(row) and with_department[rest]:
            kinds["without_department"] += 1
    return kinds


def test_a_payment_served_again_is_kept_and_flagged(build: Build) -> None:
    kinds = dict(
        query(
            build,
            "select repeat_kind, count(*) from int_chicago__payments"
            " where is_repeat group by repeat_kind",
        )
    )
    expected = repeats_of_the_cut()
    assert kinds == {k: n for k, n in expected.items() if n}
    assert kinds.keys() == {"exact", "without_department"}
    assert query(build, "select count(*) from payments where entity_id = 'chicago'") == [
        (len(CUT["payments"]),)
    ]
    paid = query(
        build,
        "select sum(amount) from payments where contract_number = '76213' and not is_repeat",
    )
    once = {
        (p["check_date"], p["amount"]) for p in CUT["payments"] if p["contract_number"] == "76213"
    }
    assert paid == [(sum(Decimal(a) for _, a in once),)]


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
        query(
            build,
            "select contract_number, award_after_revisions, amount from payments"
            " where entity_id = 'chicago'",
        )
    )
    assert found == expected
    kinds = Counter(
        "direct voucher" if n is None else "found" if a is not None else "not found"
        for n, a, _ in expected
    )
    assert min(kinds.values()) > 0 and len(kinds) == 3, kinds


def test_a_rolled_up_payment_keeps_its_year_and_gets_no_day(build: Build) -> None:
    rolled = payments_where(build, "is_rolled_up")
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
    for row in payments_where(build, "not is_rolled_up"):
        day = row["payment_date"]
        assert row["document_number"] is not None
        if 2023 <= day.year <= 2026:
            assert (row["fiscal_year"], row["fiscal_period"]) == (day.year, day.month)
        else:
            assert (row["fiscal_year"], row["fiscal_period"]) == (None, None)


def test_a_hashed_payee_is_the_landing_hash_and_never_in_clear(build: Build) -> None:
    as_person = person_hasher(TEST_KEY)
    served = Counter(
        as_person(p["vendor_name"]) for p in CUT["payments"] if p["vendor_name"] in HASHED_COMPANIES
    )
    rows = query(
        build,
        "select vendor_name, vendor_is_hashed, contract_number from payments"
        " where vendor_is_hashed and entity_id = 'chicago'",
    )
    assert served
    assert Counter(v for v, _, _ in rows) == served
    assert all(h for _, h, _ in rows)
    assert any(c is not None for _, _, c in rows), "a hashed payee's contract still joins"
    clear = {v for (v,) in query(build, "select distinct vendor_name from payments")}
    assert not clear & HASHED_COMPANIES
    # The split columns: the person key is the landing hash on exactly the hashed rows, and the
    # organisation name is the served name on exactly the others.
    split = query(
        build,
        "select vendor_organisation_name, vendor_person_key from payments"
        " where entity_id = 'chicago'",
    )
    assert Counter(k for _, k in split if k is not None) == served
    assert Counter(n for n, _ in split if n is not None) == Counter(
        p["vendor_name"] for p in CUT["payments"] if p["vendor_name"] not in HASHED_COMPANIES
    )
    assert all((n is None) != (k is None) for n, k in split)


def test_the_mart_currency_is_the_entitys_own(build: Build, access: dict[str, Any]) -> None:
    currencies = {e["id"]: e["currency"] for e in access["entities"]}
    rows = query(build, "select distinct entity_id, currency from payments")
    assert rows
    assert all(currencies[entity] == currency for entity, currency in rows)


@pytest.mark.parametrize("table", TABLES)
def test_each_chicago_seed_is_the_cut_as_the_landing_lands_it(tmp_path: Path, table: str) -> None:
    """The seeds hold exactly what the landing's step writes for the cut with the test key: the
    companies in clear, those the register does not vouch for hashed with that key."""
    land(CUT, tmp_path)
    con = duckdb.connect()
    landed = con.execute(f"select * from read_parquet('{tmp_path / table}')").fetchall()  # noqa: S608
    with (SEEDS / f"{seed_name(table)}.csv").open(newline="") as f:
        seed = [tuple(cell or None for cell in row) for row in list(csv.reader(f))[1:]]
    assert Counter(seed) == Counter(landed)
    assert any(str(cell).startswith("person:") for row in seed for cell in row)


def test_the_payment_key_is_the_same_in_another_landing_of_the_same_rows(
    build: Build, tmp_path: Path
) -> None:
    """Landed again in the reverse order, the same payments get the same keys."""
    landing = tmp_path / "landing"
    landing.mkdir()
    land({"contracts": CUT["contracts"], "payments": CUT["payments"][::-1]}, landing)
    env = build.env | {"LEDGER_LAB_LANDING": str(landing)}
    result = dbt(
        env,
        build.cwd,
        "compile",
        "--select",
        "stg_chicago__payments",
        "--vars",
        json.dumps({"source_rows": "landed"}),
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
    build = built(tmp_path, "landed", landing)
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
    repeats = {
        (kind, rolled): (n, total)
        for kind, rolled, n, total in query(
            build,
            "select repeat_kind, is_rolled_up, count(*), sum(amount) from int_chicago__payments"
            " where is_repeat group by all",
        )
    }
    print("repeats by kind:", repeats)
    assert repeats == {
        ("exact", False): (2_398, Decimal("1760586515.05")),
        ("exact", True): (162, Decimal("76866731.54")),
        ("without_department", False): (3_869, Decimal("148723005.74")),
        ("without_department", True): (3_431, Decimal("2005570237.83")),
        ("without_department_other_name", False): (1_444, Decimal("398837205.25")),
        ("without_department_other_name", True): (2_538, Decimal("2642748669.61")),
        ("rolled_up_itemised", True): (1, Decimal("426356.20")),
    }
    # Leaving the repeats out, more contracts are paid exactly their award: the evidence that a
    # repeat is the same payment served again rests on these totals, not on the City.
    paid_exactly = query(
        build,
        "with paid as (select contract_number, sum(amount) as raw,"
        " sum(case when is_repeat then 0 else amount end) as firsts from payments"
        " where contract_number is not null group by 1)"
        " select count(*) filter (where raw = award_after_revisions),"
        " count(*) filter (where firsts = award_after_revisions),"
        " count(*) filter (where award_after_revisions <> 0 and raw = 2 * award_after_revisions)"
        " from paid join int_chicago__contracts using (contract_number)",
    )
    print("contracts paid exactly their award (with repeats, without, twice):", paid_exactly)
    assert paid_exactly == [(21_264, 22_096, 849)]
    zero_and_negative = query(
        build,
        "with paid as (select contract_number, sum(amount) as paid from payments"
        " where contract_number is not null group by 1)"
        " select count(*) filter (where award_after_revisions = 0),"
        " count(paid) filter (where award_after_revisions = 0),"
        " sum(paid) filter (where award_after_revisions = 0),"
        " count(*) filter (where award_after_revisions < 0)"
        " from int_chicago__contracts left join paid using (contract_number)",
    )
    print("zero awards, of them paid, paid on them, negative awards:", zero_and_negative)
    assert zero_and_negative == [(5_328, 2_125, Decimal("2725874886.39"), 28)]
