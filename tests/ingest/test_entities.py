"""The register vouches that a payee is an organisation only for the whole name of a separate legal
person, never for a name shaped like a person's, and the steps that hash payees land those names
in clear.

The companies are real records of the register (CC0); every person-like name and every invented
organisation is fictional.
"""

import csv
import io
import zipfile
from pathlib import Path

import duckdb
import pytest

from ingest.chicago.table import Table, land_all
from ingest.entities import (
    LEGAL_PERSONS,
    REGISTER,
    Record,
    Register,
    RegisterError,
    match_key,
    person_shaped,
    separate_legal_person,
)
from ingest.gleif import legal_entities
from ingest.gleif.legal_entities import (
    CATEGORIES,
    COUNTRIES,
    NO_LEGAL_FORM,
    PARTNERSHIPS,
    SOLE_PROPRIETORSHIPS,
)
from ingest.individuals import KEY_VARIABLE, person_hasher, pseudonymiser

ENV = {KEY_VARIABLE: "k" * 32}
# Codes from GLEIF's list: a California for-profit corporation, a British public limited company,
# a Dutch private limited company, a British limited partnership and a Delaware one.
CORPORATION, PLC, BV, GB_LP, DE_LP = "H1UM", "B6ES", "54M6", "ID30", "T91T"
# An organisation the classifier alone hashes: no legal form, more words than a person's name.
HOSPITAL = "EXAMPLE HEALTH SYSTEM OF CHICAGO"
PERSON = "JANE Q EXAMPLE"
REGISTER_RECORDS = (
    Record(HOSPITAL.title(), "GENERAL", CORPORATION),
    Record("Jane Q. Example", "GENERAL", CORPORATION),
    Record("Example Widgets Of America LLC", "GENERAL", CORPORATION),
    Record("Example Partners Of Chicago", "GENERAL", DE_LP),
    Record("Example Ventures Of London", "GENERAL", GB_LP),
    Record("Example Trading Of Rotterdam", "SOLE_PROPRIETOR", BV),
    Record("Example Formless Group Of Leeds", "GENERAL", "8888"),
)


@pytest.fixture
def register() -> Register:
    return Register(REGISTER_RECORDS)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Apple Inc.", "apple inc"),
        ("ACME (U.K.) LTD", "acme u k ltd"),
        ("Doe & Roe,  Limited", "doe & roe limited"),
        ("“Example”   Holdings", "example holdings"),
        ("Ex_ample", "ex ample"),
    ],
)
def test_the_matching_key_is_the_whole_name_word_for_word(name: str, key: str) -> None:
    assert match_key(name) == key


# Invented people, spelt the ways a payee file writes a person. The shape is judged on the words the
# register is matched on, so no punctuation, spacing or Unicode form changes it, and titles,
# suffixes, degrees, name particles, initials and figures are not counted as words.
PERSON_SPELLINGS = [
    "JANE EXAMPLE",
    "EXAMPLE, JANE Q",
    "J. EXAMPLE",
    "MARY-ANN O'EXAMPLE",
    "EXAMPLE",
    "EXAMPLE,JANE",
    "J.Q.EXAMPLE",
    "JANE (Q) EXAMPLE",
    "JANE/EXAMPLE",
    "JANE_EXAMPLE",
    "JANE EXAMPLE-",
    "JANE O`EXAMPLE",
    "JANE EXAMPLE, M.D.",
    "JANE EXAMPLE PH.D",
    "JANE EXAMPLE D.D.S.",
    "JANE Q EXAMPLE JR",
    "DR. JANE Q. EXAMPLE",
    "MS JANE EXAMPLE",
    "JANE EXAMPLE III",
    "JANE EXAMPLE ESQ.",
    "JANE EXAMPLE, CPA",
    "JANE EXAMPLE SR.",
    "JANE EXAMPLE M D",
    "PROF JANE EXAMPLE PHD",
    "JANE EXAMPLE RN",
    "J. Q. R. EXAMPLE",
    "JANE DE LA EXAMPLE",
    "MARIA DE LOS ANGELES EXAMPLE",
    "JANE EXAMPLE-SAMPLE",
    "JANE EXAMPLE 2",
    "JANE AND JOHN EXAMPLE",
    "DOE & ROE",
    "EXAMPLE (UK)",
    "JANE EXAMPLE COMPANY",
    "EXAMPLE & SONS",
    "MARY-ANN JANE EXAMPLE-SAMPLE",
    "MARIA-JOSE DE LA CRUZ-EXAMPLE O'SAMPLE",
    "JANEEXAMPLE",
    "J\u00e4ne Ex\u00e4mple",
    "\uff2a\uff21\uff2e\uff25 \uff25\uff38\uff21\uff2d\uff30\uff2c\uff25",
    "JANE\u200bEXAMPLE",
    "JANE\u00a0EXAMPLE",
]


@pytest.mark.parametrize("name", PERSON_SPELLINGS)
def test_a_person_spelt_any_way_is_person_shaped(name: str) -> None:
    assert person_shaped(name)


@pytest.mark.parametrize("name", PERSON_SPELLINGS)
def test_a_register_record_with_a_persons_exact_name_never_vouches(name: str) -> None:
    register = Register([Record(name, "GENERAL", CORPORATION)])
    assert match_key(name) in register.keys, "the record has the person's very key"
    assert not register.vouches(name)


# Invented names a register record could carry exactly, which the register must never clear: a
# legal form after what may be a person's name or initials, person evidence anywhere, two people
# joined, and figures or marks that must not stretch a name past the guard.
NEVER_VOUCHED = [
    "JANE EXAMPLE PC",
    "JANE EXAMPLE MD PC",
    "JANE EXAMPLE M.D., P.C.",
    "DR JANE EXAMPLE INC",
    "JANE EXAMPLE DDS PLLC",
    "J. EXAMPLE B.V.",
    "EXAMPLE B.V.",
    "EXAMPLE, B.V.",
    "EXAMPLE N.V.",
    "EXAMPLE P.C.",
    "DE EXAMPLE B.V.",
    "J EXAMPLE N.V.",
    "EXAMPLE BV",
    "JANE EXAMPLE & JOHN SAMPLE LLP",
    "JANE EXAMPLE TRUST LLC",
    "STICHTING J. EXAMPLE",
    "VERENIGING JANE EN JOHN EXAMPLE",
    "JANE EXAMPLE, INC.",
    "JANE Q EXAMPLE B.V.",
    "CHICK-EXAMPLE, INC",
    "JANE EXAMPLE FAMILY HOLDINGS OF CHICAGO",
    "JANE EXAMPLE REVOCABLE LIVING HOLDINGS",
    "JANE MARY EXAMPLE C/O EXAMPLE HOLDINGS GROUP",
    "JANE EXAMPLE AS AGENT FOR EXAMPLE HOLDINGS",
    "JANE EXAMPLE AND JOHN EXAMPLE SAMPLE",
    "JANE & JOHN EXAMPLE CO",
    "JANE EXAMPLE & JOHN EXAMPLE",
    "JANE MARY EXAMPLE 2ND",
    "JANE MARY EXAMPLE 3RD",
    "JANE MARY EXAMPLE APT 4B",
    "JANE MARY EXAMPLE #4B",
    "JANE MARY EXAMPLE2",
    "JANE² MARY EXAMPLE",
    "JANE MARY EXAMPLE \u216b",
    "JAQ\u0301UE MARY EXAMPLE",
    "3M EXAMPLE",
    "A1 EXAMPLE",
]


@pytest.mark.parametrize("name", NEVER_VOUCHED)
def test_a_register_record_never_clears_a_name_a_person_could_carry(name: str) -> None:
    register = Register([Record(name, "GENERAL", CORPORATION)])
    assert match_key(name) in register.keys, "the record has the name's very key"
    assert not register.vouches(name)


@pytest.mark.parametrize(
    "name", [HOSPITAL, "EXAMPLE WIDGETS OF NORTH AMERICA", "JANE MARY ANN EXAMPLE"]
)
def test_four_counted_words_on_every_side_of_a_joiner_are_not_person_shaped(name: str) -> None:
    """Four counted words is past the guard on purpose: counting them would keep dozens more of the
    pinned files' register-vouched names hashed (universities, banks, charities), and a person's
    name of four words needs a legal-person record of exactly that name to come out; the pinned
    register holds about six vouching records of four-word, two-people or similar shapes."""
    assert not person_shaped(name)


def test_only_a_listed_form_of_a_kept_category_is_a_separate_legal_person() -> None:
    assert separate_legal_person("GENERAL", CORPORATION)
    assert separate_legal_person("BRANCH", PLC)
    assert not separate_legal_person("SOLE_PROPRIETOR", BV)
    assert not separate_legal_person("", BV)
    assert not separate_legal_person("GENERAL", GB_LP), "an English limited partnership is none"
    assert not separate_legal_person("GENERAL", DE_LP)
    assert not separate_legal_person("GENERAL", "8888")


def test_no_form_the_landing_leaves_out_is_listed() -> None:
    left_out = NO_LEGAL_FORM | PARTNERSHIPS.keys() | SOLE_PROPRIETORSHIPS.keys()
    assert not LEGAL_PERSONS.keys() & left_out
    assert {CORPORATION, PLC, BV} <= LEGAL_PERSONS.keys()
    assert not {GB_LP, DE_LP} & LEGAL_PERSONS.keys()
    assert all(len(code) == 4 and code.isalnum() for code in LEGAL_PERSONS)
    assert "SOLE_PROPRIETOR" not in CATEGORIES


def test_a_record_vouches_for_its_whole_name_whatever_the_punctuation(register: Register) -> None:
    assert register.vouches(HOSPITAL)
    assert register.vouches("Example Health System, of Chicago.")


@pytest.mark.parametrize(
    ("name", "why"),
    [
        (PERSON, "an exact match on a person-shaped name stays hashed"),
        ("JANE Q. EXAMPLE", "however it is punctuated"),
        ("EXAMPLE WIDGETS OF AMERICA", "the register's legal form is never stripped"),
        ("EXAMPLE HEALTH SYSTEM", "nor any other word of the legal name"),
        ("EXAMPLE PARTNERS OF CHICAGO", "a partnership is no separate legal person"),
        ("EXAMPLE VENTURES OF LONDON", "nor an English limited partnership"),
        ("EXAMPLE TRADING OF ROTTERDAM", "a sole proprietor is never vouched for"),
        ("EXAMPLE FORMLESS GROUP OF LEEDS", "a record without a real legal form vouches nothing"),
        (f"{HOSPITAL} DBA EXAMPLE CLINIC", "a person marker overrides the register"),
        ("", "a blank names nobody"),
    ],
)
def test_nothing_else_is_vouched_for(register: Register, name: str, why: str) -> None:
    assert not register.vouches(name), why


def test_the_landed_register_is_read_from_the_landing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_LAB_LANDING", str(tmp_path))
    with pytest.raises(RegisterError, match=REGISTER):
        Register.landed()
    columns = [*legal_entities.COLUMNS]
    text = io.StringIO()
    writer = csv.DictWriter(text, columns)
    writer.writeheader()
    writer.writerow(
        {
            "LEI": "0000EXAMPLE000000002",
            "Entity.LegalName": HOSPITAL.title(),
            "Entity.LegalAddress.Country": COUNTRIES[2],
            "Entity.EntityCategory": "GENERAL",
            "Entity.LegalForm.EntityLegalFormCode": CORPORATION,
        }
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("golden-copy.csv", text.getvalue())
    (tmp_path / REGISTER).write_bytes(legal_entities.step(archive.getvalue()))
    assert Register.landed().vouches(HOSPITAL)


SPEC = Table(
    ("note", "payee", "payee_id", "zip"),
    name="payee",
    identifier="payee_id",
    personal=("note", "zip"),
    descriptions=("note",),
)


def landed(tmp_path: Path, register: Register, *rows: tuple[str, str, str, str]) -> list[dict]:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(SPEC.columns)
    writer.writerows(rows)
    path = tmp_path / "landed"
    path.write_bytes(land_all({"t": (out.getvalue().encode(), SPEC)}, ENV, register)["t"])
    rel = duckdb.read_parquet(str(path))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_a_vouched_organisation_lands_in_clear_with_what_describes_it(
    tmp_path: Path, register: Register
) -> None:
    (row,) = landed(tmp_path, register, ("night shift", HOSPITAL, "V1", "60601"))
    assert row == {"note": "night shift", "payee": HOSPITAL, "payee_id": "V1", "zip": "60601"}


def test_without_the_register_the_same_organisation_is_hashed(tmp_path: Path) -> None:
    (row,) = landed(tmp_path, Register(()), ("night shift", HOSPITAL, "V1", "60601"))
    assert row["payee"] == person_hasher(ENV)(HOSPITAL)
    assert (row["note"], row["zip"]) == (None, None)


def test_a_person_sharing_a_registered_name_stays_hashed_with_the_same_hash(
    tmp_path: Path, register: Register
) -> None:
    with_register = landed(tmp_path, register, ("visit", PERSON, "V2", "60601"))
    without = landed(tmp_path, Register(()), ("visit", PERSON, "V2", "60601"))
    assert with_register == without
    assert with_register[0]["payee"] == person_hasher(ENV)(PERSON)


def test_a_hashed_name_still_blanks_a_vouched_organisations_description(
    tmp_path: Path, register: Register
) -> None:
    person, hospital = landed(
        tmp_path, register, ("", PERSON, "V2", ""), (f"care of {PERSON}", HOSPITAL, "V1", "60601")
    )
    assert str(person["payee"]).startswith("person:")
    assert (hospital["payee"], hospital["note"], hospital["zip"]) == (HOSPITAL, None, "60601")


def test_the_pseudonymiser_passes_a_known_name_but_a_person_marker_still_wins() -> None:
    hide = pseudonymiser(ENV, [HOSPITAL, f"{HOSPITAL} T/A EXAMPLE CLINIC"])
    assert hide(HOSPITAL.lower()) == HOSPITAL.lower()
    assert hide(f"{HOSPITAL} T/A EXAMPLE CLINIC") == person_hasher(ENV)(
        f"{HOSPITAL} T/A EXAMPLE CLINIC"
    )
