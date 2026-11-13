"""Shared loaders for the catalogues kept as data in the tree, and the persona identities."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from jsonschema import Draft202012Validator

from access.identities import ENTITY, identity, open_local

REPO = Path(__file__).resolve().parents[1]


def load_checked(data: Path, schema: Path) -> dict[str, Any]:
    """Load a YAML catalogue and fail with every schema error it has."""
    doc = yaml.safe_load(data.read_text())
    validator = Draft202012Validator(json.loads(schema.read_text()))
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(doc)
    ]
    assert errors == [], f"{data.relative_to(REPO)} breaks {schema.relative_to(REPO)}"
    return doc


@pytest.fixture(scope="session")
def access() -> dict[str, Any]:
    return load_checked(REPO / "access/personas.yaml", REPO / "access/schema.json")


@pytest.fixture(scope="session")
def glossary() -> dict[str, Any]:
    return load_checked(REPO / "semantics/glossary.yaml", REPO / "semantics/glossary.schema.json")


@pytest.fixture(scope="session")
def controls() -> dict[str, Any]:
    return load_checked(REPO / "controls/catalogue.yaml", REPO / "controls/schema.json")


@pytest.fixture(scope="session")
def warehouse(access: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A local warehouse holding every data product: one row per entity, or one for a group product.

    It stands in for the built models until they produce the products; ids are schema-checked slugs.
    """
    path = tmp_path_factory.mktemp("warehouse") / "warehouse.duckdb"
    entities = [e["id"] for e in access["entities"]]
    with duckdb.connect(str(path)) as db:
        for product in access["data_products"]:
            name = product["id"]
            if product["scope"] == "entity":
                db.execute(f'create table "{name}" ({ENTITY} varchar, holds varchar)')
                db.executemany(
                    f'insert into "{name}" values (?, ?)',  # noqa: S608
                    [[entity, product["holds"]] for entity in entities],
                )
            else:
                db.execute(f'create table "{name}" as select ? as holds', [product["holds"]])
    return path


@pytest.fixture
def as_persona(
    access: dict[str, Any], warehouse: Path, tmp_path: Path
) -> Iterator[Callable[[str], duckdb.DuckDBPyConnection]]:
    """`as_persona(<id>)`: a local engine connection that reaches only the persona's grants."""
    opened: list[duckdb.DuckDBPyConnection] = []

    def act(persona: str) -> duckdb.DuckDBPyConnection:
        who = identity(access, persona)
        opened.append(open_local(who, warehouse, tmp_path / f"{len(opened)}-{persona}.duckdb"))
        return opened[-1]

    yield act
    for db in opened:
        db.close()
