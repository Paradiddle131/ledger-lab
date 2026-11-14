"""Each persona in `access/personas.yaml` as an identity a test can act as.

On the local engine (DuckDB, which has no users or roles) acting as a persona means holding a
connection to a database built for it, the file `<persona>.duckdb`, so its catalog is named after
the persona and the connection can neither rename it nor switch to another; `access/compile.py`
builds the file, holding only the rows, column values and aggregates the persona may see, and
opens it read-only, with every file outside it out of reach, no extension to load, and its
configuration locked.

DuckDB lets any connection create temporary objects, and an unqualified name resolves to a
temporary one first, so a connection can shadow a product or `current_database()` within itself.
A shadow holds only what that connection could already read or write as literals; the persona's
reach is read through qualified names, `system.main.current_database()` and
`"<persona>".main."<table>"`, which no temporary object can shadow.

On BigQuery a test will impersonate the persona's service account,
`<persona>@<project>.iam.gserviceaccount.com`, and the warehouse's own rules decide what it sees;
the caller passes the project, which comes only from `platform/gcp_project.py`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Accesses that let a persona read a product's rows: `edit` changes definitions it must see.
# `aggregate` gives only the product's aggregate tables (access/compile.py), and `draft` and `post`
# act through the proposal and approval workflow, which carries the entry, so they read nothing.
READS = frozenset({"read", "edit"})


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
