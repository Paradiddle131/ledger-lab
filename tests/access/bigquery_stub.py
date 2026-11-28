"""A stand-in for the lab's BigQuery, loaded as a plugin into a child pytest run of the visibility
matrix's BigQuery parameter (`tests/access/test_visibility_on_bigquery.py`), so the engine's
wiring runs end to end with no GCP call: the persona sessions' mint, every query and metadata read
over HTTP, as each persona's token names it.

It serves the dbt build of the samples (`STUB_WAREHOUSE`) as the catalogues and `platform/gcp` set
BigQuery up, worked out here from the catalogues apart from the engine: the governed copies under
one row access policy per reader (a tag's clear readers on the clear copy, its masked readers on
the masked copy, the publisher on both), a tagged column of the clear copy refused to an identity
that is not its clear reader, the purpose views and the aggregate views reading their sources as
the persona querying them (authorized), each dataset readable and listable by the identities
`platform/gcp` grants it to and refused (403) to every other, each aggregate view its generated
statement, grouped at its grain with the minimum in a HAVING. Its SQL is BigQuery's, turned into
DuckDB's by sqlglot, with a fingerprint of its own for FARM_FINGERPRINT. Like BigQuery it refuses
a query over its bytes cap, here one naming more than `CAP_COLUMNS` distinct columns of the
payments, and pages an answer of more than `PAGE_ROWS` rows; the engine's answer budget is
lowered so its distinct values come in several parts.

It also serves the own agent's BigQuery mode (`agent/bigquery.py`, run by
`tests/agent/test_bigquery_mode.py`): `SELECT SESSION_USER()` answers the account whose token the
request carries; named query parameters are bound; each job bills, as BigQuery does at least, 10
MiB for each table its SQL names and nothing for one that names none, and a job whose bill is past
its `maximumBytesBilled` is refused before it runs (`bytesBilledLimitExceeded`); every job's cap
must be a whole number of bytes above zero and within the persona queries' cap; each answer names
its job, whose labels its creator reads back (`jobs.get`). `STUB_JOBS` is where it appends, one
JSON line per job, who ran it and with which cap, labels and bill, and how it ended; `STUB_BUILD`
is the folder of the sample build `STUB_WAREHOUSE` was built in, which the child's session build
then is, so the child builds nothing.

`STUB_FAULT` plants one fault: `leak` gives the Chicago accountant's policy on the masked copy the
UK entity's rows too; `unmasked` stores the clear person key in the masked copy it reads;
`stray-view` adds a view of its own to the group close dataset; `changed` adds one to an amount of
the clear copy as the payables clerk reads it; `no-minimum` serves the payments aggregate view
with its HAVING's minimum at zero, its stored query still the generated one; `rows-open` gives the
UK transport accountant's policies every entity's rows; `cap-ignored` runs a job past its cap; and
of the mint: `agent-token` mints the token of the chain's first account, the agent's, as the cloud
proxy's replacement would carry, `cached-token` hands every mint after the first the first one's
token, and `mint-fails` fails every mint.
`STUB_PROVEN` is the local engine's tally over the same build, which the BigQuery run must then
reach. `STUB_LOG` is where it writes how many reads of each kind it served.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import duckdb
import httpx
import pytest
import sqlglot
import yaml
from sqlglot import exp

import tests.access.visibility_on_bigquery as engine
import tests.models.dbt_build
from access import purpose_views, thresholds
from access.bigquery import MAX_BYTES_BILLED
from access.identities import identity
from access.row_policies import PUBLISHER, clear_readers
from tests.conftest import REPO


def _no_network(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
    raise RuntimeError("no network in this run")


# Every query and metadata read is answered at httpx's transport below; nothing connects.
socket.create_connection = _no_network  # type: ignore[assignment]

PROJECT = os.environ["LEDGER_LAB_GCP_PROJECT"]
FAULT = os.environ.get("STUB_FAULT", "")
# The least BigQuery bills for each table a query reads.
TABLE_BYTES = 10 * 2**20
LOCATION = "EU"
CAP_COLUMNS = 10
PAGE_ROWS = 60
TAGGED = {"vendor_name", "document_number", "vendor_person_key"}
SERVED: Counter[str] = Counter()


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((REPO / "access" / name).read_text())


ACCESS, COLUMNS = load("personas.yaml"), load("columns.yaml")
PURPOSES, AGGREGATES = load("purposes.yaml"), load("aggregates.yaml")
PURPOSE_VIEWS = purpose_views.views(ACCESS, COLUMNS, PURPOSES, AGGREGATES)
AGGREGATE_VIEWS = thresholds.views(ACCESS, AGGREGATES)
BASE = {
    "intermediate": ["payments", "payments_masked"],
    "payments": ["payments", "payments_masked"],
    "group_totals": ["group_totals", "group_totals_left_out"],
}


def fingerprint(value: str) -> int:
    digest = hashlib.blake2b(value.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def readable(who: str) -> set[str]:
    """The datasets `platform/gcp` grants the identity to read and list."""
    if who == PUBLISHER:
        return {"payments", "intermediate"}
    found = {
        v.dataset
        for v in PURPOSE_VIEWS
        for u in PURPOSES["purposes"]
        if u["id"] == v.purpose and who in u["personas"]
    }
    tables = {p["id"]: p.get("tables") for p in ACCESS["data_products"]}
    for grant in ACCESS["grants"]:
        if grant["persona"] != who:
            continue
        dataset = grant["product"].replace("-", "_")
        if grant["access"] == "aggregate" and grant["product"] in AGGREGATES["products"]:
            found.add(f"{dataset}_aggregates")
        elif grant["access"] in ("read", "edit") and not tables[grant["product"]]:
            found.add(dataset)
    return found


def rows_of(who: str, table: str) -> str:
    """The governed copy's rows the identity's row access policies give it."""
    if who == PUBLISHER:
        return "true"
    entities = set(identity(ACCESS, who).reads.get("payments") or ())
    clear = who in (clear_readers(COLUMNS, "payments", "payments") or frozenset())
    if table.endswith("_masked") == clear:
        entities = set()
    if FAULT == "leak" and who == "accountant-chicago" and table == "payments_masked":
        entities.add("uk-transport")
    if FAULT == "rows-open" and who == "accountant-uk-transport" and entities:
        return "true"
    listed = ", ".join(f"'{e}'" for e in sorted(entities)) or "null"
    return f"entity_id in ({listed})"


def catalog(who: str) -> str:
    return who.replace("-", "_")


def resolved(sql: str, who: str, hidden: bool) -> str:
    """BigQuery's SQL as DuckDB's, each table path the identity's own relation: its hidden one,
    which a view reads as the querying identity, or the one its grants let it read."""

    def place(match: re.Match[str]) -> str:
        dataset = f'"_{match[1]}"' if hidden else match[1]
        return f"{catalog(who)}.{dataset}.{match[2]}"

    sql = re.sub(rf"`{re.escape(PROJECT)}\.([a-z_]+)\.([a-z_]+)`", place, sql)
    sql = sql.replace("BYTE_LENGTH(", "STRLEN(")
    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


class Warehouse:
    """Every identity's view of the warehouse, as one DuckDB catalog each."""

    def __init__(self, path: str) -> None:
        self.db = duckdb.connect()
        self.db.create_function("farm_fingerprint", fingerprint, ["VARCHAR"], "BIGINT")
        self.db.execute(f"attach '{path}' as w (read_only)")
        self.views: dict[tuple[str, str], str] = {}
        for view in PURPOSE_VIEWS:
            self.views[(view.dataset, view.name)] = view.select(PROJECT)
        # What each view runs, where a fault plants another query under the stored one.
        self.served: dict[tuple[str, str], str] = {}
        for aggregate in AGGREGATE_VIEWS:
            generated = engine.generated_query(aggregate, PROJECT)
            self.views[(aggregate.dataset, aggregate.name)] = generated
            if FAULT == "no-minimum" and aggregate.count == "count_distinct":
                kept = f" >= {aggregate.minimum}"
                assert generated.endswith(kept), generated
                self.served[(aggregate.dataset, aggregate.name)] = generated[: -len(kept)] + " >= 0"
        if FAULT == "stray-view":
            self.views[("group_close", "notes")] = (
                f"select entity_id from `{PROJECT}.group_totals.group_totals`"  # noqa: S608
            )
        self.who = [p["id"] for p in ACCESS["personas"]] + [PUBLISHER]
        for who in self.who:
            self.build(who)

    def build(self, who: str) -> None:
        name = catalog(who)
        self.db.execute(f"attach ':memory:' as {name}")
        for dataset, tables in BASE.items():
            self.db.execute(f'create schema {name}."_{dataset}"')
            for table in tables:
                rows = rows_of(who, table) if dataset == "payments" else "true"
                select = f"select * from w.{table} where {rows}"  # noqa: S608
                if (FAULT, who, dataset, table) == (
                    "unmasked",
                    "accountant-chicago",
                    "payments",
                    "payments_masked",
                ):
                    # The clear person key in place of its hash, every other column as it is.
                    shown = ", ".join(
                        "c.vendor_person_key" if column == "vendor_person_key" else f"m.{column}"
                        for (column, *_) in self.db.execute(f"describe w.{table}").fetchall()
                    )
                    select = (
                        f"select {shown} from w.{table} m join w.payments c"  # noqa: S608
                        f" using (payment_key) where m.{rows}"
                    )
                if (FAULT, who, dataset, table) == (
                    "changed",
                    "payables-clerk",
                    "payments",
                    "payments",
                ):
                    first = "(select min(payment_key) from w.payments)"
                    select = (
                        "select * replace (amount + case when payment_key ="  # noqa: S608
                        f" {first} then 1 else 0 end as amount) from w.{table} where {rows}"
                    )
                self.db.execute(f'create view {name}."_{dataset}".{table} as {select}')
        for dataset in readable(who):
            self.db.execute(f"create schema {name}.{dataset}")
            for table in BASE.get(dataset, []):
                hidden = f'{name}."_{dataset}".{table}'
                self.db.execute(
                    f"create view {name}.{dataset}.{table} as select * from {hidden}"  # noqa: S608
                )
            for (at, table), query in self.views.items():
                if at == dataset:
                    query = self.served.get((at, table), query)
                    self.db.execute(
                        f"create view {name}.{dataset}.{table} as {resolved(query, who, True)}"
                    )

    def tables(self, who: str, dataset: str) -> list[str]:
        if dataset not in readable(who):
            raise Refused(403, "accessDenied")
        listed = self.db.execute(
            "select table_name from duckdb_columns() where database_name = ? and schema_name = ?"
            " group by table_name order by table_name",
            [catalog(who), dataset],
        ).fetchall()
        return [t for (t,) in listed]

    def table(self, who: str, dataset: str, table: str) -> dict[str, Any]:
        if table not in self.tables(who, dataset):
            raise Refused(404, "notFound")
        described = self.db.execute(
            f"select * from {catalog(who)}.{dataset}.{table} limit 0"  # noqa: S608
        ).description
        answer: dict[str, Any] = {
            "type": "VIEW" if (dataset, table) in self.views else "TABLE",
            "schema": {"fields": [{"name": d[0], "type": kind(str(d[1]))} for d in described]},
        }
        if (dataset, table) in self.views:
            answer["view"] = {"query": self.views[(dataset, table)]}
        return answer

    def query(self, who: str, sql: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        tree = sqlglot.parse_one(sql, read="bigquery")
        named = {c.name for c in tree.find_all(exp.Column)}
        # Only the payments are large enough for a query of them to reach the cap.
        large = re.search(r"\.payments(_masked)?`", sql) is not None
        if large and len(named) > CAP_COLUMNS:
            SERVED["over the cap"] += 1
            raise Refused(400, "bytesBilledLimitExceeded")
        if any(f"`{PROJECT}.{v.dataset}.{v.name}`" in sql for v in AGGREGATE_VIEWS):
            SERVED["aggregate views"] += 1
        if f"`{PROJECT}.payments.payments`" in sql or any(
            f"`{PROJECT}.{v.dataset}.{v.name}`" in sql
            for v in PURPOSE_VIEWS
            if v.audience == "clear"
        ):
            clear = clear_readers(COLUMNS, "payments", "payments") or frozenset()
            if named & TAGGED and who not in clear:
                raise Refused(403, "accessDenied")
        if "MOD(FARM_FINGERPRINT(" in sql:
            SERVED["distinct in parts"] += 1
        try:
            cursor = self.db.execute(resolved(sql, who, False), parameters or None)
        except duckdb.CatalogException:
            raise Refused(403, "accessDenied") from None
        found = cursor.fetchall()
        described = cursor.description or []
        SERVED["queries"] += 1
        if len(found) > PAGE_ROWS:
            return {"jobComplete": True, "pageToken": "more", "rows": []}
        return {
            "jobComplete": True,
            "totalBytesProcessed": "0",
            "schema": {"fields": [{"name": d[0], "type": kind(str(d[1]))} for d in described]},
            "rows": [{"f": [{"v": rendered(v)} for v in row]} for row in found],
        }


class Refused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


def kind(duck: str) -> str:
    if duck == "VARCHAR":
        return "STRING"
    if duck.startswith("DECIMAL"):
        return "NUMERIC"
    if "INT" in duck:
        return "INTEGER"
    if duck in ("DOUBLE", "FLOAT"):
        return "FLOAT"
    return duck


def rendered(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


WAREHOUSE: Warehouse | None = None
# Each job by its id, as `jobs.get` reads it back, and the first token minted.
JOBS: dict[str, dict[str, Any]] = {}
MINTED: list[str] = []

if "STUB_BUILD" in os.environ:

    def _built(tmp_path: Any, source_rows: str, landing: Any = None) -> Any:
        """The sample build the stand-in serves, made by the parent run, in place of a new one."""
        assert (source_rows, landing) == ("sample", None)
        warehouse, folder = Path(os.environ["STUB_WAREHOUSE"]), Path(os.environ["STUB_BUILD"])
        return tests.models.dbt_build.Build("", warehouse, {}, {}, folder)

    tests.models.dbt_build.built = _built


def bound(body: dict[str, Any]) -> dict[str, Any]:
    """A job's named parameters as values, as BigQuery binds them."""
    kinds = {"STRING": str, "INT64": int}
    return {
        p["name"]: kinds[p["parameterType"]["type"]](p["parameterValue"]["value"])
        for p in body.get("queryParameters", [])
    }


def ran(job: dict[str, Any]) -> None:
    if "STUB_JOBS" in os.environ:
        with open(os.environ["STUB_JOBS"], "a") as log:
            log.write(json.dumps(job) + "\n")


def run_job(warehouse: Warehouse, account: str, who: str, body: dict[str, Any]) -> dict[str, Any]:
    """One `jobs.query` call: capped, billed and logged as BigQuery would run it."""
    cap = body.get("maximumBytesBilled")
    assert isinstance(cap, str) and cap.isdigit(), body
    assert 0 < int(cap) <= MAX_BYTES_BILLED, body
    sql = body["query"]
    bill = TABLE_BYTES * len(re.findall(rf"`{re.escape(PROJECT)}\.[a-z_]+\.[a-z_]+`", sql))
    job: dict[str, Any] = {
        "who": who,
        "cap": int(cap),
        "labels": body.get("labels"),
        "creation": body.get("jobCreationMode"),
        "bill": bill,
        "query": sql,
    }
    try:
        if bill > int(cap) and FAULT != "cap-ignored":
            SERVED["over the cap"] += 1
            raise Refused(400, "bytesBilledLimitExceeded")
        if sql == "SELECT SESSION_USER()":
            answer: dict[str, Any] = {
                "jobComplete": True,
                "schema": {"fields": [{"name": "f0_", "type": "STRING"}]},
                "rows": [{"f": [{"v": account}]}],
            }
        else:
            answer = warehouse.query(who, sql, bound(body))
    except Refused as refusal:
        ran({**job, "ended": refusal.reason})
        raise
    job_id = f"job_{len(JOBS) + 1}"
    JOBS[job_id] = job
    ran({**job, "ended": "done"})
    reference = {"projectId": PROJECT, "location": LOCATION, "jobId": job_id}
    return {**answer, "jobReference": reference, "totalBytesBilled": str(bill)}


@pytest.fixture(autouse=True)
def _stubs(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    global WAREHOUSE
    if WAREHOUSE is None:
        WAREHOUSE = Warehouse(os.environ["STUB_WAREHOUSE"])
    warehouse = WAREHOUSE
    if "STUB_PROVEN" in os.environ:
        # The test module as pytest imported it, which is not the one this plugin imports by name.
        proven = json.loads(os.environ["STUB_PROVEN"])
        monkeypatch.setattr(request.module, "LANDED_PROVEN", proven)
    monkeypatch.setattr(engine, "ANSWER_BYTES", 1500)
    real_run = subprocess.run

    def run(command: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if list(command[1:3]) != ["auth", "print-access-token"]:
            # Not a mint (git, for the audit record's release): the child's PATH holds no gcloud
            # but the one that logs its call and fails.
            return real_run(command, *args, **kwargs)
        if FAULT == "mint-fails":
            return subprocess.CompletedProcess(command, 1, "", "denied")
        accounts = command[3].split("=", 1)[1].split(",")
        token = "stub-token:" + (accounts[0] if FAULT == "agent-token" else accounts[-1])
        MINTED.append(token)
        if FAULT == "cached-token":
            token = MINTED[0]
        return subprocess.CompletedProcess(command, 0, token, "")

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        account = request.headers["authorization"].split("stub-token:", 1)[-1]
        who = account.split("@", 1)[0]
        path = [unquote(p) for p in urlparse(str(request.url)).path.split("/")]
        try:
            if request.method == "POST":
                body = json.loads(request.content)
                return httpx.Response(200, json=run_job(warehouse, account, who, body))
            if "jobs" in path:
                job = JOBS.get(path[path.index("jobs") + 1])
                if job is None or job["who"] != who:
                    raise Refused(404, "notFound")
                return httpx.Response(200, json={"configuration": {"labels": job["labels"]}})
            SERVED["metadata reads"] += 1
            at = path.index("datasets")
            if len(path) == at + 3:
                listed = warehouse.tables(who, path[at + 1])
                return httpx.Response(
                    200, json={"tables": [{"tableReference": {"tableId": t}} for t in listed]}
                )
            return httpx.Response(200, json=warehouse.table(who, path[at + 1], path[at + 3]))
        except Refused as refusal:
            error = {"reason": refusal.reason, "message": f"refused on {PROJECT} for {account}"}
            return httpx.Response(refusal.status, json={"error": {"errors": [error]}})

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    yield
    with open(os.environ["STUB_LOG"], "w") as log:
        json.dump(dict(SERVED), log)
