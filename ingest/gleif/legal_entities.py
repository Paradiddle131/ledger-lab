"""The GLEIF legal-entity register: the step that lands it, and the profile of what landed.

`step` reads the one CSV inside a verified golden-copy archive and turns the records whose legal
address is in a vendor's country (`COUNTRIES`) into Parquet, keeping only the columns the vendor
matching needs (`COLUMNS`) as the text the register served, with blanks as nulls.

What may name a person never lands, and doubt means a person:
- a record the register files as a sole proprietor names an individual acting in a business
  capacity, and its LEI finds that name again in the public register, so hashing the name would
  hide nothing: the record is left out;
- a record whose legal form is a Dutch partnership (`DUTCH_PARTNERSHIPS`) is left out too;
- an other or transliterated name that `ingest.individuals.is_organisation` does not call an
  organisation is blanked, with its language and type.

Not screened: a US or UK partnership whose legal-form code does not single it out, a company's
own legal name, which lands as the register records it, and an other name the classifier passes
(a company named after a person, `Jane Doe Ltd`, is one on purpose). So the matching must let a
register entry vouch that a name is an organisation only when its legal form makes a separate
legal person.

The unzipped CSV (about 5 GB for the whole world) is never written: it is decompressed and read
one record at a time in memory, and only the records and names that land reach a temporary file,
which the step deletes before it returns. What the step left out is counted in the Parquet file's
key-value metadata (`dropped`), which `profile` reads.
"""

import csv
import io
import json
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import TypedDict

import duckdb

from ingest.individuals import is_organisation

# The legal-address countries the entities' vendors sit in.
COUNTRIES = ("GB", "NL", "US")
# The register's entity category for an individual acting in a business capacity.
SOLE_PROPRIETOR = "SOLE_PROPRIETOR"
# Dutch partnership forms by legal-form code. None makes a separate legal person, and their names
# are usually the partners' own, so a record with one of these forms never lands.
DUTCH_PARTNERSHIPS = {
    "62Y3": "vennootschap onder firma",
    "9AAK": "maatschap",
    "CODH": "commanditaire vennootschap",
}
COUNTRY = "Entity.LegalAddress.Country"
CATEGORY = "Entity.EntityCategory"
LEGAL_FORM = "Entity.LegalForm.EntityLegalFormCode"
# The register's legal-form code for a form it was not told.
LEGAL_FORM_NOT_AVAILABLE = "9999"
# The golden-copy CSV carries up to five other and five transliterated names a record.
SLOTS = range(1, 6)
OTHER_NAMES = tuple(
    f"Entity.{group}.{name}.{n}"
    for group, name in (
        ("OtherEntityNames", "OtherEntityName"),
        ("TransliteratedOtherEntityNames", "TransliteratedOtherEntityName"),
    )
    for n in SLOTS
)

COLUMNS: tuple[str, ...] = (
    "LEI",
    "Entity.LegalName",
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


class Profile(TypedDict):
    rows_by_country: dict[str, int]
    incomplete: int
    malformed_leis: int
    duplicate_leis: int
    sole_proprietors: int
    partnerships: int
    legal_form_not_available: int
    dropped_sole_proprietors: int
    dropped_partnerships: dict[str, int]
    other_names_blanked: int


class LegalEntitiesError(ValueError):
    """The served archive is not the golden copy this step knows."""


def _blank_people(row: list[str], names: list[int]) -> int:
    """Blank each other name the classifier does not call an organisation, with its language and
    type (the two columns after it); the count blanked."""
    blanked = 0
    for at in names:
        if row[at] and not is_organisation(row[at]):
            row[at : at + 3] = ["", "", ""]
            blanked += 1
    return blanked


def step(raw: bytes) -> bytes:
    """The verified archive's records for the vendors' countries as Parquet, with everything that
    may name a person left out."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as error:
        raise LegalEntitiesError("the served file is not a ZIP archive") from error
    members = archive.infolist()
    if len(members) != 1 or not members[0].filename.endswith(".csv"):
        raise LegalEntitiesError("the archive does not hold exactly one CSV file")
    dropped: Counter[str] = Counter({code: 0 for code in DUTCH_PARTNERSHIPS})
    dropped[SOLE_PROPRIETOR] = dropped["other names"] = 0
    with archive.open(members[0]) as member, tempfile.TemporaryDirectory() as work:
        reader = csv.reader(io.TextIOWrapper(member, encoding="utf-8", newline=""))
        header = next(reader, [])
        missing = [column for column in COLUMNS if column not in header]
        if missing:
            raise LegalEntitiesError(f"the served file lacks the columns {missing}")
        keep = [header.index(column) for column in COLUMNS]
        country, category, form = (header.index(c) for c in (COUNTRY, CATEGORY, LEGAL_FORM))
        names = [COLUMNS.index(name) for name in OTHER_NAMES]
        kept, parquet = Path(work, "entities.csv"), Path(work, "entities.parquet")
        with kept.open("w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            writer.writerow(COLUMNS)
            for row in reader:
                if row[country] not in COUNTRIES:
                    continue
                if row[category] == SOLE_PROPRIETOR:
                    dropped[SOLE_PROPRIETOR] += 1
                elif row[form] in DUTCH_PARTNERSHIPS:
                    dropped[row[form]] += 1
                else:
                    landing = [row[i] for i in keep]
                    dropped["other names"] += _blank_people(landing, names)
                    writer.writerow(landing)
        con = duckdb.connect()
        con.read_csv(
            str(kept), header=True, all_varchar=True, sep=",", quotechar='"', escapechar='"'
        ).create_view("kept")
        metadata = json.dumps(dict(sorted(dropped.items())))
        con.execute(
            f"COPY kept TO '{parquet}' (FORMAT parquet, KV_METADATA {{dropped: '{metadata}'}})"
        )
        return parquet.read_bytes()


def profile(path: Path) -> Profile:
    """The landed file's records counted by country, every gap the matching would trip on, and
    what the step left out."""
    con = duckdb.connect()
    con.read_parquet(str(path)).create_view("e")

    def one(sql: str) -> int:
        row = con.execute(sql).fetchone()
        if row is None:
            raise LegalEntitiesError(f"no answer from {sql!r}")
        return int(row[0])

    stored = con.execute(
        "SELECT value FROM parquet_kv_metadata(?) WHERE key = 'dropped'", [str(path)]
    ).fetchone()
    if stored is None:
        raise LegalEntitiesError("the landed file does not say what the step left out")
    dropped: dict[str, int] = json.loads(bytes(stored[0]).decode())
    partnerships = ", ".join(f"'{code}'" for code in DUTCH_PARTNERSHIPS)
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
        partnerships=one(f'SELECT count(*) FROM e WHERE "{LEGAL_FORM}" IN ({partnerships})'),  # noqa: S608
        legal_form_not_available=one(
            f"""SELECT count(*) FROM e WHERE "{LEGAL_FORM}" = '{LEGAL_FORM_NOT_AVAILABLE}'"""  # noqa: S608
        ),
        dropped_sole_proprietors=dropped[SOLE_PROPRIETOR],
        dropped_partnerships={code: dropped[code] for code in DUTCH_PARTNERSHIPS},
        other_names_blanked=dropped["other names"],
    )
