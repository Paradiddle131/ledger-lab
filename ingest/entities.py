"""Which payee names the legal-entity register vouches for, so they land in clear.

A payee is hashed unless there is evidence it is an organisation (`ingest/individuals.py`). A
record of the register is such evidence for one name only, and only when all of these hold:

- its legal name and the payee's name have the same matching key (`match_key`): the whole name,
  word for word, lower-cased in its NFC form, with punctuation between the words ignored and `&`
  kept as a word. Nothing else is stripped: a legal-form suffix the register
  carries and the payee lacks (`Example Ltd` for `Example`) does not match, since that is how a
  person sharing a company's name would come out in clear;
- its legal-form code makes a separate legal person (`LEGAL_PERSONS`; a code not listed never
  vouches, and none the landing leaves out is listed);
- its category is one the landing keeps for an organisation (`legal_entities.CATEGORIES`);
- the payee's name carries no person marker (trading as, an estate, executors, a deceased), which
  overrides the register as it overrides a legal form;
- the payee's name is not person-shaped (`person_shaped`), judged on the same key: up to three
  words of letters once titles, suffixes, degrees, particles, initials, joiners and figures are
  set aside, the shape of a person's name, stays hashed even when a register record has
  exactly that name, since a sole trader may share a name with a registered company; a name
  ending in a legal form no sole trader may use (`Ltd`, `Inc`, `LLC`, `B.V.`, never `Co`) is
  exempt, as the classifier lets a company named after a person pass.

So the register turns hashed organisations back into named ones, never a name a person could
carry. The matching key is also the `vendor_match_key` macro of the dbt models, which match
names already in clear to the register more loosely; that looser match never decides what is
hashed. The register is the landed `gleif-legal-entities` file under the landing path; a step that
hashes payees refuses to run until it is landed.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

import duckdb

from ingest import StepError
from ingest.gleif.legal_entities import (
    CATEGORIES,
    CATEGORY,
    LEGAL_FORM,
    LEGAL_NAME,
)
from ingest.individuals import marks_a_person, normalise

REGISTER = "gleif-legal-entities"
TOKEN = re.compile(r"[^\W_]+|&")
# The person-shape guard reads the matching key's words, so no punctuation, spacing or Unicode form
# changes the shape. Not counted as words of a name: titles, a person's suffixes and degrees (`m d`
# and `ph d` lose their single letters too), name particles, initials, joiners and figures.
TITLES = frozenset(
    {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "dame", "lady", "lord", "rev", "hon"}
    | {"fr", "dhr", "mevr", "mw", "drs", "ir", "ing"}
)
SUFFIXES = frozenset(
    {"jr", "junior", "sr", "senior", "ii", "iii", "iv", "esq", "phd", "ph", "md", "dds", "dmd"}
    | {"dvm", "cpa", "rn", "lpn", "np", "mba", "jd", "llm", "pe", "ra"}
)
PARTICLES = frozenset(
    {"de", "del", "della", "der", "den", "di", "da", "do", "dos", "du", "la", "las", "los", "le"}
    | {"van", "von", "het", "te", "ter", "ten", "op", "bin", "ibn", "al", "el"}
)
NAME_CHUNK = re.compile(r"[^\W_]+(?:['`-][^\W_]+)*|&")
JOINERS = frozenset({"&", "and", "en", "y"})
# A person's name, as the guard counts it, has at most this many words (`jane q example jr`).
PERSON_WORDS = 3
# Final words of a legal form no sole trader may trade under, in the key's spelling (`b v` for
# `B.V.`), and the leading Dutch forms: a name with one is exempt from the shape guard, as the
# classifier lets `Jane Doe Ltd` pass. `Co` and `Company` are not among them, since a sole trader
# may trade as `Example & Co`.
LEGAL_FORM_ENDINGS = (
    ("ltd",),
    ("limited",),
    ("plc",),
    ("public", "limited", "company"),
    ("llp",),
    ("l", "l", "p"),
    ("cic",),
    ("community", "interest", "company"),
    ("bv",),
    ("b", "v"),
    ("nv",),
    ("n", "v"),
    ("inc",),
    ("incorporated",),
    ("corp",),
    ("corporation",),
    ("llc",),
    ("l", "l", "c"),
    ("limited", "liability", "company"),
    ("pllc",),
    ("pc",),
    ("p", "c"),
    ("nfp",),
)
LEGAL_FORM_BEGINNINGS = frozenset({"stichting", "vereniging", "coöperatie", "cooperatie"})

# Legal-form codes (ISO 20275, as the register files them) that make a separate legal person, with
# the form's name in GLEIF's list and its jurisdiction. Every code here is a body corporate under
# its law: a company, corporation, limited liability company, bank, cooperative, foundation,
# association with legal personality, public-law body, or a British limited liability partnership.
# Every British and Dutch form of a legal person is listed; of the American forms (several hundred,
# one list a state), those that the pinned register files a vouching record under, so a re-pin
# that needs another adds it here. Left out on purpose: partnerships of every other kind (an
# English limited partnership has no legal personality; the rest are usually named after their
# partners), trusts, unincorporated associations, a Dutch legal person still being formed or a
# fund for joint account, a British government entity (a department is no legal person of its
# own), forms GLEIF calls only "other", and registrations of a name rather than a body.
LEGAL_PERSONS: dict[str, str] = {
    "8CF0": "Assurance Company (GB)",
    "E12O": "Building Society (GB)",
    "7T8N": "Charitable Incorporated Organisation (GB)",
    "ZZGG": "Co-operative Society (GB)",
    "7VVZ": "Co-operative Society (Northern Ireland) (GB-NIR)",
    "IYXU": "Community Benefit Society (GB)",
    "UTY8": "Community Benefit Society (Northern Ireland) (GB-NIR)",
    "17R0": "Community Interest Company (GB)",
    "WBQU": "Credit Union (GB)",
    "JTCO": "Credit Union (Northern Ireland) (GB-NIR)",
    "55MA": "Cwmni Cyfyngedig Cyhoeddus (GB)",
    "U6R9": "European Economic Interest Grouping (GB)",
    "HHYT": "European Public Limited-Liability Company (Societas Europaea) (GB)",
    "FVGZ": "Industrial and Provident Society (GB)",
    "VV0W": "Investment Company with Variable Capital (GB)",
    "Z0EY": "Limited Liability Partnership (GB)",
    "4A3J": "Northern Ireland Company (GB-NIR)",
    "ZQ6S": "Old Public Company (GB)",
    "1W62": "Oversea Company (GB)",
    "WJ0A": "Partneriaeth Atebolrwydd Cyfyngedig (GB)",
    "HX6D": "Private Limited (GB)",
    "H0PO": "Private Limited Company (GB)",
    "4GJI": "Private Limited by Guarantee (GB)",
    "G12F": "Private Limited by Guarantee/No Share Capital (GB)",
    "TYJK": "Private Limited by Guarantee/No Share Capital (Use of Limited Exemption) (GB)",
    "C605": "Private Limited by Shares (Section 30 Exemption) (GB)",
    "57V7": "Private Unlimited (GB)",
    "60IF": "Private Unlimited/No Share Capital (GB)",
    "TT2H": "Protected Cell Company (GB)",
    "B6ES": "Public Limited Company (GB)",
    "XLZV": "Registered Society (GB)",
    "9B78": "Royal Charter Company (GB)",
    "STX7": "Scottish Charitable Incorporated Organisation (GB-SCT)",
    "CDOT": "UK Establishment (GB)",
    "VYF8": "United Kingdom Economic Interest Grouping (GB)",
    "468Q": "United Kingdom Societas (GB)",
    "Q0M5": "Unregistered Company (GB)",
    "A2X8": "Unregistered Company (GB)",
    "T61R": "cyfyngedig (GB)",
    "5WU6": "Europees Economisch Samenwerkingsverband (NL)",
    "BBEB": "Europese naamloze vennootschap (Societas Europaea) (NL)",
    "M1IZ": "Overige Privaatrechtelijke Rechtspersoon (NL)",
    "Y3ZB": "Societas Cooperativa Europaea (NL)",
    "54M6": "besloten vennootschap met beperkte aansprakelijkheid (NL)",
    "NFFH": "coöperatie (NL)",
    "L7HX": "kerkgenootschap (NL)",
    "B5PM": "naamloze vennootschap (NL)",
    "DEO1": "onderlinge waarborgmaatschappij (NL)",
    "A0W7": "publiekrechtelijke rechtspersoon (NL)",
    "V44D": "stichting (NL)",
    "8VFX": "vereniging met beperkte rechtsbevoegdheid (NL)",
    "33MN": "vereniging met volledige rechtsbevoegdheid (NL)",
    "GNXT": "vereniging van eigenaars (NL)",
    "380O": "Federal Savings Association (US)",
    "62VJ": "National Bank (US)",
    "14OD": "credit union (US)",
    "H1UM": "For-Profit Corporation General Stock (US-CA)",
    "EI4J": "Limited Liability Company (US-CA)",
    "K7YU": "Nonprofit Corporation Public Benefit (US-CA)",
    "ZJPG": "Nonprofit Corporation (US-DC)",
    "XTIQ": "Corporation (US-DE)",
    "HZEH": "Limited Liability Company (US-DE)",
    "8N21": "Limited Liability Company (US-FL)",
    "S7VR": "Nonprofit Corporation (US-GA)",
    "MFYJ": "Profit Corporation (US-GA)",
    "DU35": "Profit Corporation (US-IA)",
    "AZUK": "Business Corporation (US-IL)",
    "QY7A": "Commercial Bank (US-IL)",
    "8RLE": "Limited Liability Company (US-IL)",
    "HSPI": "Nonprofit Corporation (US-IL)",
    "COKS": "Savings Bank (US-IL)",
    "QWPR": "Corporation (profit) (US-KY)",
    "F2QV": "Limited Liability Company (profit) (US-KY)",
    "TZDZ": "Commercial Bank (US-LA)",
    "R7QO": "Non-Profit Corporation (US-MA)",
    "ZVS9": "Nonstock Corporation (US-MD)",
    "HLR4": "Stock Corporation (US-MD)",
    "U7HC": "Corporation (For-Profit) (US-MI)",
    "47LQ": "Corporation (Nonprofit) (US-MI)",
    "S2K8": "Business Corporation (US-MN)",
    "9I4Y": "Non-Profit Corporation (US-MN)",
    "1QMT": "Business Corporation (US-NC)",
    "358I": "Nonprofit Corporation (US-NC)",
    "XSNP": "Business Corporation (US-NJ)",
    "8WM4": "Profit Corporation (US-NV)",
    "PJ10": "Business Corporation (US-NY)",
    "51RC": "Not-for-profit Corporation (US-NY)",
    "N566": "Commercial Bank (US-OH)",
    "EZNQ": "Limited Liability Company (US-OH)",
    "3JTE": "Business Corporation (US-PA)",
    "9EJ6": "Nonprofit Corporation (US-PA)",
    "Z54A": "Business Corporation (US-RI)",
    "ND93": "Commercial Bank (US-TN)",
    "C5K7": "For-Profit Corporation (US-TX)",
    "OGSS": "Nonprofit Corporation (US-TX)",
    "2I4P": "Profit Corporation (US-UT)",
    "PLMC": "Nonstock Corporation (US-VA)",
    "HOV4": "Stock Corporation (US-VA)",
    "RR8G": "Non-profit Corporation (US-WA)",
    "6S32": "Business Corporation (US-WI)",
    "EVBW": "Corporation Nonstock Nonprofit (US-WI)",
}


class RegisterError(StepError):
    """The legal-entity register is not landed, so no name can be vouched for."""


def match_key(name: str) -> str:
    """The name's runs of letters and digits, and `&`, in its NFC form lower-cased, joined by single
    spaces: punctuation, spacing and invisible characters between words are ignored. It is the
    `vendor_match_key` macro's key exactly (a landed test compares the two over every landed
    name), which is why it is NFC and lower case, the forms both engines have; a name written in
    compatibility characters (full-width letters) then matches no record and stays hashed."""
    # `İ` is the one letter whose full lower case (`i` and a combining dot) differs from the simple
    # one (`i`) the SQL engines use.
    lowered = unicodedata.normalize("NFC", name).replace("İ", "i").lower()
    return " ".join(TOKEN.findall(lowered))


def person_shaped(name: str) -> bool:
    """True when the name could be a person's (or two people's): its matching key holds a letter,
    no word mixing letters and figures, no legal form a sole trader may not use, and at most
    `PERSON_WORDS` words once titles, suffixes, degrees, particles, initials, joiners and figures
    are set aside."""
    words = match_key(name).split()
    if not any(c.isalpha() for w in words for c in w):
        return False
    if any(tuple(words[-len(f) :]) == f for f in LEGAL_FORM_ENDINGS):
        return False
    if words[0] in LEGAL_FORM_BEGINNINGS:
        return False
    if any(not w.isalpha() and not w.isdigit() and w != "&" for w in words):
        return False
    # A hyphenated or apostrophised name is one word (`mary-ann`, `o'example`), though the key
    # splits it.
    chunks = NAME_CHUNK.findall(normalise(name))
    counted = [
        w
        for w in chunks
        if len(w) > 1 and not w.isdigit() and w not in TITLES | SUFFIXES | PARTICLES | JOINERS
    ]
    return len(counted) <= PERSON_WORDS


def separate_legal_person(category: str, form: str) -> bool:
    """True when a register record's legal form makes a separate legal person and its category is
    one the landing keeps as an organisation."""
    return form in LEGAL_PERSONS and category in CATEGORIES


@dataclass(frozen=True)
class Record:
    """What of a register record the vouching reads."""

    legal_name: str
    category: str
    form: str


class Register:
    """The matching keys of the register's records whose legal form makes a separate legal
    person, and the names they vouch for."""

    def __init__(self, records: Iterable[Record]) -> None:
        self.keys = frozenset(
            match_key(r.legal_name) for r in records if separate_legal_person(r.category, r.form)
        )

    @classmethod
    def landed(cls) -> "Register":
        """The register landed under the landing path."""
        from ingest.fetch import landing_path  # the kit imports the steps that call this

        path = landing_path() / REGISTER
        if not path.is_file():
            raise RegisterError(f"{REGISTER} is not landed: land it before a step that hashes")
        rows = (
            duckdb.connect()
            .execute(
                f'SELECT "{LEGAL_NAME}", "{CATEGORY}", "{LEGAL_FORM}"'  # noqa: S608
                " FROM read_parquet(?)",
                [str(path)],
            )
            .fetchall()
        )
        return cls(Record(*(str(cell or "") for cell in row)) for row in rows)

    def vouches(self, name: str) -> bool:
        """True when a record vouches that this whole name is an organisation's."""
        normalised = normalise(name)
        return (
            bool(normalised)
            and not marks_a_person(normalised)
            and not person_shaped(name)
            and match_key(name) in self.keys
        )

    def vouched(self, names: Iterable[str]) -> frozenset[str]:
        """The names among these that a record vouches for."""
        return frozenset(n for n in names if n and self.vouches(n))
