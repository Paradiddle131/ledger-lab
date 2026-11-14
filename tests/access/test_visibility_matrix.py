"""The visibility matrix in `access/visibility.yaml`: complete, dutiful and agreeing with the
grants that produce it.

The matrix is the outcome the access tests act out as each persona; the grants in
`access/personas.yaml`, the tag readers in `access/columns.yaml` and the BigQuery grants
`platform/gcp` generates from both are the mechanism. The matrix must state one cell for every
persona, product, entity and tag of the catalogues, every cell must be what the mechanism gives,
and the cells must keep the personas' duties, so a change made to a catalogue and the matrix
together still fails when it breaks a duty. The planted failures act on an invented catalogue
written here, so a change to the real catalogues moves none of them.
"""

import copy
import json
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from tests.platform.test_access_rules import (
    DATASET_ROLES,
    dataset_id,
    expected_dataset_grants,
    expected_readers,
    tofu_eval,
)

MATRIX = Path(__file__).resolve().parents[2] / "access/visibility.yaml"
# A product cell's place: persona, product and, for an entity-scoped product, the entity.
Key = tuple[str, str, str | None]
Doc = dict[str, Any]
# The cell each access in access/personas.yaml gives, stated here apart from the matrix; a persona
# with no grant on a product, or on an entity of it, is denied there.
CELL = {"read": "clear", "aggregate": "aggregate", "edit": "edit", "draft": "draft", "post": "post"}
ACCESS = {cell: access for access, cell in CELL.items()}
# The cells that take a BigQuery dataset role, as the generated grants give it to their access.
DATASET_ROLE = {CELL[access]: role for access, role in DATASET_ROLES.items()}
KINDS = ("clear", "masked")
# The cells that reach rows, those that only aggregate them, and those that change something.
ROWS = {"clear", "edit"}
WRITES = {"edit", "draft", "post"}


def ids(items: list[Doc]) -> list[str]:
    return [item["id"] for item in items]


def where(key: Key) -> str:
    return " ".join(part for part in key if part)


def product_cells(matrix: Doc) -> Iterator[tuple[Key, str]]:
    for persona, row in matrix["personas"].items():
        for product, cells in row["products"].items():
            if isinstance(cells, str):
                yield (persona, product, None), cells
            else:
                for entity, cell in cells.items():
                    yield (persona, product, entity), cell


def row_cells(row: Doc, products: list[str] | None = None) -> set[str]:
    """The cells of one persona's row, on the products named or on all of them."""
    return {
        cell
        for product, cells in row["products"].items()
        if products is None or product in products
        for cell in ([cells] if isinstance(cells, str) else cells.values())
    }


def reader_role(row: Doc, cell: str) -> str | None:
    """The tag role a column cell needs: its own kind, or for a denied cell a masked role when the
    persona reads the warehouse at all (so no query of it fails on a tagged column), else none."""
    if cell != "denied":
        return cell
    return "masked" if row_cells(row) & ROWS else None


def gaps(matrix: Doc, access: Doc, columns: Doc) -> list[str]:
    """Every place the matrix states no cell, or one outside the catalogues."""
    found: list[str] = []
    personas, rows = ids(access["personas"]), matrix["personas"]
    scope = {p["id"]: p["scope"] for p in access["data_products"]}
    entities, tags = ids(access["entities"]), ids(columns["tags"])
    found += [
        f"tagged products: {p} is not a data product"
        for p in matrix["tagged_products"]
        if p not in scope
    ]
    found += [f"{p}: no row" for p in personas if p not in rows]
    found += [f"{p}: not a persona" for p in rows if p not in personas]
    for persona in [p for p in personas if p in rows]:
        products, tagged = rows[persona]["products"], rows[persona]["columns"]
        found += [f"{persona} {p}: no cell" for p in scope if p not in products]
        found += [f"{persona} {p}: not a data product" for p in products if p not in scope]
        for product in [p for p in scope if p in products]:
            cells = products[product]
            if scope[product] == "group":
                if not isinstance(cells, str):
                    found.append(f"{persona} {product}: a group product takes one cell")
            elif isinstance(cells, str):
                found.append(f"{persona} {product}: an entity-scoped product takes one per entity")
            else:
                found += [f"{persona} {product} {e}: no cell" for e in entities if e not in cells]
                found += [
                    f"{persona} {product} {e}: not an entity" for e in cells if e not in entities
                ]
        found += [f"{persona} {t}: no cell" for t in tags if t not in tagged]
        found += [f"{persona} {t}: not a tag" for t in tagged if t not in tags]
    return found


def contradictions(matrix: Doc, access: Doc, columns: Doc) -> list[str]:
    """Every cell the grants or the tag readers do not give, and every grant with no cell.

    A column cell is what the persona observes: denied exactly when it reads no rows of a product
    that can carry a tagged column, and then its masked-reader role, if any, reaches no column.
    """
    granted: dict[Key, str] = {
        (g["persona"], g["product"], g.get("entity")): CELL[g["access"]] for g in access["grants"]
    }
    cells = dict(product_cells(matrix))
    found = [
        f"{where(key)}: the matrix says {cells.get(key, 'nothing')}, "
        f"the grants give {granted.get(key, 'denied')}"
        for key in sorted(cells.keys() | granted.keys(), key=where)
        if cells.get(key) != granted.get(key, "denied")
    ]
    for tag in columns["tags"]:
        for persona, row in matrix["personas"].items():
            cell = row["columns"].get(tag["id"])
            if cell is None:
                continue
            place = f"{persona} {tag['id']}: the matrix says {cell}, but it reads"
            reaches = bool(row_cells(row, matrix["tagged_products"]) & ROWS)
            role = next((kind for kind in KINDS if persona in tag[kind]), None)
            if reaches and cell == "denied":
                found.append(f"{place} a tagged product")
            elif not reaches and cell != "denied":
                found.append(f"{place} no tagged product")
            elif role != reader_role(row, cell):
                found.append(
                    f"{persona} {tag['id']}: the matrix needs "
                    f"{reader_role(row, cell) or 'no'} role, the readers give {role or 'none'}"
                )
    return found


def duty_breaches(matrix: Doc, access: Doc) -> list[str]:
    """Every cell that breaks a persona's duty in access/personas.yaml, whatever the grants say."""
    agents = {p["id"] for p in access["personas"] if p["kind"] == "agent"}
    cells = list(product_cells(matrix))
    found: list[str] = []
    for key, cell in cells:
        persona, product, _ = key
        if persona == "auditor" and cell in WRITES:
            found.append(f"{where(key)}: the auditor changes nothing, the matrix says {cell}")
        others = product not in {"audit-log", "metric-definitions"}
        if persona == "agent-operator" and others and cell != "denied":
            found.append(f"{where(key)}: the agent operator reads only the audit log and metrics")
        if persona in agents and cell not in {"denied", "draft"}:
            found.append(f"{where(key)}: an agent holds no data role, the matrix says {cell}")
    for entity in ids(access["entities"]):
        posters = sorted(
            p for (p, product, e), c in cells if (product, e, c) == ("ledger", entity, "post")
        )
        if len(posters) > 1:
            found.append(f"ledger {entity}: one poster, the matrix has {', '.join(posters)}")
        found += [
            f"{p} ledger {entity}: a poster never drafts"
            for p in posters
            if "draft" in row_cells(matrix["personas"][p])
        ]
    paid = [
        e
        for (p, product, e), c in cells
        if (p, product) == ("payables-clerk", "payments") and c in ROWS
    ]
    if len(paid) != 1:
        found.append(f"payables-clerk payments: one entity's rows, the matrix gives {len(paid)}")
    for persona, row in matrix["personas"].items():
        if persona in agents:
            found += [
                f"{persona} {t}: an agent reads no tag in clear"
                for t, c in row["columns"].items()
                if c == "clear"
            ]
        for product in row["products"]:
            if {"aggregate"} <= row_cells(row, [product]) and row_cells(row, [product]) & ROWS:
                found.append(f"{persona} {product}: an aggregate cell beside a path to its rows")
    return found


def bigquery_contradictions(
    matrix: Doc,
    dataset_grants: set[tuple[str, str, str]],
    readers: Mapping[str, set[tuple[str, str]]],
) -> list[str]:
    """Where the BigQuery grants differ from what the matrix needs.

    A product cell of clear or edit on any entity needs its dataset role (the row rule within the
    dataset is the table's row access policies), every other cell no role; a column cell needs the
    reader role `reader_role` names.
    """
    needed = {
        (persona, dataset_id(product), DATASET_ROLE[cell])
        for (persona, product, _), cell in product_cells(matrix)
        if cell in DATASET_ROLE
    }
    found = [
        f"{' '.join(g)}: the matrix needs it, BigQuery lacks it"
        for g in sorted(needed - dataset_grants)
    ]
    found += [
        f"{' '.join(g)}: BigQuery grants it, no matrix cell needs it"
        for g in sorted(dataset_grants - needed)
    ]
    for kind in KINDS:
        roles = {
            (tag, persona)
            for persona, row in matrix["personas"].items()
            for tag, cell in row["columns"].items()
            if reader_role(row, cell) == kind
        }
        found += [
            f"{t} {p} {kind}: the matrix needs it, BigQuery lacks it"
            for t, p in sorted(roles - readers[kind])
        ]
        found += [
            f"{t} {p} {kind}: BigQuery grants it, no matrix cell needs it"
            for t, p in sorted(readers[kind] - roles)
        ]
    return found


def repeated_keys(text: str) -> list[str]:
    """Keys written twice in one mapping, which a YAML load would silently collapse into one."""
    repeated: list[str] = []
    nodes: list[yaml.Node] = [yaml.compose(text)]
    while nodes:
        node = nodes.pop()
        if isinstance(node, yaml.MappingNode):
            counts = Counter(key.value for key, _ in node.value)
            repeated += [key for key, n in counts.items() if n > 1]
            nodes += [value for _, value in node.value]
        elif isinstance(node, yaml.SequenceNode):
            nodes += node.value
    return sorted(repeated)


def test_no_key_is_written_twice() -> None:
    assert repeated_keys(MATRIX.read_text()) == []


def test_the_matrix_states_every_cell_exactly_once(
    visibility: Doc, access: Doc, columns: Doc
) -> None:
    assert gaps(visibility, access, columns) == []


def test_the_matrix_agrees_with_the_grants_and_the_tag_readers(
    visibility: Doc, access: Doc, columns: Doc
) -> None:
    assert contradictions(visibility, access, columns) == []


def test_the_matrix_keeps_the_personas_duties(visibility: Doc, access: Doc) -> None:
    assert duty_breaches(visibility, access) == []


def test_the_matrix_agrees_with_the_bigquery_grants_as_the_access_rules_tests_derive_them(
    visibility: Doc,
) -> None:
    # `tests/platform/test_access_rules.py` derives these from the catalogues and pins the HCL's
    # generating expressions to the same rule; the tofu twin below reads what OpenTofu evaluates.
    readers = {kind: expected_readers(kind) for kind in KINDS}
    assert bigquery_contradictions(visibility, expected_dataset_grants(), readers) == []


@pytest.mark.tofu
def test_the_matrix_agrees_with_the_bigquery_grants_tofu_evaluates(visibility: Doc) -> None:
    grants = {
        (g["persona"], g["dataset"], g["role"]) for g in tofu_eval("values(local.dataset_grants)")
    }
    readers = {
        kind: {(r["tag"], r["persona"]) for r in tofu_eval(f"values(local.{kind}_readers)")}
        for kind in KINDS
    }
    assert bigquery_contradictions(visibility, grants, readers) == []


# An invented catalogue, tag list and matrix, in the shapes of the real ones, that the planted
# failures below change: two entities, the real products' ids and the personas the duties name.
FIXTURE_ACCESS = """
entities: [{id: east}, {id: west}]
data_products:
  - {id: payments, scope: entity}
  - {id: ledger, scope: entity}
  - {id: totals, scope: group}
  - {id: metric-definitions, scope: group}
  - {id: audit-log, scope: group}
personas:
  - {id: payables-clerk, kind: human}
  - {id: auditor, kind: human}
  - {id: group-controller, kind: human}
  - {id: approver, kind: human}
  - {id: metric-owner, kind: human}
  - {id: agent-operator, kind: human}
  - {id: proposer, kind: agent}
grants:
  - {persona: payables-clerk, entity: east, product: payments, access: read}
  - {persona: auditor, entity: east, product: payments, access: read}
  - {persona: auditor, entity: west, product: payments, access: read}
  - {persona: auditor, entity: east, product: ledger, access: read}
  - {persona: auditor, product: totals, access: read}
  - {persona: auditor, product: metric-definitions, access: read}
  - {persona: auditor, product: audit-log, access: read}
  - {persona: group-controller, entity: east, product: payments, access: read}
  - {persona: group-controller, entity: west, product: payments, access: read}
  - {persona: group-controller, product: totals, access: read}
  - {persona: approver, entity: east, product: payments, access: read}
  - {persona: approver, entity: east, product: ledger, access: post}
  - {persona: approver, product: totals, access: aggregate}
  - {persona: metric-owner, entity: east, product: payments, access: aggregate}
  - {persona: metric-owner, entity: west, product: payments, access: aggregate}
  - {persona: metric-owner, product: totals, access: read}
  - {persona: metric-owner, product: metric-definitions, access: edit}
  - {persona: agent-operator, product: metric-definitions, access: read}
  - {persona: agent-operator, product: audit-log, access: read}
  - {persona: proposer, entity: east, product: ledger, access: draft}
"""
FIXTURE_COLUMNS = """
tags:
  - id: account
    clear: [payables-clerk, auditor]
    masked: [group-controller, approver, metric-owner, agent-operator]
"""
FIXTURE_MATRIX = """
tagged_products: [payments, ledger]
personas:
  payables-clerk:
    products:
      payments: {east: clear, west: denied}
      ledger: {east: denied, west: denied}
      totals: denied
      metric-definitions: denied
      audit-log: denied
    columns: {account: clear}
  auditor:
    products:
      payments: {east: clear, west: clear}
      ledger: {east: clear, west: denied}
      totals: clear
      metric-definitions: clear
      audit-log: clear
    columns: {account: clear}
  group-controller:
    products:
      payments: {east: clear, west: clear}
      ledger: {east: denied, west: denied}
      totals: clear
      metric-definitions: denied
      audit-log: denied
    columns: {account: masked}
  approver:
    products:
      payments: {east: clear, west: denied}
      ledger: {east: post, west: denied}
      totals: aggregate
      metric-definitions: denied
      audit-log: denied
    columns: {account: masked}
  metric-owner:
    products:
      payments: {east: aggregate, west: aggregate}
      ledger: {east: denied, west: denied}
      totals: clear
      metric-definitions: edit
      audit-log: denied
    columns: {account: denied}
  agent-operator:
    products:
      payments: {east: denied, west: denied}
      ledger: {east: denied, west: denied}
      totals: denied
      metric-definitions: clear
      audit-log: clear
    columns: {account: denied}
  proposer:
    products:
      payments: {east: denied, west: denied}
      ledger: {east: draft, west: denied}
      totals: denied
      metric-definitions: denied
      audit-log: denied
    columns: {account: denied}
"""
VIEWER, EDITOR = DATASET_ROLES["read"], DATASET_ROLES["edit"]
# The BigQuery grants the generated configuration would give the invented catalogue.
FIXTURE_DATASET_GRANTS = {
    ("payables-clerk", "payments", VIEWER),
    *(
        ("auditor", d, VIEWER)
        for d in ("payments", "ledger", "totals", "metric_definitions", "audit_log")
    ),
    ("group-controller", "payments", VIEWER),
    ("group-controller", "totals", VIEWER),
    ("approver", "payments", VIEWER),
    ("metric-owner", "totals", VIEWER),
    ("metric-owner", "metric_definitions", EDITOR),
    ("agent-operator", "metric_definitions", VIEWER),
    ("agent-operator", "audit_log", VIEWER),
}
FIXTURE_READERS = {
    "clear": {("account", "payables-clerk"), ("account", "auditor")},
    "masked": {
        ("account", p) for p in ("group-controller", "approver", "metric-owner", "agent-operator")
    },
}


def fixture() -> tuple[Doc, Doc, Doc]:
    """A fresh copy of the invented matrix, catalogue and tags."""
    return (
        yaml.safe_load(FIXTURE_MATRIX),
        yaml.safe_load(FIXTURE_ACCESS),
        yaml.safe_load(FIXTURE_COLUMNS),
    )


def fixture_readers() -> dict[str, set[tuple[str, str]]]:
    return {kind: set(readers) for kind, readers in FIXTURE_READERS.items()}


def test_the_invented_fixture_is_complete_consistent_and_dutiful() -> None:
    matrix, access, columns = fixture()
    assert gaps(matrix, access, columns) == []
    assert contradictions(matrix, access, columns) == []
    assert duty_breaches(matrix, access) == []
    assert bigquery_contradictions(matrix, set(FIXTURE_DATASET_GRANTS), fixture_readers()) == []


def test_a_key_written_twice_is_found() -> None:
    persona_twice = FIXTURE_MATRIX + "  proposer:\n    products: {}\n    columns: {}\n"
    assert repeated_keys(persona_twice) == ["proposer"]
    entity_twice = FIXTURE_MATRIX.replace(
        "{east: clear, west: denied}", "{east: clear, east: denied}", 1
    )
    assert repeated_keys(entity_twice) == ["east"]


# Changes the matrix, the catalogue or the tags in place; what it returns is ignored.
Mutation = Callable[[Doc, Doc, Doc], object]


def grant(access: Doc, persona: str, product: str, entity: str | None = None) -> Doc:
    return next(
        g
        for g in access["grants"]
        if (g["persona"], g["product"], g.get("entity")) == (persona, product, entity)
    )


def move_reader(columns: Doc, persona: str, to: str | None) -> None:
    (tag,) = columns["tags"]
    for kind in KINDS:
        if persona in tag[kind]:
            tag[kind].remove(persona)
    if to is not None:
        tag[to].append(persona)


def set_cell(matrix: Doc, persona: str, product: str, entity: str | None, cell: str) -> None:
    products = matrix["personas"][persona]["products"]
    if entity is None:
        products[product] = cell
    else:
        products[product][entity] = cell


def give(
    matrix: Doc, access: Doc, persona: str, product: str, entity: str | None, cell: str
) -> None:
    """Change a cell and its grant together, as a coordinated change to both files would."""
    set_cell(matrix, persona, product, entity, cell)
    access["grants"] = [
        g
        for g in access["grants"]
        if (g["persona"], g["product"], g.get("entity")) != (persona, product, entity)
    ]
    held = {"persona": persona, "product": product, "access": ACCESS[cell]}
    access["grants"].append(held if entity is None else {**held, "entity": entity})


def set_column(matrix: Doc, persona: str, cell: str) -> None:
    matrix["personas"][persona]["columns"]["account"] = cell


# Each contradiction planted in the grants, the tag readers or the matrix, and what it reports.
CONTRADICTIONS: dict[str, tuple[Mutation, list[str]]] = {
    "a denied cell with a read grant": (
        lambda m, a, c: a["grants"].append(
            {"persona": "payables-clerk", "entity": "west", "product": "payments", "access": "read"}
        ),
        ["payables-clerk payments west: the matrix says denied, the grants give clear"],
    ),
    "a clear cell with only an aggregate grant": (
        lambda m, a, c: grant(a, "group-controller", "totals").update(access="aggregate"),
        ["group-controller totals: the matrix says clear, the grants give aggregate"],
    ),
    "an aggregate cell with a read grant": (
        lambda m, a, c: grant(a, "metric-owner", "payments", "east").update(access="read"),
        ["metric-owner payments east: the matrix says aggregate, the grants give clear"],
    ),
    "a clear cell whose grant is removed": (
        lambda m, a, c: a["grants"].remove(grant(a, "auditor", "audit-log")),
        ["auditor audit-log: the matrix says clear, the grants give denied"],
    ),
    "a post cell whose grant drafts": (
        lambda m, a, c: grant(a, "approver", "ledger", "east").update(access="draft"),
        ["approver ledger east: the matrix says post, the grants give draft"],
    ),
    "an edit cell whose grant only reads": (
        lambda m, a, c: grant(a, "metric-owner", "metric-definitions").update(access="read"),
        ["metric-owner metric-definitions: the matrix says edit, the grants give clear"],
    ),
    "a draft cell with no grant": (
        lambda m, a, c: a["grants"].remove(grant(a, "proposer", "ledger", "east")),
        ["proposer ledger east: the matrix says draft, the grants give denied"],
    ),
    "an own-entity cell for an entity the persona is not mapped to": (
        lambda m, a, c: set_cell(m, "approver", "payments", "west", "clear"),
        ["approver payments west: the matrix says clear, the grants give denied"],
    ),
    "a cell on a product the persona has no grant on that is not denied": (
        lambda m, a, c: set_cell(m, "payables-clerk", "totals", None, "aggregate"),
        ["payables-clerk totals: the matrix says aggregate, the grants give denied"],
    ),
    "a write cell the persona holds no verb for": (
        lambda m, a, c: set_cell(m, "group-controller", "ledger", "east", "post"),
        ["group-controller ledger east: the matrix says post, the grants give denied"],
    ),
    "a masked cell whose persona is a clear reader of the tag": (
        lambda m, a, c: move_reader(c, "group-controller", "clear"),
        ["group-controller account: the matrix needs masked role, the readers give clear"],
    ),
    "a clear cell whose persona is a masked reader of the tag": (
        lambda m, a, c: move_reader(c, "payables-clerk", "masked"),
        ["payables-clerk account: the matrix needs clear role, the readers give masked"],
    ),
    "a masked cell in the matrix for a clear reader": (
        lambda m, a, c: set_column(m, "auditor", "masked"),
        ["auditor account: the matrix needs masked role, the readers give clear"],
    ),
    "a denied cell whose persona reads a tagged product": (
        lambda m, a, c: set_column(m, "group-controller", "denied"),
        ["group-controller account: the matrix says denied, but it reads a tagged product"],
    ),
    "a masked cell whose persona reads no tagged product": (
        lambda m, a, c: set_column(m, "metric-owner", "masked"),
        ["metric-owner account: the matrix says masked, but it reads no tagged product"],
    ),
    "a warehouse reader reaching no tagged column without its masked role": (
        lambda m, a, c: move_reader(c, "metric-owner", None),
        ["metric-owner account: the matrix needs masked role, the readers give none"],
    ),
    "a clear role that reaches no tagged column": (
        lambda m, a, c: move_reader(c, "agent-operator", "clear"),
        ["agent-operator account: the matrix needs masked role, the readers give clear"],
    ),
    "a reader role for a persona that reads nothing": (
        lambda m, a, c: move_reader(c, "proposer", "masked"),
        ["proposer account: the matrix needs no role, the readers give masked"],
    ),
    "a product dropped from the tagged products": (
        lambda m, a, c: m.update(tagged_products=["ledger"]),
        [
            f"{p} account: the matrix says {cell}, but it reads no tagged product"
            for p, cell in (
                ("payables-clerk", "clear"),
                ("group-controller", "masked"),
                ("approver", "masked"),
            )
        ],
    ),
}


@pytest.mark.parametrize("planted", sorted(CONTRADICTIONS))
def test_a_planted_contradiction_is_reported(planted: str) -> None:
    mutate, reported = CONTRADICTIONS[planted]
    matrix, access, columns = fixture()
    mutate(matrix, access, columns)
    assert contradictions(matrix, access, columns) == reported


# Each duty broken by a change to the grants and the matrix together, and what it reports.
BREACHES: dict[str, tuple[Mutation, list[str]]] = {
    "the agent operator given clear payments": (
        lambda m, a, c: (
            give(m, a, "agent-operator", "payments", "east", "clear"),
            set_column(m, "agent-operator", "masked"),
        ),
        ["agent-operator payments east: the agent operator reads only the audit log and metrics"],
    ),
    "the auditor given edit on the metric definitions": (
        lambda m, a, c: give(m, a, "auditor", "metric-definitions", None, "edit"),
        ["auditor metric-definitions: the auditor changes nothing, the matrix says edit"],
    ),
    "the payables clerk given a second entity": (
        lambda m, a, c: give(m, a, "payables-clerk", "payments", "west", "clear"),
        ["payables-clerk payments: one entity's rows, the matrix gives 2"],
    ),
    "the payables clerk given no entity": (
        lambda m, a, c: (
            give(m, a, "payables-clerk", "payments", "east", "aggregate"),
            m["personas"]["payables-clerk"]["columns"].update(account="denied"),
            move_reader(c, "payables-clerk", None),
        ),
        ["payables-clerk payments: one entity's rows, the matrix gives 0"],
    ),
    "the group controller given post on a ledger": (
        lambda m, a, c: give(m, a, "group-controller", "ledger", "east", "post"),
        ["ledger east: one poster, the matrix has approver, group-controller"],
    ),
    "the poster given draft on another ledger": (
        lambda m, a, c: give(m, a, "approver", "ledger", "west", "draft"),
        ["approver ledger east: a poster never drafts"],
    ),
    "the metric owner given clear payments beside its aggregates": (
        lambda m, a, c: (
            give(m, a, "metric-owner", "payments", "east", "clear"),
            set_column(m, "metric-owner", "masked"),
        ),
        ["metric-owner payments: an aggregate cell beside a path to its rows"],
    ),
    "the proposer given aggregate totals": (
        lambda m, a, c: give(m, a, "proposer", "totals", None, "aggregate"),
        ["proposer totals: an agent holds no data role, the matrix says aggregate"],
    ),
    "the proposer given clear payments and the tag in clear": (
        lambda m, a, c: (
            give(m, a, "proposer", "payments", "east", "clear"),
            set_column(m, "proposer", "clear"),
            move_reader(c, "proposer", "clear"),
        ),
        [
            "proposer payments east: an agent holds no data role, the matrix says clear",
            "proposer account: an agent reads no tag in clear",
        ],
    ),
}


@pytest.mark.parametrize("planted", sorted(BREACHES))
def test_a_duty_broken_in_the_grants_and_the_matrix_together_is_reported(planted: str) -> None:
    mutate, reported = BREACHES[planted]
    matrix, access, columns = fixture()
    mutate(matrix, access, columns)
    assert contradictions(matrix, access, columns) == []
    assert duty_breaches(matrix, access) == reported


@pytest.mark.parametrize(
    "planted", ["persona", "product", "entity", "tag", "matrix row", "matrix cells"]
)
def test_a_catalogue_entry_without_cells_or_a_cell_outside_the_catalogues_is_reported(
    planted: str,
) -> None:
    matrix, access, columns = fixture()
    personas = ids(access["personas"])
    if planted == "persona":
        access["personas"].append({"id": "treasurer", "kind": "human"})
        reported = ["treasurer: no row"]
    elif planted == "product":
        access["data_products"].append({"id": "forecasts", "scope": "group"})
        reported = [f"{p} forecasts: no cell" for p in personas]
    elif planted == "entity":
        access["entities"].append({"id": "north"})
        reported = [
            line
            for p in personas
            for line in (f"{p} payments north: no cell", f"{p} ledger north: no cell")
        ]
    elif planted == "tag":
        columns["tags"].append({"id": "invented-tag", "clear": [], "masked": []})
        reported = [f"{p} invented-tag: no cell" for p in personas]
    elif planted == "matrix row":
        matrix["personas"]["nobody"] = copy.deepcopy(matrix["personas"]["proposer"])
        reported = ["nobody: not a persona"]
    else:
        matrix["tagged_products"].append("forecasts")
        row = matrix["personas"]["auditor"]
        row["products"]["totals"] = {"east": "clear"}
        row["products"]["payments"] = "clear"
        row["products"]["forecasts"] = "denied"
        row["products"]["ledger"]["north"] = "denied"
        row["columns"]["invented-tag"] = "denied"
        reported = [
            "tagged products: forecasts is not a data product",
            "auditor forecasts: not a data product",
            "auditor payments: an entity-scoped product takes one per entity",
            "auditor ledger north: not an entity",
            "auditor totals: a group product takes one cell",
            "auditor invented-tag: not a tag",
        ]
    assert gaps(matrix, access, columns) == reported


def test_a_bigquery_grant_the_matrix_needs_and_lacks_or_does_not_need_is_reported() -> None:
    matrix, _, _ = fixture()
    grants, readers = set(FIXTURE_DATASET_GRANTS), fixture_readers()
    grants.discard(("auditor", "audit_log", VIEWER))
    grants.add(("proposer", "ledger", VIEWER))
    readers["masked"].discard(("account", "agent-operator"))
    readers["clear"].add(("account", "agent-operator"))
    readers["masked"].add(("account", "proposer"))
    assert bigquery_contradictions(matrix, grants, readers) == [
        f"auditor audit_log {VIEWER}: the matrix needs it, BigQuery lacks it",
        f"proposer ledger {VIEWER}: BigQuery grants it, no matrix cell needs it",
        "account agent-operator clear: BigQuery grants it, no matrix cell needs it",
        "account agent-operator masked: the matrix needs it, BigQuery lacks it",
        "account proposer masked: BigQuery grants it, no matrix cell needs it",
    ]
    set_cell(matrix, "metric-owner", "metric-definitions", None, "clear")
    set_cell(matrix, "group-controller", "totals", None, "denied")
    assert bigquery_contradictions(matrix, set(FIXTURE_DATASET_GRANTS), fixture_readers()) == [
        f"metric-owner metric_definitions {VIEWER}: the matrix needs it, BigQuery lacks it",
        f"group-controller totals {VIEWER}: BigQuery grants it, no matrix cell needs it",
        f"metric-owner metric_definitions {EDITOR}: BigQuery grants it, no matrix cell needs it",
    ]


def test_the_schema_refuses_a_cell_outside_its_kind() -> None:
    validator = Draft202012Validator(
        json.loads((MATRIX.parent / "visibility.schema.json").read_text())
    )
    row = {"products": {"payments": {"chicago": "clear"}}, "columns": {"individual-id": "masked"}}
    matrix = {"tagged_products": ["payments"], "personas": {"auditor": row}}
    assert validator.is_valid(matrix)
    assert not validator.is_valid({"personas": matrix["personas"]})
    for bad in (
        {"products": {"payments": {"chicago": "masked"}}, "columns": row["columns"]},
        {"products": {"group-totals": "masked"}, "columns": row["columns"]},
        {"products": row["products"], "columns": {"individual-id": "aggregate"}},
        {"products": row["products"], "columns": {"individual-id": "post"}},
        {"products": row["products"]},
        {**row, "writes": {}},
    ):
        assert not validator.is_valid({**matrix, "personas": {"auditor": bad}}), bad
