"""The own agent's one question-to-answer interface: `ask(persona, question, ...)`.

`ask` shows the model (`agent/model.py`) the question and the verified queries of the metric
context (`semantics/context.json`), runs each verified query the model names with the one
read-only tool, `run_metric_query`, writes the turn's audit record (`agent/audit.schema.json`) to
`audit.log`, and only then returns an `Answer` citing what each run returned.

What `ask` checks, in this order:
- the persona must be a string equal to a persona id of the personas catalogue (by default
  `access/personas.yaml`), compared exactly, with no stripping or case folding; a persona that is
  not a string or not such an id writes a record refused for `no-identity` and raises
  `NoPersonaError` (or `AuditWriteFailed` when that record is not written), before the metric
  context is loaded, the model is asked or a connection is opened; a `NoPersonaError` that
  `as_persona` raises later is a turn that raises, below, recorded as `failed` with
  `connection-error`;
- for each tool call: the query id names a verified query of the context; the tool policy
  (`agent/policy/tools.py`) allows the persona to run `run_metric_query` on the query's metric;
  only then is a connection opened, by calling `as_persona(persona)`, at most once per `ask`;
- the model gets at most `MAX_TOOL_CALLS` tool calls; one more is refused and ends the turn;
- a reply that ends the turn is checked by `agent.integrity.withheld`: when no cited query's
  verified question is the asked one, or `agent.integrity.trace` reports a number of the reply,
  `agent.integrity.WITHHELD`, which holds no digit, is shown and recorded in its place, and the
  turn is not `answered`; a turn stopped at the tool-call cap shows its own message, unchecked;
- once the turn has ended, its record is written (`AuditLog.append`, by default
  `agent.audit.FileLog`, which checks it against the schema and its seals before writing); when
  that raises, for any `Exception`, `ask` raises `AuditWriteFailed` and returns no `Answer`;
- when an `Exception` is raised by the model's step, the tool policy, `as_persona` or the tool
  (`run_metric_query`), the turn's record is written as `failed`, with `model-error`,
  `policy-error`, `connection-error` or `tool-error` for that step, holding the runs that returned
  before it and no answer seal; then that same exception is raised again, or `AuditWriteFailed` in
  its place when the record is not written.

What the record holds: the asker's persona (none on a `no-identity` refusal), a session id `ask`
makes and turn 1 (each `ask` is a session of one turn), `own-agent`, the time the turn ended,
`audit.release` and `audit.data` as the caller gives them, the metrics cited, and per query run
that returned the SQL as sent, the persona, `duckdb`, a run id and its start time; in place of the
question, the answer's text, each run's result (`agent.audit.sealed_result` of its columns and its
cells as `cell_text`) and its bound values, a seal, each under its own salt, the salt and value
kept by the log apart from the record.
Its outcome is `refused` with `no-identity` for no persona, `failed` when a step raised (above),
`stopped` with `model-call-cap` at the tool-call limit, else `answered` when a query returned and
the reply was not withheld, `refused` with `policy` when the tool policy denied a query and the
turn is not answered, and `abstained` otherwise, a withheld reply's turn among them.

How the tool runs a query: on a new cursor of the persona's connection (in DuckDB a cursor is a
connection of its own to the same database, so it sees none of the parent's temporary objects),
with the search path set to the schemas of the connection's current database that hold a table or
a view, so the verified query's unqualified table names resolve to the persona's tables; the SQL
is the context's text with each of its own values lifted to a named parameter (`agent.audit.bind`),
executed with those values bound, so nothing of the question or the model's output is spliced into
it. A table or view name held in two of those schemas is refused as ambiguous, and a text `bind`
refuses or an engine error comes back as a `Refusal` carrying the message, with no rows.

Stated limits:
- `ask` holds no connection of its own: the rows are whatever the connection `as_persona` returns
  can read, so the scoping is the connection-maker's (on the local engine, `access/compile.py`);
- `ask` does not close that connection, which stays its maker's to close;
- the catalogue is read as YAML without its schema check (`access/schema.json`); the id is looked
  up by `access.identities.identity`, which also raises on a grant naming an entity exactly when
  its product is not entity-scoped, and that error is a `NoPersonaError` too;
- the query is not checked to be a single read: the persona's connection on the local engine is
  read-only, which is what stops a write;
- the record names no model (the model protocol carries no name or settings yet), and its release
  and data are the caller's word, not checked here against the code or the connection;
- only `Exception` is caught for a `failed` record: a `KeyboardInterrupt`, `SystemExit` or other
  `BaseException` during the turn writes no record (and returns no answer); so does an exception
  reading a malformed personas catalogue, loading the metric context or building the model, before
  the turn starts;
- a run the engine refused is not among the record's runs, and a turn with only such runs is
  recorded as `abstained`;
- the log's head digest is not yet anchored outside the writer's reach, so whoever can write the
  log can still rewrite it from any record on, add to its end or cut its tail, and it verifies.
"""

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from access.identities import UnknownPersonaError, identity
from agent import integrity
from agent.audit import (
    AuditLog,
    AuditWriteFailed,
    UnboundValue,
    bind,
    cell_text,
    new_salt,
    seal,
    sealed_result,
)
from agent.integrity import WITHHELD
from agent.model import (
    Citation,
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
TOOL = "run_metric_query"
MAX_TOOL_CALLS = 3
# A schema of the persona's database as `access/compile.py` names it: a purpose's id with `_` for
# `-`, or `main`.
SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")

ENGINE = "duckdb"
AGENT = "own-agent"
# The stop reason of a turn that reached MAX_TOOL_CALLS, from the schema's fixed list.
CAP = "model-call-cap"
# The reasons of a `failed` record, by the step that raised.
MODEL_ERROR, POLICY_ERROR, CONNECTION_ERROR, TOOL_ERROR = (
    "model-error",
    "policy-error",
    "connection-error",
    "tool-error",
)

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


@dataclass(frozen=True)
class Audit:
    """Where `ask` writes each turn's record, and the fields of it `ask` cannot know itself: the
    commit of this repository the code runs on (`release`) and the record's `data`, the build the
    persona's connection reads and the files it was built from."""

    log: AuditLog
    release: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class Run:
    """A query run that returned, as its record holds it: the SQL as sent, its bound values, the
    persona it ran as, the run id, its start time and its result."""

    text: str
    parameters: Mapping[str, Any]
    persona: str
    job: str
    at: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


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


def ask(
    persona: Any,
    question: str,
    *,
    as_persona: AsPersona,
    audit: Audit,
    model: Model | None = None,
    queries: Mapping[str, VerifiedQuery] | None = None,
    catalogue: Mapping[str, Any] | None = None,
) -> Answer:
    """Answer `question` for `persona`, running tools only on `as_persona(persona)`'s connection.

    `model` defaults to `model_from_config(os.environ, queries)`, `queries` to the committed
    metric context, `catalogue` to `access/personas.yaml`; see the module's docstring for what is
    checked and what the record written to `audit.log` holds.
    """
    session = uuid.uuid4().hex
    refusal: NoPersonaError | None = None
    if not isinstance(persona, str):
        refusal = NoPersonaError(f"a question needs the asking persona, not {persona!r}")
    else:
        try:
            identity(load_personas() if catalogue is None else catalogue, persona)
        except (UnknownPersonaError, ValueError) as error:
            refusal = NoPersonaError(f"{persona!r} is not a persona of the catalogue")
            refusal.__cause__ = error
    if refusal is not None:
        record(audit, None, session, question, ("refused", "no-identity"), [], [], str(refusal))
        raise refusal
    queries = load_context() if queries is None else queries
    model = model_from_config(os.environ, queries) if model is None else model
    connection: duckdb.DuckDBPyConnection | None = None
    results: list[Citation | Refusal] = []
    runs: list[Run] = []
    denied = stopped = False
    calls = 0
    # The reason a `failed` record gives when the step under way raises.
    stage = MODEL_ERROR
    raised: Exception | None = None
    text = ""
    try:
        while True:
            stage = MODEL_ERROR
            step = model.step(Turn(question, tuple(queries.values()), tuple(results)))
            if isinstance(step, Reply):
                text = step.text
                break
            if calls == MAX_TOOL_CALLS:
                reason = f"the model made no reply within {MAX_TOOL_CALLS} tool calls"
                results.append(Refusal(step.query, reason))
                text, stopped = reason, True
                break
            calls += 1
            query = queries.get(step.query)
            if query is None:
                results.append(
                    Refusal(step.query, "the metric context holds no such verified query")
                )
                continue
            stage = POLICY_ERROR
            decision = authorize(
                Entity("Persona", persona),
                TOOL,
                Entity("Metric", query.metric),
                {"session": session},
            )
            if not decision:
                reasons = "; ".join(decision.reasons)
                results.append(Refusal(query.id, f"the tool policy denied it: {reasons}"))
                denied = True
                continue
            if connection is None:
                stage = CONNECTION_ERROR
                connection = as_persona(persona)
            stage = TOOL_ERROR
            result, run = _run(connection, query, persona)
            results.append(result)
            if run is not None:
                runs.append(run)
    except Exception as error:
        raised = error
    citations = [r for r in results if isinstance(r, Citation)]
    metrics = list(dict.fromkeys((c.metric, c.version) for c in citations))
    if raised is not None:
        # Written outside the `except`, so an `AuditWriteFailed` carries no context.
        record(audit, persona, session, question, ("failed", stage), metrics, runs, None)
        raise raised
    # A reply no covering run backs is not shown (`agent/integrity.py`).
    withheld = not stopped and integrity.withheld(question, text, citations, queries)
    if withheld:
        text = WITHHELD
    if stopped:
        outcome: tuple[str, str | None] = ("stopped", CAP)
    elif citations and not withheld:
        outcome = ("answered", None)
    elif denied:
        outcome = ("refused", "policy")
    else:
        outcome = ("abstained", None)
    record(audit, persona, session, question, outcome, metrics, runs, text)
    return Answer(
        persona,
        question,
        text,
        tuple(r for r in results if isinstance(r, Citation)),
        tuple(r for r in results if isinstance(r, Refusal)),
    )


def now() -> str:
    """The current instant as the record writes a time: RFC 3339 in UTC, to the microsecond."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record(
    audit: Audit,
    persona: str | None,
    session: str,
    question: Any,
    outcome: tuple[str, str | None],
    metrics: list[tuple[str, int]],
    runs: list[Run],
    text: str | None,
) -> dict[str, Any]:
    """Write one turn's record to `audit.log`, each sealed value under a salt of its own; any
    `Exception` on the way is raised as `AuditWriteFailed`, raised after the failure is handled,
    so it carries neither a cause nor a context, and its message names only the failure's type."""
    failed = ""
    try:
        kept: dict[str, tuple[str, Any]] = {}

        def sealed(path: str, value: Any) -> str:
            salt = new_salt()
            kept[path] = (salt, value)
            return seal(salt, value)

        asker: dict[str, Any] = {"session": session, "turn": 1}
        if persona is not None:
            asker["persona"] = persona
        entries = []
        for index, run in enumerate(runs):
            entry: dict[str, Any] = {"query": {"text": run.text}}
            if run.parameters:
                entry["query"]["parameters"] = sealed(
                    f"runs/{index}/query/parameters", dict(run.parameters)
                )
            entry |= {"as": run.persona, "engine": ENGINE, "job": run.job, "at": run.at}
            result = sealed_result(run.columns, ([cell_text(v) for v in r] for r in run.rows))
            entry["result"] = sealed(f"runs/{index}/result", result)
            entries.append(entry)
        status, reason = outcome
        body: dict[str, Any] = {
            "kind": "answer",
            "at": now(),
            "asker": asker,
            "agent": AGENT,
            "question": sealed("question", question),
            "release": {"commit": audit.release},
            "outcome": status,
            "data": dict(audit.data),
            "metrics": [{"name": name, "version": version} for name, version in metrics],
            "runs": entries,
        }
        if text is not None:
            body["answer"] = sealed("answer", text)
        if reason is not None:
            body["reason"] = reason
        return audit.log.append(body, kept)
    except Exception as error:
        failed = type(error).__name__
    raise AuditWriteFailed(
        f"the turn's audit record could not be written ({failed}), so no answer is given"
    )


def run_metric_query(
    connection: duckdb.DuckDBPyConnection, query: VerifiedQuery
) -> Citation | Refusal:
    """Run a verified query on a cursor of `connection`, its tables resolved in the connection's
    own database and its values bound (`agent.audit.bind`); a text `bind` refuses, an engine error
    or an ambiguous name is a `Refusal`."""
    return _run(connection, query, "")[0]


def _run(
    connection: duckdb.DuckDBPyConnection, query: VerifiedQuery, persona: str
) -> tuple[Citation | Refusal, Run | None]:
    try:
        text, parameters = bind(query.sql, ENGINE)
    except UnboundValue as error:
        return Refusal(query.id, f"the query's values could not be bound: {error}"), None
    cursor = connection.cursor()
    try:
        held = cursor.execute(
            "select table_schema, table_name from information_schema.tables"
            " where table_catalog = system.main.current_database()"
        ).fetchall()
        schemas = sorted({schema for schema, _ in held})
        names = [table for _, table in held]
        if twice := sorted({t for t in names if names.count(t) > 1}):
            return Refusal(query.id, f"tables held in two schemas: {', '.join(twice)}"), None
        if not all(SCHEMA.fullmatch(schema) for schema in schemas):
            return Refusal(query.id, f"a schema name the tool does not quote: {schemas}"), None
        cursor.execute(f"set search_path = '{','.join(schemas)}'")
        started = now()
        result = cursor.execute(text, parameters or [])
        columns = tuple(column[0] for column in result.description or ())
        rows = tuple(tuple(row) for row in result.fetchall())
        (path,) = cursor.execute(
            "select path from duckdb_databases()"
            " where database_name = system.main.current_database()"
        ).fetchone() or (None,)
    except duckdb.Error as error:
        return Refusal(query.id, f"the query failed on the persona's database: {error}"), None
    finally:
        cursor.close()
    written = None
    if path:
        try:
            written = datetime.fromtimestamp(Path(path).stat().st_mtime, UTC)
        except OSError:
            written = None
    citation = Citation(query.id, query.metric, query.version, query.sql, columns, rows, written)
    job = f"{ENGINE}-{uuid.uuid4().hex}"
    return citation, Run(text, parameters, persona, job, started, columns, rows)
