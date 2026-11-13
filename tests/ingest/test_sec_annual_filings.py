"""The regulator's annual-report facts land one row per fact per filing, so a restated period keeps
the original and the restated value, and an amended report its own flagged rows.

The rows below are real rows of the SEC's Financial Statement Data Sets for the first quarters of
2024, 2025 and 2026 (public information that may be copied or further distributed; Source: U.S.
Securities and Exchange Commission, EDGAR): Pitney Bowes's 2023 revenue as its 2023 report filed it
and as its 2024 report restated it for a discontinued business, its 2025 report and that report's
amendment, Axon Enterprise's 2022 and 2023 revenue as filed and as restated a year later, and,
to be left out, a company outside the list, a figure broken down by segment and one that is no
revenue.
"""

import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import duckdb
import pytest
from conftest import landed_file

from ingest import fetch
from ingest.entities import REGISTER
from ingest.filings import facts
from ingest.filings.facts import COLUMNS, NUM_COLUMNS, SET, SUB_COLUMNS, FilingsError, step

PITNEY_BOWES, AXON, MASTEC = "78814", "1069183", "15615"
LISTED = (PITNEY_BOWES, AXON)
ARCHIVES = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/"
REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"


def sub(adsh: str, cik: str, name: str, place: tuple[str, ...], filing: tuple[str, ...]) -> str:
    """A `sub.txt` row: the filer, its business and mailing address (`place`), and the filing's
    fiscal year end, form, period, fiscal year, period kind, filing date and acceptance time."""
    country, state, city, zipcode, street, street2, phone, incorporated, ein, former, changed = place
    business = (country, state, city, zipcode, street, street2)
    return "\t".join(
        (adsh, cik, name, "3579" if cik == PITNEY_BOWES else "3480" if cik == AXON else "1623")
        + business
        + (phone,)
        + business[:4]
        + business[4:]
        + ("US", incorporated, ein, former, changed, "1-LAF", "1")
        + filing
        + ("0", "1", f"{filing[-1]}", "1", "")
    )


STAMFORD = ("US", "CT", "STAMFORD", "06926-0700", "3001 SUMMER STREET", "", "203-356-5000")
SHELTON = ("US", "CT", "SHELTON", "06484", "27 WATERVIEW DRIVE", "", "203-922-4000")
SCOTTSDALE = ("US", "AZ", "SCOTTSDALE", "85255", "17800 N. 85TH ST.", "", "480-991-0797")
PBI = ("DE", "060495050", "", "")
AXN = ("DE", "860741227", "TASER INTERNATIONAL INC", "20010212")


def pbi_sub(adsh: str, place: tuple[str, ...], form: str, period: str, filed: str, at: str) -> str:
    return _sub(adsh, PITNEY_BOWES, "PITNEY BOWES INC /DE/", place, PBI, form, period, filed, at)


def _sub(
    adsh: str,
    cik: str,
    name: str,
    place: tuple[str, ...],
    filer: tuple[str, ...],
    form: str,
    period: str,
    filed: str,
    at: str,
) -> str:
    country, state, city, zipcode, street, street2, phone = place
    incorporated, ein, former, changed = filer
    instance = {
        PITNEY_BOWES: f"pbi-{period}_htm.xml",
        AXON: f"axon-{period}{'x10k' if period == '20231231' else ''}_htm.xml",
        MASTEC: "mtz-20241231_htm.xml",
    }[cik]
    sic = {PITNEY_BOWES: "3579", AXON: "3480", MASTEC: "1623"}[cik]
    business = (country, state, city, zipcode, street, street2)
    row = (
        (adsh, cik, name, sic, *business, phone.replace("-", "") if cik == MASTEC else phone)
        + (country, state, city, zipcode, street, street2)
        + ("US", incorporated, ein, former, changed, "1-LAF", "1", "1231", form, period)
        + (period[:4], "FY", filed, at, "0", "1", instance, "1", "")
    )
    return "\t".join(row)


SERVED_SUBS = {
    "2024q1": [
        pbi_sub("0000078814-24-000004", STAMFORD, "10-K", "20231231", "20240220",
                "2024-02-20 13:12:00.0"),
        _sub("0001069183-24-000006", AXON, "AXON ENTERPRISE, INC.", SCOTTSDALE, AXN, "10-K",
             "20231231", "20240227", "2024-02-27 16:32:00.0"),
    ],
    "2025q1": [
        _sub("0000015615-25-000021", MASTEC, "MASTEC INC",
             ("US", "FL", "CORAL GABLES", "33134", "800 S. DOUGLAS ROAD", "12TH FLOOR",
              "305-599-1800"),
             ("FL", "650829355", "BURNUP & SIMS INC", "19920703"), "10-K", "20241231",
             "20250228", "2025-02-28 06:07:00.0"),
        pbi_sub("0000078814-25-000015", STAMFORD, "10-K", "20241231", "20250221",
                "2025-02-21 11:15:00.0"),
        _sub("0001069183-25-000019", AXON, "AXON ENTERPRISE, INC.", SCOTTSDALE, AXN, "10-K",
             "20241231", "20250228", "2025-02-28 16:22:00.0"),
    ],
    "2026q1": [
        pbi_sub("0001628280-26-009650", SHELTON, "10-K", "20251231", "20260219",
                "2026-02-19 15:32:00.0"),
        pbi_sub("0001628280-26-009921", SHELTON, "10-K/A", "20251231", "20260220",
                "2026-02-19 19:05:00.0"),
    ],
}  # fmt: skip


def num(adsh: str, tag: str, version: str, ddate: str, qtrs: str, value: str, seg: str = "") -> str:
    return "\t".join((adsh, tag, f"us-gaap/{version}", ddate, qtrs, "USD", seg, "", value, ""))


SERVED_NUMS = {
    "2024q1": [
        num("0001069183-24-000006", REVENUE, "2023", "20231231", "4", "1563391000.0000"),
        num("0000078814-24-000004", "Revenues", "2023", "20231231", "4", "3266348000.0000"),
        num("0001069183-24-000006", REVENUE, "2023", "20221231", "4", "1189935000.0000"),
        num("0000078814-24-000004", "Revenues", "2023", "20221231", "4", "3538042000.0000"),
        num("0000078814-24-000004", "NetIncomeLoss", "2023", "20231231", "4", "-385627000.0000"),
        num("0001069183-24-000006", REVENUE, "2023", "20231231", "4", "566183000.0000",
            "BusinessSegments=SoftwareAndSensorsSegment;ProductOrService=AxonEvidence;"),
    ],
    "2025q1": [
        num("0000078814-25-000015", "Revenues", "2024", "20241231", "4", "2026598000.0000"),
        num("0001069183-25-000019", REVENUE, "2024", "20221231", "4", "1187143000.0000"),
        num("0001069183-25-000019", REVENUE, "2024", "20231231", "1", "430376000.0000"),
        num("0000078814-25-000015", "Revenues", "2024", "20221231", "4", "2482883000.0000"),
        num("0000015615-25-000021", "Revenues", "2024", "20241231", "4", "12303464000.0000"),
        num("0000078814-25-000015", "Revenues", "2024", "20231231", "4", "2078925000.0000"),
        num("0001069183-25-000019", REVENUE, "2024", "20231231", "4", "1560699000.0000"),
    ],
    "2026q1": [
        num("0001628280-26-009921", "Revenues", "2025", "20251231", "4", "1892629000.0000"),
        num("0001628280-26-009650", "Revenues", "2025", "20231231", "4", "2078925000.0000"),
        num("0001628280-26-009921", "Revenues", "2025", "20231231", "4", "2078925000.0000"),
        num("0001628280-26-009650", "Revenues", "2025", "20251231", "4", "1892629000.0000"),
        num("0001628280-26-009921", "Revenues", "2025", "20241231", "4", "662587000.0000",
            "BusinessSegments=PresortServices;ConsolidationItems=OperatingSegments;"),
    ],
}  # fmt: skip


def archive(subs: list[str], nums: list[str], **header: str) -> bytes:
    """One quarter's zip as the SEC serves it: tab-separated members under their headers."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("sub.txt", "\n".join([header.get("sub", "\t".join(SUB_COLUMNS)), *subs]) + "\n")
        z.writestr("num.txt", "\n".join([header.get("num", "\t".join(NUM_COLUMNS)), *nums]) + "\n")
    return out.getvalue()


def served(**changed: tuple[list[str], list[str]]) -> dict[str, bytes]:
    quarters = {q: (SERVED_SUBS[q], SERVED_NUMS[q]) for q in SERVED_SUBS} | changed
    return {f"{ARCHIVES}{q}.zip": archive(*rows) for q, rows in quarters.items()}


def landed(tmp_path: Path, files: dict[str, bytes], listed: tuple[str, ...] = LISTED) -> Path:
    path = tmp_path / SET
    path.write_bytes(step(files, listed))
    return path


def rows(path: Path) -> list[dict[str, Any]]:
    relation = duckdb.read_parquet(str(path))
    return [dict(zip(relation.columns, r, strict=True)) for r in relation.fetchall()]


def values(landed_rows: list[dict[str, Any]], cik: str, concept: str, end: str) -> list[tuple]:
    """A period's annual values by filing: accession number, form, flag, filing date, value."""
    return sorted(
        (r["accession_number"], r["form"], r["is_amendment"], r["filing_date"], r["value"])
        for r in landed_rows
        if (r["cik"], r["concept"], r["period_end"], r["quarters"]) == (cik, concept, end, "4")
    )


def test_a_restated_period_keeps_the_original_and_the_restated_value(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, served()))
    assert values(landed_rows, PITNEY_BOWES, "Revenues", "20231231") == [
        ("0000078814-24-000004", "10-K", False, "20240220", "3266348000.0000"),
        ("0000078814-25-000015", "10-K", False, "20250221", "2078925000.0000"),
        ("0001628280-26-009650", "10-K", False, "20260219", "2078925000.0000"),
        ("0001628280-26-009921", "10-K/A", True, "20260220", "2078925000.0000"),
    ], "the original, the restated value and both later reports each stay tied to their filing"
    assert values(landed_rows, AXON, REVENUE, "20231231") == [
        ("0001069183-24-000006", "10-K", False, "20240227", "1563391000.0000"),
        ("0001069183-25-000019", "10-K", False, "20250228", "1560699000.0000"),
    ]
    assert values(landed_rows, AXON, REVENUE, "20221231") == [
        ("0001069183-24-000006", "10-K", False, "20240227", "1189935000.0000"),
        ("0001069183-25-000019", "10-K", False, "20250228", "1187143000.0000"),
    ]


def test_an_amended_report_lands_its_own_flagged_rows_beside_the_original(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, served()))
    reports = {
        (r["accession_number"], r["form"], r["is_amendment"], r["report_period"])
        for r in landed_rows
        if r["report_period"] == "20251231"
    }
    assert reports == {
        ("0001628280-26-009650", "10-K", False, "20251231"),
        ("0001628280-26-009921", "10-K/A", True, "20251231"),
    }
    assert all(r["is_amendment"] == r["form"].endswith("/A") for r in landed_rows)


def test_only_the_listed_companies_total_revenue_lands_as_the_publishers_text(
    tmp_path: Path,
) -> None:
    path = landed(tmp_path, served())
    landed_rows = rows(path)
    assert len(landed_rows) == 15
    assert {r["cik"] for r in landed_rows} == set(LISTED), "a company outside the list lands"
    assert {r["concept"] for r in landed_rows} <= facts.CONCEPTS
    assert ("20231231", "1", "430376000.0000") in {
        (r["period_end"], r["quarters"], r["value"]) for r in landed_rows
    }, "a quarter's figure in an annual report lands with its length"
    segment_values = {"566183000.0000", "662587000.0000"}
    assert not segment_values & {r["value"] for r in landed_rows}, "a segment's figure lands"
    relation = duckdb.read_parquet(str(path))
    assert relation.columns == list(COLUMNS)
    assert [str(t) for t in relation.types] == [
        "BOOLEAN" if c == "is_amendment" else "VARCHAR" for c in COLUMNS
    ], "every column but the flag is the publisher's text"
    assert all(Decimal(r["value"]) for r in landed_rows)


def test_a_nil_value_lands_as_null(tmp_path: Path) -> None:
    nums = [*SERVED_NUMS["2026q1"]]
    nums[0] = nums[0].replace("1892629000.0000", "")
    landed_rows = rows(landed(tmp_path, served(**{"2026q1": (SERVED_SUBS["2026q1"], nums)})))
    assert [r["value"] for r in landed_rows if r["accession_number"].endswith("009921")] == [
        "2078925000.0000",
        None,
    ]


def changed(quarter: str, *, sub: str | None = None, num: str | None = None, **header: str) -> Any:
    subs = [*SERVED_SUBS[quarter], *([sub] if sub else [])]
    nums = [*SERVED_NUMS[quarter], *([num] if num else [])]
    return {quarter: (subs, nums)} if not header else header


@pytest.mark.parametrize(
    ("files", "listed"),
    [
        (served(**changed("2026q1", num=SERVED_NUMS["2026q1"][0])), LISTED),
        (
            served(**changed("2026q1", num=SERVED_NUMS["2026q1"][0].replace(".0000", "e3"))),
            LISTED,
        ),
        (served(**changed("2026q1", num=SERVED_NUMS["2026q1"][0].replace("20251231", "2025"))), LISTED),
        (served(**changed("2026q1", num=SERVED_NUMS["2026q1"][0] + "\textra")), LISTED),
        (
            served(**changed("2026q1", sub=SERVED_SUBS["2025q1"][0].replace("20250228", "20250401"))),
            LISTED,
        ),
        (served(**changed("2026q1", sub=SERVED_SUBS["2025q1"][1])), LISTED),
        (served(), (*LISTED, "21175")),
        (served(), (MASTEC, "21175")[1:]),
    ],
    ids=[
        "fact-twice",
        "exponent",
        "no-date",
        "wide-row",
        "filed-outside-the-quarter",
        "submission-twice",
        "listed-without-report",
        "no-fact",
    ],
)  # fmt: skip
def test_the_step_refuses_what_is_not_the_data_set(
    files: dict[str, bytes], listed: tuple[str, ...]
) -> None:
    with pytest.raises(FilingsError):
        step(files, listed)


def test_the_step_refuses_another_header_a_second_archive_or_another_name() -> None:
    header = "\t".join(NUM_COLUMNS[:-1])
    files = served()
    files[f"{ARCHIVES}2026q1.zip"] = archive(SERVED_SUBS["2026q1"], [], num=header)
    with pytest.raises(FilingsError):
        step(files, LISTED)
    files = served()
    files[f"{ARCHIVES}2026q1.zip?again"] = files[f"{ARCHIVES}2026q1.zip"]
    with pytest.raises(FilingsError):
        step(files, LISTED)
    files = {f"{ARCHIVES}financial-statements.zip": served()[f"{ARCHIVES}2026q1.zip"]}
    with pytest.raises(FilingsError):
        step(files, LISTED)


def isin_valid(isin: str) -> bool:
    """The ISIN's check digit: its letters as numbers (A is 10), then the Luhn sum."""
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return (10 - total % 10) % 10 == int(isin[-1])


def test_the_company_list_keeps_its_schema_and_real_identifiers() -> None:
    listed = facts.companies()
    assert 10 <= len(listed) <= 20
    assert all(isin_valid(c["isin"]) for c in listed), "an ISIN's check digit is wrong"
    assert isin_valid("US6200763075") and not isin_valid("US6200763076")


def test_the_set_is_the_regulators_dated_quarterly_archives_fetched_as_one() -> None:
    sources = fetch.load_sources(fetch.SOURCES)
    members = {n: e for n, e in sources.items() if n.startswith(f"{SET}-")}
    assert fetch.SET_STEPS[SET] is step and SET not in fetch.READS_THE_REGISTER
    assert len(members) == 4, "a handful of pinned requests"
    for name, entry in members.items():
        url = urlsplit(entry["url"])
        assert (url.scheme, url.netloc, url.query) == ("https", "www.sec.gov", "")
        assert url.path == f"/files/dera/data/financial-statement-data-sets/{name[-6:]}.zip"
        assert entry["names_people"] is False
    assert fetch.CLIENT_NAME == "ledger-lab research-bot", "the regulator's host refuses others"


@pytest.mark.landed
def test_the_landed_filings_keep_every_restated_period_and_amended_report() -> None:
    con = duckdb.connect()
    con.read_parquet(str(landed_file(SET))).create_view("f")
    key = "accession_number, concept, period_end, quarters, unit"
    assert con.sql(f"select count(*) - count(distinct ({key})) from f").fetchone() == (0,)
    ciks = {c["cik"] for c in facts.companies()}
    assert {r[0] for r in con.sql("select distinct cik from f").fetchall()} == ciks
    without = con.sql(
        "select accession_number from f group by accession_number, report_period"
        " having count(*) filter (where period_end = report_period) = 0"
    ).fetchall()
    assert without == [], "an annual report with no revenue for the period it reports"
    counts = con.sql(
        "select count(*), count(distinct cik), count(distinct accession_number),"
        " count(distinct accession_number) filter (where is_amendment) from f"
    ).fetchone()
    restated = con.sql(
        "select cik, concept, unit, period_end, quarters,"
        " list(accession_number || ' ' || filing_date || ' ' || value order by filing_date)"
        " from f group by all having count(distinct value) > 1 order by all"
    ).fetchall()
    one_quarter_own = con.sql(
        "select accession_number from f group by accession_number, report_period"
        " having count(*) filter (where period_end = report_period and quarters = '4') = 0"
    ).fetchall()
    concepts = con.sql("select concept, count(distinct cik) from f group by all order by all")
    print(
        f"facts {counts[0]}, companies {counts[1]}, filings {counts[2]}, amended {counts[3]},"
        f" restated periods {len(restated)}: {restated}; reports whose own year has only a"
        f" one-quarter figure: {one_quarter_own}; companies by concept: {concepts.fetchall()}"
    )
    assert counts[3] >= 1 and len(restated) >= 1
    axon = con.sql(
        "select accession_number, filing_date, value from f where cik = ? and concept = ?"
        " and period_end = '20231231' and quarters = '4' order by filing_date",
        params=[AXON, REVENUE],
    ).fetchall()
    assert axon[:2] == [
        ("0001069183-24-000006", "20240227", "1563391000.0000"),
        ("0001069183-25-000019", "20250228", "1560699000.0000"),
    ], "Axon's 2023 revenue as first filed and as restated a year later"
    amended = con.sql(
        "select distinct form, accession_number from f where cik = ? and report_period = ?"
        " order by form",
        params=[PITNEY_BOWES, "20251231"],
    ).fetchall()
    assert amended == [("10-K", "0001628280-26-009650"), ("10-K/A", "0001628280-26-009921")]


@pytest.mark.landed
def test_each_listed_company_is_a_vendor_matched_to_its_own_legal_entity(tmp_path: Path) -> None:
    """Over the vendor landing (`LEDGER_LAB_LANDING` holding the vendors' sources and the
    register): each company's LEI is the legal entity `vendor_entity` matched a Chicago vendor to
    by the exact or the same-legal-form rule, and the City paid it."""
    from tests.models.dbt_build import dbt

    landing = fetch.landing_path().resolve()
    for name in ("chicago-contracts", "chicago-payments", "uk-transport-spend-2025", REGISTER):
        landed_file(name)
    env = {
        "LEDGER_LAB_LANDING": str(landing),
        "LEDGER_LAB_DUCKDB_PATH": str(tmp_path / "vendors.duckdb"),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "PATH": "/usr/bin:/bin",
    }
    run = dbt(env, tmp_path, "run", "--select", "vendor_entity", "--vars", '{"source_rows": "landed"}')
    assert run.returncode == 0, run.stdout + run.stderr
    con = duckdb.connect(str(tmp_path / "vendors.duckdb"), read_only=True)
    paid = con.execute(
        "select v.lei, string_agg(distinct v.match_rule), sum(cast(p.amount as decimal(18, 2)))"
        f" from vendor_entity v join read_parquet('{landing / 'chicago-payments'}') p"
        "   on p.vendor_name = v.vendor_name"
        " where v.entity_id = 'chicago' and v.match_status = 'matched' group by v.lei"
    ).fetchall()
    by_lei = {lei: (rules, total) for lei, rules, total in paid}
    listed = facts.companies()
    print(json.dumps({c["ticker"]: str(by_lei.get(c["lei"], (None, None))[1]) for c in listed}))
    for company in listed:
        rules, total = by_lei[company["lei"]]
        assert set(rules.split(",")) <= {"exact", "same_legal_form"}, company["name"]
        assert total > 0, company["name"]
