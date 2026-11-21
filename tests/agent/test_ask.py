"""The own agent's question-to-answer seam, `agent.ask.ask`, on the scripted model.

Each test asks through the `as_persona` fixture (`tests/conftest.py`), so a tool runs on the
database compiled for the asking persona over the sample build; the model is the scripted one of
`agent/model.py`, so nothing here calls a model or GCP. A recording wrapper around `as_persona` and
around the model shows which personas a connection was opened for and whether the model was asked.
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from agent import ask as ask_module
from agent.ask import Answer, NoPersonaError, ask, load_context, load_glossary, run_metric_query
from agent.model import (
    MODEL_VARIABLE,
    Model,
    ModelNotConfigured,
    Refusal,
    Reply,
    ScriptedModel,
    ToolCall,
    Turn,
    VerifiedQuery,
    model_from_config,
)
from agent.policy.tools import Decision, Entity

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
# A verified question over the payments of one entity, Chicago: a persona reading Chicago's rows
# gets a share, one reading only another entity's rows gets the empty sum's null.
QUESTION = "What share of the City of Chicago's served payment rows repeat another served row?"
QUERY = "duplicate-payment-rate"
CHICAGO = "accountant-chicago"
UK = "accountant-uk-transport"
# A persona the tool policy (`agent/policy/tools.cedar`) does not permit to run a metric's query.
DENIED = "agent-operator"
# A write time set on the persona's database file, long before any test runs.
WRITTEN = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
# The tool-call limit `agent/ask.py` states, written out here so a raised limit fails a test.
LIMIT = 3


class Recorder:
    """`as_persona` and a model, each recording its calls."""

    def __init__(self, as_persona: AsPersona, model: Any) -> None:
        self.opened: list[str] = []
        self.turns: list[Turn] = []
        self._as_persona = as_persona
        self._model = model

    def connect(self, persona: str) -> duckdb.DuckDBPyConnection:
        self.opened.append(persona)
        return self._as_persona(persona)

    def step(self, turn: Turn) -> ToolCall | Reply:
        self.turns.append(turn)
        return self._model.step(turn)


@pytest.fixture(params=["local", "bigquery"])
def engine(request: pytest.FixtureRequest) -> AsPersona:
    """The connection-maker the agent asks through: `as_persona` on the local engine, or BigQuery,
    skipped before any build, so the controls test sees it skip quickly; nothing here calls GCP."""
    if request.param == "bigquery":
        pytest.skip(
            "the agent's BigQuery mode is not built: the persona's warehouse grants are not"
            " exercised"
        )
    return request.getfixturevalue("as_persona")


@pytest.fixture
def scripted() -> Model:
    return model_from_config({}, load_context())


def asked(recorder: Recorder, persona: Any, question: str = QUESTION) -> Answer:
    return ask(persona, question, as_persona=recorder.connect, model=recorder)


def values(answer: Answer) -> list[tuple[Any, ...]]:
    (citation,) = answer.citations
    return list(citation.rows)


def test_two_personas_asking_the_same_question_get_different_rows(
    engine: AsPersona, scripted: Model
) -> None:
    """The local engine's proof that a query runs as the asker: Chicago's accountant gets a share
    of Chicago's rows, the UK accountant, whose database holds no Chicago row, the null of an empty
    sum; each connection was opened for the persona asking, and for no other."""
    recorder = Recorder(engine, scripted)
    chicago = asked(recorder, CHICAGO)
    uk = asked(recorder, UK)
    assert recorder.opened == [CHICAGO, UK]
    assert values(chicago) != values(uk)
    ((share,),) = values(chicago)
    assert isinstance(share, float) and 0 <= share <= 1
    assert values(uk) == [(None,)]
    assert chicago.persona == CHICAGO and uk.persona == UK


# What stands for no persona: none at all, an empty or blank id, a value that is no string, and
# strings that are not exactly a persona id of `access/personas.yaml` (a zero-width space too).
MISSING: list[Any] = [None, "", "   ", 7, "nobody", f"{CHICAGO} ", "\u200b"]


def test_a_question_without_a_persona_gets_no_answer(engine: AsPersona, scripted: Model) -> None:
    """No persona raises before the model is asked or a connection is opened."""
    for missing in MISSING:
        recorder = Recorder(engine, scripted)
        with pytest.raises(NoPersonaError):
            asked(recorder, missing)
        assert (missing, recorder.opened, recorder.turns) == (missing, [], [])


def test_a_temporary_table_on_the_persona_connection_does_not_shadow_its_rows(
    as_persona: AsPersona, scripted: Model
) -> None:
    """A temporary `payments` on the connection `as_persona` returns would win an unqualified name
    there; the tool runs on a cursor of its own, which does not see it."""
    plain = values(asked(Recorder(as_persona, scripted), CHICAGO))

    def shadowed(persona: str) -> duckdb.DuckDBPyConnection:
        connection = as_persona(persona)
        connection.execute(
            "create temp table payments as"
            " select 'x' as payment_key, 'chicago' as entity_id, true as is_repeat"
        )
        return connection

    answer = ask(CHICAGO, QUESTION, as_persona=shadowed, model=scripted)
    assert values(answer) == plain != [(1.0,)]


def test_an_answer_cites_the_metric_its_version_the_sql_and_the_freshness(
    as_persona: AsPersona, scripted: Model
) -> None:
    def dated(persona: str) -> duckdb.DuckDBPyConnection:
        connection = as_persona(persona)
        row = connection.execute(
            "select path from duckdb_databases() where database_name = current_database()"
        ).fetchone()
        assert row is not None
        os.utime(row[0], (WRITTEN.timestamp(), WRITTEN.timestamp()))
        return connection

    answer = ask(CHICAGO, QUESTION, as_persona=dated, model=scripted)
    (citation,) = answer.citations
    metric = load_context()[QUERY]
    assert (citation.query, citation.metric, citation.version) == (
        QUERY,
        "duplicate_payment_rate",
        metric.version,
    )
    assert citation.sql == metric.sql
    assert citation.columns == ("duplicate_payment_rate",)
    # The persona's database file's time, set above, is the time the citation carries.
    assert citation.freshness == WRITTEN
    assert answer.refusals == ()
    assert QUERY in answer.text and "version 1" in answer.text


def test_a_persona_the_policy_denies_opens_no_connection(
    as_persona: AsPersona, scripted: Model
) -> None:
    recorder = Recorder(as_persona, scripted)
    answer = asked(recorder, DENIED)
    assert recorder.opened == []
    assert answer.citations == ()
    (refusal,) = answer.refusals
    assert refusal.query == QUERY
    assert "policy" in refusal.reason


def test_a_question_no_verified_query_answers_runs_no_tool(
    as_persona: AsPersona, scripted: Model
) -> None:
    recorder = Recorder(as_persona, scripted)
    answer = asked(recorder, CHICAGO, "Which vendor was paid the most?")
    assert recorder.opened == []
    assert answer.citations == () and answer.refusals == ()
    assert len(recorder.turns) == 1


def test_a_query_the_context_does_not_hold_is_refused(as_persona: AsPersona) -> None:
    recorder = Recorder(as_persona, ScriptedModel({QUESTION: "drop-everything"}))
    answer = asked(recorder, CHICAGO)
    assert recorder.opened == []
    assert answer.citations == ()
    (refusal,) = answer.refusals
    assert refusal.query == "drop-everything" and "metric context" in refusal.reason


def test_a_table_the_persona_lacks_is_refused_not_read_elsewhere(as_persona: AsPersona) -> None:
    """The contract reconciliation is in the warehouse but in no persona's database, so a query
    over it fails on the persona's connection and the answer carries the error, no rows."""
    question = "What share of the City of Chicago's fixed contract awards has been paid?"
    recorder = Recorder(as_persona, model_from_config({}, load_context()))
    answer = asked(recorder, CHICAGO, question)
    assert recorder.opened == [CHICAGO]
    assert answer.citations == ()
    (refusal,) = answer.refusals
    assert refusal.query == "spend-against-award"
    assert "chicago_contract_reconciliation" in refusal.reason


def test_a_model_that_keeps_calling_tools_is_stopped(as_persona: AsPersona) -> None:
    class Looping:
        def step(self, turn: Turn) -> ToolCall | Reply:
            return ToolCall(QUERY)

    recorder = Recorder(as_persona, Looping())
    answer = asked(recorder, CHICAGO)
    assert len(answer.citations) == LIMIT
    assert len(recorder.turns) == LIMIT + 1
    (refusal,) = answer.refusals
    assert refusal.reason.startswith("the model made no reply")


def test_one_ask_opens_one_connection_for_two_tool_calls(as_persona: AsPersona) -> None:
    class Twice:
        def step(self, turn: Turn) -> ToolCall | Reply:
            calls = [QUERY, "duplicate-payment-amount-rate"]
            if len(turn.results) < len(calls):
                return ToolCall(calls[len(turn.results)])
            return Reply("done")

    recorder = Recorder(as_persona, Twice())
    answer = asked(recorder, CHICAGO)
    assert [c.query for c in answer.citations] == [QUERY, "duplicate-payment-amount-rate"]
    assert recorder.opened == [CHICAGO]


def crafted(tmp_path: Path, *statements: str) -> duckdb.DuckDBPyConnection:
    """A database of invented tables only, built by `statements`, opened read-only."""
    path = tmp_path / "crafted.duckdb"
    with duckdb.connect(str(path)) as build:
        for statement in statements:
            build.execute(statement)
    return duckdb.connect(str(path), read_only=True)


INVENTED = VerifiedQuery("invented", "How many?", "invented_metric", 1, "select count(*) from t")


def test_a_table_name_in_two_schemas_is_refused(tmp_path: Path) -> None:
    db = crafted(
        tmp_path,
        "create schema a",
        "create schema b",
        "create table a.t as select 1 as x",
        "create table b.t as select 2 as x",
    )
    result = run_metric_query(db, INVENTED)
    assert result == Refusal("invented", "tables held in two schemas: t")


def test_a_schema_name_the_tool_does_not_quote_is_refused(tmp_path: Path) -> None:
    db = crafted(tmp_path, 'create schema "Odd-Name"', 'create table "Odd-Name".t as select 1 as x')
    result = run_metric_query(db, INVENTED)
    assert isinstance(result, Refusal)
    assert result.reason.startswith("a schema name the tool does not quote")


def test_the_factory_refuses_two_queries_sharing_a_question() -> None:
    twin = VerifiedQuery("twin", INVENTED.question, "invented_metric", 1, INVENTED.sql)
    with pytest.raises(ValueError, match="share a question"):
        model_from_config({}, {"invented": INVENTED, "twin": twin})


def test_the_model_comes_from_configuration() -> None:
    context = load_context()
    assert isinstance(model_from_config({}, context), ScriptedModel)
    assert isinstance(model_from_config({MODEL_VARIABLE: "scripted"}, context), ScriptedModel)
    with pytest.raises(ModelNotConfigured):
        model_from_config({MODEL_VARIABLE: "some-hosted-model"}, context)


class Calls:
    """A model that makes the given tool calls in order, then replies."""

    def __init__(self, *calls: ToolCall) -> None:
        self._calls = calls

    def step(self, turn: Turn) -> ToolCall | Reply:
        if len(turn.results) < len(self._calls):
            return self._calls[len(turn.results)]
        return Reply("done")


# Tools the tool policy's schema does not declare (`agent/policy/tools.cedarschema`), and the
# write it declares and no policy permits.
UNLISTED = [ToolCall("example.invalid", "fetch_url"), ToolCall("select 1", "run_sql")]
WRITE = ToolCall("chicago", "propose_entry")


@pytest.mark.parametrize("call", [*UNLISTED, WRITE], ids=lambda call: call.tool)
def test_an_unlisted_or_write_tool_is_denied_through_ask(
    as_persona: AsPersona, call: ToolCall
) -> None:
    """A tool call the model makes for any tool but the read-only one comes back a refusal from
    the tool policy, with no connection opened and nothing run."""
    recorder = Recorder(as_persona, Calls(call))
    answer = asked(recorder, CHICAGO)
    assert recorder.opened == []
    assert answer.citations == ()
    (refusal,) = answer.refusals
    assert (refusal.tool, refusal.query) == (call.tool, call.query)
    assert refusal.reason.startswith("the tool policy denied it: ")
    if call is WRITE:
        assert "no policy permits propose_entry" in refusal.reason
    else:
        assert "does not exist in the supplied schema" in refusal.reason


def test_each_tool_call_naming_a_tool_or_a_verified_query_is_put_to_the_policy(
    as_persona: AsPersona, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ask` asks `authorize` once per such call, naming the tool the model asked for; the request
    for the write tool names the ledger it would act on."""
    asked_for: list[tuple[str, Entity]] = []
    authorize = ask_module.authorize

    def recording(
        principal: Entity, action: str, resource: Entity, context: dict[str, object]
    ) -> Decision:
        asked_for.append((action, resource))
        return authorize(principal, action, resource, context)

    monkeypatch.setattr(ask_module, "authorize", recording)
    recorder = Recorder(as_persona, Calls(UNLISTED[0], WRITE, ToolCall(QUERY)))
    answer = asked(recorder, CHICAGO)
    assert asked_for == [
        ("fetch_url", Entity("Metric", "example.invalid")),
        ("propose_entry", Entity("Ledger", "chicago")),
        ("run_metric_query", Entity("Metric", "duplicate_payment_rate")),
    ]
    assert [c.query for c in answer.citations] == [QUERY]
    assert recorder.opened == [CHICAGO]


def test_a_tool_the_policy_allows_but_ask_does_not_run_is_refused(
    as_persona: AsPersona, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Were a policy to permit the write tool, `ask` would still run nothing: it runs only
    `run_metric_query`."""
    monkeypatch.setattr(
        ask_module, "authorize", lambda *_: Decision(allowed=True, policies=("planted",))
    )
    recorder = Recorder(as_persona, Calls(WRITE))
    answer = asked(recorder, CHICAGO)
    assert recorder.opened == []
    assert answer.citations == ()
    assert answer.refusals == (
        Refusal("chicago", "ask runs no tool named propose_entry", WRITE.tool),
    )


def test_the_model_is_shown_the_glossary_of_the_metric_context(
    as_persona: AsPersona, scripted: Model
) -> None:
    recorder = Recorder(as_persona, scripted)
    asked(recorder, CHICAGO)
    glossary = load_glossary()
    assert {term.id for term in glossary} >= {"duplicate-payment-rate", "constant-currency"}
    assert all(turn.glossary == glossary for turn in recorder.turns)
    assert all(turn.columns == () for turn in recorder.turns)


# Values that are not strings, as a model's tool or query.
NOT_STRINGS: list[Any] = [["run_metric_query"], None, 7]


@pytest.mark.parametrize(
    "call",
    [ToolCall(QUERY, tool) for tool in NOT_STRINGS] + [ToolCall(query) for query in NOT_STRINGS],
    ids=[f"tool={t!r}" for t in NOT_STRINGS] + [f"query={q!r}" for q in NOT_STRINGS],
)
def test_a_tool_call_whose_tool_or_query_is_no_string_is_refused_and_the_turn_goes_on(
    as_persona: AsPersona, call: ToolCall
) -> None:
    """A call whose tool or query is not a string is a `Refusal` holding the `repr` of each such
    value, with no connection opened; the model's next call still runs."""
    recorder = Recorder(as_persona, Calls(call, ToolCall(QUERY)))
    answer = asked(recorder, CHICAGO)
    query = call.query if isinstance(call.query, str) else repr(call.query)
    tool = call.tool if isinstance(call.tool, str) else repr(call.tool)
    assert answer.refusals == (Refusal(query, "the tool or the query is not a string", tool),)
    assert len(recorder.turns) == 3
    assert recorder.turns[1].results == answer.refusals
    assert [c.query for c in answer.citations] == [QUERY]
    assert recorder.opened == [CHICAGO]


def test_a_call_past_the_limit_whose_query_is_no_string_is_refused_with_its_repr(
    as_persona: AsPersona,
) -> None:
    class Looping:
        def step(self, turn: Turn) -> ToolCall | Reply:
            return ToolCall(["a"])  # type: ignore[arg-type]

    answer = asked(Recorder(as_persona, Looping()), CHICAGO)
    assert len(answer.refusals) == LIMIT + 1
    assert {(r.query, r.tool) for r in answer.refusals} == {("['a']", "run_metric_query")}
    assert answer.refusals[-1].reason.startswith("the model made no reply")
