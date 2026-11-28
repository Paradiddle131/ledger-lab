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

`STUB_FAULT` plants one fault: `leak` gives the Chicago accountant's policy on the masked copy the
UK entity's rows too; `unmasked` stores the clear person key in the masked copy it reads;
`stray-view` adds a view of its own to the group close dataset; `changed` adds one to an amount of
the clear copy as the payables clerk reads it; `no-minimum` serves the payments aggregate view
with its HAVING's minimum at zero, its stored query still the generated one.
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
from typing import Any
from urllib.parse import unquote, urlparse

import duckdb
import httpx
import pytest
import sqlglot
import yaml
from sqlglot import exp

import tests.access.visibility_on_bigquery as engine
from access import purpose_views, thresholds
from access.identities import identity
from access.row_policies import PUBLISHER, clear_readers
from tests.conftest import REPO


def _no_network(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
    raise RuntimeError("no network in this run")


# Every query and metadata read is answered at httpx's transport below; nothing connects.
socket.create_connection = _no_network  # type: ignore[assignment]

PROJECT = os.environ["LEDGER_LAB_GCP_PROJECT"]
FAULT = os.environ.get("STUB_FAULT", "")
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

    def query(self, who: str, sql: str) -> dict[str, Any]:
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
            cursor = self.db.execute(resolved(sql, who, False))
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


@pytest.fixture(autouse=True)
def _stubs(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    global WAREHOUSE
    if WAREHOUSE is None:
        WAREHOUSE = Warehouse(os.environ["STUB_WAREHOUSE"])
    warehouse = WAREHOUSE
    proven = json.loads(os.environ["STUB_PROVEN"])
    # The test module as pytest imported it, which is not the one this plugin imports by name.
    monkeypatch.setattr(request.module, "LANDED_PROVEN", proven)
    monkeypatch.setattr(engine, "ANSWER_BYTES", 1500)

    def run(command: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        token = "stub-token:" + command[3].rsplit(",", 1)[-1]
        return subprocess.CompletedProcess(command, 0, token, "")

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        account = request.headers["authorization"].rsplit(":", 1)[-1]
        who = account.split("@", 1)[0]
        path = [unquote(p) for p in urlparse(str(request.url)).path.split("/")]
        try:
            if request.method == "POST":
                body = json.loads(request.content)
                assert body["maximumBytesBilled"] == "100000000", body
                return httpx.Response(200, json=warehouse.query(who, body["query"]))
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
