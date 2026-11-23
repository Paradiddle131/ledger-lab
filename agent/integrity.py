"""Answer integrity: whether an answer's reply may be shown, by the verified question it answers
and the runs it cites, and whether the query a turn's audit record logged reproduces those runs.

`withheld(question, text, citations, queries)` is what `agent.ask.ask` calls on the model's reply
once the tool-call loop has ended (not on a turn stopped at a query's cap, the tool-call cap or the
refusal cap): when it is true, `ask` shows `WITHHELD` in place of the reply and does not record the
turn as answered (it records it abstained, or refused for the policy or as unplanned when a call
was).
`reproduce(answer, runs, as_persona)` re-runs the queries a record logged (`logged(record, kept)`
reads them with the kept values) and is the caller's or a verifier's to run; `ask` does not call it.

What `withheld` checks; the reply is withheld when any of these fails:
- a cited query covers the question: its verified question equals the asked one once both are put
  in Unicode NFKC form, casefolded and their whitespace runs collapsed to one space (`same`);
  a turn that cites no run is withheld whatever its reply says;
- `trace(text, covering)` reports no failure, where `covering` is the cited runs of the queries
  that cover the question;
- an `Exception` raised by either check withholds the reply.

What `trace` reads and checks: it accepts only figures written plainly, and reports the rest.
- the text is folded (`fold`): put in Unicode NFKC form, so fullwidth digits and small or fullwidth
  hyphen-minus become ASCII, and each format character (category Cf: a zero-width space, a word
  joiner, a soft hyphen) removed;
- each cited cell that is not a number (a text, a date, a time), its `str` folded, whose first and
  last characters are letters or digits, is set aside (each character made a space) where it is
  written with no letter, digit or `_` on either side and no `.` or `,` joining it to a digit: its
  digits and characters are that cell's own (`payments-over-25k`, `2026-10-01 00:00:00`);
- a number is ASCII digits, with thousands separated by `,` in groups of three, one decimal part
  and one exponent (`e` or `E`), and, as its only sign, an ASCII `-` touching the digits with no
  letter, digit, `_` or `-` before it;
- outside the numbers, each character that is not white space, a cased letter (category Lu, Ll or
  Lt), one of `PLAIN` (`.,;:!?='"_` and the four curly quotes), or a `-` between two cased letters
  is reported: so a dash or minus of any other kind, a minus parted from the digits, a
  parenthesis, a currency sign, a percent sign and Markdown are each reported wherever they are;
- a number joined to a letter, digit or `_` on either side, or written right after `.`, `,` or
  `-`, is reported (`42M`, `Q3`, the second end of `10-12`);
- a number right after a word of `SIGN_BEFORE` (`minus`, `negative`, `down`, `loss`, `deficit`),
  or after such a word and one of `LINKS` (`a loss of`), or right before a word of `SIGN_AFTER`
  (`CR`, `DR`), is reported; words are compared casefolded, their quotes and closing punctuation
  stripped;
- a number right before a word of `SCALE_AFTER` (`million`, `bn`, `k`, `percent`, `per`, `bps`,
  `times`) is reported, since its cell carries no such scale or unit;
- a number whose value cannot be read (an exponent past the decimal context's range) is reported;
- a number written right after the word `version` and white space is traced when it equals the
  version of a cited run;
- any other number is traced when its value, its separators removed, equals the value of a cited
  cell that is an int, a float (by Python's `repr`, the shortest text that gives back the float)
  or a Decimal, and not a bool: `1,234,567.5`, `1234567.50` and `1.2345675e6` each equal
  1234567.5;
- only runs whose citation carries a freshness count; a number found only in a run without one is
  reported as such;
- each character of the text as given, before folding, of Unicode category No (a superscript, a
  fraction, a circled number) or Nl (a letter numeral, `Ⅻ`) is reported;
- each number not traced, and each character reported, is a `Failure` naming it as folded.

What `reproduce` checks, per run, in order: the record logs one run per citation, in the same
order; the run was logged as the answer's persona; its SQL and values are the cited query's SQL
bound (`agent.audit.bind`); a run failing one of those is reported and not re-run; the others are
re-run as the logged persona on a new cursor of `as_persona(persona)`'s connection, with the search
path set to the schemas that hold its tables, and their columns and rows (as
`agent.audit.sealed_result` of their `cell_text`, so in any row order) must equal the cited run's,
and their seal under the kept salt the record's sealed result.

Stated limits:
- equality only: a rounded figure, a percentage of a share, a number written with another
  separator (a space, `'`, a decimal comma), and a year or a count in prose that is no cell's value
  are reported, not matched; so is each end of a range;
- plain figures only: an honest reply that writes a figure with a currency sign, in parentheses or
  a tuple, in Markdown, or beside any character outside the allowed set is withheld too;
- a sign or a direction written in words other than the sign words (`fell by`, `a decrease of`,
  `the opposite of`), and a scale or unit written in words other than the scale words (`lakh`,
  `crore`, `dozen`), are not read;
- a number written in words (`forty-two million`) is not read, and passes on a turn whose reply
  is not withheld for another reason;
- the question must be a verified question's own wording, as `same` compares it: a paraphrase is
  withheld, so with any model the agent answers only the verified questions;
- a number is matched to any cell of any covering run: which row, column or metric the prose
  attributes it to is not checked, so two cited figures swapped in the text pass;
- a cell that is not a number is set aside only where its folded `str` is written whole, and its
  own characters are not read, so a sign inside a cited text cell is not read; a number equal to a
  cited text cell's whole text passes as that cell;
- the text need not name a metric, a query or a freshness: those are the citations' fields;
- a turn stopped at a query's cap, the tool-call cap or the refusal cap shows `ask`'s own fixed
  message, which names the cap and is not checked; a turn at the refusal cap is recorded answered
  when a planned call returned;
- `reproduce` re-runs on whatever connection `as_persona` returns, so it shows a change of the
  persona's database since the run, not which build that database came from; it does not re-ask
  the model or open the record's answer seal (`tests/agent/test_audit.py` rebuilds those);
- `reproduce` reports, as a failed re-run, a database holding a schema name the tool does not
  quote, and does not close the connections `as_persona` returns, which stay their maker's.
"""

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import duckdb

from agent.audit import bind, cell_text, seal, sealed_result
from agent.model import Citation, VerifiedQuery

if TYPE_CHECKING:
    from agent.ask import Answer

ENGINE = "duckdb"
WITHHELD = (
    "No answer is shown: no verified query for this question returned the figures the reply gives."
)
# A number as the check reads it: ASCII digits, thousands in groups of three, one decimal part and
# one exponent, with an ASCII minus touching the digits as its only sign.
NUMBER = re.compile(
    r"(?:(?<![\w-])-)?(?:[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])|[0-9]+)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?"
)
VERSION = re.compile(r"(?<!\w)version\s+$", re.IGNORECASE)
# The characters a reply may hold outside its numbers, beside white space and cased letters.
PLAIN = frozenset("0123456789.,;:!?='\"_\u2018\u2019\u201c\u201d")
CASED = ("Lu", "Ll", "Lt")
# Words that give the number after them a sign, directly or through one of `LINKS`.
SIGN_BEFORE = frozenset(
    {
        "minus",
        "negative",
        "negatively",
        "down",
        "less",
        "loss",
        "losses",
        "deficit",
        "deficits",
        "debit",
        "overdrawn",
        "owed",
    }
)
LINKS = frozenset({"of", "by", "at"})
# Words that give the number before them a sign, or a scale or unit its cell does not carry.
SIGN_AFTER = frozenset({"cr", "dr", "negative", "debit", "deficit", "overdrawn", "owed"})
SCALE_AFTER = frozenset(
    {
        "hundred",
        "hundreds",
        "thousand",
        "thousands",
        "million",
        "millions",
        "billion",
        "billions",
        "trillion",
        "trillions",
        "k",
        "m",
        "mm",
        "mn",
        "b",
        "bn",
        "percent",
        "pct",
        "per",
        "bp",
        "bps",
        "times",
    }
)
# The schema names `agent/ask.py`'s tool sets on the search path, and no other.
SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")
# What stands in for each character of a cell's own text once it is set aside.
ASIDE = " "

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]


@dataclass(frozen=True)
class Failure:
    """A number or character of the text that was not traced (`number`, as folded), or a run that
    did not reproduce (`number` None); `query` names the cited query it concerns, when there is
    one."""

    number: str | None
    query: str | None
    reason: str


@dataclass(frozen=True)
class Traced:
    """A number of the text, as folded, and the cited query it was traced to."""

    number: str
    query: str


@dataclass(frozen=True)
class Trace:
    traced: tuple[Traced, ...]
    failures: tuple[Failure, ...]


@dataclass(frozen=True)
class Reproduced:
    reproduced: tuple[str, ...]
    failures: tuple[Failure, ...]


@dataclass(frozen=True)
class Logged:
    """A run as its record and kept values hold it: the SQL as sent, its bound values, the
    persona it ran as, the seal of its result and that seal's salt."""

    text: str
    parameters: Mapping[str, Any]
    persona: str
    result: str
    salt: str


def fold(text: str) -> str:
    """`text` in Unicode NFKC form, with each format character (category Cf) removed."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(char for char in folded if unicodedata.category(char) != "Cf")


def numbers(text: str) -> list[tuple[int, int]]:
    """The span of each number of `text`, as the matching rule reads them."""
    return [match.span() for match in NUMBER.finditer(text)]


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _read(number: str) -> Decimal:
    """A number as written, its thousands separators removed."""
    return Decimal(number.replace(",", ""))


def _value(value: Any) -> Decimal:
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _set_aside(text: str, citations: Sequence[Citation]) -> str:
    """`text` with each written cell of `citations` that is not a number, folded, its first and
    last characters letters or digits, replaced by `ASIDE` character for character."""
    written = {
        form
        for citation in citations
        for row in citation.rows
        for cell in row
        if cell is not None and not _is_number(cell)
        for form in (fold(str(cell)),)
        if form and form[0].isalnum() and form[-1].isalnum()
    }
    for form in sorted(written, key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w)(?<!\d[.,]){re.escape(form)}(?!\w)(?![.,]\d)")
        text = pattern.sub(lambda match: ASIDE * len(match.group()), text)
    return text


def _plain(text: str, index: int) -> bool:
    """Whether the character at `index`, outside every number, is one a reply may hold: white
    space, a cased letter, a character of `PLAIN`, or a `-` between two cased letters."""
    char = text[index]
    if char.isspace() or char in PLAIN or unicodedata.category(char) in CASED:
        return True
    return (
        char == "-"
        and 0 < index < len(text) - 1
        and unicodedata.category(text[index - 1]) in CASED
        and unicodedata.category(text[index + 1]) in CASED
    )


def _words(text: str) -> list[str]:
    return [w.strip(".,;:!?='\"_\u2018\u2019\u201c\u201d").casefold() for w in text.split()]


def _signed(folded: str, start: int, end: int) -> bool:
    """Whether a word of `SIGN_BEFORE` comes right before the number at `folded[start:end]`, or
    one word of `LINKS` after such a word, or a word of `SIGN_AFTER` right after it."""
    before, after = _words(folded[:start]), _words(folded[end:])
    if before and before[-1] in SIGN_BEFORE:
        return True
    if len(before) > 1 and before[-1] in LINKS and before[-2] in SIGN_BEFORE:
        return True
    return bool(after) and after[0] in SIGN_AFTER


def _scaled(folded: str, end: int) -> bool:
    """Whether a word of `SCALE_AFTER` comes right after the number ending at `folded[end]`."""
    after = _words(folded[end:])
    return bool(after) and after[0] in SCALE_AFTER


def _joined(text: str, start: int, end: int) -> bool:
    """Whether the number at `text[start:end]` is joined to a letter, digit or `_`, or follows a
    `.` or `,` with no space."""
    left, right = text[start - 1 : start], text[end : end + 1]
    return bool(left and (left.isalnum() or left in "_.,-")) or bool(
        right and (right.isalnum() or right == "_")
    )


def _cited(citation: Citation, value: Decimal) -> bool:
    return any(_is_number(cell) and _value(cell) == value for row in citation.rows for cell in row)


def trace(text: str, citations: Sequence[Citation]) -> Trace:
    """Each number of `text`, traced to a run of `citations` or reported, and each character the
    rule does not read reported; see the module's docstring for the rule."""
    fresh = [c for c in citations if c.freshness is not None]
    stale = [c for c in citations if c.freshness is None]
    folded = fold(text)
    aside = _set_aside(folded, fresh)
    spans = numbers(aside)
    traced: list[Traced] = []
    failures: list[Failure] = []
    for char in text:
        if unicodedata.category(char) in ("No", "Nl"):
            reason = "a numeral that is not a decimal digit is not read"
            failures.append(Failure(char, None, reason))
    inside = {index for start, end in spans for index in range(start, end)}
    for index, char in enumerate(aside):
        if index not in inside and not _plain(aside, index):
            reason = "a character the check does not read is written outside a number"
            failures.append(Failure(char, None, reason))
    for start, end in spans:
        number = aside[start:end]
        if _joined(aside, start, end):
            failures.append(Failure(number, None, "a number joined to other text is not read"))
            continue
        if _signed(folded, start, end):
            reason = "a word beside the number gives it a sign the check does not read"
            failures.append(Failure(number, None, reason))
            continue
        if _scaled(folded, end):
            reason = "a word after the number gives it a scale or unit the check does not read"
            failures.append(Failure(number, None, reason))
            continue
        try:
            value = _read(number)
            versioned = [c for c in fresh if c.version == value]
            found = [c for c in fresh if _cited(c, value)]
            unfresh = [c for c in stale if _cited(c, value)]
        except (InvalidOperation, OverflowError, ValueError):
            failures.append(Failure(number, None, "its value cannot be read"))
            continue
        if VERSION.search(aside[:start]) and versioned:
            traced.append(Traced(number, versioned[0].query))
            continue
        if found:
            traced.append(Traced(number, found[0].query))
            continue
        if unfresh:
            reason = "only a run whose citation carries no freshness returned it"
            failures.append(Failure(number, unfresh[0].query, reason))
            continue
        failures.append(Failure(number, None, "no run the answer cites returned it"))
    return Trace(tuple(traced), tuple(failures))


def same(asked: str, verified: str) -> bool:
    """Whether two questions are the same wording: each in NFKC form, casefolded, with each run of
    white space made one space and none at either end."""

    def form(question: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", question).casefold().split())

    return form(asked) == form(verified)


def withheld(
    question: str,
    text: str,
    citations: Sequence[Citation],
    queries: Mapping[str, VerifiedQuery],
) -> bool:
    """Whether `ask` must show `WITHHELD` in place of `text`; see the module's docstring."""
    try:
        covering = [c for c in citations if same(question, queries[c.query].question)]
        return not covering or bool(trace(text, covering).failures)
    except Exception:  # noqa: BLE001 - a check that raises withholds the reply, it is not shown
        return True


def logged(record: Mapping[str, Any], kept: Mapping[str, Sequence[Any]]) -> tuple[Logged, ...]:
    """The runs of an answer's record, each with its kept values and result salt; raises
    `ValueError` when a kept value does not open its seal in the record."""
    runs: list[Logged] = []
    for index, run in enumerate(record["runs"]):
        parameters: Mapping[str, Any] = {}
        if "parameters" in run["query"]:
            salt, parameters = kept[f"runs/{index}/query/parameters"]
            if seal(salt, parameters) != run["query"]["parameters"]:
                raise ValueError(f"run {index}: the kept values do not open their seal")
        salt, result = kept[f"runs/{index}/result"]
        if seal(salt, result) != run["result"]:
            raise ValueError(f"run {index}: the kept result does not open its seal")
        runs.append(Logged(run["query"]["text"], parameters, run["as"], run["result"], salt))
    return tuple(runs)


def _sealed(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> dict[str, Any]:
    return sealed_result(columns, ([cell_text(v) for v in row] for row in rows))


def _execute(
    connection: duckdb.DuckDBPyConnection, text: str, parameters: Mapping[str, Any]
) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
    cursor = connection.cursor()
    try:
        held = cursor.execute(
            "select distinct table_schema from information_schema.tables"
            " where table_catalog = system.main.current_database()"
        ).fetchall()
        schemas = sorted(schema for (schema,) in held)
        if not all(SCHEMA.fullmatch(schema) for schema in schemas):
            raise duckdb.Error(f"a schema name the tool does not quote: {schemas}")
        cursor.execute(f"set search_path = '{','.join(schemas)}'")
        result = cursor.execute(text, dict(parameters) or [])
        columns = tuple(column[0] for column in result.description or ())
        return columns, tuple(tuple(row) for row in result.fetchall())
    finally:
        cursor.close()


def reproduce(answer: "Answer", runs: Sequence[Logged], as_persona: AsPersona) -> Reproduced:
    """Re-run each logged run as its persona and compare it with the answer's citation of it;
    see the module's docstring for what is checked."""
    if len(runs) != len(answer.citations):
        reason = f"the record logs {len(runs)} runs where the answer cites {len(answer.citations)}"
        return Reproduced((), (Failure(None, None, reason),))
    reproduced: list[str] = []
    failures: list[Failure] = []
    connections: dict[str, duckdb.DuckDBPyConnection] = {}
    for citation, run in zip(answer.citations, runs, strict=True):
        before = len(failures)
        text, parameters = bind(citation.sql, ENGINE)
        if run.persona != answer.persona:
            reason = "the run was logged as another persona than the answer's"
            failures.append(Failure(None, citation.query, reason))
        if run.text != text:
            reason = "the logged query is not the cited query bound"
            failures.append(Failure(None, citation.query, reason))
        if dict(run.parameters) != parameters:
            reason = "the logged values are not the cited query's"
            failures.append(Failure(None, citation.query, reason))
        if len(failures) > before:
            continue
        if run.persona not in connections:
            connections[run.persona] = as_persona(run.persona)
        try:
            columns, rows = _execute(connections[run.persona], run.text, run.parameters)
        except duckdb.Error as error:
            failures.append(Failure(None, citation.query, f"the re-run failed: {error}"))
            continue
        again = _sealed(columns, rows)
        if again != _sealed(citation.columns, citation.rows):
            reason = "the re-run returned other rows than the cited run"
            failures.append(Failure(None, citation.query, reason))
        if seal(run.salt, again) != run.result:
            reason = "the re-run does not match the record's sealed result"
            failures.append(Failure(None, citation.query, reason))
        if len(failures) == before:
            reproduced.append(citation.query)
    return Reproduced(tuple(reproduced), tuple(failures))
