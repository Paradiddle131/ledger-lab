"""Each persona in `access/personas.yaml` as an identity a test can act as.

On the local engine (DuckDB, which has no users or roles) acting as a persona means holding a
connection to a database built for it: a copy of only the data products its grants let it read,
and of an entity-scoped product only the rows of the entities it is granted, opened read-only with
every file and extension out of reach, so the connection cannot see or open anything else.
`persona()` in that database names who it was built for.

On BigQuery a test will impersonate the persona's service account,
`<persona>@<project>.iam.gserviceaccount.com`, and the warehouse's own rules decide what it sees;
the project comes from `platform/gcp_project.py`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

# Accesses that let a persona read a product: `edit` changes definitions it must see; `draft` and
# `post` act through the proposal and approval workflow, which carries the entry, so they read
# nothing in the warehouse.
READS = frozenset({"read", "aggregate", "edit"})
# The column an entity-scoped data product holds its entity id in.
ENTITY = "entity"


class UnknownPersonaError(KeyError):
    """The persona is not in the catalogue."""


@dataclass(frozen=True)
class Identity:
    persona: str
    # Each readable product with the entities whose rows the persona sees; None for a group product.
    reads: Mapping[str, frozenset[str] | None]

    def service_account(self, project: str) -> str:
        """The account the BigQuery mode impersonates for this persona."""
        return f"{self.persona}@{project}.iam.gserviceaccount.com"


def identity(catalogue: Mapping[str, Any], persona: str) -> Identity:
    """The persona's identity, generated from its grants in the catalogue."""
    if persona not in {p["id"] for p in catalogue["personas"]}:
        raise UnknownPersonaError(f"{persona} is not a persona in access/personas.yaml")
    scope = {p["id"]: p["scope"] for p in catalogue["data_products"]}
    entities: dict[str, set[str]] = {}
    for grant in catalogue["grants"]:
        if grant["persona"] == persona and grant["access"] in READS:
            entities.setdefault(grant["product"], set()).update(
                [grant["entity"]] if "entity" in grant else []
            )
    reads = {
        product: frozenset(seen) if scope[product] == "entity" else None
        for product, seen in entities.items()
    }
    return Identity(persona, reads)


def open_local(who: Identity, warehouse: Path, database: Path) -> duckdb.DuckDBPyConnection:
    """Build the persona's database at `database` from `warehouse` and connect to it as the persona.

    Persona and product ids are schema-checked slugs, so quoting them in the SQL is safe.
    """
    source = str(warehouse).replace("'", "''")
    with duckdb.connect(str(database)) as build:
        build.execute(f"attach '{source}' as warehouse (read_only)")
        for product, entities in who.reads.items():
            copy = f'create table "{product}" as select * from warehouse."{product}"'  # noqa: S608
            if entities is None:
                build.execute(copy)
            else:
                build.execute(
                    f"{copy} where {ENTITY} in (select unnest(?::varchar[]))", [sorted(entities)]
                )
        build.execute("detach warehouse")
        build.execute(f"create macro persona() as '{who.persona}'")
    return duckdb.connect(
        str(database),
        read_only=True,
        config={"enable_external_access": False, "lock_configuration": True},
    )
