"""Replace every vendor or payee who is a person with a keyed hash, before anything is written.

A name counts as an organisation only on positive evidence: a legal form that makes a separate
legal person (a final `Ltd`, `B.V.`, `Inc` and the like, or a leading `Stichting`), or an exact
match in names the caller already knows are organisations. A person marker (a trading-as part, an
estate, executors, trustees, a trust, a surname followed by initials) overrides both. Anything else,
sole traders and partnerships included, counts as a person.

A person becomes `person:` and the 64 hex digits of HMAC-SHA-256 over the normalised name, keyed
by `LEDGER_LAB_HASH_KEY`: fixed length, so it carries no part of the name and not its length.
Generate a key with `python -c "import secrets; print(secrets.token_hex(32))"`.
"""

import hashlib
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
    ("pllc",),
)
# First words of Dutch legal forms: foundation, association, cooperative.
LEGAL_FORM_PREFIXES = frozenset({"stichting", "vereniging", "coöperatie", "cooperatie"})

TRADING_AS = re.compile(r"(?:^| )(?:t/a|d/b/a|dba|trading as|doing business as)(?: |$)")
ABOUT_A_PERSON = re.compile(r"(?:^| )(?:estate|executors?|trustees?) of ")
# One or two letters, dotted or not: "j", "j.", "jb", "j.b.", and so "n.v." after a comma too.
INITIAL = re.compile(r"^[a-z]\.?(?:[a-z]\.?)?$")
PUNCTUATION = re.compile(r"[.,;:()]")


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
    """NFKC, case folded, whitespace runs collapsed to one space, edge punctuation dropped."""
    folded = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    return " ".join(folded.strip(" .,;:").split())


def _is_person_marked(normalised: str, words: list[str]) -> bool:
    if TRADING_AS.search(normalised) or ABOUT_A_PERSON.search(normalised):
        return True
    if words and words[-1] == "trust":
        return True
    if "," in normalised:
        after = normalised.rsplit(",", 1)[1].split()
        if after and all(INITIAL.match(word) for word in after):
            return True
    return False


def _has_legal_form(words: list[str]) -> bool:
    for suffix in LEGAL_FORM_SUFFIXES:
        if len(words) > len(suffix) and tuple(words[-len(suffix) :]) == suffix:
            return True
    return len(words) > 1 and words[0] in LEGAL_FORM_PREFIXES


def _classify(normalised: str, register: frozenset[str]) -> bool:
    words = [w for w in (PUNCTUATION.sub("", w) for w in normalised.split()) if w]
    if _is_person_marked(normalised, words):
        return False
    return normalised in register or _has_legal_form(words)


def is_organisation(name: str, organisations: Iterable[str] = ()) -> bool:
    """True only on positive evidence of an organisation; any doubt answers False (a person)."""
    return _classify(normalise(name), frozenset(map(normalise, organisations)))


def pseudonymiser(
    env: Mapping[str, str] | None = None, organisations: Iterable[str] = ()
) -> Callable[[str], str]:
    """A function that passes an organisation's name through and hashes a person's.

    Reads the key first, so it raises before any name is seen.
    `organisations` are names already known to be organisations, matched after normalisation.
    """
    key = hash_key(env)
    register = frozenset(map(normalise, organisations))

    def hide(name: str) -> str:
        normalised = normalise(name)
        if _classify(normalised, register):
            return name
        digest = hmac.new(key, normalised.encode(), hashlib.sha256).hexdigest()
        return f"person:{digest}"

    return hide
