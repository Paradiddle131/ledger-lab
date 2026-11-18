"""Shared loaders for the catalogues kept as data in the tree, and the persona identities."""

import json
import logging
import os
import shutil
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator

from access.bigquery import PersonaQueryError, PersonaSession, chain, mint
from access.compile import open_persona
from access.identities import identity

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
    """A test taking `persona` runs once per persona, `human` once per persona of kind human and
    `agent` once per persona of kind agent."""
    kinds_of = {"persona": {"human", "agent"}, "human": {"human"}, "agent": {"agent"}}
    for name, kinds in kinds_of.items():
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


def load_purposes() -> dict[str, Any]:
    return load_checked(REPO / "access/purposes.yaml", REPO / "access/purposes.schema.json")


@pytest.fixture(scope="session")
def purposes() -> dict[str, Any]:
    return load_purposes()


@pytest.fixture(scope="session")
def warehouse(build: "Build") -> Path:
    """The warehouse the personas read: the dbt project built over the committed samples."""
    return build.database


@pytest.fixture
def as_persona(
    access: dict[str, Any],
    columns: dict[str, Any],
    aggregates: dict[str, Any],
    purposes: dict[str, Any],
    warehouse: Path,
    tmp_path: Path,
) -> Iterator[Callable[[str], duckdb.DuckDBPyConnection]]:
    """`as_persona(<id>)`: a local engine connection to a database compiled for the persona, holding
    only its purposes' rows and columns, its column values and its aggregates
    (`access/compile.py`); each database, derived data with hashed names in it, is deleted when the
    test ends."""
    opened: list[duckdb.DuckDBPyConnection] = []
    directories: list[Path] = []

    def act(persona: str) -> duckdb.DuckDBPyConnection:
        directory = tmp_path / f"persona-{len(directories)}"
        directory.mkdir()
        directories.append(directory)
        opened.append(
            open_persona(access, columns, aggregates, purposes, persona, warehouse, directory)
        )
        return opened[-1]

    yield act
    for db in opened:
        db.close()
    for directory in directories:
        shutil.rmtree(directory)


@pytest.fixture
def as_persona_on_bigquery(
    access: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], PersonaSession]]:
    """`as_persona_on_bigquery(<id>)`: the BigQuery mode of `as_persona`, a session on the lab's
    BigQuery that carries a token minted for the persona's service account, so the warehouse's own
    grants decide what it sees (`access/bigquery.py`). It needs the lab project, read only through
    `platform/gcp_project.py`, which refuses a session carrying another lab's `GCP_PROJECT`, and a
    login that may mint the token, off the cloud session's proxy; the tokens live only in memory,
    and a test using it reports a failure as `pytest_runtest_makereport` below writes it."""
    # Imported here: platform/ is on pytest's path, not on a child process's that loads this file.
    from gcp_project import lab_project

    # httpx logs each request's URL, which names the project, to pytest's captured and live logs.
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", True)
    project = lab_project()
    with httpx.Client(timeout=60) as client:

        def act(persona: str) -> PersonaSession:
            account = identity(access, persona).service_account(project)
            return PersonaSession(project, mint(chain(account, project, os.environ)), client)

        yield act


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """A failure of a test holding a persona's token, in any phase, reports one line: the test's
    parameters and either its own verdict (a `pytest.fail`), a `PersonaQueryError`'s text, both
    worded to name no account or project, or only the type of any other error. pytest would
    otherwise render a traceback whose frames, locals and chained errors name the project, the
    accounts, the token and paths under the login's home, under any `--tb`, `-l` or
    `--full-trace`."""
    report = yield
    holds_a_token = (
        isinstance(item, pytest.Function) and "as_persona_on_bigquery" in item.fixturenames
    )
    if holds_a_token and report.failed and call.excinfo:
        error = call.excinfo.value
        trusted = isinstance(error, PersonaQueryError | pytest.fail.Exception)
        said = str(error) if trusted else f"{type(error).__name__}, its message left out"
        callspec = getattr(item, "callspec", None)
        report.longrepr = f"{callspec.id if callspec else item.name}: {said}"
    return report
