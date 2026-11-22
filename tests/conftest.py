"""Shared loaders for the catalogues kept as data in the tree, and the persona identities."""

import json
import logging
import os
import shutil
import signal
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
from agent.ask import Audit
from agent.audit import FileLog
from tests import options_check, workers
from tests.models.dbt_build import SAMPLED_SOURCES, git_out

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


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """`-n auto`'s worker count, capped by the memory available (`tests/workers.py`); with
    `PYTEST_XDIST_AUTO_NUM_WORKERS` set to a count, xdist's own reading of it decides. Optional,
    so a run without xdist does not stop on a hook it does not know."""
    if workers.count_set(os.environ):
        return None
    return workers.auto_workers(workers.usable_cpus(), workers.available_memory())


def pytest_configure(config: pytest.Config) -> None:
    """Stop any run whose options let a plain run select other tests than the declared markers
    leave, and load the plugin that fails a run in which a conftest holds `collect_ignore`
    (`tests/options_check.py`); both run whatever the run selects."""
    if found := options_check.problems(REPO, config.inipath):
        raise pytest.UsageError("pytest's options: " + "; ".join(found))
    if not config.pluginmanager.is_registered(options_check):
        config.pluginmanager.register(options_check, "tests.options_check")


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


def sample_build_data(build: "Build") -> dict[str, Any]:
    """An audit record's `data` for the session's sample build (`tests/models/dbt_build.py`)."""
    from tests.models.dbt_build import build_data

    return build_data(build.cwd)


@pytest.fixture
def audit(build: "Build", tmp_path: Path) -> Audit:
    """Where an `ask` in a test writes its record: a `FileLog` under the test's own folder, with
    the checkout's HEAD as the release and the session's sample build as the data."""
    log = FileLog(tmp_path / "audit" / "log.jsonl", tmp_path / "audit" / "kept")
    log.path.parent.mkdir()
    return Audit(log, git_out("rev-parse", "HEAD"), sample_build_data(build))


__all__ = ["SAMPLED_SOURCES", "git_out"]


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

    # httpx logs each request's URL, which names the project, to pytest's captured and live logs;
    # httpcore logs each connection's host and the answer's headers, and a log format with the
    # record's path prints the checkout's. httpcore logs through child loggers, which a parent's
    # `disabled` does not reach, so both are held at WARNING, a level every child without its own
    # inherits.
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.WARNING)
    project = lab_project()
    try:
        with httpx.Client(timeout=60) as client:

            def act(persona: str) -> PersonaSession:
                account = identity(access, persona).service_account(project)
                return PersonaSession(project, mint(chain(account, project, os.environ)), client)

            yield act
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def holds_a_persona_token(item: pytest.Item) -> bool:
    return isinstance(item, pytest.Function) and "as_persona_on_bigquery" in item.fixturenames


def persona_test_id(item: pytest.Item) -> str:
    callspec = getattr(item, "callspec", None)
    return callspec.id if callspec else item.name


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None]:
    """An interrupt (Ctrl-C) while a test holding a persona's token runs ends the run with one
    line naming the test, exit status 2. pytest would otherwise report the interrupt itself, past
    `pytest_runtest_makereport`: the interrupted frame's path, which on the owner's machine is
    under the login's home, and under `--full-trace` every frame's arguments, which name the
    project and the accounts. An interrupt between tests is pytest's own and holds neither."""
    if not holds_a_persona_token(item):
        return (yield)
    try:
        return (yield)
    except KeyboardInterrupt:
        # A second Ctrl-C, common on a hung mint, would land while pytest still renders this one,
        # past every hook here: the run is ending, so later ones are ignored.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        item.config.stash[PERSONA_INTERRUPTED] = True
        stop = pytest.exit.Exception(
            f"{persona_test_id(item)}: interrupted", returncode=pytest.ExitCode.INTERRUPTED
        )
        raise stop from None


PERSONA_INTERRUPTED = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> Generator[None]:
    """After an interrupted persona test, pytest tears its fixtures down here, at the session's
    end, past both hooks above; a second interrupt or an error there would print a traceback
    through the checkout. It ends the run with one line instead, which pytest prints as such."""
    try:
        return (yield)
    except BaseException as error:
        if not session.config.stash.get(PERSONA_INTERRUPTED, False):
            raise
        stop = pytest.exit.Exception(
            f"a persona test's cleanup after the interrupt failed: {type(error).__name__}",
            returncode=pytest.ExitCode.INTERRUPTED,
        )
        raise stop from None


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
    if holds_a_persona_token(item) and report.failed and call.excinfo:
        error = call.excinfo.value
        trusted = isinstance(error, PersonaQueryError | pytest.fail.Exception)
        said = str(error) if trusted else f"{type(error).__name__}, its message left out"
        report.longrepr = f"{persona_test_id(item)}: {said}"
    return report
