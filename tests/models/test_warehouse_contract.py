"""The copy of the payments contract `models/contracts/warehouse.py` tests BigQuery with (one
BigQuery server whose project is a placeholder, BigQuery's physical types, nothing else changed),
what the run refuses before starting the engine, and the restriction it puts on the engine's
connection, against stubs. The run itself needs the lab project
(`python models/contracts/warehouse.py`)."""

import copy
import subprocess
from pathlib import Path
from types import SimpleNamespace
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
            assert after["physicalType"] == warehouse.TYPES[before["physicalType"]]
            assert {k: v for k, v in after.items() if k != "physicalType"} == {
                k: v for k, v in before.items() if k != "physicalType"
            }


def test_a_type_with_no_bigquery_type_stops_the_copy() -> None:
    changed = contract()
    changed["schema"][0]["properties"][0]["physicalType"] = "HUGEINT"
    with pytest.raises(ValueError, match="no BigQuery type for HUGEINT"):
        warehouse.for_bigquery(changed, "the-region")


class Engine:
    """The engine's command, run in place of `uv run ...`: what the copy it is handed holds."""

    def __init__(self) -> None:
        self.copies: list[str] = []

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.copies.append(Path(command[-1]).read_text())
        return subprocess.CompletedProcess(command, 0, "result: ResultEnum.passed\n", "")


def start(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> Engine:
    for name in ("LEDGER_LAB_GCP_PROJECT", "LEDGER_LAB_BQ_LOCATION", "GCP_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    engine = Engine()
    monkeypatch.setattr(warehouse.subprocess, "run", engine)
    return engine


def test_the_copy_on_disk_holds_no_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    project = "example-project"
    env = {"LEDGER_LAB_GCP_PROJECT": project, "LEDGER_LAB_BQ_LOCATION": "us-central1"}
    engine = start(monkeypatch, env)
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
