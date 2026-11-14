"""The regulator's annual-report facts land one row per fact per filing, so a restated period keeps
the original and the restated value, and an amended report its own flagged rows.

The rows below are real rows of the SEC's Financial Statement Data Sets for the first quarters of
2024, 2025 and 2026 (public information that may be copied or further distributed; Source: U.S.
Securities and Exchange Commission, EDGAR): Pitney Bowes's 2023 revenue as its 2023 report filed it
and as its 2024 report restated it for a discontinued business, its 2025 report and that report's
amendment, Axon Enterprise's 2022 and 2023 revenue as filed and as restated a year later, and,
to be left out, a company outside the list, a figure broken down by segment and one that is no
revenue. Of each filing's submission row only what the step reads and the filer's name are kept.
"""

import io
import json
import os
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
# The list's inclusion rule: at least this much paid by the City, but for Pitney Bowes, kept for its
# amended report.
MILLION = Decimal(1_000_000)
KEPT_BELOW_THE_BAR = {"PBI"}


def sub(adsh: str, cik: str, name: str, form: str, period: str, filed: str, accepted: str) -> str:
    """A `sub.txt` row with the fields the step reads and the filer's name; the others, which
    describe the filer (addresses, telephone, tax number, former names) and its XBRL, are blank."""
    read = {"adsh": adsh, "cik": cik, "name": name, "form": form, "period": period}
    read |= {"filed": filed, "accepted": accepted}
    return "\t".join(read.get(column, "") for column in SUB_COLUMNS)


PBI_NAME, AXON_NAME = "PITNEY BOWES INC /DE/", "AXON ENTERPRISE, INC."
# Each filing as `sub.txt` serves it, but for the blanked fields.
SERVED_SUBS = {
    "2024q1": [
        sub("0000078814-24-000004", PITNEY_BOWES, PBI_NAME, "10-K", "20231231", "20240220",
            "2024-02-20 13:12:00.0"),
        sub("0001069183-24-000006", AXON, AXON_NAME, "10-K", "20231231", "20240227",
            "2024-02-27 16:32:00.0"),
    ],
    "2025q1": [
        sub("0000015615-25-000021", MASTEC, "MASTEC INC", "10-K", "20241231", "20250228",
            "2025-02-28 06:07:00.0"),
        sub("0000078814-25-000015", PITNEY_BOWES, PBI_NAME, "10-K", "20241231", "20250221",
            "2025-02-21 11:15:00.0"),
        sub("0001069183-25-000019", AXON, AXON_NAME, "10-K", "20241231", "20250228",
            "2025-02-28 16:22:00.0"),
    ],
    "2026q1": [
        sub("0001628280-26-009650", PITNEY_BOWES, PBI_NAME, "10-K", "20251231", "20260219",
            "2026-02-19 15:32:00.0"),
        sub("0001628280-26-009921", PITNEY_BOWES, PBI_NAME, "10-K/A", "20251231", "20260220",
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
    assert len(landed_rows) == 14
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


def adding(quarter: str, *, subs: tuple[str, ...] = (), nums: tuple[str, ...] = ()) -> dict:
    """The served quarters with rows added to one of them."""
    return served(**{quarter: ([*SERVED_SUBS[quarter], *subs], [*SERVED_NUMS[quarter], *nums])})


FIRST_NUM = SERVED_NUMS["2026q1"][0]
# A fact the amended report does not serve (its 2022 revenue), valid on its own, so a refusal of
# it below has the one defect each case gives it as its only cause.
NEW_FACT = FIRST_NUM.replace("\t20251231\t", "\t20221231\t")
WITHOUT_MASTEC = [n for n in SERVED_NUMS["2025q1"] if not n.startswith("0000015615")]


@pytest.mark.parametrize(
    ("files", "listed"),
    [
        (adding("2026q1", nums=(FIRST_NUM,)), LISTED),
        (adding("2026q1", nums=(NEW_FACT.replace("1892629000.0000", "1.892629e9"),)), LISTED),
        (adding("2026q1", nums=(NEW_FACT.replace("20221231", "20221331"),)), LISTED),
        (adding("2026q1", nums=(NEW_FACT.replace("\t4\t", "\tfour\t"),)), LISTED),
        (adding("2026q1", nums=(NEW_FACT + "\textra",)), LISTED),
        (adding("2026q1", subs=(SERVED_SUBS["2025q1"][0],)), LISTED),
        (adding("2026q1", subs=(SERVED_SUBS["2026q1"][0],)), LISTED),
        (adding("2026q1", subs=(SERVED_SUBS["2026q1"][0][:-3],)), LISTED),
        (served(), (*LISTED, "21175")),
        (served(**{"2025q1": (SERVED_SUBS["2025q1"], WITHOUT_MASTEC)}), (MASTEC,)),
    ],
    ids=[
        "fact-twice",
        "exponent",
        "no-date",
        "no-count",
        "wide-row",
        "filed-outside-the-quarter",
        "submission-twice",
        "short-row",
        "listed-without-report",
        "no-fact",
    ],
)
def test_the_step_refuses_what_is_not_the_data_set(
    files: dict[str, bytes], listed: tuple[str, ...]
) -> None:
    with pytest.raises(FilingsError):
        step(files, listed)


def test_the_fact_the_refusals_spoil_lands_when_unspoilt(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, adding("2026q1", nums=(NEW_FACT,))))
    assert ("0001628280-26-009921", "20221231", "1892629000.0000") in {
        (r["accession_number"], r["period_end"], r["value"]) for r in landed_rows
    }


def test_a_report_accepted_on_the_quarters_last_evening_belongs_to_it(tmp_path: Path) -> None:
    """Filed the next day, accepted after hours on 31 March: the archive of that quarter may
    hold it."""
    late = sub("0001069183-24-000099", AXON, AXON_NAME, "10-K/A", "20231231", "20240401",
               "2024-03-31 17:45:00.0")  # fmt: skip
    files = served(**{"2024q1": ([*SERVED_SUBS["2024q1"], late], SERVED_NUMS["2024q1"])})
    assert rows(landed(tmp_path, files))


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


# The fiscal years (by the year of the period each report covers, `/A` for an amendment) whose
# annual reports the set holds, where they differ from 2022 to 2025: the set is first quarters
# only, and an archive need not hold every report of its quarter.
REPORT_GAPS = {
    "27419": "2023 2024 2025 2026",  # Target's years end in late January
    "1351636": "2022 2024 2025",  # SoundThinking filed its 2023 report on 1 April 2024
    "1488139": "2022 2023 2024",  # Ameresco's 2025 report (3 March 2026) is not in the archive
    "78814": "2022 2023 2024 2025 2025/A",  # Pitney Bowes amended its 2025 report in February
}
# Lengths the filer got wrong, as the archives serve them: L3Harris's 2025 report gives its 2025
# annual revenue no four-quarter length, and its 2024 annual revenue also a one-quarter one.
WRONG_LENGTHS = [("0000202058-26-000015",), ("0000202058-26-000015", "20241231")]


@pytest.mark.landed
def test_the_landed_filings_keep_every_restated_period_and_amended_report() -> None:
    con = duckdb.connect()
    con.read_parquet(str(landed_file(SET))).create_view("f")
    duplicates = con.execute(
        "select count(*) - count(distinct (accession_number, concept, period_end, quarters, unit))"
        " from f"
    )
    assert duplicates.fetchone() == (0,), "one row per fact per filing"
    ciks = {c["cik"] for c in facts.companies()}
    assert {r[0] for r in con.execute("select distinct cik from f").fetchall()} == ciks
    without = con.execute(
        "select accession_number from f group by accession_number, report_period"
        " having count(*) filter (where period_end = report_period) = 0"
    ).fetchall()
    assert without == [], "an annual report with no revenue for the period it reports"
    reports = dict(
        con.execute(
            "select cik, string_agg(year, ' ' order by year) from (select distinct cik,"
            " left(report_period, 4) || case when is_amendment then '/A' else '' end as year"
            " from f) group by cik"
        ).fetchall()
    )
    expected = {cik: "2022 2023 2024 2025" for cik in ciks} | REPORT_GAPS
    assert reports == expected, "a company's reports in the set differ from the stated ones"
    one_quarter_own = con.execute(
        "select accession_number from f group by accession_number, report_period"
        " having count(*) filter (where period_end = report_period and quarters = '4') = 0"
    ).fetchall()
    annual_as_one_quarter = con.execute(
        "select a.accession_number, a.period_end from f a join f b"
        " using (accession_number, concept, unit, period_end, value)"
        " where a.quarters = '1' and b.quarters = '4'"
    ).fetchall()
    assert sorted(one_quarter_own + annual_as_one_quarter) == WRONG_LENGTHS, (
        "a report whose own year's revenue has no four-quarter length, or an annual figure"
        " served with a one-quarter length, beyond the known ones"
    )
    counts = con.execute(
        "select count(*), count(distinct cik), count(distinct accession_number),"
        " count(distinct accession_number) filter (where is_amendment) from f"
    ).fetchall()[0]
    restated = con.execute(
        "select cik, concept, unit, period_end, quarters,"
        " list(accession_number || ' ' || filing_date || ' ' || value order by filing_date)"
        " from f group by all having count(distinct value) > 1 order by all"
    ).fetchall()
    concepts = con.execute("select concept, count(distinct cik) from f group by all order by all")
    print(
        f"facts {counts[0]}, companies {counts[1]}, filings {counts[2]}, amended {counts[3]},"
        f" restated periods {len(restated)}: {restated}; companies by concept:"
        f" {concepts.fetchall()}"
    )
    assert counts[3] >= 1 and len(restated) >= 1
    axon = con.execute(
        "select accession_number, filing_date, value from f where cik = ? and concept = ?"
        " and period_end = '20231231' and quarters = '4' order by filing_date",
        [AXON, REVENUE],
    ).fetchall()
    assert axon[:2] == [
        ("0001069183-24-000006", "20240227", "1563391000.0000"),
        ("0001069183-25-000019", "20250228", "1560699000.0000"),
    ], "Axon's 2023 revenue as first filed and as restated a year later"
    amended = con.execute(
        "select distinct form, accession_number from f where cik = ? and report_period = ?"
        " order by form",
        [PITNEY_BOWES, "20251231"],
    ).fetchall()
    assert amended == [("10-K", "0001628280-26-009650"), ("10-K/A", "0001628280-26-009921")]


@pytest.mark.landed
def test_each_listed_company_is_a_vendor_matched_to_its_own_legal_entity(tmp_path: Path) -> None:
    """Over the vendor landing (`LEDGER_LAB_LANDING` holding the vendors' sources and the
    register): each company's LEI is the legal entity `vendor_entity` matched a Chicago vendor to
    by the exact or the same-legal-form rule, and the City paid it at least a million dollars, but
    for the one company the list names as kept for its amended report."""
    from tests.models.dbt_build import dbt

    landing = fetch.landing_path().resolve()
    for name in ("chicago-contracts", "chicago-payments", "uk-transport-spend-2025", REGISTER):
        landed_file(name)
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_LAB_GCP_PROJECT"} | {
        "LEDGER_LAB_LANDING": str(landing),
        "LEDGER_LAB_DUCKDB_PATH": str(tmp_path / "vendors.duckdb"),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    run = dbt(
        env, tmp_path, "run", "--select", "vendor_entity", "--vars", '{"source_rows": "landed"}'
    )
    assert run.returncode == 0, run.stdout + run.stderr
    con = duckdb.connect(str(tmp_path / "vendors.duckdb"), read_only=True)
    paid = con.execute(
        "select v.lei, string_agg(distinct v.match_rule), sum(cast(p.amount as decimal(18, 2)))"
        " from vendor_entity v join read_parquet(?) p on p.vendor_name = v.vendor_name"
        " where v.entity_id = 'chicago' and v.match_status = 'matched' group by v.lei",
        [str(landing / "chicago-payments")],
    ).fetchall()
    by_lei = {lei: (rules, total) for lei, rules, total in paid}
    listed = facts.companies()
    print(json.dumps({c["ticker"]: str(by_lei.get(c["lei"], (None, None))[1]) for c in listed}))
    for company in listed:
        rules, total = by_lei[company["lei"]]
        assert set(rules.split(",")) <= {"exact", "same_legal_form"}, company["name"]
        if company["ticker"] in KEPT_BELOW_THE_BAR:
            assert 0 < total < MILLION, company["name"]
        else:
            assert total >= MILLION, company["name"]
