"""The Department for Transport's monthly spend lands as one set, every layout mapped to one shape,
with every supplier who may be a person hashed and nothing else in any row naming them.

The organisation rows are real rows of the Department's files (Contains public sector information
licensed under the Open Government Licence v3.0); every person's name is invented.
"""

import calendar
import csv
import hashlib
import io
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest
from conftest import landed_file

from ingest import fetch
from ingest.entities import Record, Register, person_shaped
from ingest.individuals import KEY_VARIABLE, is_organisation, normalise
from ingest.uk_transport.spend import (
    BODIES,
    COLUMNS,
    LAYOUTS,
    SET,
    SpendError,
    profile,
    step,
)

ENV = {KEY_VARIABLE: "k" * 32}
# A register that vouches for no name, so only the classifier decides.
NONE = Register(())
STANDARD, DEPARTMENTAL_FAMILY, EXPENSE_AREA_SPACED = LAYOUTS
GRANT = (
    "Department for Transport,Department for Transport,30/04/2025,Grt Aid to NDPBs,"
    "DG Road Transport Group,National Highways Limited,2000002905,Grant in Aid NH for May 2025,"
    '"£283,000,000.00",B1 1RN'
)
RAIL = (
    "Department for Transport,Department for Transport,24/04/2025,TOCOpCosts(Pub),"
    "DG Rail Services Group,NORTHERN TRAINS LIMITED,2000002315,"
    '2602 Wk4 NTL P2602 CBrp Cost Budget Payment - Nort,"£93,019,000.00",M1 2NF'
)
BUS = (
    "Department for Transport,Department for Transport,12/05/2025,Subsidies Private Se,"
    'DG Public Transport and Local Group,CENTREBUS LTD,2000004009,Centrebus Ltd," £25,018.57 ",'
    "LE4 9HU"
)
SMALL = (
    "Department for Transport,National Highways,11/04/2025,TA Renewal of Structures - Capital,"
    'OPERATIONS DIRECTORATE,VolkerLaser Ltd,1019180,Renewals of Structures,"£25,007.97",EN11 9BX'
)


def person_row(date: str = "15/04/2025", supplier: str = "JANE EXAMPLE", **cells: str) -> str:
    row = {
        "family": "Department for Transport",
        "entity": "Maritime and Coastguard Agency",
        "type": "Compensation",
        "area": "Search and Rescue",
        "number": "4400123",
        "item": "Settlement JANE EXAMPLE",
        "amount": "£30,000.00",
        "postcode": "ZZ1 1ZZ",
        **cells,
    }
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow(
        [
            row["family"],
            row["entity"],
            date,
            row["type"],
            row["area"],
            supplier,
            row["number"],
            row["item"],
            row["amount"],
            row["postcode"],
        ]
    )
    return out.getvalue()


def served(*lines: str, header: tuple[str, ...] = STANDARD) -> bytes:
    """A month as the Department serves it: Windows-1252, CRLF line ends."""
    return "".join(f"{line}\r\n" for line in (",".join(header), *lines)).encode("cp1252")


def url(name: str) -> str:
    return f"https://assets.publishing.service.gov.uk/media/0123/{name}"


# The months of fiscal year 2025, by `YYYY-MM`.
YEAR = [f"2025-{m:02d}" for m in range(4, 13)] + [f"2026-{m:02d}" for m in range(1, 4)]


def filler(month: str) -> bytes:
    """A month holding one real organisation row, dated in that month."""
    year, number = month.split("-")
    return served(GRANT.replace("30/04/2025", f"15/{number}/{year}"))


def year_files(months: dict[str, bytes], drop: tuple[str, ...] = ()) -> dict[str, bytes]:
    """The twelve files of a year, by URL `<YYYY-MM>.csv`: the given months, filler elsewhere."""
    return {url(f"{m}.csv"): months.get(m) or filler(m) for m in YEAR if m not in drop}


def land(tmp_path: Path, months: dict[str, bytes]) -> Path:
    path = tmp_path / SET
    path.write_bytes(step(year_files(months), ENV, NONE))
    return path


def rows(path: Path, month: str | None = None) -> list[dict[str, Any]]:
    """The landed rows, or only those of one month's file."""
    rel = duckdb.read_parquet(str(path))
    landed = [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]
    return [r for r in landed if month is None or r["source_file"] == f"{month}.csv"]


def one_month(tmp_path: Path, *lines: str, header: tuple[str, ...] = STANDARD) -> list[dict]:
    """A year whose April holds these lines; April's landed rows."""
    return rows(land(tmp_path, {"2025-04": served(*lines, header=header)}), "2025-04")


def refused(april: bytes) -> str:
    """The step's refusal of a year whose April is this file."""
    with pytest.raises(SpendError) as raised:
        step(year_files({"2025-04": april}), ENV, NONE)
    return str(raised.value)


def test_an_organisation_lands_with_the_source_file_and_the_publishers_digits(
    tmp_path: Path,
) -> None:
    (row,) = one_month(tmp_path, GRANT)
    assert row == {
        "source_file": "2025-04.csv",
        "source_row": 1,
        "department_family": "Department for Transport",
        "entity": "Department for Transport",
        "date": "30/04/2025",
        "expense_type": "Grt Aid to NDPBs",
        "expense_area": "DG Road Transport Group",
        "supplier": "National Highways Limited",
        "transaction_number": "2000002905",
        "item_text": "Grant in Aid NH for May 2025",
        "amount": "283000000.00",
        "postal_code": "B1 1RN",
    }


@pytest.mark.parametrize("header", LAYOUTS)
def test_each_layout_variant_maps_to_the_same_columns(tmp_path: Path, header: tuple) -> None:
    rel_rows = one_month(tmp_path, SMALL, header=header)
    rel = duckdb.read_parquet(str(tmp_path / SET))
    assert tuple(rel.columns) == COLUMNS
    assert [(r["expense_area"], r["supplier"], r["amount"]) for r in rel_rows] == [
        ("OPERATIONS DIRECTORATE", "VolkerLaser Ltd", "25007.97")
    ]


def test_the_layouts_differ_only_in_how_the_publisher_spells_a_header() -> None:
    assert len(set(LAYOUTS)) == len(LAYOUTS) == 3
    assert {len(header) for header in LAYOUTS} == {len(COLUMNS) - 2}


def test_an_amount_keeps_its_digits_with_spaces_pound_sign_and_commas_dropped(
    tmp_path: Path,
) -> None:
    landed = one_month(tmp_path, BUS.replace("12/05/2025", "12/04/2025"), RAIL)
    assert [r["amount"] for r in landed] == ["25018.57", "93019000.00"]
    rel = duckdb.read_parquet(str(tmp_path / SET))
    types = dict(zip(rel.columns, map(str, rel.types), strict=True))
    assert types.pop("source_row") == "BIGINT"
    assert set(types.values()) == {"VARCHAR"}, "every served cell lands as text"


@pytest.mark.parametrize(
    "amount", ["-£25,000.00", "£25000.00", "£25,000", "25,000.00", "£25,000.001", "(£25,000.00)"]
)
def test_an_amount_in_an_unknown_shape_is_refused(amount: str) -> None:
    assert refused(served(person_row(amount=amount))) == (
        "2025-04.csv row 1: the amount is not £ with thousands commas and pence"
    )


def test_an_unknown_layout_is_refused_loudly() -> None:
    renamed = tuple("Supplier Name" if h == "Supplier" else h for h in STANDARD)
    assert refused(served(GRANT, header=renamed)) == "2025-04.csv: an unknown layout"


def test_a_file_in_another_encoding_is_refused() -> None:
    utf8 = served(GRANT).decode("cp1252").encode("utf-8")
    assert refused(utf8) == "2025-04.csv: an unknown layout"


@pytest.mark.parametrize("date", ["2025-04-15", "15/4/2025", "31/04/2025", ""])
def test_a_date_in_an_unknown_shape_is_refused(date: str) -> None:
    assert refused(served(person_row(date=date))) == (
        "2025-04.csv row 1: the date is not a dd/mm/yyyy day"
    )


def test_a_file_spanning_two_months_is_refused() -> None:
    assert refused(served(GRANT, BUS)) == "2025-04.csv: dates from more than one month, or none"


def test_two_files_for_one_month_are_refused() -> None:
    april = filler("2025-04")
    with pytest.raises(SpendError, match="2025-05.csv: a second file for 2025-04"):
        step(year_files({"2025-05": april}), ENV, NONE)


def test_a_row_with_a_missing_field_is_refused() -> None:
    assert refused(served(GRANT, GRANT.rsplit(",", 1)[0])) == "2025-04.csv row 2: 9 fields"


def test_a_year_missing_a_month_is_refused() -> None:
    with pytest.raises(SpendError, match="11 months, not the twelve of one April-to-March year"):
        step(year_files({}, drop=("2025-09",)), ENV, NONE)


def test_twelve_months_that_do_not_open_in_april_are_refused() -> None:
    files = year_files({}, drop=("2025-04",))
    files[url("2026-04.csv")] = filler("2026-04")
    with pytest.raises(SpendError, match="12 months, not the twelve of one April-to-March year"):
        step(files, ENV, NONE)


@pytest.mark.parametrize(
    ("column", "body"),
    [("family", "Ministry of Examples"), ("entity", "Example Highways Agency")],
)
def test_a_paying_body_the_step_does_not_know_is_refused(column: str, body: str) -> None:
    assert refused(served(person_row(**{column: body}))) == (
        "2025-04.csv row 1: a paying body the step does not know"
    )


def test_every_month_lands_once_with_its_own_file_name_on_each_row(tmp_path: Path) -> None:
    months = {"2025-04": served(GRANT, SMALL), "2025-05": served(BUS, header=DEPARTMENTAL_FAMILY)}
    landed = rows(land(tmp_path, months))
    assert [(r["source_file"], r["source_row"]) for r in landed[:3]] == [
        ("2025-04.csv", 1),
        ("2025-04.csv", 2),
        ("2025-05.csv", 1),
    ]
    counts = profile(tmp_path / SET)
    assert list(counts["months"]) == YEAR
    assert (counts["months"]["2025-04"], counts["months"]["2025-05"]) == (2, 1)
    assert counts["files"] == 12


def test_a_person_is_hashed_and_nothing_on_their_row_names_or_locates_them(
    tmp_path: Path,
) -> None:
    path = land(tmp_path, {"2025-04": served(person_row(), GRANT)})
    person, grant = rows(path, "2025-04")
    assert str(person["supplier"]).startswith("person:")
    assert str(person["transaction_number"]).startswith("id:")
    assert (person["item_text"], person["postal_code"]) == (None, None)
    assert (person["amount"], person["date"], person["entity"]) == (
        "30000.00",
        "15/04/2025",
        "Maritime and Coastguard Agency",
    )
    assert grant["transaction_number"] == "2000002905", "an organisation's reference stays"
    assert b"EXAMPLE" not in path.read_bytes().upper()
    assert b"4400123" not in path.read_bytes()


def test_a_reference_hash_is_keyed_and_never_equals_a_name_hash(tmp_path: Path) -> None:
    same = person_row(supplier="4400123", number="4400123")
    (row,) = one_month(tmp_path, same)
    assert row["supplier"].removeprefix("person:") != row["transaction_number"].removeprefix("id:")
    unkeyed = hashlib.sha256(b"4400123").hexdigest()
    assert unkeyed not in (row["supplier"], row["transaction_number"])


@pytest.mark.parametrize(
    "item",
    [
        "Refund re JANE EXAMPLE",
        "Refund (JANE EXAMPLE)",
        "JANE EXAMPLE'S claim",
        "EXAMPLE, JANE",
        "example jane",
        "Jane-Example settlement",
    ],
)
def test_a_hashed_name_in_another_months_free_text_blanks_that_cell(
    tmp_path: Path, item: str
) -> None:
    files = {
        "2025-04": served(person_row()),
        "2025-05": served(
            person_row(
                date="20/05/2025",
                supplier="EXAMPLE SERVICES LIMITED",
                item=item,
                type=item,
                area=item,
                postcode="SW1A 2AA",
            )
        ),
    }
    (other,) = rows(land(tmp_path, files), "2025-05")
    assert other["supplier"] == "EXAMPLE SERVICES LIMITED"
    assert (other["item_text"], other["expense_type"], other["expense_area"]) == (None,) * 3
    assert other["postal_code"] == "SW1A 2AA", "an organisation's row keeps its location"


def test_a_description_ending_in_a_hashed_name_cut_short_is_blanked(tmp_path: Path) -> None:
    files = {
        "2025-04": served(person_row()),
        "2025-05": served(
            person_row(
                date="20/05/2025",
                supplier="CENTREBUS LTD",
                item="Claim JANE EXAMP",
                area="Claims JANE EXAMP",
            )
        ),
    }
    (ending,) = rows(land(tmp_path, files), "2025-05")
    assert ending["item_text"] is None
    assert ending["expense_area"] == "Claims JANE EXAMP", "the Department cuts only descriptions"


def test_free_text_that_only_shares_a_word_with_a_hashed_name_stays(tmp_path: Path) -> None:
    files = {
        "2025-04": served(person_row()),
        "2025-05": served(
            person_row(date="20/05/2025", supplier="CENTREBUS LTD", item="Example route 7")
        ),
    }
    (other,) = rows(land(tmp_path, files), "2025-05")
    assert other["item_text"] == "Example route 7"


def description(tmp_path: Path, item: str) -> str | None:
    """The landed description of an organisation's row carrying this one."""
    (row,) = one_month(tmp_path, person_row(supplier="CENTREBUS LTD", item=item))
    return row["item_text"]


@pytest.mark.parametrize(
    "item",
    [
        "Estate of Sam Testperson",
        "Sam Testperson T/A Example Taxis",
        "Payment to executors of Sam Testperson",
    ],
)
def test_a_description_introducing_a_person_is_blanked(tmp_path: Path, item: str) -> None:
    assert description(tmp_path, item) is None


@pytest.mark.parametrize(
    "item", ["Office estate rent", "Estates management", "TA Renewal of Structures", "TAS support"]
)
def test_a_marker_word_that_introduces_no_one_stays(tmp_path: Path, item: str) -> None:
    assert description(tmp_path, item) == item


@pytest.mark.parametrize(
    "item",
    [
        "Payment Mr Testperson",
        "PAYMENT MR TESTPERSON",
        "FAO Sam Testperson",
        "Dr. Testperson",
        "Ms Testperson",
        "Attn: J Testperson",
    ],
)
def test_free_text_naming_someone_by_title_or_for_the_attention_of_is_blanked(
    tmp_path: Path, item: str
) -> None:
    assert description(tmp_path, item) is None


@pytest.mark.parametrize(
    "item", ["BAU 3 MS Azure", "MS Teams licences", "MS 365", "Dr 4 inspection", "Sir 2 bridge"]
)
def test_a_title_word_before_a_product_or_code_stays(tmp_path: Path, item: str) -> None:
    assert description(tmp_path, item) == item


@pytest.mark.parametrize(
    "item",
    [
        "Professional Services by J Testperson January 2026",
        "Advice from J. K. Testperson",
        "Report for J K Testperson",
        "Support for J.Testperson",
    ],
)
def test_a_person_introduced_by_initials_is_blanked(tmp_path: Path, item: str) -> None:
    assert description(tmp_path, item) is None


def test_a_person_found_by_initials_is_blanked_wherever_the_year_names_them(
    tmp_path: Path,
) -> None:
    months = {
        "2025-04": served(
            person_row(supplier="CENTREBUS LTD", item="Professional Services by J Testperson")
        ),
        "2026-02": served(
            person_row(
                date="10/02/2026",
                supplier="CENTREBUS LTD",
                item="Professional Services J Testperson February 2026.",
            )
        ),
    }
    path = land(tmp_path, months)
    assert [r["item_text"] for r in rows(path, "2025-04") + rows(path, "2026-02")] == [None, None]


@pytest.mark.parametrize(
    "item",
    [
        "Works for L&P scheme",
        "Payment for T&M works",
        "Hire from J R B Plant Hire",
        "Package A Lighting",
        "Resurfacing for A14 junction",
        "Support for HS2 Phase One",
    ],
)
def test_codes_and_initialled_trading_names_are_not_taken_for_a_person(
    tmp_path: Path, item: str
) -> None:
    assert description(tmp_path, item) == item


def test_the_departments_own_redaction_stays_and_its_row_is_treated_as_a_person(
    tmp_path: Path,
) -> None:
    (row,) = one_month(tmp_path, person_row(supplier="REDACTED", item="NAME REMOVED"))
    assert row["supplier"] == "REDACTED"
    assert str(row["transaction_number"]).startswith("id:")
    assert (row["item_text"], row["postal_code"]) == (None, None)
    counts = profile(tmp_path / SET)
    assert (counts["rows_redacted"], counts["suppliers_hashed"]) == (1, 0)


def test_the_departments_own_bodies_are_known_organisations(tmp_path: Path) -> None:
    landed = one_month(
        tmp_path,
        person_row(supplier="Maritime and Coastguard Agency", item="Maritime and Coastguard"),
    )
    assert landed[0]["supplier"] == "Maritime and Coastguard Agency"
    assert landed[0]["item_text"] == "Maritime and Coastguard"


def test_a_supplier_the_register_vouches_for_lands_in_clear_and_a_person_stays_hashed(
    tmp_path: Path,
) -> None:
    """Both register records are invented, filed as British private companies; the person-shaped
    one stays hashed."""
    company = "EXAMPLE RAIL INFRASTRUCTURE OF BRITAIN"
    register = Register(
        (Record(company.title(), "GENERAL", "H0PO"), Record("Jane Example", "GENERAL", "H0PO"))
    )
    april = served(
        person_row(supplier=company, item="Track renewals"), person_row(item="Settlement")
    )
    path = tmp_path / SET
    path.write_bytes(step(year_files({"2025-04": april}), ENV, register))
    vouched, person = rows(path, "2025-04")
    assert (vouched["supplier"], vouched["item_text"]) == (company, "Track renewals")
    assert vouched["postal_code"] == "ZZ1 1ZZ"
    assert vouched["transaction_number"] == "4400123"
    assert str(person["supplier"]).startswith("person:")
    assert (person["item_text"], person["postal_code"]) == (None, None)


def test_the_step_refuses_without_the_hash_key() -> None:
    with pytest.raises(Exception, match=KEY_VARIABLE):
        step(year_files({}), {}, NONE)


def test_the_step_writes_nothing_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    step(year_files({"2025-04": served(person_row(), GRANT)}), ENV, NONE)
    assert list(tmp_path.rglob("*")) == []


def test_the_profile_counts_hashed_suppliers_rows_and_references(tmp_path: Path) -> None:
    files = {
        "2025-04": served(person_row(), person_row(number="4400124"), GRANT),
        "2025-05": served(person_row(date="01/05/2025", supplier="Sam Testperson")),
    }
    counts = profile(land(tmp_path, files))
    assert counts["rows"] == 4 + 10
    assert counts["suppliers_hashed"] == 2
    assert counts["rows_hashed"] == 3
    assert counts["references_hashed"] == 3


def test_the_fetch_kit_lands_the_year_only_as_a_set_through_this_step() -> None:
    assert fetch.SET_STEPS[SET] is step
    sources = fetch.load_sources(fetch.SOURCES)
    members = [name for name in sources if name.startswith(f"{SET}-")]
    assert len(members) == 12
    assert all(sources[name]["names_people"] for name in members)
    assert not set(members) & set(fetch.STEPS), "no month lands alone, past the set's scrub"


# The real files, landed with `uv run python -m ingest.fetch uk-transport-spend-2025`.

CONTENT_API = "https://www.gov.uk/api/content/government/publications/dft-spending-over-25000-"


def squash(text: str) -> tuple[str, set[int]]:
    """The leak hunt's own spelling, character by character: letters and digits only, accents
    dropped, case folded; and the offsets in it where a run of letters and digits began."""
    folded = unicodedata.normalize("NFKD", text).casefold()
    squashed, starts, inside = "", set(), False
    for c in folded:
        if unicodedata.combining(c):
            continue
        if c.isalnum() and not inside:
            starts.add(len(squashed))
        inside = c.isalnum()
        squashed += c if inside else ""
    return squashed, starts


def named_in(cell: str, spellings: set[str]) -> bool:
    """A spelling of 5 or more characters starting where a word starts, whatever follows it
    (`NAME'S`, `NAMEPAYMENT`, `(NAME)`); a shorter one only as a whole word."""
    squashed, starts = squash(cell)
    ends = {*starts, len(squashed)}
    for spelling in spellings:
        at = squashed.find(spelling)
        while at != -1:
            if at in starts and (len(spelling) >= 5 or at + len(spelling) in ends):
                return True
            at = squashed.find(spelling, at + 1)
    return False


@pytest.fixture(scope="module")
def real() -> Iterator[tuple[dict[str, bytes], list[dict[str, Any]]]]:
    """The served files, downloaded again and verified in memory, and the landed set's rows."""
    path = landed_file(SET)
    sources = fetch.load_sources(fetch.SOURCES)
    files = {}
    for name, entry in sources.items():
        if name.startswith(f"{SET}-"):
            raw = fetch.download(entry)
            fetch.verify(name, entry, raw)
            files[entry["url"]] = raw
    yield files, rows(path)


@pytest.mark.landed
def test_every_served_row_of_the_year_is_landed_once(real: tuple) -> None:
    """Against the files served again; the months, files and counts of the landed set are its
    quality suite's (`ingest/quality`)."""
    files, landed = real
    served_rows = sum(
        len(list(csv.reader(io.StringIO(raw.decode("cp1252"))))) for raw in files.values()
    )
    assert len(files) == 12
    assert len(landed) == served_rows - len(files), "every served row lands, headers aside"


@pytest.mark.pages
def test_no_pinned_month_has_been_revised_on_gov_uk() -> None:
    """The Department revises a month by replacing its file on the publication page with a change
    note; the files themselves mark nothing as provisional. A pin is current while its page still
    lists it as the only file and notes no change after first publication. Each member is named by
    its month's abbreviation (`uk-transport-spend-2025-apr`), January to March in the next year."""
    revised = {}
    for name, entry in fetch.load_sources(fetch.SOURCES).items():
        if not name.startswith(f"{SET}-"):
            continue
        month = list(calendar.month_abbr).index(name.rsplit("-", 1)[1].title())
        year = int(SET.rsplit("-", 1)[1]) + (month < 4)
        pinned = entry["url"]
        page = httpx.get(
            f"{CONTENT_API}{calendar.month_name[month].lower()}-{year}",
            headers={"User-Agent": fetch.CLIENT_NAME},
            timeout=60,
        ).json()["details"]
        notes = [c["note"] for c in page["change_history"] if c["note"] != "First published."]
        attached = [a["url"] for a in page["attachments"]]
        if notes or attached != [pinned]:
            revised[pinned] = (notes, attached)
    assert revised == {}


@pytest.mark.landed
def test_no_hashed_supplier_is_named_anywhere_in_the_landed_set(real: tuple) -> None:
    """Independent of the step's word windows: a hashed supplier's name, squashed character by
    character to letters and digits, in either word order, must not start at a word in any clear
    cell of any row but a supplier; their row keeps no reference, postcode or description; and a
    reference carried only by hashed rows appears nowhere in clear (one shared with clear rows,
    such as a payment-run number or `#`, stays clear there, and the keyed hash does not tie the
    clear copy to its hashed row)."""
    files, landed = real
    by_position = {(r["source_file"], r["source_row"]): r for r in landed}
    hashed_names, hashed_refs, clear_refs = set(), set(), set()
    for pinned, raw in files.items():
        served_rows = list(csv.reader(io.StringIO(raw.decode("cp1252"), newline="")))[1:]
        for position, cells in enumerate(served_rows, 1):
            row = by_position[(pinned.rsplit("/", 1)[1], position)]
            supplier, reference = cells[5], cells[6]
            if row["supplier"] != supplier:
                assert row["supplier"].startswith("person:"), "a supplier changes only to a hash"
                hashed_names.add(supplier)
            if row["supplier"] != supplier or row["supplier"] == "REDACTED":
                hashed_refs.add(reference)
                assert row["transaction_number"] == "id:" + row["transaction_number"][3:]
                assert len(row["transaction_number"]) == 67
                assert (row["item_text"], row["postal_code"]) == (None, None)
            else:
                clear_refs.add(reference)
    spellings = set()
    for name in hashed_names:
        words = re.findall(r"\w+", name)
        spellings |= {squash(name)[0], squash(" ".join(reversed(words)))[0]}
    spellings.discard("")
    found: dict[str, int] = {}
    inside_clear_suppliers = 0
    for row in landed:
        for column in COLUMNS[2:]:
            cell = row[column]
            if cell is None or cell.startswith(("person:", "id:")):
                continue
            if named_in(cell, spellings):
                if column == "supplier":
                    inside_clear_suppliers += 1
                else:
                    found[column] = found.get(column, 0) + 1
    print(
        f"{len(hashed_names)} names hashed, {len(hashed_refs)} references hashed, of which"
        f" {len(hashed_refs & clear_refs)} also on clear rows; clear suppliers containing a"
        f" hashed name: {inside_clear_suppliers}"
    )
    assert found == {}
    leaked_refs = {r["transaction_number"] for r in landed} & (hashed_refs - clear_refs)
    assert leaked_refs == set()
    assert {normalise(n) for n in hashed_names} & {
        normalise(r["supplier"]) for r in landed
    } == set()
    register = Register.landed()
    vouched = {
        r["supplier"]
        for r in landed
        if not r["supplier"].startswith("person:")
        and r["supplier"] != "REDACTED"
        and not is_organisation(r["supplier"], BODIES)
    }
    print(f"{len(vouched)} clear suppliers on the register's word alone")
    assert {n for n in vouched if not register.vouches(n) or person_shaped(n)} == set()
