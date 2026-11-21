"""The own agent's one question-to-answer interface: `ask(persona, question, as_persona=...)`.

`ask` shows the model (`agent/model.py`) the question, the verified queries and the glossary of
the metric context (`semantics/context.json`, or what the caller passes), and the column
descriptions the caller passes, runs each verified query the model planned and then names with
the one read-only tool, `run_metric_query`, and returns an `Answer` citing what each run returned.

What `ask` checks, in this order:
- the persona must be a string equal to a persona id of the personas catalogue (by default
  `access/personas.yaml`), compared exactly, with no stripping or case folding; anything else
  raises `NoPersonaError`, before the metric context is loaded, the model is asked or a
  connection is opened;
- plan, then execute: the model first plans its tool calls (`Model.plan`) on a turn holding the
  question and the verified queries only, with no glossary, no column description and no result;
  the turns after it show those as well;
- for each tool call the model then makes, in this order:
  - once `MAX_TOOL_CALLS` queries have been run (a query whose run raised an engine error counts;
    a refused call does not), the call is refused, not put to the policy, and ends the turn;
  - at the `MAX_STEPS`th model step, the call is refused, not put to the policy, and ends the
    turn; each limit's refusal holds the `repr` of a tool or query that is not a string;
  - the call must equal (tool and query) a planned call not yet made, each planned call counted
    as often as the plan holds it; any other call is a `Refusal` and is not put to the policy;
  - its tool and its query must each be a string, or the call is a `Refusal` holding the `repr`
    of each that is not;
  - a call to `run_metric_query` must name a verified query of the context;
  - the call is put to the tool policy (`agent/policy/tools.py`) with the tool the model named,
    the persona, and as resource the query's metric for `run_metric_query`, the `Ledger` of the
    call's id for `propose_entry`, and a `Metric` of the call's id for any other tool; a denial
    is a `Refusal` naming the policy's reasons;
  - a call the policy allows to any tool but `run_metric_query` is a `Refusal`, since `ask` runs
    no other tool;
  - only then is a connection opened, by calling `as_persona(persona)`, at most once per `ask`,
    and the query run.

How the tool runs a query: on a new cursor of the persona's connection (in DuckDB a cursor is a
connection of its own to the same database, so it sees none of the parent's temporary objects),
with the search path set to the schemas of the connection's current database that hold a table or
a view, so the verified query's unqualified table names resolve to the persona's tables; the SQL
is the context's text, executed with an empty parameter list, so nothing of the question or the
model's output is spliced into it. A table or view name held in two of those schemas is refused as
ambiguous, and an engine error comes back as a `Refusal` carrying the engine's message, with no
rows.

Stated limits:
- `ask` holds no connection of its own: the rows are whatever the connection `as_persona` returns
  can read, so the scoping is the connection-maker's (on the local engine, `access/compile.py`);
- `ask` does not close that connection, which stays its maker's to close;
- the catalogue is read as YAML without its schema check (`access/schema.json`); the id is looked
  up by `access.identities.identity`, which also raises on a grant naming an entity exactly when
  its product is not entity-scoped, and that error is a `NoPersonaError` too;
- the query is not checked to be a single read: the persona's connection on the local engine is
  read-only, which is what stops a write;
- no audit record is written;
- the glossary, the column descriptions and a result's cells reach the model as they are, on
  every turn but the planning one: an instruction there cannot add a tool call to the plan, but it
  can shape the reply's text, and it can make the model skip a planned call;
- the plan is the model's own reading of the question and the verified queries; a question that
  itself carries an instruction is planned on like any other;
- no column description is given to the model unless the caller passes it: the metric context
  holds none.
"""

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from access.identities import UnknownPersonaError, identity
from agent.model import (
    READ_ONLY_TOOL,
    Citation,
    ColumnDescription,
    GlossaryTerm,
    Model,
    Refusal,
    Reply,
    Turn,
    VerifiedQuery,
    model_from_config,
)
from agent.policy.tools import Entity, authorize

REPO = Path(__file__).resolve().parents[1]
CONTEXT = REPO / "semantics" / "context.json"
PERSONAS = REPO / "access" / "personas.yaml"
TOOL = READ_ONLY_TOOL
# The resource type a tool call is put to the policy with, by tool, as the policy's schema
# (`agent/policy/tools.cedarschema`) declares it; a tool it does not declare is asked about a
# `Metric`, and the schema refuses the request for its action.
RESOURCE = {TOOL: "Metric", "propose_entry": "Ledger"}
MAX_TOOL_CALLS = 3
# The model steps one `ask` takes at most, refused calls and the reply included: a call refused as
# unplanned or by a check does not count toward `MAX_TOOL_CALLS`, so without this bound a model
# that kept making such calls would never end the turn; it leaves room for the tool calls, as many
# refusals again and more, and the reply.
MAX_STEPS = 10
UNPLANNED = (
    "the call is not one the model planned before it was shown anything but the question and"
    " the verified queries, or it was planned fewer times"
)
# A schema of the persona's database as `access/compile.py` names it: a purpose's id with `_` for
# `-`, or `main`.
SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]


class NoPersonaError(ValueError):
    """A question asked without a persona of the catalogue: no answer, no model step, no
    connection."""


@dataclass(frozen=True)
class Answer:
    """The model's reply, each query run as the persona (`citations`) and each tool call that was
    refused or failed (`refusals`), each in the order they were made."""

    persona: str
    question: str
    text: str
    citations: tuple[Citation, ...]
    refusals: tuple[Refusal, ...]


def load_personas(path: Path = PERSONAS) -> dict[str, Any]:
    """The personas catalogue as YAML holds it; its schema is not checked here."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_context(path: Path = CONTEXT) -> dict[str, VerifiedQuery]:
    """The verified queries of the metric context by id, each with its metric's name and version."""
    context = json.loads(path.read_text(encoding="utf-8"))
    return {
        query["id"]: VerifiedQuery(
            query["id"], query["question"], name, metric["version"], query["sql"]
        )
        for name, metric in context["metrics"].items()
        for query in metric.get("verified_queries", [])
    }


def load_glossary(path: Path = CONTEXT) -> tuple[GlossaryTerm, ...]:
    """The glossary of the metric context, its terms in the file's order."""
    context = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        GlossaryTerm(term_id, term["term"], term["definition"])
        for term_id, term in context["glossary"].items()
    )


def ask(
    persona: Any,
    question: str,
    *,
    as_persona: AsPersona,
    model: Model | None = None,
    queries: Mapping[str, VerifiedQuery] | None = None,
    catalogue: Mapping[str, Any] | None = None,
    glossary: Sequence[GlossaryTerm] | None = None,
    columns: Sequence[ColumnDescription] = (),
) -> Answer:
    """Answer `question` for `persona`, running tools only on `as_persona(persona)`'s connection.

    `model` defaults to `model_from_config(os.environ, queries)`, `queries` to the committed
    metric context, `catalogue` to `access/personas.yaml`; see the module's docstring for what is
    checked.
    """
    if not isinstance(persona, str):
        raise NoPersonaError(f"a question needs the asking persona, not {persona!r}")
    try:
        identity(load_personas() if catalogue is None else catalogue, persona)
    except (UnknownPersonaError, ValueError) as error:
        raise NoPersonaError(f"{persona!r} is not a persona of the catalogue") from error
    queries = load_context() if queries is None else queries
    shown = (tuple(load_glossary() if glossary is None else glossary), tuple(columns))
    model = model_from_config(os.environ, queries) if model is None else model
    session = uuid.uuid4().hex
    connection: duckdb.DuckDBPyConnection | None = None
    results: list[Citation | Refusal] = []
    planned = list(model.plan(Turn(question, tuple(queries.values()), ())))
    calls = steps = 0
    while True:
        step = model.step(Turn(question, tuple(queries.values()), tuple(results), *shown))
        steps += 1
        if isinstance(step, Reply):
            text = step.text
            break
        if calls == MAX_TOOL_CALLS:
            reason = f"the model made no reply within {MAX_TOOL_CALLS} tool calls"
            results.append(Refusal(_text(step.query), reason, _text(step.tool)))
            text = reason
            break
        if steps == MAX_STEPS:
            reason = f"the model made no reply within {MAX_STEPS} steps"
            results.append(Refusal(_text(step.query), reason, _text(step.tool)))
            text = reason
            break
        if step not in planned:
            results.append(Refusal(_text(step.query), UNPLANNED, _text(step.tool)))
            continue
        planned.remove(step)
        if not (isinstance(step.query, str) and isinstance(step.tool, str)):
            reason = "the tool or the query is not a string"
            results.append(Refusal(_text(step.query), reason, _text(step.tool)))
            continue
        query = queries.get(step.query) if step.tool == TOOL else None
        if step.tool == TOOL and query is None:
            results.append(Refusal(step.query, "the metric context holds no such verified query"))
            continue
        resource = Entity(RESOURCE.get(step.tool, "Metric"), query.metric if query else step.query)
        decision = authorize(Entity("Persona", persona), step.tool, resource, {"session": session})
        if not decision:
            reasons = "; ".join(decision.reasons)
            results.append(Refusal(step.query, f"the tool policy denied it: {reasons}", step.tool))
            continue
        if query is None:
            results.append(Refusal(step.query, f"ask runs no tool named {step.tool}", step.tool))
            continue
        if connection is None:
            connection = as_persona(persona)
        calls += 1
        results.append(run_metric_query(connection, query))
    return Answer(
        persona,
        question,
        text,
        tuple(r for r in results if isinstance(r, Citation)),
        tuple(r for r in results if isinstance(r, Refusal)),
    )


def _text(value: object) -> str:
    """A model's value as a refusal holds it: a string as it is, anything else as its `repr`."""
    return value if isinstance(value, str) else repr(value)


def run_metric_query(
    connection: duckdb.DuckDBPyConnection, query: VerifiedQuery
) -> Citation | Refusal:
    """Run a verified query on a cursor of `connection`, its tables resolved in the connection's
    own database; an engine error or an ambiguous name is a `Refusal`."""
    cursor = connection.cursor()
    try:
        held = cursor.execute(
            "select table_schema, table_name from information_schema.tables"
            " where table_catalog = system.main.current_database()"
        ).fetchall()
        schemas = sorted({schema for schema, _ in held})
        names = [table for _, table in held]
        if twice := sorted({t for t in names if names.count(t) > 1}):
            return Refusal(query.id, f"tables held in two schemas: {', '.join(twice)}")
        if not all(SCHEMA.fullmatch(schema) for schema in schemas):
            return Refusal(query.id, f"a schema name the tool does not quote: {schemas}")
        cursor.execute(f"set search_path = '{','.join(schemas)}'")
        result = cursor.execute(query.sql, [])
        columns = tuple(column[0] for column in result.description or ())
        rows = tuple(tuple(row) for row in result.fetchall())
        (path,) = cursor.execute(
            "select path from duckdb_databases()"
            " where database_name = system.main.current_database()"
        ).fetchone() or (None,)
    except duckdb.Error as error:
        return Refusal(query.id, f"the query failed on the persona's database: {error}")
    finally:
        cursor.close()
    written = None
    if path:
        try:
            written = datetime.fromtimestamp(Path(path).stat().st_mtime, UTC)
        except OSError:
            written = None
    return Citation(query.id, query.metric, query.version, query.sql, columns, rows, written)
