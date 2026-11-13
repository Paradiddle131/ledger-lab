"""The GLEIF legal-entity register: the step that lands it, and the profile of what landed.

`step` reads the one CSV inside a verified golden-copy archive and turns the records whose legal
address is in a vendor's country (`COUNTRIES`) into Parquet, keeping only the columns the vendor
matching needs (`COLUMNS`) as the text the register served, with blanks as nulls.
A record the register files as a sole proprietor names an individual acting in a business
capacity, and its LEI finds that name again in the public register, so hashing the name would
hide nothing: the record never lands.
"""

import csv
import io
import tempfile
import zipfile
from pathlib import Path
from typing import TypedDict

import duckdb

# The legal-address countries the entities' vendors sit in.
COUNTRIES = ("GB", "NL", "US")
# The register's entity category for an individual acting in a business capacity.
SOLE_PROPRIETOR = "SOLE_PROPRIETOR"
COUNTRY = "Entity.LegalAddress.Country"
CATEGORY = "Entity.EntityCategory"
LEGAL_FORM = "Entity.LegalForm.EntityLegalFormCode"
# The register's legal-form code for a form it was not told.
LEGAL_FORM_NOT_AVAILABLE = "9999"


def _names(group: str, name: str) -> tuple[str, ...]:
    """A name group's columns: the golden-copy CSV carries up to five names, each with its
    language and type."""
    parts = ("", ".xmllang", ".type")
    return tuple(f"Entity.{group}.{name}.{n}{part}" for n in range(1, 6) for part in parts)


COLUMNS: tuple[str, ...] = (
    "LEI",
    "Entity.LegalName",
    "Entity.LegalName.xmllang",
    *_names("OtherEntityNames", "OtherEntityName"),
    *_names("TransliteratedOtherEntityNames", "TransliteratedOtherEntityName"),
    COUNTRY,
    "Entity.LegalJurisdiction",
    CATEGORY,
    LEGAL_FORM,
    "Entity.LegalForm.OtherLegalForm",
    "Entity.EntityStatus",
    "Registration.RegistrationStatus",
    "Registration.LastUpdateDate",
)


class Profile(TypedDict):
    rows_by_country: dict[str, int]
    incomplete: int
    malformed_leis: int
    duplicate_leis: int
    sole_proprietors: int
    legal_form_not_available: int


class LegalEntitiesError(ValueError):
    """The served archive is not the golden copy this step knows."""


def step(raw: bytes) -> bytes:
    """The verified archive's records for the vendors' countries as Parquet, sole proprietors left
    out."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as error:
        raise LegalEntitiesError("the served file is not a ZIP archive") from error
    members = archive.infolist()
    if len(members) != 1 or not members[0].filename.endswith(".csv"):
        raise LegalEntitiesError("the archive does not hold exactly one CSV file")
    with archive.open(members[0]) as member, tempfile.TemporaryDirectory() as work:
        reader = csv.reader(io.TextIOWrapper(member, encoding="utf-8", newline=""))
        header = next(reader, [])
        missing = [column for column in COLUMNS if column not in header]
        if missing:
            raise LegalEntitiesError(f"the served file lacks the columns {missing}")
        keep = [header.index(column) for column in COLUMNS]
        country, category = header.index(COUNTRY), header.index(CATEGORY)
        kept, parquet = Path(work, "entities.csv"), Path(work, "entities.parquet")
        with kept.open("w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            writer.writerow(COLUMNS)
            for row in reader:
                if row[country] in COUNTRIES and row[category] != SOLE_PROPRIETOR:
                    writer.writerow([row[i] for i in keep])
        duckdb.read_csv(
            str(kept), header=True, all_varchar=True, sep=",", quotechar='"', escapechar='"'
        ).write_parquet(str(parquet))
        return parquet.read_bytes()


def profile(path: Path) -> Profile:
    """The landed file's records counted by country, and every gap the matching would trip on."""
    con = duckdb.connect()
    con.read_parquet(str(path)).create_view("e")

    def one(sql: str) -> int:
        row = con.execute(sql).fetchone()
        if row is None:
            raise LegalEntitiesError(f"no answer from {sql!r}")
        return int(row[0])

    by_country = con.execute(f'SELECT "{COUNTRY}", count(*) FROM e GROUP BY 1').fetchall()  # noqa: S608
    return Profile(
        rows_by_country={str(k): int(v) for k, v in by_country},
        incomplete=one(
            f'SELECT count(*) FROM e WHERE "LEI" IS NULL OR "Entity.LegalName" IS NULL'  # noqa: S608
            f' OR "{LEGAL_FORM}" IS NULL OR "{COUNTRY}" IS NULL'
        ),
        malformed_leis=one(
            """SELECT count(*) FROM e WHERE NOT regexp_full_match("LEI", '[0-9A-Z]{18}[0-9]{2}')"""
        ),
        duplicate_leis=one('SELECT count(*) - count(DISTINCT "LEI") FROM e'),
        sole_proprietors=one(
            f"""SELECT count(*) FROM e WHERE "{CATEGORY}" = '{SOLE_PROPRIETOR}'"""  # noqa: S608
        ),
        legal_form_not_available=one(
            f"""SELECT count(*) FROM e WHERE "{LEGAL_FORM}" = '{LEGAL_FORM_NOT_AVAILABLE}'"""  # noqa: S608
        ),
    )
