"""The model behind the own agent: one protocol, one factory, one scripted implementation.

The agent (`agent/ask.py`) first asks the model to plan: `plan(turn)` gets a `Turn` holding the
question and the verified queries only, and returns the tool calls the model will make. Then it
shows the model a `Turn` at each step: the question, the verified queries it may name, the
glossary's terms, the column descriptions `ask` is given, the results of the tool calls made so
far, and, when the question is asked in a session (`agent/sessions.py`), that session's earlier
questions and answers. The model answers each such turn with one step: a `ToolCall` naming a tool
(by default the one read-only tool, `run_metric_query`) and what it acts on (for that tool a
verified query by its id), or a `Reply` that ends the turn. A model names a query only by id; the
SQL the tool runs is the text of the verified queries `ask` is given (by default the metric
context's), with no value bound from the question or the model.

The glossary's definitions, the column descriptions, the cells of a result and the session's
earlier questions and answers are text the model reads and the lab does not vouch for: a model may
take an instruction it finds there as its own.
So `ask` refuses a call that is not in the plan, made before that text was shown, and asks the
tool policy before it runs any call; the call past the tool-call limit is refused without
asking it.

`model_from_config(environ, context)` is the one place a model is chosen, by the variable
`LEDGER_LAB_AGENT_MODEL`: unset, empty or `scripted` gives the `ScriptedModel`; any other value
raises `ModelNotConfigured`. A free hosted model, or an adapter over a LangGraph graph or an MCP
client, would be another value here and another class with the same `plan` and `step`.

Stated limits: the scripted model makes no network call and reads no model SDK; it matches a
question to a verified query by exact text only, so a paraphrase gets no tool call; it reads
neither the glossary nor the column descriptions, so a test of what a planted instruction can do
needs a model that obeys what it reads (`tests/agent/test_injection.py` holds one).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

MODEL_VARIABLE = "LEDGER_LAB_AGENT_MODEL"
SCRIPTED = "scripted"
# The one tool `ask` runs: a verified query of the metric context, read-only.
READ_ONLY_TOOL = "run_metric_query"


@dataclass(frozen=True)
class VerifiedQuery:
    """One verified query of the metric context, with its metric's name and version."""

    id: str
    question: str
    metric: str
    version: int
    sql: str


@dataclass(frozen=True)
class GlossaryTerm:
    """One term of the glossary the metric context carries: its id, its name and its definition."""

    id: str
    term: str
    definition: str


@dataclass(frozen=True)
class ColumnDescription:
    """What a column of a table is said to hold, as its documentation words it."""

    table: str
    column: str
    description: str


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
    """A tool call that was not run or failed, and why (`agent/ask.py` lists each check).
    `query` and `tool` are the call's, or the `repr` of either that is not a string."""

    query: str
    reason: str
    tool: str = READ_ONLY_TOOL


@dataclass(frozen=True)
class ToolCall:
    """Call `tool` on `query` as the asking persona: for `run_metric_query`, the default, `query`
    is a verified query's id; for any other tool it is the id of what the tool would act on."""

    query: str
    tool: str = READ_ONLY_TOOL


@dataclass(frozen=True)
class Reply:
    """The model's answer in words; it ends the turn."""

    text: str


@dataclass(frozen=True)
class Exchange:
    """One earlier turn of the session the question is asked in, as its transcript keeps it: the
    turn's number, the question and the text the turn showed."""

    turn: int
    question: str
    answer: str


@dataclass(frozen=True)
class Turn:
    """What the model sees at one step: the question, the verified queries, the results so far,
    the glossary's terms, the column descriptions and the earlier turns of the question's own
    session (`history`); it holds no persona id and no connection."""

    question: str
    queries: tuple[VerifiedQuery, ...]
    results: tuple[Citation | Refusal, ...]
    glossary: tuple[GlossaryTerm, ...] = ()
    columns: tuple[ColumnDescription, ...] = ()
    history: tuple[Exchange, ...] = ()


class Model(Protocol):
    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        """The tool calls the model will make for the turn's question, chosen on a turn that holds
        the question and the verified queries only."""
        ...

    def step(self, turn: Turn) -> ToolCall | Reply: ...


class ModelNotConfigured(ValueError):
    """`LEDGER_LAB_AGENT_MODEL` names no model this module builds."""


class ScriptedModel:
    """Replays a fixed script: it plans the query the script maps the question to, if any; on the
    first step, that query, or a reply that no verified query answers it; once a tool has
    answered, a reply built from the results."""

    def __init__(self, script: Mapping[str, str]) -> None:
        self._script = dict(script)

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        query = self._script.get(turn.question)
        return () if query is None else (ToolCall(query),)

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
