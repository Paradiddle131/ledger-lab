"""The persona set and its grants: shape, cross-references and the duties no persona may combine."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


def ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in items]


def test_ids_are_unique(access: dict[str, Any]) -> None:
    for section in ("entities", "data_products", "personas"):
        repeated = [i for i, n in Counter(ids(access[section])).items() if n > 1]
        assert repeated == [], section


def test_every_grant_names_a_known_persona_entity_and_product(access: dict[str, Any]) -> None:
    personas, entities = set(ids(access["personas"])), set(ids(access["entities"]))
    products = set(ids(access["data_products"]))
    for grant in access["grants"]:
        assert grant["persona"] in personas, grant
        assert grant["product"] in products, grant
        if "entity" in grant:
            assert grant["entity"] in entities, grant


def test_entity_products_name_an_entity_and_group_products_none(access: dict[str, Any]) -> None:
    scope = {p["id"]: p["scope"] for p in access["data_products"]}
    for grant in access["grants"]:
        assert ("entity" in grant) == (scope[grant["product"]] == "entity"), grant


def test_one_grant_per_persona_entity_and_product(access: dict[str, Any]) -> None:
    keys = Counter((g["persona"], g.get("entity"), g["product"]) for g in access["grants"])
    assert [k for k, n in keys.items() if n > 1] == []


def test_every_persona_holds_a_grant(access: dict[str, Any]) -> None:
    granted = {g["persona"] for g in access["grants"]}
    assert set(ids(access["personas"])) - granted == set()


def test_each_entity_has_exactly_one_accountant_seeing_only_its_payments(
    access: dict[str, Any],
) -> None:
    for entity in ids(access["entities"]):
        accountant = f"accountant-{entity}"
        assert accountant in ids(access["personas"]), entity
        seen = {
            g["entity"]
            for g in access["grants"]
            if g["persona"] == accountant and g["product"] == "payments"
        }
        assert seen == {entity}


def test_an_agent_holds_no_data_role_and_only_drafts(access: dict[str, Any]) -> None:
    agents = {p["id"] for p in access["personas"] if p["kind"] == "agent"}
    assert agents, "the acting agent's identity is a persona"
    for grant in access["grants"]:
        if grant["persona"] in agents:
            assert grant["access"] == "draft", grant


def test_drafting_and_posting_never_share_an_identity(access: dict[str, Any]) -> None:
    by_persona: dict[str, set[str]] = {}
    for grant in access["grants"]:
        by_persona.setdefault(grant["persona"], set()).add(grant["access"])
    assert [p for p, held in by_persona.items() if {"draft", "post"} <= held] == []


def test_write_access_is_only_on_products_that_take_writes(access: dict[str, Any]) -> None:
    writable = {"ledger": {"draft", "post", "read"}, "metric-definitions": {"edit", "read"}}
    for grant in access["grants"]:
        allowed = writable.get(grant["product"], {"read", "aggregate"})
        assert grant["access"] in allowed, grant


def test_the_schema_refuses_an_id_no_service_account_can_take() -> None:
    schema = json.loads((Path(__file__).parents[1] / "access/schema.json").read_text())
    validator = Draft202012Validator(schema)
    ok = {"id": "auditor", "name": "Auditor", "kind": "human", "duties": "Reads."}
    full = {
        "entities": [{"id": "city", "name": "City", "currency": "USD", "brings": "Payments."}],
        "data_products": [{"id": "payments", "scope": "entity", "holds": "Payments."}],
        "grants": [],
    }
    assert validator.is_valid({**full, "personas": [ok]})
    for bad in ("ab", "Auditor", "a" * 31, "auditor-"):
        assert not validator.is_valid({**full, "personas": [{**ok, "id": bad}]}), bad
