"""Row access policies on BigQuery's entity-scoped data products, generated from the persona
mapping, and the governed copy of each product table they sit on.

dbt builds the payments mart in `intermediate`, which no persona reads, and replaces it on every
build, so a policy on it would be dropped each time. The product's own table in its dataset is a
governed copy instead: created once, empty, with the mart's columns (`create`); its policies are
applied by the owner, since they grant rows to accounts (`plan` prints the statements, `apply`
runs them); and each publish (`publish`) first checks that the table holds the generated policies
by name and filter, not grantees, then replaces its rows from the mart in one transaction, so the
table, and with it its policies, is never replaced, and rows never sit in it without them.

One policy per product table and entity, `entity_id = '<entity>'`, granted to the personas with a
`read` or `edit` grant on that entity's product (`access/identities.py`) that read every tagged
column of the table in clear (`access/columns.yaml`); an aggregate grant gets none, and a tag's
masked readers get no rows of the clear table, where a policy tag is only a second lock. One more,
`TRUE`, is granted to the publisher account (`platform/gcp/identities.tf`), since BigQuery runs a
DELETE or INSERT on a table with policies only for an account that sees every row of it. BigQuery
shows a user the union of the policies granting it and no row of a table with policies none of
which does.

The checks before a publish read metadata as the session's login; the publish's one write runs as
the publisher, with a token minted through the agent account (`access/bigquery.py`), so the owner's
login needs only delegation on the agent account and no policy grants it a row. Behind the cloud
session's proxy that token is replaced with the agent's own credential, which no policy grants every
row, so a publish there is refused and writes nothing.

The project comes from `LEDGER_LAB_GCP_PROJECT` and the location from `LEDGER_LAB_BQ_LOCATION`;
the agent account, for the publisher's token, from `LEDGER_LAB_AGENT_ACCOUNT`. Nothing here is
written down: the statements are printed for the owner and never stored. Queries are capped
(`MAX_BYTES_BILLED`); a policy listing reads metadata and bills nothing.
"""

import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from access.bigquery import PersonaQueryError, PersonaSession, chain, mint, service_account
from access.identities import identity

HERE = Path(__file__).resolve().parent
PERSONAS = HERE / "personas.yaml"
COLUMNS = HERE / "columns.yaml"
SOURCE_DATASET = "intermediate"
ENTITY = "entity_id"
# The account a publish writes as, declared in platform/gcp/identities.tf, and its policy.
PUBLISHER = "publisher"
PUBLISHER_POLICY = "publisher_writes_all_rows"
# The payments mart reads a sample-sized table on BigQuery; the cap admits it several times over and
# stays far inside the free tier's 1 TiB a month.
MAX_BYTES_BILLED = 1_000_000_000
SLUG = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
DATASET_OR_TABLE = re.compile(r"^[a-z][a-z0-9_]*$")

Run = Callable[[Sequence[str]], str]
Write = Callable[[str], None]


class RowPolicyError(RuntimeError):
    """The governed copy cannot be created or published as it stands."""


@dataclass(frozen=True)
class Policy:
    """One row access policy: its name, the table it is on, its filter and its grantees, as
    persona ids and, for the publisher's, the publisher account."""

    dataset: str
    table: str
    name: str
    condition: str
    personas: tuple[str, ...]
    publisher: bool = False

    def grantees(self, project: str) -> list[str]:
        accounts = [service_account(p, project) for p in self.personas]
        if self.publisher:
            accounts.append(service_account(PUBLISHER, project))
        return [f"serviceAccount:{a}" for a in accounts]

    def create(self, project: str) -> str:
        grantees = ", ".join(f'"{g}"' for g in self.grantees(project))
        return (
            f"CREATE OR REPLACE ROW ACCESS POLICY `{self.name}`"
            f" ON {path(project, self.dataset, self.table)}"
            f" GRANT TO ({grantees}) FILTER USING ({self.condition});"
        )


def load(path: Path = PERSONAS) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def entity_tables(catalogue: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Each entity-scoped product's dataset and table, in the catalogue's order."""
    return [
        (product["id"].replace("-", "_"), table)
        for product in catalogue["data_products"]
        if product["scope"] == "entity"
        for table in product.get("tables", [])
    ]


def clear_readers(columns: Mapping[str, Any], product: str, table: str) -> frozenset[str] | None:
    """The personas that read every tagged column of a product table in clear
    (`access/columns.yaml`), or None for a table with no tagged column, which every reader reads as
    it is. A tag's other readers read it masked, so they get no rows of the clear table: a policy
    tag on its column is a second lock, not the only one."""
    tagged = {
        tag
        for column, tag in (
            columns.get("products", {}).get(product, {}).get("columns", {}) or {}
        ).items()
        if column.split(".", 1)[0] == table
    }
    if not tagged:
        return None
    readers = [set(t["clear"]) for t in columns["tags"] if t["id"] in tagged]
    if len(readers) != len(tagged):
        raise RowPolicyError(f"{product}.{table} carries a tag columns.yaml does not define")
    return frozenset(set.intersection(*readers))


def policies(catalogue: Mapping[str, Any], columns: Mapping[str, Any]) -> list[Policy]:
    """The row access policies of every entity-scoped product table: per entity with a reader,
    one granting its rows to those readers that read the table's tagged columns in clear, then the
    publisher's over all rows."""
    personas = [p["id"] for p in catalogue["personas"]]
    entities = [e["id"] for e in catalogue["entities"]]
    for name in (*personas, *entities):
        if not SLUG.fullmatch(name):
            raise RowPolicyError(f"{name!r} is not a slug, so it cannot name a policy or account")
    reads = {p: identity(catalogue, p).reads for p in personas}
    found: list[Policy] = []
    for dataset, table in entity_tables(catalogue):
        product = next(
            p["id"] for p in catalogue["data_products"] if p["id"].replace("-", "_") == dataset
        )
        clear = clear_readers(columns, product, table)
        for entity in entities:
            readers = tuple(
                p
                for p in personas
                if entity in (reads[p].get(product) or frozenset())
                and (clear is None or p in clear)
            )
            if readers:
                found.append(
                    Policy(
                        dataset,
                        table,
                        f"{entity.replace('-', '_')}_rows",
                        f"{ENTITY} = '{entity}'",
                        readers,
                    )
                )
        found.append(Policy(dataset, table, PUBLISHER_POLICY, "TRUE", (), publisher=True))
    return found


def bq(project: str, where: str, *command: str) -> list[str]:
    return ["bq", f"--project_id={project}", f"--location={where}", "--headless", *command]


def query(project: str, where: str, sql: str) -> list[str]:
    return bq(
        project,
        where,
        "--format=json",
        "query",
        "--use_legacy_sql=false",
        "--nouse_cache",
        f"--maximum_bytes_billed={MAX_BYTES_BILLED}",
        sql,
    )


def run_tool(command: Sequence[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603


def path(project: str, dataset: str, table: str) -> str:
    """A table's quoted path, each part checked against its shape, since the statements are built
    as text: the project an id, the dataset and table lower-case names."""
    if not PROJECT_ID.fullmatch(project):
        raise RowPolicyError("the lab's project is not a project id")
    for part in (dataset, table):
        if not DATASET_OR_TABLE.fullmatch(part):
            raise RowPolicyError(f"{part!r} is not a dataset or table name")
    return f"`{project}.{dataset}.{table}`"


def columns_sql(project: str, dataset: str, table: str) -> str:
    path(project, dataset, table)
    return (
        "SELECT column_name, data_type"  # noqa: S608
        f" FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`"
        f" WHERE table_name = '{table}' ORDER BY ordinal_position"
    )


def policies_sql(project: str, dataset: str, table: str) -> str:
    path(project, dataset, table)
    return (
        "SELECT row_access_policy_name, filter_predicate"  # noqa: S608
        f" FROM `{project}.{dataset}.INFORMATION_SCHEMA.ROW_ACCESS_POLICIES`"
        f" WHERE table_name = '{table}' ORDER BY row_access_policy_name"
    )


def create_sql(project: str, dataset: str, table: str) -> str:
    """The governed copy, empty, with the mart's columns; a table already there is left as it is."""
    return (
        f"CREATE TABLE IF NOT EXISTS {path(project, dataset, table)}"  # noqa: S608
        f" AS SELECT * FROM {path(project, SOURCE_DATASET, table)} WHERE FALSE"
    )


def publish_sql(project: str, dataset: str, table: str) -> str:
    """The copy's rows replaced by the mart's in one transaction: a reader sees the old rows or the
    new, the table and its policies are never replaced."""
    target = path(project, dataset, table)
    return (
        "BEGIN TRANSACTION;"  # noqa: S608
        f" DELETE FROM {target} WHERE TRUE;"
        f" INSERT INTO {target} SELECT * FROM {path(project, SOURCE_DATASET, table)};"
        " COMMIT TRANSACTION;"
    )


def rows(output: str) -> list[dict[str, Any]]:
    return json.loads(output) if output.strip() else []


def normal(predicate: str) -> str:
    """A filter as BigQuery reports it, compared without spacing or case of keywords."""
    return re.sub(r"\s+", "", predicate).lower()


def check_policies(
    expected: Sequence[Policy], found: Sequence[Mapping[str, Any]], dataset: str, table: str
) -> None:
    """Refuse a table whose policies differ from the generated ones by name or filter."""
    want = {p.name: normal(p.condition) for p in expected}
    have = {r["row_access_policy_name"]: normal(r["filter_predicate"]) for r in found}
    if want != have:
        missing = sorted(set(want) - set(have))
        extra = sorted(set(have) - set(want))
        changed = sorted(n for n in set(want) & set(have) if want[n] != have[n])
        raise RowPolicyError(
            f"{dataset}.{table} does not hold its generated row access policies (missing:"
            f" {', '.join(missing) or 'none'}; not generated: {', '.join(extra) or 'none'};"
            f" another filter: {', '.join(changed) or 'none'}): no rows are published until the"
            " owner applies them (`python -m access.row_policies plan`) and drops each policy"
            " that is no longer generated (`DROP ROW ACCESS POLICY`), which `plan` does not emit"
        )


def check_columns(source: Sequence[Mapping[str, Any]], copy: Sequence[Mapping[str, Any]]) -> None:
    if [(c["column_name"], c["data_type"]) for c in source] != [
        (c["column_name"], c["data_type"]) for c in copy
    ]:
        raise RowPolicyError(
            "the mart's columns differ from its governed copy's: the owner drops the copy, and"
            " `create` and the policies' apply make it again"
        )


def create(catalogue: Mapping[str, Any], project: str, where: str, run: Run = run_tool) -> None:
    for dataset, table in entity_tables(catalogue):
        run(query(project, where, create_sql(project, dataset, table)))


def publisher_write(project: str, env: Mapping[str, str]) -> Write:
    """A statement run as the publisher: its token minted through the agent account when
    `LEDGER_LAB_AGENT_ACCOUNT` names it, held only for the one query, capped as the others."""
    accounts = chain(service_account(PUBLISHER, project), project, env)

    def write(sql: str) -> None:
        token = mint(accounts)
        with httpx.Client(timeout=120) as client:
            PersonaSession(project, token, client, MAX_BYTES_BILLED).query(sql)

    return write


def publish(
    catalogue: Mapping[str, Any],
    columns: Mapping[str, Any],
    project: str,
    where: str,
    write: Write,
    run: Run = run_tool,
) -> None:
    """Each governed copy's rows from its mart, once its policies and columns check out: the checks
    through `run`, the write through `write`, as the publisher."""
    generated = policies(catalogue, columns)
    for dataset, table in entity_tables(catalogue):
        expected = [p for p in generated if (p.dataset, p.table) == (dataset, table)]
        found = rows(run(query(project, where, policies_sql(project, dataset, table))))
        check_policies(expected, found, dataset, table)
        source = rows(run(query(project, where, columns_sql(project, SOURCE_DATASET, table))))
        copy = rows(run(query(project, where, columns_sql(project, dataset, table))))
        check_columns(source, copy)
        write(publish_sql(project, dataset, table))


def plan(catalogue: Mapping[str, Any], columns: Mapping[str, Any], project: str) -> list[str]:
    """The statements the owner runs: every generated policy, created or replaced."""
    return [p.create(project) for p in policies(catalogue, columns)]


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    from ingest.load import LoadError, lab

    command = argv[0] if argv else ""
    if command not in ("plan", "apply", "create", "publish"):
        print("usage: python -m access.row_policies plan|apply|create|publish", file=sys.stderr)
        return 2
    catalogue = load()
    columns = load(COLUMNS)
    try:
        project, where = lab(env)
        if command in ("plan", "apply"):
            statements = plan(catalogue, columns, project)
            if command == "plan":
                print("\n".join(statements))
            for statement in statements if command == "apply" else []:
                run_tool(query(project, where, statement))
        elif command == "create":
            create(catalogue, project, where)
        else:
            publish(catalogue, columns, project, where, publisher_write(project, env))
    except (RowPolicyError, LoadError, PersonaQueryError) as error:
        print(f"row-policies: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        # bq's message names the project and the accounts: only its exit status is reported.
        print(f"row-policies: bq failed with exit status {error.returncode}", file=sys.stderr)
        return 1
    if command != "plan":
        print(f"row-policies: {command} done")
    return 0


if __name__ == "__main__":
    import os

    sys.exit(main(sys.argv[1:], os.environ))
