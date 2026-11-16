"""The copy of the payments contract `models/contracts/warehouse.py` tests BigQuery with: one
BigQuery server, BigQuery's physical types, and nothing else changed. The run itself needs the lab
project (`python models/contracts/warehouse.py`)."""

import copy

import pytest
import yaml

from models.contracts import warehouse


def contract() -> dict:
    return yaml.safe_load(warehouse.CONTRACT.read_text())


def test_the_copy_has_one_bigquery_server_on_the_dataset_the_mart_is_built_in() -> None:
    servers = warehouse.for_bigquery(contract(), "the-lab", "the-region")["servers"]
    assert servers == [
        {
            "server": "bigquery",
            "type": "bigquery",
            "project": "the-lab",
            "dataset": "intermediate",
            "customProperties": [{"property": "location", "value": "the-region"}],
        }
    ]


def test_the_copy_changes_only_the_servers_and_the_physical_types() -> None:
    committed = contract()
    bigquery = warehouse.for_bigquery(copy.deepcopy(committed), "the-lab", "the-region")
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
        warehouse.for_bigquery(changed, "the-lab", "the-region")
