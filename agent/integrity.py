"""Answer integrity: the model writes no figure; `ask` renders each one from a cell of a run the
turn cites, and shows the reply only when the verified question it answers and the runs it cites
back it; and whether the query a turn's audit record logged reproduces those runs.

The reply's form: the model writes prose and, where a figure goes, a reference to a cited cell,
`{query:row:column}`: the verified query's id, the row's number counted from 1 in the run's order,
and the column's name (`{duplicate-payment-rate:1:duplicate_payment_rate}`).
`check(question, text, citations, queries)` is what `agent.ask.ask` calls on the model's reply once
the tool-call loop has ended (not on a turn stopped at a query's cap, the tool-call cap or the
refusal cap). It returns the reply with each reference replaced by its cell's rendering, which is
what `ask` shows and records, or, when any check fails, no text: `ask` then shows and records
`WITHHELD` in its place and does not record the turn as answered (it records it abstained, or
refused for the policy or as unplanned when a call was).
`reproduce(answer, runs, as_persona)` re-runs the queries a record logged (`logged(record, kept)`
reads them with the kept values) and is the caller's or a verifier's to run; `ask` does not call it.

What `check` requires; the reply is withheld when any of these fails:
- a cited query covers the question: its verified question equals the asked one once both are put
  in Unicode NFKC form, casefolded and their whitespace runs collapsed to one space (`same`);
  a turn that cites no run is withheld whatever its reply says;
- the reply is a string holding something other than white space;
- outside its references, the reply holds only ASCII letters, a space, a line break and the
  characters of `PROSE` (`. , ; : ! ? ' " = _`), and a `-` only between two ASCII letters: so any
  digit, any character that is not ASCII (each Unicode digit, numeral and look-alike letter, every
  format character and bidi control among them), every other ASCII symbol (`- + ( ) $ % #`, a
  brace that is not part of a whole reference) and every control character but the line break are
  reported where they are;
- each reference is written whole by the grammar `REFERENCE`, with a space, a line break or the
  start of the reply right before it, and right after it the end of the reply, a space, a line
  break, or one of `. , ; : ! ?` followed by one of those; so no character the model writes touches
  a rendered figure;
- between two references the model's text holds at least one ASCII letter, so no two rendered
  cells read as one figure (`1 000`, `1,250`, `12.25`);
- each reference names a query cited exactly once in this turn, by a run that covers the question
  and whose citation carries a freshness, a row that run returned, and a column the run returned
  once;
- that cell can be rendered (`rendered`): a number in one fixed format, with its sign; a null, a
  bool, a date or a time as a fixed word or its ISO text; a text cell as itself;
- an `Exception` raised by any check withholds the reply.

How `rendered` writes a cell, the one format a figure is shown in:
- an int, a float or a Decimal (not a bool) in positional notation, with no thousands separator
  and no exponent, an ASCII `-` before it when it is below zero and no sign otherwise; a float by
  its shortest round-trip text (Python's `repr`) and a Decimal with the digits it carries
  (`Decimal("1.50")` is `1.50`); every digit is kept, none rounded; zero, negative zero among them,
  is written with no sign;
- a number past `LONGEST` positional characters, a NaN and an infinity are not rendered;
- a null as `null`, a bool as `true` or `false`, a date, a datetime or a time as its `isoformat()`;
- a text cell as its own text, when it is printable ASCII with no brace and at most `LONGEST_TEXT`
  characters; any other text, and a cell of any other type, is not rendered.

What `reproduce` checks, per run, in order: the record logs one run per citation, in the same
order; the run was logged as the answer's persona; its SQL and values are the cited query's SQL
bound (`agent.audit.bind`); a run failing one of those is reported and not re-run; the others are
re-run as the logged persona on a new cursor of `as_persona(persona)`'s connection, with the search
path set to the schemas that hold its tables, and their columns and rows (as
`agent.audit.sealed_result` of their `cell_text`, so in any row order) must equal the cited run's,
and their seal under the kept salt the record's sealed result.

Stated limits:
- the words around a reference are prose the check does not read: a sign, direction or scale
  written in words (`minus`, `a net-loss of`, `down about`, `in millions`, `USD million`) beside a
  rendered figure is shown as written, so a reader can be told a positive cell is a loss or a cell
  is in thousands; the figure itself is always the cell's own, with the cell's own sign;
- a figure written in words (`forty-two`) or in ASCII letters that look like digits (`O.OS`, `lOO`)
  is prose to the check and is shown;
- which row, column or metric the prose attributes a rendered figure to is not checked: two
  references swapped in the prose are shown as written;
- a text cell is rendered as its own text and its characters are not read, so a text cell holding
  digits or a sign shows them as the query returned them;
- an honest reply that writes a figure itself, a currency or percent sign, Markdown, parentheses or
  any non-ASCII letter is withheld; so is a reply naming a query cited twice in the turn, a column
  name outside the grammar or a reference to a run that does not cover the question;
- a reply that holds prose and no reference is shown on a covering run, though it states no figure;
- a citation of the agent's BigQuery mode (`agent/bigquery.py`) carries no freshness, so on BigQuery
  every reply that references a cell is withheld, and only prose is shown;
- the question must be a verified question's own wording, as `same` compares it: a paraphrase is
  withheld, so with any model the agent answers only the verified questions;
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

import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import duckdb

from agent.audit import bind, cell_text, seal, sealed_result
from agent.model import Citation, VerifiedQuery

if TYPE_CHECKING:
    from agent.ask import Answer

ENGINE = "duckdb"
WITHHELD = (
    "No answer is shown: the reply did not give its figures as references to the cells of a"
    " verified query for this question that ran."
)
# A reference to a cited cell: the query's id, the row counted from 1, and the column's name.
REFERENCE = re.compile(r"\{([a-z][a-z0-9-]*):([1-9][0-9]{0,8}):([A-Za-z_][A-Za-z0-9_]*)\}")
# The characters the model's prose may hold beside ASCII letters, a space and a line break.
PROSE = frozenset(".,;:!?'\"=_")
# What may close a reference before the space, line break or end that must follow.
CLOSING = frozenset(".,;:!?")
SPACES = frozenset(" \n")
LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
# The most characters a rendered number, or a rendered text cell, may take.
LONGEST = 64
LONGEST_TEXT = 200
PRINTABLE = frozenset(chr(code) for code in range(0x20, 0x7F)) - {"{", "}"}
# The schema names `agent/ask.py`'s tool sets on the search path, and no other.
SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]


@dataclass(frozen=True)
class Failure:
    """A character or reference of the reply that cannot be shown (`number`, the text concerned),
    or a run that did not reproduce (`number` None); `query` names the cited query it concerns,
    when there is one."""

    number: str | None
    query: str | None
    reason: str


@dataclass(frozen=True)
class Reference:
    """A reference of the reply as written, the cell it names and that cell's rendering."""

    written: str
    query: str
    row: int
    column: str
    rendered: str


@dataclass(frozen=True)
class Rendered:
    """The reply with each reference rendered (`text`), or None when any check failed."""

    text: str | None
    references: tuple[Reference, ...]
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


class Unrendered(ValueError):
    """A cell `rendered` does not write."""


def rendered(cell: Any) -> str:
    """A cell as `ask` shows it; see the module's docstring. Raises `Unrendered`."""
    if cell is None:
        return "null"
    if isinstance(cell, bool):
        return "true" if cell else "false"
    if isinstance(cell, int):
        # A positional text of at most LONGEST characters, the sign included.
        if abs(int(cell)) >= 10 ** (LONGEST - 1):
            raise Unrendered("a number longer than the renderer writes")
        return format(int(cell), "d")
    if isinstance(cell, float):
        if not math.isfinite(cell):
            raise Unrendered("a number that is not finite")
        return _positional(Decimal(float.__repr__(cell)))
    if isinstance(cell, Decimal):
        if not cell.is_finite():
            raise Unrendered("a number that is not finite")
        return _positional(Decimal(cell))
    if isinstance(cell, datetime | date | time):
        return cell.isoformat()
    if isinstance(cell, str):
        if not cell or len(cell) > LONGEST_TEXT or not set(cell) <= PRINTABLE:
            raise Unrendered("a text that is not short printable ASCII without a brace")
        return str.__str__(cell)
    raise Unrendered(f"a cell of type {type(cell).__name__}, which the renderer does not write")


def _positional(value: Decimal) -> str:
    """A finite Decimal in positional notation with every digit it carries, no sign at zero."""
    if value.is_zero():
        value = abs(value)
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)  # noqa: S101 - a finite Decimal's exponent is an int
    # The positional text holds the digits and, beyond them, the zeros the exponent adds.
    if len(digits) + abs(exponent) + 2 > LONGEST:
        raise Unrendered("a number longer than the renderer writes")
    return format(value, "f")


def _prose(char: str) -> str | None:
    """Why the model's prose may not hold `char`, or None when it may (a `-` is judged apart)."""
    if char in LETTERS or char in SPACES or char in PROSE:
        return None
    if char.isascii() and char.isdigit():
        return "a digit outside a reference: the model writes no figure"
    if not char.isascii():
        name = unicodedata.name(char, f"U+{ord(char):04X}")
        return f"a character that is not ASCII ({name}) outside a reference"
    if char in "{}":
        return "a brace that is not part of a whole reference"
    return "a character the prose may not hold outside a reference"


def render(text: Any, citations: Sequence[Citation]) -> Rendered:
    """`text` with each reference replaced by the rendering of the cell of `citations` it names,
    or None with the failures; covering and freshness are `check`'s, not read here."""
    if not isinstance(text, str):
        return Rendered(None, (), (Failure(None, None, "the reply is not a string"),))
    failures: list[Failure] = []
    matches = list(REFERENCE.finditer(text))
    inside = {index for match in matches for index in range(*match.span())}
    for index, char in enumerate(text):
        if index in inside:
            continue
        if char == "-":
            between = (
                0 < index < len(text) - 1
                and text[index - 1] in LETTERS
                and text[index + 1] in LETTERS
            )
            if not between:
                failures.append(Failure(char, None, "a - that is not between two letters"))
            continue
        if (reason := _prose(char)) is not None:
            failures.append(Failure(char, None, reason))
    references: list[Reference] = []
    for number, match in enumerate(matches):
        start, end = match.span()
        written = match.group()
        before, after = text[start - 1 : start], text[end : end + 2]
        if before and before not in SPACES:
            failures.append(Failure(written, None, "a character touches the reference before it"))
        closed = (
            not after
            or after[0] in SPACES
            or (after[0] in CLOSING and (len(after) == 1 or after[1] in SPACES))
        )
        if not closed:
            failures.append(Failure(written, None, "a character touches the reference after it"))
        if False and not set(text[matches[number - 1].end() : start]) & LETTERS:
            failures.append(Failure(written, None, "no letter parts it from the reference before"))
        query, row, column = match.group(1), int(match.group(2)), match.group(3)
        runs = [c for c in citations if c.query == query]
        if len(runs) != 1:
            reason = "no run of this turn" if not runs else "more than one run of this turn"
            failures.append(Failure(written, query, f"{reason} cites the query"))
            continue
        (run,) = runs
        if row > len(run.rows):
            failures.append(Failure(written, query, f"the run returned {len(run.rows)} rows"))
            continue
        if run.columns.count(column) != 1:
            failures.append(Failure(written, query, "the run returns no such column once"))
            continue
        cell = run.rows[row - 1][run.columns.index(column)]
        try:
            shown = rendered(cell)
        except Unrendered as error:
            failures.append(Failure(written, query, f"the cell is not rendered: {error}"))
            continue
        references.append(Reference(written, query, row, column, shown))
    if failures:
        return Rendered(None, tuple(references), tuple(failures))
    parts, at = [], 0
    for match, reference in zip(matches, references, strict=True):
        parts += [text[at : match.start()], reference.rendered]
        at = match.end()
    return Rendered("".join([*parts, text[at:]]), tuple(references), ())


def same(asked: str, verified: str) -> bool:
    """Whether two questions are the same wording: each in NFKC form, casefolded, with each run of
    white space made one space and none at either end."""

    def form(question: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", question).casefold().split())

    return form(asked) == form(verified)


def check(
    question: str,
    text: Any,
    citations: Sequence[Citation],
    queries: Mapping[str, VerifiedQuery],
) -> Rendered:
    """The reply as `ask` shows and records it, or None with the failures; see the module's
    docstring."""
    try:
        covering = [c for c in citations if same(question, queries[c.query].question)]
        if not covering:
            reason = "no cited run is of a verified query of the question"
            return Rendered(None, (), (Failure(None, None, reason),))
        if not isinstance(text, str) or not text.strip():
            return Rendered(None, (), (Failure(None, None, "the reply holds no text"),))
        result = render(text, citations)
        failures = list(result.failures)
        for reference in result.references:
            (run,) = [c for c in citations if c.query == reference.query]
            if not any(run is c for c in covering):
                reason = "the run does not cover the question"
                failures.append(Failure(reference.written, run.query, reason))
            elif run.freshness is None:
                reason = "the run's citation carries no freshness"
                failures.append(Failure(reference.written, run.query, reason))
        if failures:
            return Rendered(None, result.references, tuple(failures))
        return result
    except Exception as error:  # noqa: BLE001 - a check that raises withholds the reply
        reason = f"the check raised {type(error).__name__}"
        return Rendered(None, (), (Failure(None, None, reason),))


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
