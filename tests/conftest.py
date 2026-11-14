"""Shared loaders for the catalogues kept as data in the tree, and the persona identities."""

import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
import yaml
from jsonschema import Draft202012Validator

from access.compile import open_persona

if TYPE_CHECKING:
    from tests.models.dbt_build import Build

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


def landed_file(name: str) -> Path:
    """A landed file under the landing path; a landed test skips when it is not landed."""
    from ingest.fetch import landing_path

    path = landing_path() / name
    if not path.exists():
        pytest.skip(f"{name} is not landed")
    return path


@pytest.fixture(scope="session")
def build(tmp_path_factory: pytest.TempPathFactory) -> "Build":
    """The whole dbt project built once over the committed samples (`tests/models/dbt_build.py`)."""
    from tests.models.dbt_build import built

    return built(tmp_path_factory.mktemp("dbt"), "sample")


def load_access() -> dict[str, Any]:
    return load_checked(REPO / "access/personas.yaml", REPO / "access/schema.json")


@pytest.fixture(scope="session")
def access() -> dict[str, Any]:
    return load_access()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """A test taking `persona` runs once per persona, `agent` once per persona of kind agent."""
    for name, kinds in (("persona", {"human", "agent"}), ("agent", {"agent"})):
        if name in metafunc.fixturenames:
            personas = [p["id"] for p in load_access()["personas"] if p["kind"] in kinds]
            metafunc.parametrize(name, personas)


def load_columns() -> dict[str, Any]:
    return load_checked(REPO / "access/columns.yaml", REPO / "access/columns.schema.json")


@pytest.fixture(scope="session")
def columns() -> dict[str, Any]:
    return load_columns()


@pytest.fixture(scope="session")
def visibility() -> dict[str, Any]:
    return load_checked(REPO / "access/visibility.yaml", REPO / "access/visibility.schema.json")


@pytest.fixture(scope="session")
def glossary() -> dict[str, Any]:
    return load_checked(REPO / "semantics/glossary.yaml", REPO / "semantics/glossary.schema.json")


@pytest.fixture(scope="session")
def controls() -> dict[str, Any]:
    return load_checked(REPO / "controls/catalogue.yaml", REPO / "controls/schema.json")


def load_aggregates() -> dict[str, Any]:
    return load_checked(REPO / "access/aggregates.yaml", REPO / "access/aggregates.schema.json")


@pytest.fixture(scope="session")
def aggregates() -> dict[str, Any]:
    return load_aggregates()


@pytest.fixture(scope="session")
def warehouse(build: "Build") -> Path:
    """The warehouse the personas read: the dbt project built over the committed samples."""
    return build.database


@pytest.fixture
def as_persona(
    access: dict[str, Any],
    columns: dict[str, Any],
    aggregates: dict[str, Any],
    warehouse: Path,
    tmp_path: Path,
) -> Iterator[Callable[[str], duckdb.DuckDBPyConnection]]:
    """`as_persona(<id>)`: a local engine connection to a database compiled for the persona, holding
    only its rows, column values and aggregates (`access/compile.py`); each database, derived data
    with hashed names in it, is deleted when the test ends."""
    opened: list[duckdb.DuckDBPyConnection] = []
    directories: list[Path] = []

    def act(persona: str) -> duckdb.DuckDBPyConnection:
        directory = tmp_path / f"persona-{len(directories)}"
        directory.mkdir()
        directories.append(directory)
        opened.append(open_persona(access, columns, aggregates, persona, warehouse, directory))
        return opened[-1]

    yield act
    for db in opened:
        db.close()
    for directory in directories:
        shutil.rmtree(directory)
