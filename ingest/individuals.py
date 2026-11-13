"""Replace every vendor or payee who is a person with a keyed hash, before anything is written.

A name counts as an organisation only on positive evidence, and doubt always means a person:

- A legal form that makes a separate legal person, as the final words (`Ltd`, `B.V.`, `Inc` and the
  like) or a leading Dutch form (`Stichting`, `Vereniging`, `Coöperatie`), is evidence only when
  the rest of the name is one plain party: words of letters and digits, with nothing that could
  join a second party to the company (a comma other than one directly before the form, a dash, a
  slash, a bracket, a dot, `&`, a word such as `and` or `as`, a title, a person's suffix or
  degree, a role such as executor or guardian, a trust).
- Before a final `B.V.` or `N.V.`, which also read as a surname's initials, the name needs two
  words that are neither single letters nor Dutch name particles.
- A name the caller already knows is an organisation counts as one.
- A trading-as or also-known-as part, an estate, executors or a deceased overrides all of these.

A company named after a person (`Jane Doe Ltd`) is a separate legal person and passes in clear.

A person becomes `person:` and the 64 hex digits of HMAC-SHA-256 over the normalised name, keyed
by `LEDGER_LAB_HASH_KEY`: fixed length, so it carries no part of the name and not its length.
Generate a key with `python -c "import secrets; print(secrets.token_hex(32))"`.
"""

import hmac
import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping

KEY_VARIABLE = "LEDGER_LAB_HASH_KEY"
MIN_KEY_LENGTH = 32

# Final words that make a separate legal person: England and Wales, the Netherlands, the US.
LEGAL_FORM_SUFFIXES = (
    ("ltd",),
    ("limited",),
    ("plc",),
    ("public", "limited", "company"),
    ("llp",),
    ("cic",),
    ("community", "interest", "company"),
    ("bv",),
    ("nv",),
    ("inc",),
    ("incorporated",),
    ("corp",),
    ("corporation",),
    ("llc",),
    ("limited", "liability", "company"),
    ("pllc",),
)
# Dutch forms that also read as a surname's two initials.
INITIALS_FORMS = frozenset({("bv",), ("nv",)})
# First words of Dutch legal forms: foundation, association, cooperative.
LEGAL_FORM_PREFIXES = frozenset({"stichting", "vereniging", "coöperatie", "cooperatie"})
# The liability suffix a Dutch cooperative carries after its name.
COOPERATIVE_SUFFIXES = frozenset({"ua", "wa", "ba"})

# Words, punctuation stripped, that make a name a person's wherever they stand, register included:
# trading as, doing business as, also and formerly known as, handelend onder de naam, estates,
# executors, as trustee for, deceased.
PERSON_MARKERS = frozenset(
    {"ta", "tas", "dba", "aka", "fka", "hodn", "estate", "exor", "exors", "executor", "executors"}
    | {"executrix", "atf", "deceased"}
)
# Words that leave doubt whether a legal form covers the whole name: joiners, trusts, titles,
# a person's suffixes and degrees, and the roles in which a person is paid on someone's behalf.
DOUBT_WORDS = frozenset(
    {"and", "en", "or", "andor", "as", "for", "via", "per", "attn", "care", "with", "obo", "x"}
    | {"plus", "und", "vs", "v", "versus", "doing", "handelend", "onder", "namens", "tav", "tnv"}
    | {"pa", "trust", "trustee", "trustees"}
    | {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "dame", "lady", "lord", "rev"}
    | {"dhr", "mevr", "mw", "drs", "ir", "ing"}
    | {"esq", "jr", "junior", "sr", "senior", "ii", "iii", "phd", "md", "dds", "cpa"}
    | {"exec", "exr", "admin", "administrator", "administratrix", "guardian", "minor", "heir"}
    | {"heirs", "beneficiary", "receiver", "liquidator", "attorney", "poa", "custodian", "cust"}
    | {"utma", "ugma", "payee", "plaintiff", "claimant", "personally", "herself", "himself"}
    | {"self", "sole", "trader", "agent", "rep", "representative", "ttee", "uta", "fbo", "re"}
    | {"ref", "inz", "eo", "cs", "mother", "father", "parent", "son", "daughter", "wife"}
    | {"husband", "spouse", "widow", "widower", "tr", "name"}
)
# Dutch name particles, which do not count as a word of a company name before `B.V.` or `N.V.`.
PARTICLES = frozenset({"de", "der", "den", "van", "von", "het", "te", "ter", "ten", "'t", "op"})

PLAIN_WORD = re.compile(r"^[^\W_]+(?:'[^\W_]+)*$")
FORM_WORD = re.compile(r"^[^\W\d_]+(?:\.[^\W\d_]+)*$")
NOT_A_LETTER = re.compile(r"[\W_]")
SPELLINGS = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "″": '"',
        "​": " ",
    }
)


class NoHashKeyError(RuntimeError):
    """The hash key is missing or too short, so no name may be read."""


def hash_key(env: Mapping[str, str] | None = None) -> bytes:
    """The HMAC key from the environment; raises without naming or echoing its value."""
    value = (os.environ if env is None else env).get(KEY_VARIABLE, "")
    if not value.strip():
        raise NoHashKeyError(f"{KEY_VARIABLE} is unset: refusing to read any name without it")
    if len(value) < MIN_KEY_LENGTH:
        raise NoHashKeyError(f"{KEY_VARIABLE} must be at least {MIN_KEY_LENGTH} characters")
    return value.encode()


def normalise(name: str) -> str:
    """The form a name is classified and hashed in.

    NFKC; curly quotes made straight; a zero-width space made a space and every other invisible
    format character dropped; case folded; whitespace runs collapsed to one space; edge
    punctuation dropped.
    """
    text = unicodedata.normalize("NFKC", name).translate(SPELLINGS)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    folded = " ".join(text.casefold().split())
    return " ".join(folded.strip(" .,;:").split())


def _bare(word: str) -> str:
    return NOT_A_LETTER.sub("", word)


def _one_party(body: list[str], initials_form: bool) -> bool:
    """True only when the words beside a legal form can be nothing but one company's name."""
    if not body:
        return False
    if body[-1].endswith(","):
        body = [*body[:-1], body[-1][:-1]]
    if not all(PLAIN_WORD.match(word) for word in body):
        return False
    if any(word in DOUBT_WORDS for word in body):
        return False
    if initials_form:
        return len([w for w in body if len(w) > 1 and w not in PARTICLES]) >= 2
    return True


def _has_legal_form(words: list[str]) -> bool:
    for suffix in LEGAL_FORM_SUFFIXES:
        tail = words[-len(suffix) :]
        if (
            len(tail) == len(suffix)
            and all(FORM_WORD.match(w) for w in tail)
            and tuple(map(_bare, tail)) == suffix
        ):
            return _one_party(words[: -len(suffix)], suffix in INITIALS_FORMS)
    if words and words[0] in LEGAL_FORM_PREFIXES:
        body = words[1:]
        if body and FORM_WORD.match(body[-1]) and _bare(body[-1]) in COOPERATIVE_SUFFIXES:
            body = body[:-1]
        return _one_party(body, initials_form=False)
    return False


def _marker_spellings(words: list[str]) -> set[str]:
    """Each word bare and unaccented, plus each run of single letters joined (`t a` as `ta`)."""
    bare = [
        "".join(c for c in unicodedata.normalize("NFKD", _bare(w)) if not unicodedata.combining(c))
        for w in words
    ]
    spellings, run = set(bare), ""
    for word in [*bare, ""]:
        if len(word) == 1:
            run += word
        else:
            spellings.add(run)
            run = ""
    return spellings


def _classify(normalised: str, register: frozenset[str]) -> bool:
    if _marks_a_person(normalised):
        return False
    return normalised in register or _has_legal_form(normalised.split())


def _marks_a_person(normalised: str) -> bool:
    return bool(_marker_spellings(normalised.split()) & PERSON_MARKERS)


def marks_a_person(text: str) -> bool:
    """True when the text carries one of the markers that make a name a person's wherever they
    stand (an estate, executors, a deceased, trading as, also known as): a free-text cell that
    does names a party who is or was a person."""
    return _marks_a_person(normalise(text))


def is_organisation(name: str, organisations: Iterable[str] = ()) -> bool:
    """True only on positive evidence of an organisation; any doubt answers False (a person).

    It classifies only and needs no key; `pseudonymiser` is what refuses to run without one.
    """
    return _classify(normalise(name), frozenset(map(normalise, organisations)))


def pseudonymiser(env: Mapping[str, str] | None = None) -> Callable[[str | None], str | None]:
    """A function that passes an organisation's name through and hashes a person's.

    Reads the key first, so it raises before any name is seen.
    A missing payee (None, or nothing left after normalisation) stays None: it names nobody,
    and one shared hash for every blank would read as one person.
    """
    as_person = person_hasher(env)

    def hide(name: str | None) -> str | None:
        if name is None:
            return None
        normalised = normalise(name)
        if not normalised:
            return None
        if _classify(normalised, frozenset()):
            return name
        return as_person(name)

    return hide


def person_hasher(env: Mapping[str, str] | None = None) -> Callable[[str], str]:
    """A function that hashes any name as a person's, whatever it reads as: `person:` and the HMAC
    of the normalised name. Reads the key first."""
    key = hash_key(env)
    return lambda name: "person:" + _digest(key, normalise(name).encode())


def identifier_hasher(env: Mapping[str, str] | None = None) -> Callable[[str], str]:
    """A function that hashes a publisher's identifier of a person (a vendor id): `id:` and the HMAC
    of the identifier with a label of its own before it, so no identifier's hash equals a name's.
    It stays a join key between a publisher's tables but can no longer be looked up. Reads the key
    first."""
    key = hash_key(env)
    return lambda ident: "id:" + _digest(key, b"id\0" + ident.strip().encode())


def _digest(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, "sha256").hexdigest()
