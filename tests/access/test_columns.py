"""The policy tags on sensitive columns, cross-checked against the persona catalogue, and the column
list, cross-checked against the data contracts.

Every persona that reads the warehouse reads every tag either in clear or masked, never both, so no
query it may run is refused on a tagged column; a persona that reads nothing there is neither; and
only the people who need the value itself read it in clear.

The tagged columns of a product are exactly the columns its contract classifies personal, so the
contract stays the one place that says a column is personal; a product with no contract only
awaits tags, and a tag that marks no column is awaited. The planted failures act on an invented
contract and column list written here, so a change to the real ones moves none of them.
"""

from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest
import yaml

from access.identities import READS
from models.contracts import gate

Doc = dict[str, Any]
NULLIFY = "nullify"


def tagged_products(columns: Doc) -> set[str]:
    """The data products that carry a tagged column; one that only awaits tags carries none."""
    return {p for p, entry in columns["products"].items() if "columns" in entry}


def contracts() -> dict[str, Doc]:
    """Every data contract of the working tree, by contract id."""
    found: dict[str, Doc] = {}
    for path, text in gate.working(gate.REPO).items():
        contract = gate.parsed(path, text)
        assert not isinstance(contract, str), contract
        found[contract["id"]] = contract
    return found


def required(contract: Doc) -> set[str]:
    return {
        f"{schema['name']}.{p['name']}"
        for schema in contract.get("schema") or []
        for p in schema.get("properties") or []
        if p.get("required")
    }


def column_list_breaks(columns: Doc, access: Doc, by_id: dict[str, Doc]) -> list[str]:
    """Every place the column list and the contracts disagree, each as one line."""
    found: list[str] = []
    products = {p["id"] for p in access["data_products"]}
    masking = {t["id"]: t["masking"] for t in columns["tags"]}
    entries = columns["products"]
    marked: dict[str, list[str]] = {t: [] for t in masking}
    awaited: set[str] = set()
    for product, entry in entries.items():
        if product not in products:
            found.append(f"{product}: not a data product")
        for col, tag in entry.get("columns", {}).items():
            if tag not in masking:
                found.append(f"{product} {col}: {tag} is not a tag")
            else:
                marked[tag].append(f"{product} {col}")
        for tag in entry.get("awaiting", []):
            if tag not in masking:
                found.append(f"{product}: awaits {tag}, which is not a tag")
            awaited.add(tag)
        if "columns" in entry and product not in by_id:
            found.append(f"{product}: tags columns but has no contract")
        if "awaiting" in entry and product in by_id:
            found.append(f"{product}: awaits tags but has a contract, so its columns are tagged")
    for product, contract in sorted(by_id.items()):
        tagged = entries.get(product, {}).get("columns", {})
        present, personal = gate.columns(contract), gate.personal(contract)
        for col in sorted(tagged):
            if col not in present:
                found.append(f"{product} {col}: tagged, but not in the contract")
            elif col not in personal:
                found.append(
                    f"{product} {col}: tagged, but the contract does not classify it personal"
                )
            elif masking.get(tagged[col]) == NULLIFY and col in required(contract):
                found.append(f"{product} {col}: required by the contract, nullified by its tag")
        found += [
            f"{product} {col}: personal, but not tagged" for col in sorted(personal - set(tagged))
        ]
    for tag, places in marked.items():
        if not places and tag not in awaited:
            found.append(f"{tag}: marks no column and no product awaits it")
        if places and tag in awaited:
            found.append(f"{tag}: awaited, but already marks {', '.join(places)}")
    return found


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


def test_the_tagged_columns_are_exactly_the_contracts_personal_columns(
    columns: Doc, access: Doc
) -> None:
    assert column_list_breaks(columns, access, contracts()) == []


def test_the_bank_tags_await_the_erp_and_mark_no_column_yet(columns: Doc) -> None:
    # The public files carry no bank details; once a contract lists the ERP's bank columns, the
    # awaiting entry goes and this pin moves with it.
    awaited = {p: e["awaiting"] for p, e in columns["products"].items() if "awaiting" in e}
    assert awaited == {"ledger": ["bank-account-number", "bank-account-holder"]}


# An invented contract, persona catalogue and column list, in the shapes of the real ones, that the
# planted failures below change.
FIXTURE_CONTRACT = """
id: payments
schema:
  - name: payments
    properties:
      - {name: payment_key, required: true}
      - {name: payee, required: true, classification: personal}
      - {name: reference, classification: personal}
      - {name: amount, required: true}
"""
FIXTURE_ACCESS = """
data_products: [{id: payments}, {id: ledger}, {id: totals}]
"""
FIXTURE_COLUMNS = """
tags:
  - {id: account, masking: last-four}
  - {id: holder, masking: nullify}
  - {id: person, masking: hash}
products:
  payments:
    columns: {payments.payee: person, payments.reference: holder}
  ledger:
    awaiting: [account]
"""


def fixture() -> tuple[Doc, Doc, dict[str, Doc]]:
    contract = yaml.safe_load(FIXTURE_CONTRACT)
    return yaml.safe_load(FIXTURE_COLUMNS), yaml.safe_load(FIXTURE_ACCESS), {"payments": contract}


def prop(by_id: dict[str, Doc], name: str) -> Doc:
    (schema,) = by_id["payments"]["schema"]
    return next(p for p in schema["properties"] if p["name"] == name)


def test_the_invented_fixture_agrees_with_its_contract() -> None:
    assert column_list_breaks(*fixture()) == []


# Changes the column list, the catalogue or the contracts in place; what it returns is ignored.
Mutation = Callable[[Doc, Doc, dict[str, Doc]], object]
PAYMENTS = "payments"
PLANTED: dict[str, tuple[Mutation, list[str]]] = {
    "a tagged column not in the contract": (
        lambda c, a, k: c["products"][PAYMENTS]["columns"].update({"payments.iban": "person"}),
        ["payments payments.iban: tagged, but not in the contract"],
    ),
    "a tagged column the contract does not classify personal": (
        lambda c, a, k: c["products"][PAYMENTS]["columns"].update({"payments.amount": "person"}),
        ["payments payments.amount: tagged, but the contract does not classify it personal"],
    ),
    "a personal column left untagged": (
        lambda c, a, k: c["products"][PAYMENTS]["columns"].pop("payments.reference"),
        [
            "payments payments.reference: personal, but not tagged",
            "holder: marks no column and no product awaits it",
        ],
    ),
    "a column newly classified personal in the contract": (
        lambda c, a, k: prop(k, "payment_key").update(classification="personal"),
        ["payments payments.payment_key: personal, but not tagged"],
    ),
    "a tagged column whose classification is dropped": (
        lambda c, a, k: prop(k, "reference").pop("classification"),
        ["payments payments.reference: tagged, but the contract does not classify it personal"],
    ),
    "a required column nullified by its tag": (
        lambda c, a, k: c["products"][PAYMENTS]["columns"].update({"payments.payee": "holder"}),
        [
            "payments payments.payee: required by the contract, nullified by its tag",
            "person: marks no column and no product awaits it",
        ],
    ),
    "a contract with personal columns and no column list": (
        lambda c, a, k: c["products"].pop(PAYMENTS),
        [
            "payments payments.payee: personal, but not tagged",
            "payments payments.reference: personal, but not tagged",
            "holder: marks no column and no product awaits it",
            "person: marks no column and no product awaits it",
        ],
    ),
    "a column tagged with no tag": (
        lambda c, a, k: c["products"][PAYMENTS]["columns"].update({"payments.payee": "pin"}),
        [
            "payments payments.payee: pin is not a tag",
            "person: marks no column and no product awaits it",
        ],
    ),
    "a product that is not a data product": (
        lambda c, a, k: c["products"].update(forecasts={"awaiting": ["account"]}),
        ["forecasts: not a data product"],
    ),
    "a product with tagged columns and no contract": (
        lambda c, a, k: c["products"].update(ledger={"columns": {"ledger.payee": "account"}}),
        ["ledger: tags columns but has no contract"],
    ),
    "a product that awaits tags once its contract exists": (
        lambda c, a, k: k.update(ledger={"id": "ledger", "schema": []}),
        ["ledger: awaits tags but has a contract, so its columns are tagged"],
    ),
    "a tag that marks no column and is awaited by none": (
        lambda c, a, k: c["products"].pop("ledger"),
        ["account: marks no column and no product awaits it"],
    ),
    "a tag awaited that already marks a column": (
        lambda c, a, k: c["products"]["ledger"]["awaiting"].append("person"),
        ["person: awaited, but already marks payments payments.payee"],
    ),
    "an awaited tag that is no tag": (
        lambda c, a, k: c["products"]["ledger"]["awaiting"].append("pin"),
        ["ledger: awaits pin, which is not a tag"],
    ),
}


@pytest.mark.parametrize("planted", sorted(PLANTED))
def test_a_planted_disagreement_is_reported(planted: str) -> None:
    mutate, reported = PLANTED[planted]
    columns, access, by_id = fixture()
    mutate(columns, access, by_id)
    assert column_list_breaks(columns, access, by_id) == reported
