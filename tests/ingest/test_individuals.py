"""A vendor or payee who is a person leaves ingest only as a keyed hash.

Every name here is invented. The key is built in the test, never read from a file.
"""

import hashlib
import hmac
import re

import pytest

from ingest.individuals import (
    KEY_VARIABLE,
    NoHashKeyError,
    is_organisation,
    normalise,
    pseudonymiser,
)

KEY = "k" * 32
ENV = {KEY_VARIABLE: KEY}
HASHED = re.compile(r"^person:[0-9a-f]{64}$")


@pytest.mark.parametrize(
    "name",
    [
        # English (UK) legal forms.
        "Acme Widgets Ltd",
        "Acme Widgets Ltd.",
        "ACME WIDGETS LTD",
        "Acme Widgets Limited",
        "Acme Widgets PLC",
        "Acme Widgets p.l.c.",
        "Acme Widgets Public Limited Company",
        "Acme Advisers LLP",
        "Acme Community Interest Company",
        "Acme Community CIC",
        # Dutch legal forms.
        "Voorbeeld Holding B.V.",
        "Voorbeeld Holding BV",
        "Voorbeeld Holding N.V.",
        "Van Dijk Holding BV",
        "Stichting Voorbeeldfonds",
        "Vereniging Voorbeeld",
        "Coöperatie Voorbeeld U.A.",
        # US legal forms.
        "Acme Inc",
        # A comma directly before the form, with nothing else joined.
        "Acme, Inc.",
        "Acme Widgets, Ltd.",
        "Acme Incorporated",
        "Acme Corp.",
        "Acme Corporation",
        "Acme LLC",
        "Acme Limited Liability Company",
        "Acme, L.L.C.",
        "Acme Dental PLLC",
        # A company named after a person is still a separate legal person.
        "Jane Doe Ltd",
        "J Doe Ltd",
        "O’Doe Ltd",
    ],
)
def test_a_legal_form_marks_an_organisation(name: str) -> None:
    assert is_organisation(name)


@pytest.mark.parametrize(
    "name",
    [
        # Plain names, titles, all caps and trailing punctuation.
        "Jane Doe",
        "DR JANE DOE",
        "Mr Richard Roe.",
        "Doe, Jane",
        # Initials, before or after the surname.
        "J. Doe",
        "J Doe",
        "Doe, J.",
        "DOE J",
        "J.B. Voorbeeld",
        "Voorbeeld, J.B.",
        # Initials that spell a Dutch legal form after a comma are a surname and initials.
        "Voorbeeld, N.V.",
        "Voorbeeld, B.V.",
        # A surname and initials that spell a Dutch form: one word before `bv` or `nv`, not
        # counting name particles or single letters, is not enough evidence of a company.
        "ROE BV",
        "DOE N.V.",
        "Doe NV",
        "De Vries BV",
        "van der Doe NV",
        "Doe J BV",
    ],
)
def test_a_name_of_a_person_is_a_person(name: str) -> None:
    assert not is_organisation(name)


@pytest.mark.parametrize(
    "name",
    [
        # A trading-as part names the person behind the trade.
        "Jane Doe t/a Doe Plumbing",
        "Jane Doe T/A Doe Plumbing Ltd",
        "Jane Doe trading as Doe Plumbing Ltd",
        "Richard Roe d/b/a Roe Corp",
        "Richard Roe DBA Roe LLC",
        # Estates, executors and trusts are about people.
        "Estate of Jane Doe",
        "The Estate of Richard Roe Ltd",
        "Executors of Jane Doe",
        "Trustees of the Doe Family Trust",
        "Doe Family Trust",
    ],
)
def test_a_person_marker_wins_over_a_legal_form(name: str) -> None:
    assert not is_organisation(name)


@pytest.mark.parametrize(
    "name",
    [
        # No legal form, so unsure, so a person: these may be sole traders.
        "Doe Plumbing Services",
        "Doe & Co",
        "Doe and Sons",
        "Acme Holdings",
        "Acme Company",
        "Gemeente Voorbeeldstad",
        # A legal-form word that is not the final word.
        "Doe Limited Edition Prints",
        "Acme Ltd (in liquidation)",
        # A legal form with nothing before it.
        "Ltd",
        "",
    ],
)
def test_an_unsure_name_counts_as_a_person(name: str) -> None:
    assert not is_organisation(name)


@pytest.mark.parametrize(
    "name",
    [
        # A person joined to a company: settlement payees, care-of and listed parties.
        "JANE DOE & ROE LAW GROUP LLC",
        "JANE DOE AND HER ATTORNEY ROE LAW OFFICES LLC",
        "Jane Doe c/o Roe Solicitors LLP",
        "Mrs J Doe c/o Acme Ltd",
        "Jane Doe, Acme Ltd",
        "Jane Doe - Acme Ltd",
        "Jane Doe – Acme Ltd",
        "Jane Doe / Doe Plumbing Ltd",
        "Jane Doe for Acme Ltd",
        "Jane Doe en Roe Holding BV",
        # Trading-as and also-known-as in any spelling.
        "Jane Doe T/AS Doe Plumbing Ltd",
        "Jane Doe (t/a Doe Plumbing Ltd)",
        "Jane Doe T/A: Doe Plumbing Ltd",
        "Jane Doe T.A. Doe Plumbing Ltd",
        "Jane Doe TA Doe Plumbing Ltd",
        "Jane Doe d.b.a. Roe LLC",
        "Jane Doe a/k/a Roe LLC",
        "Jane Doe AKA Roe LLC",
        "Jane Doe known as Roe LLC",
        "J. de Vries h.o.d.n. Vries Holding B.V.",
        "Jane Doe hodn Vries Holding B.V.",
        "Jane Doe t/a​ Doe Ltd",
        "Jane Doe t​/a Doe Ltd",
        # Estates, executors, trustees and trusts before the form.
        "Exors of Jane Doe Ltd",
        "Jane Doe Estate Ltd",
        "Jane Doe Trustee Acme Ltd",
        "Jane Doe ATF Doe Family Trust Ltd",
        "Jane Doe Deceased Ltd",
        # Joiners spelled as words, spaced letters and accents.
        "Jane Doe Plus Roe LLC",
        "Jane Doe Vs Roe LLC",
        "Jane Doe x Roe LLC",
        "Jane Doe Handelend Onder Doe Holding BV",
        "Jane Doe Tnv Acme Holding BV",
        "Jane Doe T A Doe Plumbing Ltd",
        "Jane Doe D B A Roe LLC",
        "Jane Doe Tá Doe Plumbing Ltd",
        # A title, a person's suffix or degree, or a role on someone's behalf before the form.
        "Mrs Jane Doe Ltd",
        "Mrs. Jane Doe Ltd",
        "Dhr J Voorbeeld Holding BV",
        "Jane Doe Esq LLC",
        "Jane Doe Jr Ltd",
        "Jane Doe MD PLLC",
        "Jane Doe Exec Ltd",
        "Jane Doe Guardian Ltd",
        "Jane Doe Cust Doe LLC",
        "Jane Doe Sole Trader Ltd",
        "Jane Doe Mother Of Roe LLC",
        "Jane Doe Ttee Acme Ltd",
        "Jane Doe Tr Acme Ltd",
        "Jane Doe Ten Name Van Acme Holding BV",
        "Jane Doe Fbo Roe LLC",
        "Jane Doe Inz Acme Holding BV",
        # The same joins after a leading Dutch form.
        "Stichting Doe & Roe",
        "Stichting Jane Doe c/o Acme",
    ],
)
def test_a_second_party_before_the_legal_form_counts_as_a_person(name: str) -> None:
    assert not is_organisation(name)
    assert HASHED.match(pseudonymiser(ENV)(name) or ""), name


@pytest.mark.parametrize(
    "name",
    [
        # Real companies this rule hashes, on purpose: a join is no evidence of one party.
        "Doe & Roe Ltd",
        "Doe and Roe LLP",
        "Acme (UK) Ltd",
        "Rolls-Example Ltd",
        "St. Example Holdings Ltd",
        "Dr Example Drinks Ltd",
    ],
)
def test_a_joined_company_is_hashed_unless_the_register_knows_it(name: str) -> None:
    assert not is_organisation(name)
    assert is_organisation(name, {name})


def test_a_register_match_marks_an_organisation() -> None:
    register = {"Gemeente Voorbeeldstad"}
    assert is_organisation("GEMEENTE  VOORBEELDSTAD", register)
    assert not is_organisation("Gemeente Voorbeeldstad Noord", register)


@pytest.mark.parametrize(
    "name", ["Estate of Jane Doe", "Jane Doe t/a Acme Ltd", "Jane Doe d.b.a. Acme Ltd"]
)
def test_a_person_marker_wins_over_a_register_match(name: str) -> None:
    assert not is_organisation(name, {name})


def test_a_person_is_replaced_by_a_hash_that_does_not_leak_the_name() -> None:
    hide = pseudonymiser(ENV)
    short = hide("Jane Doe") or ""
    long = hide("Bartholomew Alexander Voorbeeld-Example") or ""
    for hashed in (short, long):
        assert HASHED.match(hashed)
    assert len(short) == len(long)
    assert "jane" not in short.casefold()


def test_the_hash_is_hmac_sha256_of_the_normalised_name() -> None:
    expected = hmac.new(KEY.encode(), b"jane doe", hashlib.sha256).hexdigest()
    assert pseudonymiser(ENV)("Jane Doe") == f"person:{expected}"


def test_only_organisations_leave_in_clear() -> None:
    payees = {
        "Acme Widgets Ltd": "organisation",
        "Voorbeeld Holding B.V.": "organisation",
        "Acme, Inc.": "organisation",
        "Gemeente Voorbeeldstad": "person",
        "DR JANE DOE": "person",
        "Doe, J.": "person",
        "Jane Doe t/a Doe Plumbing Ltd": "person",
        "Estate of Richard Roe": "person",
        "Doe Plumbing Services": "person",
        "JANE DOE & ROE LAW GROUP LLC": "person",
        "Jane Doe c/o Roe Solicitors LLP": "person",
        "DOE N.V.": "person",
    }
    hide = pseudonymiser(ENV)
    hashes = set()
    for name, kind in payees.items():
        out = hide(name)
        if kind == "organisation":
            assert out == name
        else:
            assert HASHED.match(out or ""), name
            hashes.add(out)
    assert len(hashes) == list(payees.values()).count("person"), "two people share a hash"
    with pytest.raises(NoHashKeyError):
        pseudonymiser({})


def test_an_organisation_passes_unchanged() -> None:
    assert pseudonymiser(ENV)("Acme Widgets Ltd") == "Acme Widgets Ltd"


def test_a_register_match_counts_as_an_organisation() -> None:
    assert is_organisation("Gemeente Voorbeeldstad", {"Gemeente Voorbeeldstad"})
    assert not is_organisation("Gemeente Elders", {"Gemeente Voorbeeldstad"})


def test_the_same_name_and_key_give_the_same_hash() -> None:
    assert pseudonymiser(ENV)("Jane Doe") == pseudonymiser(dict(ENV))("Jane Doe")


@pytest.mark.parametrize(
    "spelling",
    [
        "JANE DOE",
        "jane doe",
        "  Jane   Doe ",
        "Jane\tDoe",
        "Jane\u00a0Doe",
        "Jane Doe.",
        "Jane\u200bDoe",
        "Ja\u00adne Doe",
        "\ufeffJane Doe",
    ],
)
def test_trivial_spellings_give_the_same_hash(spelling: str) -> None:
    hide = pseudonymiser(ENV)
    assert hide(spelling) == hide("Jane Doe")


def test_curly_and_straight_quotes_give_the_same_hash() -> None:
    hide = pseudonymiser(ENV)
    assert hide("Jane O’Doe") == hide("Jane O'Doe") == hide("Jane O‘Doe")
    assert hide("Jane “JJ” Doe") == hide('Jane "JJ" Doe')


@pytest.mark.parametrize("missing", [None, "", "   ", "​", " . "])
def test_a_missing_payee_stays_missing(missing: str | None) -> None:
    assert pseudonymiser(ENV)(missing) is None


def test_another_key_or_person_gives_another_hash() -> None:
    hide = pseudonymiser(ENV)
    assert hide("Jane Doe") != pseudonymiser({KEY_VARIABLE: "q" * 32})("Jane Doe")
    assert hide("Jane Doe") != hide("Richard Roe")


def test_normalisation_folds_case_and_whitespace_and_trailing_punctuation() -> None:
    assert normalise("  DR  Jane\t\tDoe.,  ") == "dr jane doe"


@pytest.mark.parametrize("env", [{}, {KEY_VARIABLE: ""}, {KEY_VARIABLE: "   "}])
def test_without_the_key_it_refuses_before_reading_a_name(env: dict[str, str]) -> None:
    with pytest.raises(NoHashKeyError, match=KEY_VARIABLE):
        pseudonymiser(env)


def test_a_short_key_is_refused() -> None:
    with pytest.raises(NoHashKeyError, match="at least 32"):
        pseudonymiser({KEY_VARIABLE: "k" * 31})


def test_the_key_never_appears_in_the_refusal() -> None:
    with pytest.raises(NoHashKeyError) as refused:
        pseudonymiser({KEY_VARIABLE: "short-key-value"})
    assert "short-key-value" not in str(refused.value)


def test_the_key_comes_from_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    with pytest.raises(NoHashKeyError):
        pseudonymiser()
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    assert pseudonymiser()("Jane Doe") == pseudonymiser(ENV)("Jane Doe")
