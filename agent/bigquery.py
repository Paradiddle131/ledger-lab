"""The own agent's BigQuery mode: a connection that carries the asking persona's own token, and the
one read-only tool run on it, so the warehouse's grants, row access policies and policy tags decide
what each query returns.

The connection (`connector(project, client)(persona)`) mints a token for the persona's own service
account, through the agent account when `LEDGER_LAB_AGENT_ACCOUNT` names it
(`access/bigquery.py`'s `chain` and `mint`), every time it is asked for one: no token is kept
between connections, so none crosses from one persona or one turn to another, and a mint that fails
raises, with no fallback to any other identity. The chain's last account is always the persona's,
so no query carries the agent account's own token.

`agent/ask.py` opens one connection per turn and, before the first tool call runs on it, checks
the identity BigQuery runs the connection's jobs as (`verify`): `SELECT SESSION_USER()`, which
reads no table and bills nothing, must answer the asking persona's own account, worked out from
the personas catalogue and the connection's project, not from the connection; any other answer
raises `WrongIdentity` and no query runs. Behind the cloud session's proxy, which replaces the
token on `*.googleapis.com` with the session's own (the agent account's), that check fails, so the
mode runs only off that proxy, on the owner's machine.

A verified query is the metric context's DuckDB SQL turned into BigQuery's by sqlglot (`prepare`),
each table it names placed in the dataset the persona reads it through, as the catalogues lay the
warehouse out (`relations`): a table of one of its purposes' authorised views
(`access/purpose_views.py`), the clear copy's view or the masked copy's (`<table>_masked`) as its
tag treatment gives it, or an aggregate view of a product it reads in aggregate
(`access/thresholds.py`); a table the persona has no relation of, a name two of them share, and a
table the query already qualifies are refused before any job. Its values are then bound as named
parameters (`agent.audit.bind` on BigQuery's dialect).

Every job, the identity check's included, carries:
- `maximumBytesBilled` of the bytes left of the turn's `bytes_billed_per_turn` (`agent/limits.py`),
  never more than the persona queries' own cap (`access/bigquery.py`'s `MAX_BYTES_BILLED`), so a
  turn's jobs together bill at most the turn's bytes; a job BigQuery refuses at that cap
  (`bytesBilledLimitExceeded`, refused before it runs and bills) stops the turn `stopped` with
  `query-bytes-cap`, as the local engine's row cap does; with no bytes left, no job is sent. After a
  job, the bytes it billed (`totalBytesBilled`) are taken from what is left; an answer that does not
  say, and a job that failed in any other way, are counted as billing the whole cap they carried. No
  job is retried, and a query refused at the cap is not read again in parts (`in_parts` is not
  used): the turn stops;
- `jobTimeoutMs` and the wait of `seconds_per_query`; an answer not finished by then stops the turn
  `stopped` with `query-time-cap`;
- the turn's labels (`job_labels`): the agent (`own-agent`), the conversation's session id and the
  turn, which tie the job to the turn's audit record; the job is always created, since a query
  answered without one carries no label. BigQuery's label rules
  (https://docs.cloud.google.com/bigquery/docs/labels-intro#requirements): keys and values hold only
  lowercase letters, digits, `_` and `-`, a key starts with a lowercase letter, each is at most 63
  characters, and a resource holds at most 64 labels. A session id of the store's form holds only
  lowercase letters, digits and `-` but may be 64 characters long, so one longer than 63 is
  labelled by `_` and the first 62 hex digits of its SHA-256, which no session id can equal.

Stated limits:
- the identity check reads `SESSION_USER()` once per connection, before its first query; it shows
  whose token BigQuery accepted, and a connection-maker that swapped the session after the check
  is not seen;
- the SQL is sqlglot's rendering of the DuckDB text in BigQuery's dialect: a verified query whose
  meaning differs between the two engines is not detected here, only by its answer;
- a citation's freshness is None: the mode reads no table's modification time;
- the agent account's own declared rights are not narrowed here: `platform/gcp/warehouse.tf` grants
  it the builder role, which reads every table, on each dataset the models build, though none on
  the purpose and aggregate datasets the mode's queries name.
"""

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import sqlglot
import yaml
from sqlglot import exp
from sqlglot.errors import ParseError

from access import purpose_views, thresholds
from access.bigquery import (
    MAX_BYTES_BILLED,
    OVER_CAP,
    UNFINISHED,
    PersonaQueryError,
    PersonaSession,
    Runner,
    chain,
    mint,
)
from access.bigquery import _run as run_gcloud
from access.identities import identity
from access.row_policies import clear_readers, path
from agent.audit import UnboundValue, bind
from agent.limits import BYTES_CAP, TIME_CAP, CapReached, Limits
from agent.model import Citation, Refusal, VerifiedQuery

ENGINE = "bigquery"
ACCESS = Path(__file__).resolve().parents[1] / "access"
# A label key or value as BigQuery takes it (lowercase letters, digits, `_` and `-`, at most 63),
# and a key's first character; see the module's docstring for the documented rules.
LABEL_VALUE = re.compile(r"^[a-z0-9_-]{0,63}$")
LABEL_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
# A job reference as the audit record's `job` takes it: `<project>:<location>.<job id>`.
JOB = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SESSION_USER = "SELECT SESSION_USER()"


class WrongIdentity(PersonaQueryError):
    """A connection whose jobs BigQuery would run as another identity than the asking persona's;
    its message names no account."""


class Unplaced(ValueError):
    """A table a verified query names that the persona reads through no relation, or through two."""


@dataclass(frozen=True)
class Catalogues:
    """The catalogues the warehouse's layout is generated from (`access/`)."""

    access: Mapping[str, Any]
    columns: Mapping[str, Any]
    purposes: Mapping[str, Any]
    aggregates: Mapping[str, Any]


def load_catalogues(folder: Path = ACCESS) -> Catalogues:
    def read(name: str) -> dict[str, Any]:
        return yaml.safe_load((folder / name).read_text(encoding="utf-8"))

    return Catalogues(
        read("personas.yaml"), read("columns.yaml"), read("purposes.yaml"), read("aggregates.yaml")
    )


def relations(persona: str, catalogues: Catalogues) -> dict[str, tuple[tuple[str, str], ...]]:
    """Each table name the persona's queries may name, with the dataset and relation it reads it
    through on BigQuery: its purposes' views of the copy its tag treatment gives it, and the
    aggregate views of the products it reads in aggregate. A name with two places is kept with
    both, and refused when a query names it."""
    c = catalogues
    found: dict[str, list[tuple[str, str]]] = {}
    mine = {u["id"] for u in c.purposes["purposes"] if persona in u["personas"]}
    for view in purpose_views.views(c.access, c.columns, c.purposes, c.aggregates):
        if view.purpose not in mine:
            continue
        if view.audience != "all":
            clear = clear_readers(c.columns, view.product, view.table) or frozenset()
            if (view.audience == "clear") != (persona in clear):
                continue
        found.setdefault(view.table, []).append((view.dataset, view.name))
    readable = {
        thresholds.aggregates_dataset(product)
        for product in c.aggregates["products"]
        if persona in thresholds.readers(c.access, product)
    }
    for aggregate in thresholds.views(c.access, c.aggregates):
        if aggregate.dataset in readable:
            found.setdefault(aggregate.name, []).append((aggregate.dataset, aggregate.name))
    return {name: tuple(places) for name, places in found.items()}


@dataclass(frozen=True, eq=False)
class BigQueryConnection:
    """One persona's connection to the lab's BigQuery: a session carrying a token minted for the
    persona's own account, and where its tables are."""

    persona: str
    project: str
    session: PersonaSession
    relations: Mapping[str, tuple[tuple[str, str], ...]]


def connector(
    project: str,
    client: httpx.Client,
    *,
    env: Mapping[str, str] | None = None,
    catalogues: Catalogues | None = None,
    run: Runner = run_gcloud,
) -> Callable[[str], BigQueryConnection]:
    """The connection-maker `agent.ask.ask` takes on BigQuery: each call mints a new token for the
    persona's own account and keeps none."""
    known = load_catalogues() if catalogues is None else catalogues

    def connect(persona: str) -> BigQueryConnection:
        account = identity(known.access, persona).service_account(project)
        token = mint(chain(account, project, os.environ if env is None else env), run)
        session = PersonaSession(project, token, client)
        return BigQueryConnection(persona, project, session, relations(persona, known))

    return connect


def label_value(value: str) -> str:
    """A value as a label holds it: as it is when BigQuery takes it, else `_` and the first 62 hex
    digits of its SHA-256."""
    if LABEL_VALUE.fullmatch(value):
        return value
    return "_" + hashlib.sha256(value.encode()).hexdigest()[:62]


def job_labels(agent: str, session: str, turn: int) -> dict[str, str]:
    """The labels every job of a turn carries, tying it to the turn's audit record."""
    labels = {
        "agent": label_value(agent),
        "conversation": label_value(session),
        "turn": label_value(str(turn)),
    }
    if not all(LABEL_KEY.fullmatch(k) and LABEL_VALUE.fullmatch(v) for k, v in labels.items()):
        raise ValueError("a job label BigQuery does not take")
    return labels


def job_cap(limits: Limits, spent: int) -> int:
    """The bytes a job of the turn may bill: what is left of the turn's, within the persona
    queries' own cap; 0 when nothing is left."""
    return max(0, min(limits.bytes_billed_per_turn - spent, MAX_BYTES_BILLED))


def prepare(sql: str, connection: BigQueryConnection) -> tuple[str, dict[str, Any]]:
    """A verified query's DuckDB SQL as BigQuery's, each table placed where the persona reads it,
    its values bound as named parameters; raises `Unplaced` or `UnboundValue`."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except ParseError as error:
        raise UnboundValue(f"the SQL does not parse: {error}") from error
    ctes = {cte.alias for cte in tree.find_all(exp.CTE)}
    for table in list(tree.find_all(exp.Table)):
        if table.name in ctes and not table.db:
            continue
        if table.db or table.catalog:
            raise Unplaced(f"the query names a qualified table, {table.sql()}")
        places = connection.relations.get(table.name)
        if not places:
            raise Unplaced(f"the persona reads no table named {table.name} on BigQuery")
        if len(places) != 1:
            raise Unplaced(f"tables held in two datasets: {table.name}")
        ((dataset, name),) = places
        placed = exp.to_table(path(connection.project, dataset, name), dialect="bigquery")
        # Aliased by the name the query gave it, so a column it qualifies by that name resolves.
        alias = table.args.get("alias") or exp.TableAlias(this=exp.to_identifier(table.name))
        placed.set("alias", alias)
        table.replace(placed)
    return bind(tree.sql(dialect="bigquery"), ENGINE)


def cell(kind: str, value: Any) -> Any:
    """A REST answer's cell, given as text, as the type its schema names."""
    if value is None:
        return None
    if kind in ("INTEGER", "INT64"):
        return int(value)
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return Decimal(value)
    if kind in ("FLOAT", "FLOAT64"):
        return float(value)
    if kind in ("BOOLEAN", "BOOL"):
        return value == "true"
    return value


def verify(
    connection: BigQueryConnection,
    persona: str,
    catalogue: Mapping[str, Any],
    labels: Mapping[str, str],
    limits: Limits,
    spent: int,
) -> int:
    """Check that BigQuery runs the connection's jobs as the asking persona's own account; the
    bytes the check billed. Raises `WrongIdentity` otherwise, and `CapReached` when no bytes are
    left."""
    if connection.persona != persona:
        raise WrongIdentity("the connection-maker returned another persona's connection")
    expected = identity(catalogue, persona).service_account(connection.project)
    cap = job_cap(limits, spent)
    if cap == 0:
        raise CapReached(BYTES_CAP, "no bytes are left of the turn's to check its identity")
    try:
        answer = connection.session.query(
            SESSION_USER,
            labels=labels,
            max_bytes_billed=cap,
            timeout_ms=int(limits.seconds_per_query * 1000),
        )
    except PersonaQueryError as error:
        raise WrongIdentity(f"the identity check was refused: {error}") from None
    if answer.rows != [(expected,)]:
        raise WrongIdentity(
            "BigQuery runs the connection's jobs as another identity than the asker"
        )
    return cap if answer.bytes_billed is None else answer.bytes_billed


@dataclass(frozen=True)
class Ran:
    """A job that returned: the SQL as sent, its bound values, its reference, start and result."""

    text: str
    parameters: Mapping[str, Any]
    job: str
    at: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


def run(
    connection: BigQueryConnection,
    query: VerifiedQuery,
    limits: Limits,
    spent: int,
    labels: Mapping[str, str],
    clock: Callable[[], str],
) -> tuple[Citation | Refusal, Ran | None, int, CapReached | None]:
    """Run one verified query as the connection's persona under the turn's caps; also the bytes
    counted as billed and the cap it reached, if any. `spent` is what the turn billed before."""
    try:
        text, parameters = prepare(query.sql, connection)
    except (Unplaced, UnboundValue) as error:
        return Refusal(query.id, f"the query could not be placed: {error}"), None, 0, None
    cap = job_cap(limits, spent)
    if cap == 0:
        reached = CapReached(
            BYTES_CAP, f"no bytes are left of the turn's {limits.bytes_billed_per_turn}"
        )
        return Refusal(query.id, str(reached)), None, 0, reached
    started = clock()
    try:
        answer = connection.session.query(
            text,
            parameters=parameters,
            labels=labels,
            max_bytes_billed=cap,
            timeout_ms=int(limits.seconds_per_query * 1000),
        )
    except PersonaQueryError as error:
        if error.reason == OVER_CAP:
            reached = CapReached(
                BYTES_CAP,
                f"BigQuery refused it before it ran, as billing more than the {cap} bytes left of"
                f" the turn's {limits.bytes_billed_per_turn}",
            )
            return Refusal(query.id, str(reached)), None, 0, reached
        if error.reason == UNFINISHED:
            seconds = f"{limits.seconds_per_query:g}"
            reached = CapReached(TIME_CAP, f"the query ran past the {seconds} second cap")
            return Refusal(query.id, str(reached)), None, cap, reached
        return Refusal(query.id, f"the query failed on BigQuery: {error}"), None, cap, None
    billed = cap if answer.bytes_billed is None else answer.bytes_billed
    reference = answer.job
    job = f"{reference.get('projectId')}:{reference.get('location')}.{reference.get('jobId')}"
    if not (set(reference) >= {"projectId", "location", "jobId"} and JOB.fullmatch(job)):
        return Refusal(query.id, "BigQuery answered with no job reference"), None, billed, None
    kinds = answer.types or ("",) * (len(answer.rows[0]) if answer.rows else 0)
    rows = tuple(tuple(cell(k, v) for k, v in zip(kinds, row, strict=True)) for row in answer.rows)
    columns = answer.columns
    citation = Citation(query.id, query.metric, query.version, query.sql, columns, rows, None)
    return citation, Ran(text, parameters, job, started, columns, rows), billed, None
