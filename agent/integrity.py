"""Answer integrity: whether an answer's reply may be shown, by the verified question it answers
and the runs it cites, and whether the query a turn's audit record logged reproduces those runs.

`withheld(question, text, citations, queries)` is what `agent.ask.ask` calls on the model's reply
once the tool-call loop has ended (not on a turn stopped at the tool-call cap): when it is true,
`ask` shows `WITHHELD` in place of the reply and records the turn as abstained.
`reproduce(answer, runs, as_persona)` re-runs the queries a record logged (`logged(record, kept)`
reads them with the kept values) and is the caller's or a verifier's to run; `ask` does not call it.

What `withheld` checks; the reply is withheld when any of these fails:
- a cited query covers the question: its verified question equals the asked one once both are put
  in Unicode NFKC form, casefolded and their whitespace runs collapsed to one space (`same`);
  a turn that cites no run is withheld whatever its reply says;
- `trace(text, covering)` reports no failure, where `covering` is the cited runs of the queries
  that cover the question;
- an `Exception` raised by either check withholds the reply.

What `trace` reads and checks:
- a number is a run of Unicode decimal digits (category Nd), with thousands separated by `,` in
  groups of three, one decimal part and one exponent (`e` or `E`), and a leading sign when no
  letter, digit or `_` comes before it: an ASCII `-`, U+2212 minus, or one of the dashes U+2010
  to U+2015, U+FE58, U+FE63 and U+FF0D, each read as a minus;
- before numbers are read, each place where the `str` or the `repr` of a cited cell that is not a
  number (a text, a date, a time) is written, with no letter, digit or `_` on either side, is set
  aside: its digits are that cell's own text (`payments-over-25k`, a timestamp);
- a number written in parentheses, `(12.25)`, or followed directly by a minus or one of those
  dashes, `12.25-`, is reported: its sign is ambiguous;
- a number whose value cannot be read (an exponent past the decimal context's range) is reported;
- a number written right after the word `version` and white space is traced when it equals the
  version of a cited run;
- any other number is traced when its value, its separators removed, equals the value of a cited
  cell that is an int, a float (by Python's `repr`, the shortest text that gives back the float)
  or a Decimal, and not a bool: `1,234,567.5`, `1234567.50` and `1.2345675e6` each equal
  1234567.5;
- only runs whose citation carries a freshness count; a number found only in a run without one is
  reported as such;
- each character of Unicode category No (a superscript, a fraction, a circled number) is reported;
- each number not traced is a `Failure` naming it.

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
  are reported, not matched;
- a number written in words (`forty-two million`) is not read, and passes on a turn whose reply
  is not withheld for another reason;
- the question must be a verified question's own wording, as `same` compares it: a paraphrase is
  withheld, so with any model the agent answers only the verified questions;
- a number is matched to any cell of any covering run: which row, column or metric the prose
  attributes it to is not checked, so two cited figures swapped in the text pass;
- a cell that is not a number is matched only by its `str` or `repr` written whole; a number equal
  to a cited text cell's whole text passes as that cell;
- the text need not name a metric, a query or a freshness: those are the citations' fields;
- a turn stopped at the tool-call cap shows `ask`'s own message, which names the cap and is not
  checked;
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
# The characters read as a minus directly before or after a number.
DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"
NUMBER = re.compile(
    rf"(?:(?<!\w)[{DASHES}])?(?:\d{{1,3}}(?:,\d{{3}})+(?!\d)|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?"
)
VERSION = re.compile(r"(?<!\w)version\s+$", re.IGNORECASE)
# The schema names `agent/ask.py`'s tool sets on the search path, and no other.
SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")
# What stands in for a cell's own text once it is set aside: no digit, the same length.
ASIDE = "#"

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]


@dataclass(frozen=True)
class Failure:
    """A number of the text that was not traced (`number`, as written), or a run that did not
    reproduce (`number` None); `query` names the cited query it concerns, when there is one."""

    number: str | None
    query: str | None
    reason: str


@dataclass(frozen=True)
class Traced:
    """A number of the text, as written, and the cited query it was traced to."""

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


def numbers(text: str) -> list[tuple[int, int]]:
    """The span of each number of `text`, as the matching rule reads them."""
    return [match.span() for match in NUMBER.finditer(text)]


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _read(number: str) -> Decimal:
    """A number as written, its separators removed and a dash before it read as a minus."""
    digits = number.replace(",", "")
    if digits[0] in DASHES:
        digits = "-" + digits[1:]
    return Decimal(digits)


def _value(value: Any) -> Decimal:
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _set_aside(text: str, citations: Sequence[Citation]) -> str:
    """`text` with each written cell of `citations` that is not a number replaced by `ASIDE`."""
    written = {
        form
        for citation in citations
        for row in citation.rows
        for cell in row
        if cell is not None and not _is_number(cell)
        for form in (str(cell), repr(cell))
        if re.search(r"\d", form)
    }
    for form in sorted(written, key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w){re.escape(form)}(?!\w)")
        text = pattern.sub(lambda match: ASIDE * len(match.group()), text)
    return text


def _cited(citation: Citation, value: Decimal) -> bool:
    return any(_is_number(cell) and _value(cell) == value for row in citation.rows for cell in row)


def trace(text: str, citations: Sequence[Citation]) -> Trace:
    """Each number of `text`, traced to a run of `citations` or reported; see the module's
    docstring for the rule."""
    fresh = [c for c in citations if c.freshness is not None]
    stale = [c for c in citations if c.freshness is None]
    aside = _set_aside(text, fresh)
    traced: list[Traced] = []
    failures: list[Failure] = []
    for start, end in numbers(aside):
        number = text[start:end]
        if aside[start - 1 : start] == "(" and aside[end : end + 1] == ")":
            failures.append(Failure(number, None, "a number in parentheses has an ambiguous sign"))
            continue
        if aside[end : end + 1] and aside[end] in DASHES:
            failures.append(
                Failure(number, None, "a number followed by a minus has an ambiguous sign")
            )
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
    for char in text:
        if unicodedata.category(char) == "No":
            reason = "a numeral that is not a decimal digit is not read"
            failures.append(Failure(char, None, reason))
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
