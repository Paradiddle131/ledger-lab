"""The own agent's spending caps, read from the cost ceilings of the adversarial cases
(`agent/evals/adversarial.yaml`), and the checks `agent/ask.py` runs them with.

What is capped, and where:
- a turn's model calls: `agent/ask.py` asks the model for one plan and at most its `MAX_STEPS`
  steps, which a test holds within `model_calls_per_turn`;
- a turn's queries: `agent/ask.py`'s `MAX_TOOL_CALLS`, which a test holds within
  `queries_per_turn`;
- a person's turns: a persona's turns are held one at a time (`AuditLog.turn`), and a persona whose
  turns in the 24 hours before a new one (`turns_today`, counted in the audit log) reach
  `turns_per_person_per_day` is answered with no model call and no query;
- the rows a turn's queries read, on the local engine, as the stand-in for bytes billed: before a
  query runs, `estimated_rows` reads the engine's plan, and a query whose estimate, added to what
  the turn's earlier queries were estimated to read, passes `product_passes_per_turn` times the
  rows of the persona's tables (`rows_held`) is not run;
- a query's run time, on the local engine: `run_capped` interrupts a query still running after
  `seconds_per_query`.

Stated limits:
- the row cap reads the engine's estimates, not the rows a query reads as it runs: a query whose
  plan estimates fewer rows than it reads runs, and is interrupted only if it is still running at
  `seconds_per_query`;
- `rows_held` counts the rows of the database's base tables by the engine's own estimate, so a view
  adds nothing to it;
- the BigQuery mode of the own agent is not built, so nothing here sets `maximum_bytes_billed` on
  a job; `bytes_billed_per_turn` is the ceiling that mode is to set;
- the 24 hours are counted back from the new turn's start on the records' own times, so a log whose
  times were written wrong counts wrong, and a log removed or replaced counts from what it holds;
- the turn lock is `FileLog`'s `flock` on a file per persona beside the log, so it holds turns
  apart only among processes that share that folder.
"""

import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import yaml

from agent.audit import instant

CASES = Path(__file__).resolve().parent / "evals" / "adversarial.yaml"

# The reasons the audit record's schema gives a turn stopped at each cap.
BYTES_CAP = "query-bytes-cap"
TIME_CAP = "query-time-cap"
TURN_CAP = "turn-cap"

# Join operators whose work is every row of one side against every row of the other: their estimate
# is at least the product of their inputs' estimates, whatever rows they are estimated to give.
PAIRING = frozenset(
    {"CROSS_PRODUCT", "NESTED_LOOP_JOIN", "BLOCKWISE_NL_JOIN", "PIECEWISE_MERGE_JOIN"}
)
DAY = timedelta(hours=24)


@dataclass(frozen=True)
class Limits:
    """The cost ceilings of `agent/evals/adversarial.yaml`, by their names there."""

    bytes_billed_per_turn: int
    product_passes_per_turn: int
    queries_per_turn: int
    model_calls_per_turn: int
    turns_per_person_per_day: int
    seconds_per_query: float


def load_limits(path: Path = CASES) -> Limits:
    """The cost ceilings as the cases file holds them; its schema is checked by `tests/agent`."""
    ceilings = yaml.safe_load(path.read_text(encoding="utf-8"))["cost_ceilings"]
    return Limits(**ceilings)


class CapReached(Exception):
    """A query that was not run, or was stopped, at a cap; `reason` is the record's."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def rows_held(cursor: duckdb.DuckDBPyConnection) -> int:
    """The rows of the base tables of the cursor's current database, by the engine's estimate."""
    (held,) = cursor.execute(
        "select coalesce(sum(estimated_size), 0) from duckdb_tables()"
        " where database_name = system.main.current_database()"
    ).fetchone() or (0,)
    return int(held)


def _estimate(node: Mapping[str, Any]) -> tuple[int, int]:
    """A plan node's rows out and the rows its subtree is estimated to read: each node counts its
    estimate, a pairing join at least the product of its inputs', and a node with no estimate the
    largest of its inputs'."""
    children = [_estimate(child) for child in node.get("children", ())]
    given = str((node.get("extra_info") or {}).get("Estimated Cardinality", "")).strip()
    rows = int(given) if given.isdigit() else max((out for out, _ in children), default=0)
    work = rows
    if node.get("name") in PAIRING and children:
        product = 1
        for out, _ in children:
            product *= out
        work = max(rows, product)
    return rows, work + sum(read for _, read in children)


def estimated_rows(
    cursor: duckdb.DuckDBPyConnection, sql: str, parameters: Mapping[str, Any] | None
) -> int:
    """The rows the engine's plan for `sql` is estimated to read, summed over its operators."""
    plan = cursor.execute(f"explain (format json) {sql}", parameters or []).fetchall()
    nodes: Iterable[Mapping[str, Any]] = json.loads(plan[0][1])
    return sum(_estimate(node)[1] for node in nodes)


def run_capped(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: Mapping[str, Any] | None,
    seconds: float,
) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
    """Run `sql` and fetch its rows, interrupting it once it has run `seconds`; a query the timer
    fired on raises `CapReached` with the time cap, even when it returned as the timer fired, so
    the cursor, which may hold the interrupt, is used for nothing after it."""
    lock = threading.Lock()
    fired = threading.Event()
    finished = False

    def stop() -> None:
        with lock:
            if not finished:
                fired.set()
                cursor.interrupt()

    timer = threading.Timer(seconds, stop)
    timer.start()
    try:
        result = cursor.execute(sql, parameters or [])
        columns = tuple(column[0] for column in result.description or ())
        rows = tuple(tuple(row) for row in result.fetchall())
    except duckdb.Error:
        if fired.is_set():
            raise CapReached(TIME_CAP, f"the query ran past the {seconds:g} second cap") from None
        raise
    finally:
        with lock:
            finished = True
        timer.cancel()
    if fired.is_set():
        raise CapReached(TIME_CAP, f"the query ran past the {seconds:g} second cap")
    return columns, rows


def turns_today(records: Sequence[Mapping[str, Any]], persona: str, at: datetime) -> int:
    """The turns `persona` asked in the 24 hours before `at`: the log's answer records naming it as
    asker, those stopped at the turn cap left out, since they ran nothing."""
    since = at - DAY
    return sum(
        1
        for record in records
        if record.get("kind") == "answer"
        and (record.get("asker") or {}).get("persona") == persona
        and record.get("reason") != TURN_CAP
        and since <= instant(record.get("at")) <= at
    )
