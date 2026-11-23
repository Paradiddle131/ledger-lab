"""The contract gate's one way through for a deliberate break: a new major version of the contract
whose `breakingChanges` custom property lists exactly the breaks the gate finds against
`origin/main`, each with a reason, and nothing else; and the breaks no version can declare.

Every contract here is the committed one changed inside the test, in a throwaway clone whose
`origin/main` the test sets.
"""

import copy
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from models.contracts import gate
from tests.models.test_payments_contract import (
    CONTRACT,
    HASHED,
    Contract,
    changed,
    column,
    committed,
    drop,
    drop_rule,
    git_repo,
    on_rule,
    properties,
    rename,
    setting,
    unsetting,
    with_files,
)

PATH = str(gate.CONTRACT)
SPLIT = "payee-columns-split-the-published-name"
REMOVED_NAME = "schema.payments.properties.vendor_name: Removed property vendor_name"
Change = Callable[[Contract], None]


def remove_the_payee_name(c: Contract) -> None:
    """The published payee name gone, with the two rules that read it."""
    drop("vendor_name")(c)
    drop_rule(HASHED)(c)
    drop_rule(SPLIT)(c)


def bigquery_types(c: Contract) -> None:
    """Every column's storage type as BigQuery names it."""
    names = {"VARCHAR": "STRING", "DECIMAL(28,6)": "NUMERIC", "BIGINT": "INT64", "BOOLEAN": "BOOL"}
    for p in properties(c):
        p["physicalType"] = names.get(p["physicalType"], p["physicalType"])


def add_trivial_rule(c: Contract) -> None:
    (schema,) = c["schema"]
    schema["quality"].append({"id": "trivial", "type": "sql", "query": "select 0", "mustBe": 0})


def add(name: str, **settings: object) -> Change:
    def change(c: Contract) -> None:
        properties(c).append({"name": name, "logicalType": "string", "physicalType": "VARCHAR"})
        properties(c)[-1].update(settings)

    return change


def both(*changes: Change) -> Change:
    def change(c: Contract) -> None:
        for each in changes:
            each(c)

    return change


def found(change: Change) -> list[str]:
    """The declarable breaks the gate finds in `change` made to the committed contract."""
    after = committed()
    change(after)
    declarable, refused = gate.breaks(committed(), after)
    assert not refused, refused
    return declarable


def declaring(version: str, entries: object) -> Change:
    """The contract at `version`, its `breakingChanges` holding `entries`."""

    def change(c: Contract) -> None:
        c["version"] = version
        c["customProperties"] = [{"property": gate.DECLARED, "value": entries}]

    return change


def listed(lines: list[str], reason: str = "The split columns hold the same fact.") -> list[object]:
    return [{"change": line, "reason": reason} for line in lines]


@pytest.fixture(scope="module")
def published_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clone whose `origin/main` holds the committed contract, at version 1.0.0."""
    assert committed()["version"] == "1.0.0"
    return git_repo(tmp_path_factory.mktemp("published"), {PATH: CONTRACT.read_text()})


def gate_on(repo: Path, change: Change, path: str = PATH) -> tuple[bool, str]:
    return gate.check(with_files(repo, {path: changed(change)}))


def test_removing_the_payee_name_breaks_these_and_fails_without_a_new_major_version(
    published_repo: Path,
) -> None:
    breaks = found(remove_the_payee_name)
    assert breaks == [
        REMOVED_NAME,
        "schema.payments.quality.[1]: Removed quality rule at schema.payments.quality.[1]",
        "schema.payments.quality.[5]: Removed quality rule at schema.payments.quality.[5]",
        f"quality rule payments/{HASHED} is gone (removed, moved or renamed)",
        f"quality rule payments/{SPLIT} is gone (removed, moved or renamed)",
    ]
    ok, report = gate_on(
        published_repo, both(remove_the_payee_name, lambda c: c.update(version="2.0.0"))
    )
    assert not ok
    assert all(f"payments: {line} (not listed in {gate.DECLARED})" in report for line in breaks)


PASSING = {
    "the payee name removed": remove_the_payee_name,
    "every storage type changed for BigQuery": bigquery_types,
    "a rule's query changed": on_rule(HASHED, "query", "select 0"),
    "the payee name removed beside an added personal column": both(
        remove_the_payee_name, add("payee_key", classification="personal")
    ),
    "a column not classified personal removed beside an added column": both(
        drop("contract_number"), add("department")
    ),
    "a personal column renamed with its classification": both(
        rename("vendor_person_key", "payee_ref")
    ),
    "the table named with its server schema and in capitals": lambda c: c["schema"][0].update(
        physicalName="MAIN.Payments"
    ),
    "two rules with no id added to one list": lambda c: two_idless_rules(c),
}


@pytest.mark.parametrize("name", list(PASSING))
@pytest.mark.parametrize("version", ["2.0.0", "3.1.4"])
def test_a_new_major_version_listing_exactly_its_breaks_passes(
    published_repo: Path, name: str, version: str
) -> None:
    change = PASSING[name]
    ok, report = gate_on(published_repo, both(change, declaring(version, listed(found(change)))))
    assert ok, report


def test_a_new_major_version_with_no_break_and_no_list_passes(published_repo: Path) -> None:
    ok, report = gate_on(published_repo, lambda c: c.update(version="2.0.0"))
    assert ok, report


def entries_without(line: str, breaks: list[str]) -> list[object]:
    return listed([b for b in breaks if b != line])


BREAKS = found(remove_the_payee_name)
FAILING: dict[str, tuple[Change, str]] = {
    "a break not listed": (
        declaring("2.0.0", entries_without(REMOVED_NAME, BREAKS)),
        f"payments: {REMOVED_NAME} (not listed in {gate.DECLARED})",
    ),
    "a listed break that did not happen": (
        declaring(
            "2.0.0", listed([*BREAKS, "schema.payments.properties.amount: Removed property amount"])
        ),
        "lists a break that did not happen: schema.payments.properties.amount",
    ),
    "a reason missing": (
        declaring("2.0.0", [*listed(BREAKS[1:]), {"change": REMOVED_NAME}]),
        f"lists a break without a reason: {REMOVED_NAME}",
    ),
    "a blank reason": (
        declaring("2.0.0", [*listed(BREAKS[1:]), {"change": REMOVED_NAME, "reason": "  "}]),
        f"lists a break without a reason: {REMOVED_NAME}",
    ),
    "a break listed at a path the gate does not report": (
        declaring(
            "2.0.0",
            [*listed(BREAKS[1:]), *listed(["payments.vendor_name: Removed property vendor_name"])],
        ),
        f"payments: {REMOVED_NAME} (not listed in {gate.DECLARED})",
    ),
    "a catch-all entry": (
        declaring("2.0.0", listed(["*"])),
        "lists a break that did not happen: *",
    ),
    "a break listed twice": (
        declaring("2.0.0", listed([*BREAKS, REMOVED_NAME])),
        f"lists a break twice: {REMOVED_NAME}",
    ),
    "an entry without a change": (
        declaring("2.0.0", [*listed(BREAKS), {"reason": "r"}]),
        f"{gate.DECLARED} entry 6 names no change",
    ),
    "an entry with a blank change": (
        declaring("2.0.0", [*listed(BREAKS), {"change": " ", "reason": "r"}]),
        f"{gate.DECLARED} entry 6 names no change",
    ),
    "a list that is not a list": (
        declaring("2.0.0", "everything the split needs"),
        f"{gate.DECLARED} must be a list",
    ),
    "an entry with more than a change and a reason": (
        declaring(
            "2.0.0", [*listed(BREAKS[1:]), {"change": REMOVED_NAME, "reason": "r", "scope": "*"}]
        ),
        f"{gate.DECLARED} entry 5 must hold a change and a reason",
    ),
    "the list given twice": (
        lambda c: c.update(
            version="2.0.0",
            customProperties=[{"property": gate.DECLARED, "value": listed(BREAKS)}] * 2,
        ),
        f"customProperties holds {gate.DECLARED} more than once",
    ),
    "a minor version raised": (
        declaring("1.1.0", listed(BREAKS)),
        "version '1.1.0' is not a major version above '1.0.0'",
    ),
    "the version unchanged": (
        declaring("1.0.0", listed(BREAKS)),
        "version '1.0.0' is not a major version above '1.0.0'",
    ),
    "the major version lowered": (
        declaring("0.9.0", listed(BREAKS)),
        "version '0.9.0' is not a major version above '1.0.0'",
    ),
    "a version that is not MAJOR.MINOR.PATCH": (
        declaring("2.0", listed(BREAKS)),
        "version '2.0' is not a major version above '1.0.0'",
    ),
    "a version with a pre-release suffix": (
        declaring("2.0.0-rc.1", listed(BREAKS)),
        "version '2.0.0-rc.1' is not a major version above '1.0.0'",
    ),
    "a version with trailing text": (
        declaring("2.0.0x", listed(BREAKS)),
        "version '2.0.0x' is not a major version above '1.0.0'",
    ),
    "a version with a prefix": (
        declaring("v2.0.0", listed(BREAKS)),
        "version 'v2.0.0' is not a major version above '1.0.0'",
    ),
}


@pytest.mark.parametrize("name", list(FAILING))
def test_a_break_fails_unless_a_new_major_version_lists_exactly_it_with_a_reason(
    published_repo: Path, name: str
) -> None:
    declare, says = FAILING[name]
    ok, report = gate_on(published_repo, both(remove_the_payee_name, declare))
    assert not ok, report
    assert says in report


def test_a_break_under_the_same_major_version_fails_as_before(published_repo: Path) -> None:
    ok, report = gate_on(
        published_repo, both(remove_the_payee_name, lambda c: c.update(version="1.1.0"))
    )
    assert not ok
    assert f"payments: {REMOVED_NAME}\n" in report
    assert "no break can be declared" in report


def test_a_moved_contract_keeps_its_list_and_its_limits(published_repo: Path) -> None:
    moved = "contracts/payments.odcs.yaml"
    declared = both(remove_the_payee_name, declaring("2.0.0", listed(BREAKS)))
    ok, report = gate_on(published_repo, declared, moved)
    assert ok, report
    ok, report = gate_on(published_repo, both(declared, drop("amount")), moved)
    assert not ok
    assert "Removed property amount (not listed" in report


def at_two(change: Change, lines: list[str]) -> str:
    """The committed contract changed, at 2.0.0, listing `lines`."""
    return changed(both(change, declaring("2.0.0", listed(lines))))


def declared(change: Change, version: str) -> str:
    """The committed contract changed; at a new major `version`, listing every break the gate
    may let it declare, exactly as the gate reports it, so only a refusal can fail it."""
    if version == "1.0.0":
        return changed(change)
    return at_two(change, found_any(change))


def found_any(change: Change) -> list[str]:
    after = committed()
    change(after)
    return gate.breaks(committed(), after)[0]


def schema_of(c: Contract) -> dict[str, Any]:
    (schema,) = c["schema"]
    return schema


def rename_schema(name: str) -> Change:
    def change(c: Contract) -> None:
        schema_of(c)["name"] = name

    return change


def schema_table(table: str) -> Change:
    def change(c: Contract) -> None:
        schema_of(c)["physicalName"] = table

    return change


def declassify_all(c: Contract) -> None:
    for p in properties(c):
        p.pop("classification", None)


def twin(name: str, table: str, *, move: bool = False) -> Change:
    """A copy of the schema, named `name` over `table`, its classifications dropped; with
    `move`, the original pointed at another table."""

    def change(c: Contract) -> None:
        copied = copy.deepcopy(schema_of(c))
        copied.update(name=name, physicalName=table)
        for p in copied["properties"]:
            p.pop("classification", None)
        if move:
            schema_of(c)["physicalName"] = "payments_old"
        c["schema"].append(copied)

    return change


def nest(key: str) -> Change:
    inner = [{"name": "name", "logicalType": "string", "physicalType": "VARCHAR"}]
    shape: dict[str, object] = (
        {"properties": inner}
        if key == "properties"
        else {"items": {"logicalType": "object", "properties": inner}}
    )

    def change(c: Contract) -> None:
        kind = "object" if key == "properties" else "array"
        properties(c).append({"name": "payees", "logicalType": kind, **shape})

    return change


def swap_physical(c: Contract) -> None:
    """The personal column handed to a new unclassified property."""
    column(c, "vendor_name")["physicalName"] = "vendor_name_old"
    add("payee", physicalName="vendor_name")(c)


def duplicate(name: str, **settings: object) -> Change:
    def change(c: Contract) -> None:
        copied = copy.deepcopy(column(c, name))
        copied.pop("classification", None)
        copied.update(settings)
        properties(c).insert(0, copied)

    return change


def duplicate_schema(name: str) -> Change:
    def change(c: Contract) -> None:
        copied = copy.deepcopy(schema_of(c))
        copied["name"] = name
        for p in copied["properties"]:
            p.pop("classification", None)
        c["schema"].append(copied)

    return change


def duplicate_rule(rule_id: str, on: str | None = None, **settings: object) -> Change:
    """A copy of the schema's quality rule `rule_id`, changed by `settings`, added under the
    same id to the schema, or to property `on` beside a copy of its own."""

    def change(c: Contract) -> None:
        copied = copy.deepcopy(next(q for q in schema_of(c)["quality"] if q["id"] == rule_id))
        copied.update(settings)
        if on is None:
            schema_of(c)["quality"].append(copied)
        else:
            column(c, on)["quality"] = [copy.deepcopy(copied), copied]

    return change


def rule_key_twin(c: Contract, with_id: bool = True) -> None:
    """A property holding a copy of a schema rule, and a second schema entry, over a table of
    its own, named `<schema>.<property>` and holding a copy under the same id, or both copies
    first in their lists with no id when `with_id` is false."""
    rule = copy.deepcopy(next(q for q in schema_of(c)["quality"] if q["id"] == HASHED))
    if not with_id:
        del rule["id"]
    column(c, "vendor_name")["quality"] = [copy.deepcopy(rule)]
    c["schema"].append(
        {
            "name": "payments.vendor_name",
            "physicalName": "payee_rules",
            "properties": [{"name": "k", "logicalType": "string", "physicalType": "VARCHAR"}],
            "quality": [rule],
        }
    )


def idless_twin_second(c: Contract) -> None:
    """`rule_key_twin` with no id, each copy second in its list, after a rule with an id of its
    own: both are read at position 1."""
    own = copy.deepcopy(next(q for q in schema_of(c)["quality"] if q["id"] == HASHED))
    vendor_name = column(c, "vendor_name")
    rule_key_twin(c, with_id=False)
    vendor_name["quality"].insert(0, {**copy.deepcopy(own), "id": "on-the-column"})
    c["schema"][-1]["quality"].insert(0, {**own, "id": "on-the-twin"})


def two_idless_rules(c: Contract) -> None:
    """Two copies of a schema rule with no id added to the schema's own list, read at two
    positions."""
    rule = copy.deepcopy(next(q for q in schema_of(c)["quality"] if q["id"] == HASHED))
    del rule["id"]
    schema_of(c)["quality"] += [copy.deepcopy(rule), rule]


def weaken_first(rule_id: str) -> Change:
    """The first of the schema's quality rules with id `rule_id` weakened to `select 0`."""

    def change(c: Contract) -> None:
        next(q for q in schema_of(c)["quality"] if q["id"] == rule_id)["query"] = "select 0"

    return change


IN = "origin/main"
SWAPPED = "payments's payments.payee (payments.vendor_name)"
REFUSED_BY_VERSION: dict[str, tuple[Change, str, list[str]]] = {
    "a twin schema over the table named in capitals": (
        twin("payments_open", "PAYMENTS"),
        "table payments is described by both payments's schema payments and payments's"
        " schema payments_open",
        ["1.0.0", "2.0.0"],
    ),
    "a twin schema over the table named with its server schema": (
        twin("payments_open", "main.payments"),
        "table payments is described by both",
        ["1.0.0", "2.0.0"],
    ),
    "a twin schema over the table named in double quotes": (
        twin("payments_open", '"Payments"'),
        "table payments is described by both",
        ["1.0.0"],
    ),
    "the table moved to a new schema named in capitals": (
        twin("payments_v2", "PAYMENTS", move=True),
        f"payments's schema payments_v2 describes table payments, which {IN} describes as"
        " payments's schema payments",
        ["2.0.0"],
    ),
    "the table moved to a new schema named with its server schema": (
        twin("payments_v2", "main.payments", move=True),
        "payments's payments_v2.vendor_name (payments.vendor_name) describes a column"
        f" {IN} classifies personal and is classified nothing",
        ["2.0.0"],
    ),
    "the personal column handed to a new property by physical name": (
        swap_physical,
        f"{SWAPPED} describes a column {IN} classifies personal and is classified nothing",
        ["2.0.0"],
    ),
    "a column renamed and retyped over its published physical column": (
        both(
            rename("amount", "amount_v2"),
            setting("amount_v2", "physicalName", "amount"),
            setting("amount_v2", "physicalType", "FLOAT64"),
        ),
        "payments's payments.amount_v2 (payments.amount) describes a column origin/main describes"
        " as payments's payments.amount",
        ["2.0.0"],
    ),
    "a property aliasing the personal column": (
        add("payee_label", physicalName="vendor_name"),
        "schema payments has two properties over column vendor_name",
        ["1.0.0"],
    ),
    "a second schema with the schema's name": (
        duplicate_schema("payments"),
        "two schema entries are named payments",
        ["1.0.0"],
    ),
    "a second schema with the schema's name in capitals": (
        duplicate_schema("Payments"),
        "two schema entries are named Payments",
        ["1.0.0"],
    ),
    "a second property with a personal column's name": (
        duplicate("vendor_name"),
        "schema payments has two properties named vendor_name",
        ["1.0.0"],
    ),
    "a second property with the amount's name, retyped and nullable": (
        duplicate("amount", physicalType="FLOAT64", required=False),
        "schema payments has two properties named amount",
        ["1.0.0"],
    ),
    "a second quality rule with a rule's id": (
        duplicate_rule(HASHED),
        f"two quality rules are both read as payments/{HASHED}",
        ["1.0.0", "2.0.0"],
    ),
    "two quality rules with one id on a property": (
        duplicate_rule(HASHED, on="vendor_name"),
        f"two quality rules are both read as payments.vendor_name/{HASHED}",
        ["1.0.0", "2.0.0"],
    ),
    "a schema named as a property's place, sharing a rule id with it": (
        rule_key_twin,
        f"two quality rules are both read as payments.vendor_name/{HASHED}",
        ["2.0.0"],
    ),
    "a schema named as a property's place, each holding a first rule with no id": (
        lambda c: rule_key_twin(c, with_id=False),
        "two quality rules are both read as payments.vendor_name/#0 (no id)",
        ["2.0.0"],
    ),
    "a schema named as a property's place, each holding a second rule with no id": (
        idless_twin_second,
        "two quality rules are both read as payments.vendor_name/#1 (no id)",
        ["2.0.0"],
    ),
    "a field nested in a column's properties": (
        nest("properties"),
        "payments.payees nests properties",
        ["1.0.0"],
    ),
    "a field nested in a column's items": (
        nest("items"),
        "payments.payees nests properties",
        ["1.0.0"],
    ),
    "a personal column declassified": (
        unsetting("vendor_name", "classification"),
        "payments's payments.vendor_name (payments.vendor_name) describes a column"
        f" {IN} classifies personal and is classified nothing",
        ["1.0.0", "2.0.0"],
    ),
    "a personal column reclassified": (
        setting("document_number", "classification", "restricted"),
        "is classified restricted",
        ["1.0.0", "2.0.0"],
    ),
    "a personal column renamed without its classification": (
        both(rename("vendor_person_key", "payee_ref"), unsetting("payee_ref", "classification")),
        f"payments.vendor_person_key, which {IN} classifies personal, is described nowhere while"
        " payments's payments.payee_ref (payments.payee_ref) is added without that classification",
        ["2.0.0"],
    ),
    "a personal column removed beside an added column not classified personal": (
        both(drop("vendor_name"), add("department")),
        f"payments.vendor_name, which {IN} classifies personal, is described nowhere while"
        " payments's payments.department",
        ["2.0.0"],
    ),
    "the schema renamed": (
        rename_schema("payments_v2"),
        "schema payments is gone or renamed",
        ["2.0.0"],
    ),
    "the schema renamed with every type changed": (
        both(rename_schema("payments_v2"), bigquery_types),
        "schema payments is gone or renamed",
        ["2.0.0"],
    ),
    "a table under a schema no server names": (
        schema_table("other.payments"),
        "schema payments's table name cannot be normalised",
        ["2.0.0"],
    ),
    "a table name of three parts": (
        schema_table("db.main.payments"),
        "schema payments's table name cannot be normalised",
        ["2.0.0"],
    ),
    "a table name holding a quote": (
        schema_table('pay"ments'),
        "schema payments's table name cannot be normalised",
        ["2.0.0"],
    ),
    "a column name holding a dot": (
        setting("amount", "physicalName", "payments.amount"),
        "payments.amount's column name cannot be normalised",
        ["2.0.0"],
    ),
    "a contract the CLI cannot read": (
        setting("payment_key", "required", "maybe"),
        "contract payments is unreadable",
        ["2.0.0"],
    ),
}
CASES = [
    (name, version) for name, (_, _, versions) in REFUSED_BY_VERSION.items() for version in versions
]


@pytest.mark.parametrize(("name", "version"), CASES)
def test_no_version_passes_a_shape_the_gate_cannot_compare_or_a_personal_column_unclassified(
    published_repo: Path, name: str, version: str
) -> None:
    change, says, _ = REFUSED_BY_VERSION[name]
    ok, report = gate.check(with_files(published_repo, {PATH: declared(change, version)}))
    assert not ok, report
    assert says in report, report
    for other in ["(not listed", "did not happen", "no break can be declared"]:
        assert other not in report, report


def test_a_refusal_listed_verbatim_still_fails(published_repo: Path) -> None:
    change = unsetting("vendor_name", "classification")
    after = committed()
    change(after)
    refused = gate.refusals({"payments": committed()}, {"payments": after})["payments"]
    assert refused
    ok, report = gate.check(with_files(published_repo, {PATH: at_two(change, refused)}))
    assert not ok
    assert refused[0] in report
    assert "(not listed" not in report and "did not happen" not in report


def contract(cid: str, schemas: list[dict[str, Any]]) -> str:
    """A new contract of `schemas`, each `{name: ..., physicalName: ..., properties: [...]}`."""
    body = {"apiVersion": "v3.2.0", "kind": "DataContract", "id": cid, "version": "1.0.0"}
    body |= {"status": "active", "servers": committed()["servers"], "schema": schemas}
    return yaml.safe_dump(body, sort_keys=False)


def schema(
    name: str, table: str, *columns: str, classification: str | None = None
) -> dict[str, Any]:
    props = [{"name": c, "logicalType": "string", "physicalType": "VARCHAR"} for c in columns]
    if classification:
        for p in props:
            p["classification"] = classification
    return {"name": name, "physicalName": table, "properties": props}


def unnamed(entry: dict[str, Any]) -> dict[str, Any]:
    """The schema entry or property without its name."""
    return {k: v for k, v in entry.items() if k != "name"}


def with_unnamed_property(entry: dict[str, Any]) -> dict[str, Any]:
    entry["properties"].append(unnamed(entry["properties"][0]) | {"physicalName": "b"})
    return entry


NEW = "models/contracts/payees.odcs.yaml"
WITHOUT_PAYEE = declared(drop("vendor_name"), "2.0.0")
ACROSS: dict[str, tuple[dict[str, str], str]] = {
    "the personal column moved to a new contract without its classification": (
        {PATH: WITHOUT_PAYEE, NEW: contract("payees", [schema("payees", "payees", "vendor_name")])},
        f"payments.vendor_name, which {IN} classifies personal, is described nowhere while"
        " payees's payees.vendor_name (payees.vendor_name) is added without that classification",
    ),
    "a new contract over the published table without its classification": (
        {
            PATH: CONTRACT.read_text(),
            NEW: contract("payees", [schema("payees", "payments", "vendor_name")]),
        },
        "payees's payees.vendor_name (payments.vendor_name) describes a column origin/main"
        " classifies personal and is classified nothing",
    ),
    "a new contract over the published table named in capitals": (
        {PATH: CONTRACT.read_text(), NEW: contract("payees", [schema("payees", "PAYMENTS", "x")])},
        "table payments is described by both payees's schema payees and payments's schema payments",
    ),
    "a new contract with two schemas over one table": (
        {
            PATH: CONTRACT.read_text(),
            NEW: contract(
                "payees", [schema("payees", "payees", "a"), schema("view", "Payees", "a")]
            ),
        },
        "table payees is described by both payees's schema payees and payees's schema view",
    ),
    "a new contract with a schema entry without a name": (
        {
            PATH: CONTRACT.read_text(),
            NEW: contract("payees", [unnamed(schema("payees", "payees", "a"))]),
        },
        "payees: a schema entry has no name, which no version can declare",
    ),
    "a new contract with a property without a name": (
        {
            PATH: CONTRACT.read_text(),
            NEW: contract("payees", [with_unnamed_property(schema("payees", "payees", "a"))]),
        },
        "payees: a property of schema payees has no name, which no version can declare",
    ),
    "a new contract with two schemas of one name": (
        {
            PATH: CONTRACT.read_text(),
            NEW: contract(
                "payees", [schema("payees", "payees", "a"), schema("payees", "other", "a")]
            ),
        },
        "payees: two schema entries are named payees",
    ),
}


@pytest.mark.parametrize("name", list(ACROSS))
def test_the_rules_hold_across_the_contracts_of_the_tree_new_ones_included(
    published_repo: Path, name: str
) -> None:
    files, says = ACROSS[name]
    ok, report = gate.check(with_files(published_repo, files))
    assert not ok, report
    assert says in report, report
    assert "(not listed" not in report and "no break can be declared" not in report, report


def test_a_personal_column_moved_to_a_new_contract_with_its_classification_passes(
    published_repo: Path,
) -> None:
    payees = contract(
        "payees", [schema("payees", "payees", "vendor_name", classification="personal")]
    )
    ok, report = gate.check(with_files(published_repo, {PATH: WITHOUT_PAYEE, NEW: payees}))
    assert ok, report


def test_a_published_contract_of_two_schemas_over_one_table_fails_until_fixed(
    tmp_path: Path,
) -> None:
    doubled = changed(twin("payments_view", "payments"))
    repo = git_repo(tmp_path, {PATH: doubled})
    for text in [
        doubled,
        changed(both(twin("payments_view", "payments"), lambda c: c.update(name="P"))),
    ]:
        ok, report = gate.check(with_files(repo, {PATH: text}))
        assert not ok
        assert "table payments is described by both payments's schema payments and" in report
        assert "now under" not in report and "describes table" not in report, report
    before = yaml.safe_load(doubled)
    fixed = yaml.safe_load(doubled)
    fixed["schema"][1]["physicalName"] = "payments_view"
    declare = gate.breaks(before, fixed)[0]
    assert declare, "pointing the twin at a table of its own is a break the CLI reports"
    declaring("2.0.0", listed(declare))(fixed)
    ok, report = gate.check(with_files(repo, {PATH: yaml.safe_dump(fixed, sort_keys=False)}))
    assert ok, report


@pytest.mark.parametrize(
    "path", ["models/contracts/zahlungen-ü.odcs.yaml", 'models/contracts/a "quoted" b.odcs.yaml']
)
def test_a_contract_at_a_path_git_quotes_is_compared(tmp_path: Path, path: str) -> None:
    repo = git_repo(tmp_path, {path: CONTRACT.read_text()})
    ok, report = gate.check(with_files(repo, {path: changed(both(declassify_all, drop("amount")))}))
    assert not ok, report
    assert "Removed property amount" in report
    assert "classifies personal and is classified nothing" in report


GONE = f"contract payments ({PATH} on origin/main) is gone or has a new id"
REFUSED: dict[str, tuple[dict[str, str], str]] = {
    "the contract's id changed": (
        {PATH: at_two(lambda c: c.update(id="payments-v2"), [GONE])},
        GONE,
    ),
    "the contract retired": ({}, GONE),
    "the unchanged contract beside a broken copy with its id at models/contracts/a.odcs.yaml": (
        {PATH: CONTRACT.read_text(), "models/contracts/a.odcs.yaml": changed(drop("amount"))},
        "contract payments is in both ",
    ),
    "the unchanged contract beside a broken copy with its id at models/contracts/zz.odcs.yaml": (
        {PATH: CONTRACT.read_text(), "models/contracts/zz.odcs.yaml": changed(drop("amount"))},
        "contract payments is in both ",
    ),
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_no_version_declares_a_contract_gone_or_doubled(published_repo: Path, name: str) -> None:
    files, says = REFUSED[name]
    ok, report = gate.check(with_files(published_repo, files))
    assert not ok, report
    assert says in report


def with_id(text: str, cid: object) -> str:
    """The contract `text` with its id set to `cid`, or without an id when `cid` is None."""
    body = yaml.safe_load(text)
    body.pop("id")
    if cid is not None:
        body["id"] = cid
    return yaml.safe_dump(body, sort_keys=False)


NO_TEXT_ID: dict[str, tuple[object, str]] = {
    "no id": (None, "is unreadable: no contract id"),
    "an empty id": ("", "is unreadable: no contract id"),
    "a number as id": (42, "is unreadable: its contract id 42 is not text"),
    "a list as id": (["x"], "is unreadable: its contract id ['x'] is not text"),
}


@pytest.mark.parametrize("name", list(NO_TEXT_ID))
def test_a_working_tree_contract_without_a_text_id_fails_the_gate(
    published_repo: Path, name: str
) -> None:
    cid, says = NO_TEXT_ID[name]
    new = contract("payees", [schema("x", "payments", "vendor_name")])
    files = {PATH: CONTRACT.read_text(), NEW: with_id(new, cid)}
    ok, report = gate.check(with_files(published_repo, files))
    assert not ok, report
    assert f"- {NEW} {says}" in report, report


@pytest.mark.parametrize("name", list(NO_TEXT_ID))
def test_an_origin_main_contract_without_a_text_id_stops_the_gate(
    tmp_path: Path, name: str
) -> None:
    cid, says = NO_TEXT_ID[name]
    repo = git_repo(tmp_path, {PATH: with_id(CONTRACT.read_text(), cid)})
    with_files(repo, {PATH: CONTRACT.read_text()})
    with pytest.raises(gate.GateError, match=re.escape(f"origin/main's {PATH} {says}")):
        gate.check(repo)


def test_no_version_declares_an_unreadable_contract(published_repo: Path) -> None:
    text = at_two(remove_the_payee_name, BREAKS) + "schema: [\n"
    ok, report = gate.check(with_files(published_repo, {PATH: text}))
    assert not ok
    assert f"{gate.CONTRACT} is unreadable" in report


def test_a_schema_rename_hides_no_break_and_in_place_each_needs_its_line(
    published_repo: Path,
) -> None:
    retyped = found(bigquery_types)
    assert len(retyped) == 16
    for n, left_out in enumerate(retyped):
        others = retyped[:n] + retyped[n + 1 :]
        ok, report = gate_on(
            published_repo, both(bigquery_types, declaring("2.0.0", listed(others)))
        )
        assert not ok, left_out
        assert f"payments: {left_out} (not listed in {gate.DECLARED})" in report, report
        assert report.count("not listed in") == 1, report
    renamed = both(rename_schema("payments_v2"), bigquery_types)
    ok, report = gate.check(with_files(published_repo, {PATH: declared(renamed, "2.0.0")}))
    assert not ok
    assert "schema payments is gone or renamed, which no version can declare" in report


def test_a_rule_copied_under_its_id_then_the_original_weakened_fails_the_gate(
    tmp_path: Path,
) -> None:
    """Of two rules sharing an id only the last is compared, so with a copy appended on
    origin/main, weakening the original, the earlier of the two, changes no compared rule; the
    copy is refused in the working tree while origin/main holds a contract."""
    original = next(q for q in schema_of(committed())["quality"] if q["id"] == HASHED)
    repo = git_repo(tmp_path, {PATH: changed(duplicate_rule(HASHED))})
    weakened = both(duplicate_rule(HASHED), weaken_first(HASHED))
    ok, report = gate.check(with_files(repo, {PATH: changed(weakened)}))
    assert original["query"] != "select 0"
    assert not ok, report
    assert f"payments: two quality rules are both read as payments/{HASHED}" in report, report


@pytest.fixture(scope="module")
def merged_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clone whose `origin/main` holds the payee name's removal, merged as 2.0.0 with its list."""
    merged = changed(both(remove_the_payee_name, declaring("2.0.0", listed(BREAKS))))
    return git_repo(tmp_path_factory.mktemp("merged"), {PATH: merged})


def on_merged(version: str, *changes: Change, entries: list[object] | None = None) -> Change:
    """The merged contract, changed, at `version`, its list the merged one unless `entries`."""
    return both(
        remove_the_payee_name,
        *changes,
        declaring(version, listed(BREAKS) if entries is None else entries),
    )


AFTER_MERGE: dict[str, tuple[Change, bool]] = {
    "unchanged": (on_merged("2.0.0"), True),
    "a minor version without a break, the list kept": (on_merged("2.1.0", add_trivial_rule), True),
    "a new break under the merged major version, the list kept": (
        on_merged("2.1.0", drop("amount")),
        False,
    ),
    "a new break under the merged major version, the list naming it": (
        on_merged("2.1.0", drop("amount"), entries=listed([*BREAKS, *found(drop("amount"))])),
        False,
    ),
    "a new major version keeping only the merged list": (on_merged("3.0.0", drop("amount")), False),
    "a new major version listing the merged and the new breaks": (
        on_merged("3.0.0", drop("amount"), entries=listed([*BREAKS, *found(drop("amount"))])),
        False,
    ),
    "a new major version listing only its own break": (
        on_merged("3.0.0", drop("amount"), entries=listed(found(drop("amount")))),
        True,
    ),
}


@pytest.mark.parametrize("name", list(AFTER_MERGE))
def test_a_merged_major_versions_list_excuses_no_later_break(merged_repo: Path, name: str) -> None:
    change, passes = AFTER_MERGE[name]
    ok, report = gate.check(with_files(merged_repo, {PATH: changed(change)}))
    assert ok == passes, report


def test_main_returns_failure_on_a_break_or_when_it_cannot_compare(
    published_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main` returns 1 on a break or when the gate cannot compare, 0 on a pass."""
    check = gate.check
    with_files(published_repo, {PATH: CONTRACT.read_text()})
    monkeypatch.setattr(gate, "check", lambda: check(published_repo))
    assert gate.main() == 0
    assert "no change breaks a consumer" in capsys.readouterr().out
    with_files(published_repo, {PATH: changed(drop("amount"))})
    assert gate.main() == 1
    assert "break consumers" in capsys.readouterr().err

    def cannot() -> tuple[bool, str]:
        raise gate.GateError("origin/main is not fetched")

    monkeypatch.setattr(gate, "check", cannot)
    assert gate.main() == 1
    assert "contract: origin/main is not fetched" in capsys.readouterr().err


@pytest.mark.parametrize(("change", "status"), [(None, 0), (drop("amount"), 1)])
def test_the_gate_script_exits_with_failure_on_a_break(
    tmp_path: Path, change: Change | None, status: int
) -> None:
    """`python models/contracts/gate.py`, what `make contract` runs, exits with `main`'s status:
    the script, copied from this tree into a clone, run against that clone's origin/main."""
    repo = git_repo(tmp_path, {PATH: CONTRACT.read_text()})
    script = repo / "models/contracts/gate.py"
    script.write_text(Path(gate.__file__).read_text())
    with_files(repo, {PATH: CONTRACT.read_text() if change is None else changed(change)})
    run = subprocess.run([sys.executable, str(script)], cwd=repo, capture_output=True, text=True)
    assert run.returncode == status, run.stdout + run.stderr
    assert ("break consumers" in run.stderr) == bool(status), run.stdout + run.stderr


GIT = ["git"]


def partial_clone(tmp_path: Path, missing: str) -> Path:
    """A clone of a repository whose main holds the committed contract, made without its
    `missing` objects (`blob` or `tree`), its remote then gone, so git cannot read them."""
    source = git_repo(tmp_path / "source", {PATH: CONTRACT.read_text()})
    subprocess.run([*GIT, "config", "uploadpack.allowFilter", "true"], cwd=source, check=True)
    clone = tmp_path / "clone"
    filtered = {"blob": "--filter=blob:none", "tree": "--filter=tree:0"}[missing]
    subprocess.run(
        [*GIT, "clone", "--quiet", "--no-checkout", filtered, source.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [*GIT, "remote", "set-url", "origin", (tmp_path / "gone").as_uri()], cwd=clone, check=True
    )
    subprocess.run([*GIT, "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=clone, check=True)
    return clone


@pytest.mark.parametrize("missing", ["blob", "tree"])
def test_a_base_git_cannot_read_fails_the_gate(tmp_path: Path, missing: str) -> None:
    (tmp_path / "source").mkdir()
    clone = partial_clone(tmp_path, missing)
    with_files(clone, {PATH: changed(drop("amount"))})
    with pytest.raises(gate.GateError, match="cannot be read"):
        gate.check(clone)
