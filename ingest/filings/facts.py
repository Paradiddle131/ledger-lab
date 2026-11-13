"""The securities regulator's annual-report facts for a fixed list of listed companies: the step
that lands them as one set.

The SEC's Financial Statement Data Sets publish, once a quarter, one zip of the numbers on the
primary financial statements of every report filed with XBRL that quarter, "without change from
the as filed financial reports": `sub.txt` holds one row per submission (its accession number,
the filer's CIK, the form, the filing date, the period it reports), `num.txt` one row per number
(the accession number, the concept and its taxonomy, the period's end rounded to a month end and
its length in quarters, the unit, the dimensions it is broken down by, a co-registrant, the value
as filed); both are tab-separated UTF-8 under a fixed header (`SUB_COLUMNS`, `NUM_COLUMNS`). A
filing is never changed once accepted, so a value a later report restates is a second number in
a second filing, and an amended report is a submission of its own whose form ends in `/A`.

`step` receives the verified quarters by URL and lands one Parquet file: for each annual report
(`FORMS`) of a company in `companies.yaml`, every revenue figure (`CONCEPTS`, the US GAAP
taxonomy's only) for the company as a whole (no dimension, no co-registrant), one row per fact per
filing (`COLUMNS`): the CIK, the filing's accession number, form, amendment flag, filing date and
the period the filing reports, then the concept, the fact's period end and length in quarters,
its unit and its value as filed. Every column but the flag is the publisher's text, so a value
never passes through a float, and a nil value lands as null; nothing is collapsed to a latest
value.

The step refuses, naming the file and row, never a value: an archive without both files or with
another header, a row of another width, a submission filed outside the quarter its archive is
named for (so none is in two archives), a quarter or a submission served twice, a period end or
length that is no date or count, a value that is not a plain decimal, one fact served twice in one
filing, a company of the list with no annual report in the set, and a set with no fact at all.
"""

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator

from ingest import StepError
from ingest.scrub import parquet_from_csv

SET = "sec-annual-filings"
HERE = Path(__file__).resolve().parent
COMPANIES = HERE / "companies.yaml"
SCHEMA = HERE / "companies.schema.json"

# The annual reports: a 10-K, a transition-period 10-KT, and an amendment of either.
FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A"})
# Total revenue as the listed companies tag it in the pinned quarters: 13 use the contract-with-
# customer concept, 6 `Revenues`, and CNA Financial both (`Revenues` is its total, premiums and
# investment income included; the other leaves its premiums out); none moves between them over the
# four years, and none
# uses the taxonomy's third, `RevenueFromContractWithCustomerIncludingAssessedTax`. A company that
# moves to it leaves a report with no revenue for the period it reports, which the landed test
# refuses, so a re-pin finds it.
CONCEPTS = frozenset({"Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"})
SUB_COLUMNS: tuple[str, ...] = (
    "adsh", "cik", "name", "sic", "countryba", "stprba", "cityba", "zipba", "bas1", "bas2",
    "baph", "countryma", "stprma", "cityma", "zipma", "mas1", "mas2", "countryinc", "stprinc",
    "ein", "former", "changed", "afs", "wksi", "fye", "form", "period", "fy", "fp", "filed",
    "accepted", "prevrpt", "detail", "instance", "nciks", "aciks",
)  # fmt: skip
NUM_COLUMNS: tuple[str, ...] = (
    "adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value", "footnote",
)  # fmt: skip
COLUMNS: tuple[str, ...] = (
    "cik",
    "accession_number",
    "form",
    "is_amendment",
    "filing_date",
    "report_period",
    "concept",
    "period_end",
    "quarters",
    "unit",
    "value",
)
QUARTER = re.compile(r"(?P<year>[0-9]{4})q(?P<quarter>[1-4])\.zip")
DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
COUNT = re.compile(r"[0-9]+")


class FilingsError(StepError):
    """The served archives are not the financial statement data sets this step knows."""


def companies(path: Path = COMPANIES) -> list[dict[str, str]]:
    """The listed companies, refusing a file that breaks its schema or repeats a company."""
    doc = yaml.safe_load(path.read_text())
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(doc)
    ]
    if errors:
        raise FilingsError(f"{path.name} breaks {SCHEMA.name}: " + "; ".join(errors))
    listed: list[dict[str, str]] = doc["companies"]
    for key in ("cik", "lei", "isin", "ticker"):
        if len({c[key] for c in listed}) != len(listed):
            raise FilingsError(f"{path.name}: two companies share a {key}")
    return listed


def _quarter(source: str) -> tuple[date, date]:
    """The first and last day of the quarter an archive is named for (`2025q1.zip`)."""
    named = QUARTER.fullmatch(source)
    if not named:
        raise FilingsError(f"{source}: not a quarterly archive's name")
    year, quarter = int(named["year"]), int(named["quarter"])
    first = date(year, 3 * quarter - 2, 1)
    last = date(year + quarter // 4, 3 * quarter % 12 + 1, 1)
    return first, date.fromordinal(last.toordinal() - 1)


def _day(text: str) -> date | None:
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:])) if len(text) == 8 else None
    except ValueError:
        return None


def _rows(archive: zipfile.ZipFile, member: str, columns: tuple[str, ...]) -> Iterator[list[str]]:
    """A member's rows after its header, each split on tabs, the header checked."""
    if member not in archive.namelist():
        raise FilingsError(f"{member}: missing from the archive")
    with archive.open(member) as raw:
        lines = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        if tuple(next(lines, "").rstrip("\n").split("\t")) != columns:
            raise FilingsError(f"{member}: the served columns differ from the data set's")
        yield from (line.rstrip("\n").split("\t") for line in lines)


def _filings(
    source: str, archive: zipfile.ZipFile, ciks: frozenset[str]
) -> dict[str, dict[str, str]]:
    """The listed companies' annual reports in one archive, by accession number."""
    first, last = _quarter(source)
    kept: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    for number, row in enumerate(_rows(archive, "sub.txt", SUB_COLUMNS), start=2):
        if len(row) != len(SUB_COLUMNS):
            raise FilingsError(f"{source} sub.txt row {number}: not {len(SUB_COLUMNS)} fields")
        sub = dict(zip(SUB_COLUMNS, row, strict=True))
        filed = _day(sub["filed"])
        if filed is None or not first <= filed <= last:
            raise FilingsError(f"{source} sub.txt row {number}: filed outside the quarter")
        if sub["adsh"] in seen:
            raise FilingsError(f"{source} sub.txt row {number}: a submission served twice")
        seen.add(sub["adsh"])
        if sub["cik"] in ciks and sub["form"] in FORMS:
            kept[sub["adsh"]] = sub
    return kept


def _facts(
    source: str, archive: zipfile.ZipFile, filings: Mapping[str, Mapping[str, str]]
) -> Iterator[dict[str, Any]]:
    """The kept filings' revenue facts for the company as a whole, as rows of `COLUMNS`."""
    for number, row in enumerate(_rows(archive, "num.txt", NUM_COLUMNS), start=2):
        if row[0] not in filings or len(row) < 2 or row[1] not in CONCEPTS:
            continue
        if len(row) != len(NUM_COLUMNS):
            raise FilingsError(f"{source} num.txt row {number}: not {len(NUM_COLUMNS)} fields")
        num = dict(zip(NUM_COLUMNS, row, strict=True))
        if not num["version"].startswith("us-gaap/") or num["segments"] or num["coreg"]:
            continue
        where = f"{source} num.txt row {number}"
        if _day(num["ddate"]) is None or not COUNT.fullmatch(num["qtrs"]) or not num["uom"]:
            raise FilingsError(f"{where}: the period or unit is not a date, count and unit")
        if num["value"] and not DECIMAL.fullmatch(num["value"]):
            raise FilingsError(f"{where}: the value is not a plain decimal")
        sub = filings[num["adsh"]]
        yield {
            "cik": sub["cik"],
            "accession_number": sub["adsh"],
            "form": sub["form"],
            "is_amendment": sub["form"].endswith("/A"),
            "filing_date": sub["filed"],
            "report_period": sub["period"],
            "concept": num["tag"],
            "period_end": num["ddate"],
            "quarters": num["qtrs"],
            "unit": num["uom"],
            "value": num["value"] or None,
            "_where": where,
        }


def facts(files: Mapping[str, bytes], ciks: Iterable[str]) -> list[dict[str, Any]]:
    """Every listed company's annual-report revenue facts in the verified archives, by URL, one
    row per fact per filing, in a fixed order."""
    ciks = frozenset(ciks)
    seen_quarters: set[str] = set()
    reported: set[str] = set()
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    for url, raw in files.items():
        source = Path(urlsplit(url).path).name
        if source in seen_quarters:
            raise FilingsError(f"{source}: a second archive for the quarter")
        seen_quarters.add(source)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            filings = _filings(source, archive, ciks)
            reported |= {sub["cik"] for sub in filings.values()}
            for row in _facts(source, archive, filings):
                where = row.pop("_where")
                key = tuple(
                    row[c]
                    for c in ("accession_number", "concept", "period_end", "quarters", "unit")
                )
                if key in rows:
                    raise FilingsError(f"{where}: a fact served twice in one filing")
                rows[key] = row
    missing = ciks - reported
    if missing:
        raise FilingsError(f"{len(missing)} listed companies have no annual report in the set")
    if not rows:
        raise FilingsError("the served archives hold no fact for the listed companies")
    order = ("cik", "concept", "unit", "period_end", "quarters", "filing_date", "accession_number")
    return sorted(rows.values(), key=lambda r: tuple(r[c] for c in order))


def step(files: Mapping[str, bytes], listed: Iterable[str] | None = None) -> bytes:
    """The verified quarters, by URL, as one Parquet file of the listed companies' facts (the
    CIKs of `companies.yaml` unless given)."""
    ciks = [c["cik"] for c in companies()] if listed is None else listed
    out = io.StringIO()
    writer = csv.DictWriter(out, COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in facts(files, ciks):
        writer.writerow({**row, "is_amendment": "true" if row["is_amendment"] else "false"})
    dtype = {c: "BOOLEAN" if c == "is_amendment" else "VARCHAR" for c in COLUMNS}
    return parquet_from_csv(out.getvalue(), dtype=dtype)
