"""The copy of the payments contract `models/contracts/warehouse.py` tests BigQuery with (one
BigQuery server whose project is a placeholder, BigQuery's physical types, nothing else changed),
what the run refuses before starting the engine, what the engine is started with and what the run
prints and returns, and what the engine refuses and restricts on the connection it opens, against
stubs of the engine's command and of the CLI. The run itself needs the lab project
(`python models/contracts/warehouse.py`)."""

import copy
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from ingest.load import LoadError
from models.contracts import warehouse


def contract() -> dict:
    return yaml.safe_load(warehouse.CONTRACT.read_text())


def test_the_copy_has_one_bigquery_server_on_the_profiles_dataset() -> None:
    servers = warehouse.for_bigquery(contract(), "the-region")["servers"]
    assert servers == [
        {
            "server": "bigquery",
            "type": "bigquery",
            "project": warehouse.PROJECT_PLACEHOLDER,
            "dataset": "intermediate",
            "customProperties": [{"property": "location", "value": "the-region"}],
        }
    ]


# BigQuery's type for each DuckDB type the contract declares, written out here, not read from
# `warehouse.TYPES`, so a change to that map shows.
BIGQUERY_TYPES = {
    "VARCHAR": "STRING",
    "BIGINT": "INT64",
    "BOOLEAN": "BOOL",
    "DATE": "DATE",
    "DECIMAL(28,6)": "NUMERIC",
}


def test_the_copy_changes_only_the_servers_and_the_physical_types() -> None:
    committed = contract()
    bigquery = warehouse.for_bigquery(copy.deepcopy(committed), "the-region")
    assert {k: v for k, v in bigquery.items() if k not in {"servers", "schema"}} == {
        k: v for k, v in committed.items() if k not in {"servers", "schema"}
    }
    for theirs, ours in zip(committed["schema"], bigquery["schema"], strict=True):
        assert {k: v for k, v in ours.items() if k != "properties"} == {
            k: v for k, v in theirs.items() if k != "properties"
        }
        for before, after in zip(theirs["properties"], ours["properties"], strict=True):
            assert after["physicalType"] == BIGQUERY_TYPES[before["physicalType"]]
            assert {k: v for k, v in after.items() if k != "physicalType"} == {
                k: v for k, v in before.items() if k != "physicalType"
            }


def test_a_type_with_no_bigquery_type_stops_the_copy() -> None:
    changed = contract()
    changed["schema"][0]["properties"][0]["physicalType"] = "HUGEINT"
    with pytest.raises(ValueError, match="no BigQuery type for HUGEINT"):
        warehouse.for_bigquery(changed, "the-region")


class Engine:
    """The engine's command, run in place of `uv run ...`: what the copy it is handed holds, the
    environment and folder it is started with, and what it answers."""

    def __init__(self, stdout: str = "result: ResultEnum.passed\n", status: int = 0) -> None:
        self.stdout, self.status = stdout, status
        self.copies: list[str] = []
        self.folders: list[Path] = []
        self.envs: list[dict[str, str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.copies.append(Path(command[-1]).read_text())
        self.folders.append(Path(command[-1]).parent)
        self.envs.append(kwargs.get("env", dict(os.environ)))
        return subprocess.CompletedProcess(command, self.status, self.stdout, "")


ENV = {"LEDGER_LAB_GCP_PROJECT": "example-project", "LEDGER_LAB_BQ_LOCATION": "us-central1"}


def start(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], engine: Engine | None = None
) -> Engine:
    for name in ("LEDGER_LAB_GCP_PROJECT", "LEDGER_LAB_BQ_LOCATION", "GCP_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    engine = engine or Engine()
    monkeypatch.setattr(warehouse.subprocess, "run", engine)
    return engine


SETTINGS = [
    "DATACONTRACT_BIGQUERY_LOCATION",
    "DATACONTRACT_BIGQUERY_DATASET",
    "DATACONTRACT_BIGQUERY_BILLING_PROJECT",
    "DATACONTRACT_BIGQUERY_ACCOUNT_INFO_JSON_PATH",
    "DATACONTRACT_BIGQUERY_IMPERSONATION_ACCOUNT",
]


@pytest.mark.parametrize("name", SETTINGS)
def test_the_clis_own_settings_do_not_reach_the_engine(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    engine = start(monkeypatch, ENV | {name: "elsewhere"})
    assert warehouse.main() == 0
    assert name not in engine.envs[0]
    assert engine.envs[0]["LEDGER_LAB_BQ_LOCATION"] == "us-central1"


# Names the engine must not be handed: the individuals' hash key, another lab's project, the CLI's
# settings under each prefix it reads, and a name the allowlist has never heard of.
DROPPED = [
    "LEDGER_LAB_HASH_KEY",
    "GCP_PROJECT",
    "DATACONTRACT_BIGQUERY_PROJECT",
    "ENTROPY_DATA_API_KEY",
    "DATAMESH_MANAGER_API_KEY",
    "SOME_UNKNOWN_NAME",
]
# Names the engine needs to fetch the CLI through a proxy and open the lab's connection, written
# out here, not read from `warehouse.ENGINE_NAMES`, so a change to that list shows.
KEPT = [
    "PATH",
    "HOME",
    "TMPDIR",
    "UV",
    "UV_CACHE_DIR",
    "UV_SYSTEM_CERTS",
    "XDG_CACHE_HOME",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "LEDGER_LAB_GCP_PROJECT",
    "LEDGER_LAB_BQ_LOCATION",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
]


@pytest.mark.parametrize("name", DROPPED)
def test_the_engine_environment_drops_a_name_outside_the_allowlist(name: str) -> None:
    assert name not in warehouse.engine_environment({name: "stand-in", "PATH": "/bin"})


@pytest.mark.parametrize("name", KEPT)
def test_the_engine_environment_keeps_what_uv_and_the_connection_need(name: str) -> None:
    assert warehouse.engine_environment({name: "stand-in"}) == {name: "stand-in"}


def test_the_hash_key_does_not_reach_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = start(monkeypatch, ENV | {"LEDGER_LAB_HASH_KEY": "an-invented-key"})
    assert warehouse.main() == 0
    assert "LEDGER_LAB_HASH_KEY" not in engine.envs[0]
    assert "an-invented-key" not in engine.envs[0].values()
    assert engine.envs[0]["LEDGER_LAB_GCP_PROJECT"] == "example-project"


def test_the_project_id_in_the_engines_output_is_masked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    start(monkeypatch, ENV, Engine("ran on example-project\nresult: ResultEnum.passed\n"))
    assert warehouse.main() == 0
    out = capsys.readouterr().out
    assert "ran on <project>" in out
    assert "example-project" not in out


def test_a_failed_contract_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    start(monkeypatch, ENV, Engine("result: ResultEnum.failed\n", status=1))
    assert warehouse.main() == 1


def test_the_copys_folder_is_outside_the_repository_and_gone_after_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = start(monkeypatch, ENV)
    assert warehouse.main() == 0
    folder = engine.folders[0]
    assert not folder.resolve().is_relative_to(warehouse.REPO)
    assert not folder.exists()


class Client:
    def __init__(self) -> None:
        self.default_query_job_config = SimpleNamespace(maximum_bytes_billed=None)


class Connection:
    def __init__(self) -> None:
        self.client = Client()
        self.storage_client = object()


# The CLI's BigQuery settings as datacontract-cli 1.2.9 names them, written out here, not read
# from `warehouse.GETTERS`, so a change to that list shows.
BIGQUERY_SETTINGS = [
    "location",
    "dataset",
    "billing_project",
    "project",
    "account_info_json_path",
    "impersonation_account",
    "kms_key_name",
]


class Settings:
    """The CLI's settings: `get_bigquery_*` getters, each answering its value in `values` or None,
    less any named in `without`, plus any named in `extra`."""

    def __init__(
        self,
        values: dict[str, str] | None = None,
        without: tuple[str, ...] = (),
        extra: tuple[str, ...] = (),
    ) -> None:
        values = values or {}
        for name in [*BIGQUERY_SETTINGS, *extra]:
            if name not in without:
                setattr(self, f"get_bigquery_{name}", lambda name=name: values.get(name))


def run_engine(
    monkeypatch: pytest.MonkeyPatch, project: str, settings: Settings
) -> tuple[int, list[tuple[Any, Any]]]:
    """`engine()` with the CLI stood in for: its connect module records the server each
    connection is opened on, and its DataContract opens one connection, as a test run does."""
    opened: list[tuple[Any, Any]] = []
    connect = ModuleType("connect")

    def open_bigquery(ibis: Any, server: Any, config: Any) -> Connection:
        con = Connection()
        opened.append((SimpleNamespace(**vars(server)), con))
        return con

    connect._bigquery_credentials = lambda config: None  # type: ignore[attr-defined]
    connect._connect_bigquery = open_bigquery  # type: ignore[attr-defined]

    class DataContract:
        def __init__(self, data_contract_file: str, server: str) -> None:
            assert server == "bigquery"

        def test(self) -> Any:
            server = SimpleNamespace(project=project, dataset="intermediate")
            connect._connect_bigquery(None, server, settings)  # type: ignore[attr-defined]
            return SimpleNamespace(checks=[], result="passed", has_passed=lambda: True)

    data_contract = ModuleType("datacontract.data_contract")
    data_contract.DataContract = DataContract  # type: ignore[attr-defined]
    connections = ModuleType("datacontract.engines.ibis.connections")
    connections.connect = connect  # type: ignore[attr-defined]
    for name in ("datacontract", "datacontract.engines", "datacontract.engines.ibis"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, "datacontract.data_contract", data_contract)
    monkeypatch.setitem(sys.modules, "datacontract.engines.ibis.connections", connections)
    for name in ("GCP_PROJECT", "CLOUDSDK_AUTH_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEDGER_LAB_GCP_PROJECT", "the-lab")
    return warehouse.engine("copy.odcs.yaml"), opened


def test_the_engine_opens_the_lab_project_with_every_job_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, opened = run_engine(monkeypatch, warehouse.PROJECT_PLACEHOLDER, Settings())
    assert status == 0
    [(server, con)] = opened
    assert server.project == "the-lab"
    assert con.client.default_query_job_config.maximum_bytes_billed == 1_000_000_000
    assert con.storage_client is None
    assert con.client._ensure_bqstorage_client() is None


def test_the_engine_refuses_a_copy_that_names_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="names a project"):
        run_engine(monkeypatch, "another-lab", Settings())


def test_the_engine_checks_the_seven_getters_of_the_pinned_cli() -> None:
    assert sorted(warehouse.GETTERS) == sorted(f"get_bigquery_{s}" for s in BIGQUERY_SETTINGS)


@pytest.mark.parametrize("setting", BIGQUERY_SETTINGS)
def test_the_engine_refuses_settings_that_lack_a_getter(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    """A CLI that renamed or dropped one of the getters stops the run before any connection."""
    with pytest.raises(RuntimeError, match=f"lack the getters.*get_bigquery_{setting}"):
        run_engine(monkeypatch, warehouse.PROJECT_PLACEHOLDER, Settings(without=(setting,)))


def test_the_engine_refuses_settings_with_no_getters(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="lack the getters"):
        run_engine(
            monkeypatch, warehouse.PROJECT_PLACEHOLDER, Settings(without=tuple(BIGQUERY_SETTINGS))
        )


def test_the_engine_refuses_a_getter_it_does_not_name_that_holds_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings({"region": "elsewhere"}, extra=("region",))
    with pytest.raises(RuntimeError, match="get_bigquery_region"):
        run_engine(monkeypatch, warehouse.PROJECT_PLACEHOLDER, settings)


@pytest.mark.parametrize("setting", BIGQUERY_SETTINGS)
def test_the_engine_refuses_a_cli_setting_that_overrides_the_copy(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    with pytest.raises(RuntimeError, match=f"get_bigquery_{setting}"):
        run_engine(monkeypatch, warehouse.PROJECT_PLACEHOLDER, Settings({setting: "elsewhere"}))


def test_the_copy_on_disk_holds_no_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    project = "example-project"
    engine = start(monkeypatch, ENV)
    assert warehouse.main() == 0
    assert len(engine.copies) == 1
    assert project not in engine.copies[0]
    assert warehouse.PROJECT_PLACEHOLDER in engine.copies[0]


@pytest.mark.parametrize("where", [None, "US", "EU", "europe-west1"])
def test_a_region_outside_the_free_tier_is_refused_before_the_engine_starts(
    monkeypatch: pytest.MonkeyPatch, where: str | None
) -> None:
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab"} | (
        {"LEDGER_LAB_BQ_LOCATION": where} if where else {}
    )
    engine = start(monkeypatch, env)
    with pytest.raises(LoadError, match="LEDGER_LAB_BQ_LOCATION"):
        warehouse.main()
    assert engine.copies == []


def test_the_engines_connection_caps_every_job_and_reads_over_rest() -> None:
    class Client:
        default_query_job_config = SimpleNamespace(maximum_bytes_billed=None)

    con = SimpleNamespace(client=Client(), storage_client=object())
    assert warehouse.restrict(con) is con
    assert con.client.default_query_job_config.maximum_bytes_billed == 1_000_000_000
    assert con.storage_client is None
    assert Client()._ensure_bqstorage_client() is None  # type: ignore[attr-defined]
