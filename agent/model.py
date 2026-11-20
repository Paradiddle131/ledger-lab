"""The model behind the own agent: one protocol, one factory, one scripted implementation.

The agent (`agent/ask.py`) shows the model a `Turn`: the question, the verified queries of the
metric context it may name, and the results of the tool calls made so far. The model answers each
turn with one step: a `ToolCall` naming a verified query by its id, or a `Reply` that ends the
turn. A model names a query only by id; the SQL the tool runs comes from the metric context, not
from the model or the question.

`model_from_config(environ, context)` is the one place a model is chosen, by the variable
`LEDGER_LAB_AGENT_MODEL`: unset, empty or `scripted` gives the `ScriptedModel`; any other value
raises `ModelNotConfigured`. A free hosted model, or an adapter over a LangGraph graph or an MCP
client, would be another value here and another class with the same `step`.

Stated limits: the scripted model makes no network call and reads no model SDK; it matches a
question to a verified query by exact text only, so a paraphrase gets no tool call.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

MODEL_VARIABLE = "LEDGER_LAB_AGENT_MODEL"
SCRIPTED = "scripted"


@dataclass(frozen=True)
class VerifiedQuery:
    """One verified query of the metric context, with its metric's name and version."""

    id: str
    question: str
    metric: str
    version: int
    sql: str


@dataclass(frozen=True)
class Citation:
    """A verified query the tool ran as the asking persona, and what it returned.

    `freshness` is the modification time, in UTC, of the database file the persona's connection
    reads, that is when that database was last written; it is None when the connection reads no
    file or the file's time cannot be read. It says nothing about how recent the source data under
    that database is.
    """

    query: str
    metric: str
    version: int
    sql: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    freshness: datetime | None


@dataclass(frozen=True)
class Refusal:
    """A tool call that returned no rows, and why: the context holds no such query, the tool
    policy denied it, the persona's database holds a table name in two schemas or a schema name
    the tool does not quote, the engine raised an error, or the model went past the tool-call
    limit."""

    query: str
    reason: str


@dataclass(frozen=True)
class ToolCall:
    """Run the verified query of this id as the asking persona."""

    query: str


@dataclass(frozen=True)
class Reply:
    """The model's answer in words; it ends the turn."""

    text: str


@dataclass(frozen=True)
class Turn:
    """What the model sees at one step: the question, the verified queries and the results so far;
    it holds no persona id and no connection."""

    question: str
    queries: tuple[VerifiedQuery, ...]
    results: tuple[Citation | Refusal, ...]


class Model(Protocol):
    def step(self, turn: Turn) -> ToolCall | Reply: ...


class ModelNotConfigured(ValueError):
    """`LEDGER_LAB_AGENT_MODEL` names no model this module builds."""


class ScriptedModel:
    """Replays a fixed script: on the first step, the query the script maps the question to, or a
    reply that no verified query answers it; once a tool has answered, a reply built from the
    results."""

    def __init__(self, script: Mapping[str, str]) -> None:
        self._script = dict(script)

    def step(self, turn: Turn) -> ToolCall | Reply:
        if not turn.results:
            query = self._script.get(turn.question)
            if query is None:
                return Reply("No verified query answers this question.")
            return ToolCall(query)
        return Reply("\n".join(_said(result) for result in turn.results))


def _said(result: Citation | Refusal) -> str:
    if isinstance(result, Citation):
        return (
            f"{result.metric} (version {result.version}), by the verified query {result.query}: "
            f"{', '.join(result.columns)} = {list(result.rows)}"
        )
    return f"The verified query {result.query} gave no answer: {result.reason}"


def model_from_config(environ: Mapping[str, str], queries: Mapping[str, VerifiedQuery]) -> Model:
    """The model `LEDGER_LAB_AGENT_MODEL` names; the scripted one maps each verified query's
    question to its id, and raises `ValueError` when two queries share a question."""
    name = environ.get(MODEL_VARIABLE, "") or SCRIPTED
    if name != SCRIPTED:
        raise ModelNotConfigured(f"{MODEL_VARIABLE}={name!r}: the only model built is {SCRIPTED!r}")
    script: dict[str, str] = {}
    for query in queries.values():
        if query.question in script:
            raise ValueError(f"{query.id} and {script[query.question]} share a question")
        script[query.question] = query.id
    return ScriptedModel(script)
