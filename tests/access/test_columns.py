"""The policy tags on sensitive columns, cross-checked against the persona catalogue.

Every persona that reads the warehouse reads every tag either in clear or masked, never both, so no
query it may run is refused on a tagged column; a persona that reads nothing there is neither; and
only the people who need the value itself read it in clear.
"""

from collections import Counter
from typing import Any

from access.identities import READS


def warehouse_readers(access: dict[str, Any]) -> set[str]:
    return {g["persona"] for g in access["grants"] if g["access"] in READS}


def test_tag_ids_are_unique(columns: dict[str, Any]) -> None:
    counts = Counter(t["id"] for t in columns["tags"])
    assert [i for i, n in counts.items() if n > 1] == []


def test_masking_rule_ids_stay_unique_once_hyphens_become_underscores(
    columns: dict[str, Any],
) -> None:
    ids = [t["id"].replace("-", "_") for t in columns["tags"]]
    assert len(set(ids)) == len(ids)


def test_every_reader_is_a_known_persona(columns: dict[str, Any], access: dict[str, Any]) -> None:
    personas = {p["id"] for p in access["personas"]}
    for tag in columns["tags"]:
        assert set(tag["clear"]) | set(tag["masked"]) <= personas, tag["id"]


def test_each_warehouse_reader_reads_each_tag_in_clear_or_masked_never_both(
    columns: dict[str, Any], access: dict[str, Any]
) -> None:
    readers = warehouse_readers(access)
    for tag in columns["tags"]:
        clear, masked = set(tag["clear"]), set(tag["masked"])
        assert clear & masked == set(), tag["id"]
        assert clear | masked == readers, tag["id"]


def test_a_persona_reading_nothing_in_the_warehouse_reads_no_tag(
    columns: dict[str, Any], access: dict[str, Any]
) -> None:
    rowless = {p["id"] for p in access["personas"]} - warehouse_readers(access)
    assert rowless, "the proposer reads nothing in the warehouse"
    for tag in columns["tags"]:
        assert rowless.isdisjoint(tag["clear"] + tag["masked"]), tag["id"]


def test_no_agent_persona_reads_a_tag_in_clear(
    columns: dict[str, Any], access: dict[str, Any]
) -> None:
    agents = {p["id"] for p in access["personas"] if p["kind"] == "agent"}
    for tag in columns["tags"]:
        assert agents.isdisjoint(tag["clear"]), tag["id"]


def test_the_auditor_reads_every_tag_in_clear(columns: dict[str, Any]) -> None:
    # The auditor "reads everything, changes nothing" in the persona catalogue.
    assert [t["id"] for t in columns["tags"] if "auditor" not in t["clear"]] == []


def test_entity_accountants_read_every_tag_masked(
    columns: dict[str, Any], access: dict[str, Any]
) -> None:
    accountants = {f"accountant-{e['id']}" for e in access["entities"]}
    for tag in columns["tags"]:
        assert accountants <= set(tag["masked"]), tag["id"]
