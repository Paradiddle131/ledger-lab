"""The GLEIF legal-entity register: the step that lands it, and the profile of what landed.

`step` reads the one CSV inside a verified golden-copy archive and turns the records whose legal
address is in a vendor's country (`COUNTRIES`) into Parquet, keeping only the columns the vendor
matching needs (`COLUMNS`) as the text the register served, with blanks as nulls.

What may name a person never lands, and doubt means a person. A record is left out when:
- the register files it as a sole proprietor, an individual acting in a business capacity, whose
  LEI finds the name again in the public register, so hashing the name would hide nothing;
- its category is blank or one this step does not know (`CATEGORIES`);
- its legal form is a sole proprietorship (`SOLE_PROPRIETORSHIPS`) or a partnership that is no
  separate legal person and is usually named after its partners (`PARTNERSHIPS`), whatever its
  category;
- it has no real legal-form code (`NO_LEGAL_FORM`: blank, "other" or "not available") and
  `ingest.individuals.is_organisation` does not call its legal name an organisation.
In a record that lands, an other or transliterated name the classifier does not call an
organisation is blanked, with its language and type.

Not screened: the legal name of a record with a real legal-form code, which lands as the register
records it; partnerships that only a free-text legal form reveals (an English general partnership
has no code); and other names the classifier passes (a company named after a person, `Jane Doe
Ltd`, is one on purpose). Matching on a headquarters address is out of scope: only the legal
address's country is landed. So the matching must let a register entry vouch that a name is an
organisation only when its legal form makes a separate legal person, and never a record without a
real legal-form code.

The unzipped CSV (about 5 GB for the whole world) is never written: it is decompressed and read
one record at a time in memory, and an archive declaring more than `MAX_UNZIPPED` is refused before
any of it is read. Only the screened records that land are written, to a temporary CSV in a private
folder (`.gleif-legal-entities.<random>`, mode 0700) under the landing path, next to the landed
file, or in the system's temporary folder when the landing is the bucket, and DuckDB writes the
Parquet beside it; the step removes the folder before it returns, also on an error. A process
killed outright leaves that folder behind, holding at most those screened records and a partial
Parquet file, never more than the landing would. What the step left out is
counted by reason in the Parquet file's key-value metadata (`left out`), which `profile` reads.
"""

import csv
import io
import json
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TypedDict

import duckdb

from ingest.individuals import is_organisation

# The legal-address countries the entities' vendors sit in.
COUNTRIES = ("GB", "NL", "US")
# The register's entity category for an individual acting in a business capacity.
SOLE_PROPRIETOR = "SOLE_PROPRIETOR"
# The other categories the register uses; a record with any other or a blank one is left out.
CATEGORIES = frozenset(
    {"GENERAL", "FUND", "BRANCH", "RESIDENT_GOVERNMENT_ENTITY", "INTERNATIONAL_ORGANIZATION"}
)
# Sole proprietorships by legal-form code, from GLEIF's list of entity legal forms for the
# vendors' countries (England and Wales has none).
SOLE_PROPRIETORSHIPS = {
    "4QXM": "eenmanszaak (NL)",
    "88XU": "sole proprietorship (US-MI)",
    "AREY": "sole proprietorship (US-KS)",
    "EQAQ": "sole proprietorship (US-AK)",
    "XHT1": "sole proprietorship (US-MD)",
}
# Partnerships that make no separate legal person, or a general partnership whose name is usually
# its partners', by legal-form code from the same list. A Dutch European economic interest
# grouping (5WU6) is a legal person under Dutch law and lands; so do limited partnerships and
# limited liability partnerships, which are registered with a name of their own.
PARTNERSHIPS = {
    "62Y3": "vennootschap onder firma (NL)",
    "9AAK": "maatschap (NL)",
    "CODH": "commanditaire vennootschap (NL)",
    "UNJ2": "rederij (NL)",
    "1CZS": "general partnership (US-UT)",
    "456O": "partnership (US-VA)",
    "6JCE": "general partnership (US-ND)",
    "7TJ1": "general partnership (US-TN)",
    "7U8S": "general partnership (US-KS)",
    "8PUO": "foreign partnership (US-LA)",
    "8YBQ": "general partnership (US-MS)",
    "95OK": "general partnership (US-OH)",
    "B38Q": "general partnership (US-HI)",
    "DEMP": "general partnership (US-MN)",
    "EBP8": "general partnership (US-KY)",
    "KFPS": "domestic general partnership (US-NE)",
    "NK3V": "general partnership (US-MI)",
    "O6QP": "domestic partnership (US-LA)",
    "QF4W": "partnership (US-DE)",
    "R2PI": "general partnership (US-MD)",
    "R3J6": "foreign general partnership (US-NE)",
    "RSWJ": "general partnership (US-AZ)",
    "SQ7B": "general partnership (US-CA)",
    "UK9P": "general partnership (US-ID)",
    "WNKG": "partnership (US-AK)",
}
# Legal-form codes that name no form: blank, "other" (described only in free text) and
# "not available".
NO_LEGAL_FORM = frozenset({"", "8888", "9999"})
LEGAL_FORM_NOT_AVAILABLE = "9999"
# The largest unzipped CSV the step reads: the pinned file declares 5,060,480,759 bytes, and the
# register grows by a few per cent a year, so 6 GiB leaves room for a re-pin and refuses a wrong
# or hostile archive before any of it is decompressed.
MAX_UNZIPPED = 6 * 1024**3
COUNTRY = "Entity.LegalAddress.Country"
CATEGORY = "Entity.EntityCategory"
LEGAL_FORM = "Entity.LegalForm.EntityLegalFormCode"
LEGAL_NAME = "Entity.LegalName"
# The golden-copy CSV carries up to five other and five transliterated names a record.
OTHER_NAMES = tuple(
    f"Entity.{group}.{name}.{n}"
    for group, name in (
        ("OtherEntityNames", "OtherEntityName"),
        ("TransliteratedOtherEntityNames", "TransliteratedOtherEntityName"),
    )
    for n in range(1, 6)
)

COLUMNS: tuple[str, ...] = (
    "LEI",
    LEGAL_NAME,
    "Entity.LegalName.xmllang",
    *(f"{name}{part}" for name in OTHER_NAMES for part in ("", ".xmllang", ".type")),
    COUNTRY,
    "Entity.LegalJurisdiction",
    CATEGORY,
    LEGAL_FORM,
    "Entity.LegalForm.OtherLegalForm",
    "Entity.EntityStatus",
    "Registration.RegistrationStatus",
    "Registration.LastUpdateDate",
)
# The reasons a record is left out, each counted even when nothing was.
REASONS = (
    "sole proprietor category",
    "unknown or blank category",
    *(f"sole proprietorship {code}" for code in SOLE_PROPRIETORSHIPS),
    *(f"partnership {code}" for code in PARTNERSHIPS),
    *(f"no legal form, {country}" for country in COUNTRIES),
)


class Profile(TypedDict):
    rows_by_country: dict[str, int]
    incomplete: int
    malformed_leis: int
    duplicate_leis: int
    legal_form_not_available: int
    left_out: dict[str, int]
    other_names_blanked: int
    still_to_leave_out: dict[str, int]


class LegalEntitiesError(ValueError):
    """The served archive is not the golden copy this step knows."""


def reason_to_leave_out(country: str, category: str, form: str, legal_name: str) -> str | None:
    """Why a record in a vendor's country may name a person, or None when it lands."""
    if category == SOLE_PROPRIETOR:
        return "sole proprietor category"
    if category not in CATEGORIES:
        return "unknown or blank category"
    if form in SOLE_PROPRIETORSHIPS:
        return f"sole proprietorship {form}"
    if form in PARTNERSHIPS:
        return f"partnership {form}"
    if form in NO_LEGAL_FORM and not is_organisation(legal_name):
        return f"no legal form, {country}"
    return None


def _blank_people(row: list[str], names: Sequence[int]) -> int:
    """Blank each other name the classifier does not call an organisation, with its language and
    type (the two columns after it); the count blanked."""
    blanked = 0
    for at in names:
        if row[at] and not is_organisation(row[at]):
            row[at : at + 3] = ["", "", ""]
            blanked += 1
    return blanked


def _member(raw: bytes) -> tuple[zipfile.ZipFile, zipfile.ZipInfo]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as error:
        raise LegalEntitiesError("the served file is not a ZIP archive") from error
    members = archive.infolist()
    if len(members) != 1 or not members[0].filename.endswith(".csv"):
        raise LegalEntitiesError("the archive does not hold exactly one CSV file")
    if members[0].file_size > MAX_UNZIPPED:
        raise LegalEntitiesError(
            f"the archive declares {members[0].file_size} bytes unzipped, over {MAX_UNZIPPED}"
        )
    return archive, members[0]


def _screen(records: Iterable[list[str]], header: list[str], out: io.TextIOBase) -> Counter[str]:
    """Write the records that land, screened, and count what was left out."""
    missing = [column for column in COLUMNS if column not in header]
    if missing:
        raise LegalEntitiesError(f"the served file lacks the columns {missing}")
    keep = [header.index(column) for column in COLUMNS]
    country, category, form, name = (
        header.index(c) for c in (COUNTRY, CATEGORY, LEGAL_FORM, LEGAL_NAME)
    )
    names = [COLUMNS.index(other) for other in OTHER_NAMES]
    counts: Counter[str] = Counter({reason: 0 for reason in (*REASONS, "other names blanked")})
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for row in records:
        if row[country] not in COUNTRIES:
            continue
        reason = reason_to_leave_out(row[country], row[category], row[form], row[name])
        if reason:
            counts[reason] += 1
            continue
        landing = [row[i] for i in keep]
        counts["other names blanked"] += _blank_people(landing, names)
        writer.writerow(landing)
    return counts


def step(raw: bytes) -> bytes:
    """The verified archive's records for the vendors' countries as Parquet, with everything that
    may name a person left out."""
    from ingest.fetch import work_folder  # the kit imports this module to register its step

    archive, member = _member(raw)
    with (
        archive.open(member) as served,
        tempfile.TemporaryDirectory(dir=work_folder(), prefix=".gleif-legal-entities.") as work,
    ):
        reader = csv.reader(io.TextIOWrapper(served, encoding="utf-8", newline=""))
        kept, parquet = Path(work, "entities.csv"), Path(work, "entities.parquet")
        with kept.open("w", newline="", encoding="utf-8") as out:
            counts = _screen(reader, next(reader, []), out)
        con = duckdb.connect()
        con.read_csv(
            str(kept), header=True, all_varchar=True, sep=",", quotechar='"', escapechar='"'
        ).create_view("kept")
        metadata = json.dumps(dict(counts))
        con.execute(
            f"COPY kept TO '{parquet}' (FORMAT parquet, KV_METADATA {{'left out': '{metadata}'}})"
        )
        return parquet.read_bytes()


def profile(path: Path) -> Profile:
    """The landed file's records counted by country, every gap the matching would trip on, what
    the step left out, and any landed record it should have left out."""
    con = duckdb.connect()
    con.read_parquet(str(path)).create_view("e")

    def one(sql: str) -> int:
        row = con.execute(sql).fetchone()
        if row is None:
            raise LegalEntitiesError(f"no answer from {sql!r}")
        return int(row[0])

    stored = con.execute(
        "SELECT value FROM parquet_kv_metadata(?) WHERE key = 'left out'", [str(path)]
    ).fetchone()
    if stored is None:
        raise LegalEntitiesError("the landed file does not say what the step left out")
    left_out: dict[str, int] = json.loads(bytes(stored[0]).decode())
    blanked = left_out.pop("other names blanked")
    landed = con.execute(
        f'SELECT "{COUNTRY}", "{CATEGORY}", coalesce("{LEGAL_FORM}", \'\'),'  # noqa: S608
        f" coalesce(\"{LEGAL_NAME}\", '') FROM e"
    ).fetchall()
    still = Counter(reason for r in landed if (reason := reason_to_leave_out(*r)))
    by_country = con.execute(f'SELECT "{COUNTRY}", count(*) FROM e GROUP BY 1').fetchall()  # noqa: S608
    return Profile(
        rows_by_country={str(k): int(v) for k, v in by_country},
        incomplete=one(
            f'SELECT count(*) FROM e WHERE "LEI" IS NULL OR "{LEGAL_NAME}" IS NULL'  # noqa: S608
            f' OR "{LEGAL_FORM}" IS NULL OR "{COUNTRY}" IS NULL'
        ),
        malformed_leis=one(
            """SELECT count(*) FROM e WHERE NOT regexp_full_match("LEI", '[0-9A-Z]{18}[0-9]{2}')"""
        ),
        duplicate_leis=one('SELECT count(*) - count(DISTINCT "LEI") FROM e'),
        legal_form_not_available=one(
            f"""SELECT count(*) FROM e WHERE "{LEGAL_FORM}" = '{LEGAL_FORM_NOT_AVAILABLE}'"""  # noqa: S608
        ),
        left_out=left_out,
        other_names_blanked=blanked,
        still_to_leave_out=dict(still),
    )
