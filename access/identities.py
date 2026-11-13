"""Each persona in `access/personas.yaml` as an identity a test can act as.

On the local engine (DuckDB, which has no users or roles) acting as a persona means holding a
connection to a database built for it, the file `<persona>.duckdb`, so its catalog is named after
the persona and the connection can neither rename it nor switch to another. The file holds a copy
of only the data products the persona may read row by row, and of an entity-scoped product only
the rows of its granted entities; it is opened read-only, with every file outside it out of reach,
no extension beyond those built into DuckDB, and its configuration locked.

DuckDB lets any connection create temporary objects, and an unqualified name resolves to a
temporary one first, so a connection can shadow a product or `current_database()` within itself.
A shadow holds only what that connection could already read or write as literals; the persona's
reach is read through qualified names, `system.main.current_database()` and
`"<persona>".main."<product>"`, which no temporary object can shadow.

On BigQuery a test will impersonate the persona's service account,
`<persona>@<project>.iam.gserviceaccount.com`, and the warehouse's own rules decide what it sees;
the caller passes the project, which comes only from `platform/gcp_project.py`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

# Accesses that let a persona read a product's rows: `edit` changes definitions it must see.
# `aggregate` allows only aggregates above the minimum group, a rule not built yet, so until it
# exists an aggregate grant reaches nothing. `draft` and `post` act through the proposal and
# approval workflow, which carries the entry, so they read nothing in the warehouse.
READS = frozenset({"read", "edit"})
# The column an entity-scoped data product holds its entity id in.
ENTITY = "entity"
# The catalog name the build attaches the source warehouse under.
SOURCE = "warehouse"


class UnknownPersonaError(KeyError):
    """The persona is not in the catalogue."""


class ReservedPersonaError(ValueError):
    """The persona id is a catalog name DuckDB reserves or the build uses, so it cannot name one."""


@dataclass(frozen=True)
class Identity:
    persona: str
    # Each readable product with the entities whose rows the persona sees; None for a group product.
    reads: Mapping[str, frozenset[str] | None]

    def service_account(self, project: str) -> str:
        """The account the BigQuery mode impersonates for this persona in the lab's project."""
        return f"{self.persona}@{project}.iam.gserviceaccount.com"


def identity(catalogue: Mapping[str, Any], persona: str) -> Identity:
    """The persona's identity, generated from its grants in the catalogue."""
    if persona not in {p["id"] for p in catalogue["personas"]}:
        raise UnknownPersonaError(f"{persona} is not a persona in access/personas.yaml")
    scope = {p["id"]: p["scope"] for p in catalogue["data_products"]}
    entities: dict[str, set[str]] = {}
    for grant in catalogue["grants"]:
        if ("entity" in grant) != (scope[grant["product"]] == "entity"):
            raise ValueError(
                f"an entity is named exactly when the product is entity-scoped: {grant}"
            )
        if grant["persona"] == persona and grant["access"] in READS:
            entities.setdefault(grant["product"], set()).update(
                [grant["entity"]] if "entity" in grant else []
            )
    reads = {
        product: frozenset(seen) if scope[product] == "entity" else None
        for product, seen in entities.items()
    }
    return Identity(persona, reads)


def open_local(who: Identity, warehouse: Path, directory: Path) -> duckdb.DuckDBPyConnection:
    """Build `<persona>.duckdb` in `directory` from `warehouse` and connect to it as the persona.

    Persona and product ids are schema-checked slugs, so quoting them in the SQL is safe.
    """
    database = str(directory / f"{who.persona}.duckdb")
    source = str(warehouse).replace("'", "''")
    with duckdb.connect(database) as build:
        # DuckDB renames a file whose stem it reserves (`system` opens as `system_db`), and the
        # build attaches the source as SOURCE: either way the catalog could not be the persona.
        (catalog,) = build.execute("select system.main.current_database()").fetchone() or ("",)
        if catalog != who.persona or who.persona == SOURCE:
            raise ReservedPersonaError(
                f"{who.persona} cannot name a DuckDB catalog: it is reserved or used by the build"
            )
        build.execute(f"attach '{source}' as {SOURCE} (read_only)")
        for product, entities in who.reads.items():
            copy = f'create table "{product}" as select * from {SOURCE}."{product}"'  # noqa: S608
            if entities is None:
                build.execute(copy)
            else:
                build.execute(
                    f"{copy} where {ENTITY} in (select unnest(?::varchar[]))", [sorted(entities)]
                )
        build.execute(f"detach {SOURCE}")
    return duckdb.connect(
        database,
        read_only=True,
        config={"enable_external_access": False, "lock_configuration": True},
    )
