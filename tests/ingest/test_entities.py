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

from ingest.chicago.table import Table, land
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


@pytest.mark.parametrize(
    "name", ["JANE EXAMPLE", "EXAMPLE, JANE Q", "J. EXAMPLE", "MARY-ANN O'EXAMPLE", "EXAMPLE"]
)
def test_one_to_three_words_of_letters_are_person_shaped(name: str) -> None:
    assert person_shaped(name)


@pytest.mark.parametrize(
    "name", [HOSPITAL, "3M COMPANY", "DOE & ROE", "EXAMPLE (UK)", "A1 EXAMPLE", ""]
)
def test_more_words_a_figure_or_a_joiner_is_not_person_shaped(name: str) -> None:
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
    path.write_bytes(land(out.getvalue().encode(), SPEC, ENV, register))
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
