"""The own agent's spending caps (`agent/limits.py`), on the local engine over the sample build.

Each test asks through the `as_persona` fixture (`tests/conftest.py`) with a model of its own, so
nothing here calls a model or GCP. A join of the payments with themselves is the runaway join of the
denial-of-wallet cases (`agent/evals/adversarial.yaml`): every payment against every other.
"""

import dataclasses
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import duckdb
import pytest

from agent.ask import (
    MAX_STEPS,
    MAX_TOOL_CALLS,
    TURN_CAPPED,
    Audit,
    ask,
    load_context,
    run_metric_query,
)
from agent.audit import AuditWriteFailed, FileLog, read_log
from agent.limits import (
    BYTES_CAP,
    TIME_CAP,
    TURN_CAP,
    Limits,
    estimated_rows,
    load_limits,
    rows_held,
    turns_today,
)
from agent.model import Refusal, Reply, ToolCall, Turn, VerifiedQuery, model_from_config

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
# A reader of every entity's payments, as in the denial-of-wallet cases.
READER = "group-controller"
OTHER = "accountant-chicago"
UK = "accountant-uk-transport"
QUESTION = "What share of the City of Chicago's served payment rows repeat another served row?"

RUNAWAY = VerifiedQuery(
    "pairs-within-one-unit",
    "Which payments are within one unit of each other?",
    "invented_metric",
    1,
    "select count(*) as pairs from payments as a join payments as b"
    " on abs(a.amount - b.amount) < 1",
)
# Five copies of the payments paired with each other: more rows than any machine counts in seconds.
LONG = VerifiedQuery(
    "five-way-pairs",
    "How many ways can five payments be picked?",
    "invented_metric",
    1,
    "select sum(a.amount + b.amount + c.amount + d.amount + e.amount) as total"
    " from payments as a, payments as b, payments as c, payments as d, payments as e",
)
COUNT = VerifiedQuery(
    "payment-total",
    "What do the payments add up to?",
    "invented_metric",
    1,
    "select sum(amount) as total from payments",
)
AGAIN = dataclasses.replace(COUNT, id="payment-total-again", question="And what again?")


class Calls:
    """A model that plans `calls` and makes them in order, then replies; it counts its calls."""

    def __init__(self, *calls: str) -> None:
        self.calls = [ToolCall(call) for call in calls]
        self.asked = 0

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        self.asked += 1
        return tuple(self.calls)

    def step(self, turn: Turn) -> ToolCall | Reply:
        self.asked += 1
        made = len(turn.results)
        return self.calls[made] if made < len(self.calls) else Reply("done")


class Planned(Calls):
    """A model that plans `calls` and then makes the first of them on every step."""

    def step(self, turn: Turn) -> ToolCall | Reply:
        self.asked += 1
        return self.calls[0]


def last(audit: Audit) -> dict[str, Any]:
    assert isinstance(audit.log, FileLog)
    return read_log(audit.log.path.read_bytes())[-1]


def cap_of(connection: duckdb.DuckDBPyConnection, limits: Limits) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute("set search_path = 'group_close'")
        return limits.product_passes_per_turn * rows_held(cursor)
    finally:
        cursor.close()


@pytest.fixture(params=["local", "bigquery"])
def engine(request: pytest.FixtureRequest) -> str:
    """The engine a cap is proven on: the local engine, or BigQuery, skipped before any build since
    the own agent has no BigQuery mode, so no job of it carries `maximum_bytes_billed` yet; nothing
    here calls GCP."""
    if request.param == "bigquery":
        pytest.skip(
            "the agent's BigQuery mode is not built: no job of it carries maximum_bytes_billed"
        )
    return request.param


def test_runaway_join_stops_at_the_cap(as_persona: AsPersona, audit: Audit, engine: str) -> None:
    """Pairing every payment with every other is planned past the turn's row cap, so it is not
    run: the turn is stopped at the byte cap's stand-in, with no run and no rows."""
    limits = load_limits()
    connection = as_persona(READER)
    cursor = connection.cursor()
    cursor.execute("set search_path = 'group_close'")
    assert estimated_rows(cursor, RUNAWAY.sql, None) > cap_of(connection, limits)
    cursor.close()
    model = Calls(RUNAWAY.id)
    started = time.monotonic()
    answer = ask(
        READER,
        RUNAWAY.question,
        as_persona=lambda _: connection,
        audit=audit,
        model=model,
        queries={RUNAWAY.id: RUNAWAY},
    )
    assert time.monotonic() - started < limits.seconds_per_query
    assert answer.citations == ()
    (refusal,) = answer.refusals
    assert refusal.query == RUNAWAY.id
    assert "rows" in refusal.reason
    assert answer.text.startswith("A query reached a spending cap")
    assert model.asked == 2  # the plan and the one step that made the call
    record = last(audit)
    assert (record["outcome"], record["reason"]) == ("stopped", BYTES_CAP)
    assert record["runs"] == []


def test_a_query_past_the_time_cap_is_interrupted(as_persona: AsPersona, audit: Audit) -> None:
    """With the row cap out of reach, a query that runs on is interrupted at the time cap."""
    limits = dataclasses.replace(
        load_limits(), product_passes_per_turn=10**15, seconds_per_query=0.2
    )
    started = time.monotonic()
    answer = ask(
        READER,
        LONG.question,
        as_persona=as_persona,
        audit=audit,
        model=Calls(LONG.id),
        queries={LONG.id: LONG},
        limits=limits,
    )
    assert time.monotonic() - started < 10
    assert answer.citations == ()
    (refusal,) = answer.refusals
    assert "second cap" in refusal.reason
    record = last(audit)
    assert (record["outcome"], record["reason"]) == ("stopped", TIME_CAP)
    assert record["runs"] == []


def test_the_rows_left_shrink_with_each_query(as_persona: AsPersona, audit: Audit) -> None:
    """The row cap is the turn's: a query that fits alone is refused once the turn's earlier
    queries have used the rows it needs, and the run before it stays cited and recorded."""
    connection = as_persona(READER)
    cursor = connection.cursor()
    cursor.execute("set search_path = 'group_close'")
    read = estimated_rows(cursor, COUNT.sql, None)
    held = rows_held(cursor)
    cursor.close()
    # The fewest passes that hold one sum of the payments, and not two.
    passes = -(-read // held)
    assert read <= passes * held < 2 * read
    limits = dataclasses.replace(load_limits(), product_passes_per_turn=passes)
    answer = ask(
        READER,
        COUNT.question,
        as_persona=lambda _: connection,
        audit=audit,
        model=Calls(COUNT.id, AGAIN.id),
        queries={COUNT.id: COUNT, AGAIN.id: AGAIN},
        limits=limits,
    )
    assert [c.query for c in answer.citations] == [COUNT.id]
    assert [r.query for r in answer.refusals] == [AGAIN.id]
    record = last(audit)
    assert (record["outcome"], record["reason"]) == ("stopped", BYTES_CAP)
    assert len(record["runs"]) == 1


def test_the_refusal_cap_stops_at_a_query_cap(as_persona: AsPersona, audit: Audit) -> None:
    """Planned calls run after the refusal limit stop at a query cap like the model's own."""
    model = Planned("not-a-query", RUNAWAY.id)
    answer = ask(
        READER,
        RUNAWAY.question,
        as_persona=as_persona,
        audit=audit,
        model=model,
        queries={RUNAWAY.id: RUNAWAY},
    )
    assert answer.citations == ()
    assert answer.refusals[-1].query == RUNAWAY.id
    record = last(audit)
    assert (record["outcome"], record["reason"]) == ("stopped", BYTES_CAP)


@pytest.mark.parametrize("who", [READER, OTHER, UK])
def test_every_verified_query_fits_the_caps(as_persona: AsPersona, who: str) -> None:
    """Each verified query of the metric context, alone, runs within one turn's row and time caps
    on the sample, as a reader of every entity's figures and as each entity's accountant; a query
    the persona's database cannot run (a column it does not hold) is refused by the engine, not by
    a cap."""
    limits = load_limits()
    connection = as_persona(who)
    for query in load_context().values():
        result = run_metric_query(connection, query, limits)
        if isinstance(result, Refusal):
            assert result.reason.startswith("the query failed on the persona's database"), (
                query.id,
                result.reason,
            )


def test_a_turn_takes_no_more_model_calls_or_queries_than_the_ceilings() -> None:
    """A turn's plan and its at most `MAX_STEPS` steps, which `tests/agent/test_ask.py` counts on
    a model that never replies, and its at most `MAX_TOOL_CALLS` queries fit the ceilings."""
    limits = load_limits()
    assert limits.model_calls_per_turn >= MAX_STEPS + 1  # the plan and the steps
    assert limits.queries_per_turn >= MAX_TOOL_CALLS


def test_a_persons_turns_stop_at_the_daily_cap(as_persona: AsPersona, audit: Audit) -> None:
    """Past a person's turns in 24 hours, a turn asks no model and opens no connection, and is
    recorded stopped at the turn cap; another persona still asks."""
    limits = dataclasses.replace(load_limits(), turns_per_person_per_day=2)
    opened: list[str] = []

    def connect(persona: str) -> duckdb.DuckDBPyConnection:
        opened.append(persona)
        return as_persona(persona)

    scripted = model_from_config({}, load_context())
    for _ in range(2):
        answer = ask(
            OTHER, QUESTION, as_persona=connect, audit=audit, model=scripted, limits=limits
        )
        assert answer.citations
    model = Calls("duplicate-payment-rate")
    for _ in range(2):
        answer = ask(OTHER, QUESTION, as_persona=connect, audit=audit, model=model, limits=limits)
        assert answer.text == TURN_CAPPED
        assert (answer.citations, answer.refusals) == ((), ())
        record = last(audit)
        assert (record["outcome"], record["reason"]) == ("stopped", TURN_CAP)
        assert record["runs"] == []
    assert model.asked == 0
    assert opened == [OTHER, OTHER]
    other = ask(UK, QUESTION, as_persona=connect, audit=audit, model=scripted, limits=limits)
    assert other.citations


class Slow(Calls):
    """A model whose plan takes long enough for the other turns asked with it to start."""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        time.sleep(0.3)
        return super().plan(turn)


def test_turns_asked_at_once_are_held_to_the_daily_cap(as_persona: AsPersona, audit: Audit) -> None:
    """One persona's turns asked at the same time run one at a time, so each counts the ones
    before it: with a cap of one, one turn runs and the rest are stopped at the turn cap."""
    limits = dataclasses.replace(load_limits(), turns_per_person_per_day=1)
    lock = threading.Lock()

    def connect(persona: str) -> duckdb.DuckDBPyConnection:
        with lock:
            return as_persona(persona)

    model = Slow("duplicate-payment-rate")
    raised: list[BaseException] = []

    def turn() -> None:
        try:
            ask(OTHER, QUESTION, as_persona=connect, audit=audit, model=model, limits=limits)
        except BaseException as error:  # noqa: BLE001 - reported by the assertion below
            raised.append(error)

    threads = [threading.Thread(target=turn) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert raised == []
    assert isinstance(audit.log, FileLog)
    reasons = [r.get("reason") for r in read_log(audit.log.path.read_bytes())]
    assert sorted(reasons, key=str) == sorted([None] + [TURN_CAP] * 4, key=str)


def test_a_log_that_cannot_be_read_takes_no_turn(as_persona: AsPersona, audit: Audit) -> None:
    """A torn log raises before the model is asked or a connection is opened, and is left as it
    was."""
    assert isinstance(audit.log, FileLog)
    audit.log.path.write_bytes(b'{"seq":0')
    model = Calls("duplicate-payment-rate")
    opened: list[str] = []

    def connect(persona: str) -> duckdb.DuckDBPyConnection:
        opened.append(persona)
        return as_persona(persona)

    with pytest.raises(AuditWriteFailed, match="could not be read"):
        ask(OTHER, QUESTION, as_persona=connect, audit=audit, model=model)
    assert (model.asked, opened) == (0, [])
    assert audit.log.path.read_bytes() == b'{"seq":0'


def test_no_planned_query_runs_after_one_stopped_at_a_cap(
    as_persona: AsPersona, audit: Audit
) -> None:
    """On the refusal limit's path, a planned query after the one stopped at the row cap is not
    run."""
    answer = ask(
        READER,
        RUNAWAY.question,
        as_persona=as_persona,
        audit=audit,
        model=Planned("not-a-query", RUNAWAY.id, COUNT.id),
        queries={RUNAWAY.id: RUNAWAY, COUNT.id: COUNT},
    )
    assert answer.citations == ()
    record = last(audit)
    assert (record["outcome"], record["reason"]) == ("stopped", BYTES_CAP)
    assert record["runs"] == []


def test_the_time_cap_is_held_within_a_minute() -> None:
    """The committed time cap is a cap: at most a minute a query."""
    assert 0 < load_limits().seconds_per_query <= 60


def answer_at(persona: str | None, at: datetime, reason: str | None = None) -> dict[str, Any]:
    asker: dict[str, Any] = {"session": "s", "turn": 1}
    if persona is not None:
        asker["persona"] = persona
    record: dict[str, Any] = {
        "kind": "answer",
        "asker": asker,
        "at": at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    if reason is not None:
        record["reason"] = reason
    return record


@pytest.mark.parametrize(
    ("records", "counted"),
    [
        ([], 0),
        ([answer_at(OTHER, datetime(2026, 1, 2, 0, 0, tzinfo=UTC))], 1),
        ([answer_at(OTHER, datetime(2026, 1, 1, 0, 0, tzinfo=UTC))], 1),
        ([answer_at(OTHER, datetime(2025, 12, 31, 23, 59, tzinfo=UTC))], 0),
        ([answer_at(OTHER, datetime(2026, 1, 2, 0, 1, tzinfo=UTC))], 0),
        ([answer_at(READER, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))], 0),
        ([answer_at(None, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))], 0),
        ([answer_at(OTHER, datetime(2026, 1, 1, 12, 0, tzinfo=UTC), TURN_CAP)], 0),
        ([answer_at(OTHER, datetime(2026, 1, 1, 12, 0, tzinfo=UTC), BYTES_CAP)], 1),
        ([{**answer_at(OTHER, datetime(2026, 1, 1, 12, 0, tzinfo=UTC)), "kind": "proposal"}], 0),
    ],
)
def test_turns_are_counted_over_the_24_hours_before_the_turn(
    records: Sequence[dict[str, Any]], counted: int
) -> None:
    at = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    assert turns_today(records, OTHER, at) == counted


def test_a_pairing_join_is_estimated_at_its_inputs_product() -> None:
    """The engine estimates the join's rows out at about one side's; the work counted is every
    row against every other."""
    db = duckdb.connect()
    db.execute("create table p as select range as id, range * 1.5 as amount from range(1000)")
    read = estimated_rows(
        db, "select count(*) from p as a join p as b on abs(a.amount - b.amount) < 1", None
    )
    assert read >= 1000 * 1000
    assert estimated_rows(db, "select count(*) from p", None) < 2 * 1000
    assert rows_held(db) == 1000
    db.close()


def test_a_log_not_yet_written_holds_no_turns(audit: Audit) -> None:
    assert isinstance(audit.log, FileLog)
    assert not audit.log.path.exists()
    assert audit.log.records() == []
