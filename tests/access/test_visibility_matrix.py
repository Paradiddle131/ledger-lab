"""The visibility matrix in `access/visibility.yaml`, checked against the grants that produce it.

The matrix is the outcome the access tests act out as each persona, written from the personas'
duties; the grants in `access/personas.yaml`, the tag readers in `access/columns.yaml` and the
BigQuery grants `platform/gcp` generates from both are the mechanism. The matrix must state one
cell for every persona, product, entity and tag in the catalogues, and every cell must be what the
mechanism gives; each check is shown to fail on a planted contradiction in each direction.
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
# The cells that take a BigQuery dataset role, as the generated grants give it to their access.
DATASET_ROLE = {CELL[access]: role for access, role in DATASET_ROLES.items()}
KINDS = ("clear", "masked")


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


def gaps(matrix: Doc, access: Doc, columns: Doc) -> list[str]:
    """Every place the matrix states no cell, or one outside the catalogues, or a cell twice."""
    found: list[str] = []
    personas, rows = ids(access["personas"]), matrix["personas"]
    found += [f"{p}: no row" for p in personas if p not in rows]
    found += [f"{p}: not a persona" for p in rows if p not in personas]
    scope = {p["id"]: p["scope"] for p in access["data_products"]}
    entities, tags = ids(access["entities"]), ids(columns["tags"])
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
    """Every cell the grants or the tag readers do not give, and every grant with no cell."""
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
            given = next((kind for kind in KINDS if persona in tag[kind]), "denied")
            cell = row["columns"].get(tag["id"])
            if cell is not None and cell != given:
                found.append(
                    f"{persona} {tag['id']}: the matrix says {cell}, the readers give {given}"
                )
    return found


def bigquery_contradictions(
    matrix: Doc,
    dataset_grants: set[tuple[str, str, str]],
    readers: Mapping[str, set[tuple[str, str]]],
) -> list[str]:
    """Where the BigQuery grants differ from what the matrix needs.

    A product cell of clear or edit on any entity needs its dataset role (the row rule within the
    dataset is the table's row access policies), every other cell no role; a column cell of clear
    or masked needs the persona to be that kind of reader of the tag, denied neither.
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
        cells = {
            (tag, persona)
            for persona, row in matrix["personas"].items()
            for tag, cell in row["columns"].items()
            if cell == kind
        }
        found += [
            f"{t} {p} {kind}: the matrix needs it, BigQuery lacks it"
            for t, p in sorted(cells - readers[kind])
        ]
        found += [
            f"{t} {p} {kind}: BigQuery grants it, no matrix cell needs it"
            for t, p in sorted(readers[kind] - cells)
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


def test_a_key_written_twice_is_found() -> None:
    text = MATRIX.read_text()
    persona_twice = text + "  proposer:\n    products: {}\n    columns: {}\n"
    assert repeated_keys(persona_twice) == ["proposer"]
    entity_twice = text.replace(
        "{chicago: clear, uk-transport: denied", "{chicago: clear, chicago: denied", 1
    )
    assert repeated_keys(entity_twice) == ["chicago"]


def test_the_matrix_states_every_cell_exactly_once(
    visibility: Doc, access: Doc, columns: Doc
) -> None:
    assert gaps(visibility, access, columns) == []


def test_the_matrix_agrees_with_the_grants_and_the_tag_readers(
    visibility: Doc, access: Doc, columns: Doc
) -> None:
    assert contradictions(visibility, access, columns) == []


def test_the_matrix_agrees_with_the_generated_bigquery_grants(visibility: Doc) -> None:
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


Mutation = Callable[[Doc, Doc, Doc], None]


def grant(access: Doc, persona: str, product: str, entity: str | None = None) -> Doc:
    return next(
        g
        for g in access["grants"]
        if (g["persona"], g["product"], g.get("entity")) == (persona, product, entity)
    )


def move_reader(columns: Doc, tag: str, persona: str, to: str) -> None:
    entry = next(t for t in columns["tags"] if t["id"] == tag)
    for kind in KINDS:
        if persona in entry[kind]:
            entry[kind].remove(persona)
    entry[to].append(persona)


def set_cell(matrix: Doc, persona: str, product: str, entity: str | None, cell: Any) -> None:
    products = matrix["personas"][persona]["products"]
    if entity is None:
        products[product] = cell
    else:
        products[product][entity] = cell


# Each contradiction planted in the grants, the tag readers or the matrix, and what it reports.
CONTRADICTIONS: dict[str, tuple[Mutation, list[str]]] = {
    "a denied cell with a read grant": (
        lambda m, a, c: a["grants"].append(
            {
                "persona": "accountant-chicago",
                "entity": "uk-transport",
                "product": "payments",
                "access": "read",
            }
        ),
        ["accountant-chicago payments uk-transport: the matrix says denied, the grants give clear"],
    ),
    "a clear cell with only an aggregate grant": (
        lambda m, a, c: grant(a, "group-controller", "group-totals").update(access="aggregate"),
        ["group-controller group-totals: the matrix says clear, the grants give aggregate"],
    ),
    "an aggregate cell with a read grant": (
        lambda m, a, c: grant(a, "metric-owner", "payments", "chicago").update(access="read"),
        ["metric-owner payments chicago: the matrix says aggregate, the grants give clear"],
    ),
    "a clear cell whose grant is removed": (
        lambda m, a, c: a["grants"].remove(grant(a, "auditor", "audit-log")),
        ["auditor audit-log: the matrix says clear, the grants give denied"],
    ),
    "a masked cell whose persona is a clear reader of the tag": (
        lambda m, a, c: move_reader(c, "bank-account-number", "accountant-chicago", "clear"),
        ["accountant-chicago bank-account-number: the matrix says masked, the readers give clear"],
    ),
    "a clear cell whose persona is a masked reader of the tag": (
        lambda m, a, c: move_reader(c, "individual-id", "payables-clerk", "masked"),
        ["payables-clerk individual-id: the matrix says clear, the readers give masked"],
    ),
    "a denied column cell whose persona reads the tag": (
        lambda m, a, c: move_reader(c, "bank-account-holder", "proposer", "masked"),
        ["proposer bank-account-holder: the matrix says denied, the readers give masked"],
    ),
    "a masked column cell whose persona reads no tag": (
        lambda m, a, c: next(t for t in c["tags"] if t["id"] == "individual-id")["masked"].remove(
            "agent-operator"
        ),
        ["agent-operator individual-id: the matrix says masked, the readers give denied"],
    ),
    "a post cell whose grant drafts": (
        lambda m, a, c: grant(a, "approver", "ledger", "chicago").update(access="draft"),
        ["approver ledger chicago: the matrix says post, the grants give draft"],
    ),
    "an edit cell whose grant only reads": (
        lambda m, a, c: grant(a, "metric-owner", "metric-definitions").update(access="read"),
        ["metric-owner metric-definitions: the matrix says edit, the grants give clear"],
    ),
    "a draft cell with no grant": (
        lambda m, a, c: a["grants"].remove(grant(a, "proposer", "ledger", "chicago")),
        ["proposer ledger chicago: the matrix says draft, the grants give denied"],
    ),
    "an own-entity cell for an entity the persona is not mapped to": (
        lambda m, a, c: set_cell(m, "accountant-chicago", "payments", "uk-transport", "clear"),
        ["accountant-chicago payments uk-transport: the matrix says clear, the grants give denied"],
    ),
    "a cell on a product the persona has no grant on that is not denied": (
        lambda m, a, c: set_cell(m, "payables-clerk", "group-totals", None, "aggregate"),
        ["payables-clerk group-totals: the matrix says aggregate, the grants give denied"],
    ),
    "a write cell the persona holds no verb for": (
        lambda m, a, c: set_cell(m, "accountant-chicago", "ledger", "chicago", "post"),
        ["accountant-chicago ledger chicago: the matrix says post, the grants give denied"],
    ),
    "a masked column cell in the matrix for a clear reader": (
        lambda m, a, c: m["personas"]["auditor"]["columns"].update(
            {"bank-account-number": "masked"}
        ),
        ["auditor bank-account-number: the matrix says masked, the readers give clear"],
    ),
}


@pytest.mark.parametrize("planted", sorted(CONTRADICTIONS))
def test_a_planted_contradiction_is_reported(
    visibility: Doc, access: Doc, columns: Doc, planted: str
) -> None:
    mutate, reported = CONTRADICTIONS[planted]
    matrix, catalogue, tags = (copy.deepcopy(d) for d in (visibility, access, columns))
    mutate(matrix, catalogue, tags)
    assert contradictions(matrix, catalogue, tags) == reported


def every(access: Doc, message: str) -> list[str]:
    return [message.format(persona=p) for p in ids(access["personas"])]


@pytest.mark.parametrize(
    "planted", ["persona", "product", "entity", "tag", "matrix row", "matrix cells"]
)
def test_a_catalogue_entry_without_cells_or_a_cell_outside_the_catalogues_is_reported(
    visibility: Doc, access: Doc, columns: Doc, planted: str
) -> None:
    matrix, catalogue, tags = (copy.deepcopy(d) for d in (visibility, access, columns))
    if planted == "persona":
        catalogue["personas"].append(
            {"id": "treasurer", "name": "Treasurer", "kind": "human", "duties": "Invented."}
        )
        reported = ["treasurer: no row"]
    elif planted == "product":
        catalogue["data_products"].append(
            {"id": "forecasts", "scope": "group", "holds": "Invented."}
        )
        reported = every(access, "{persona} forecasts: no cell")
    elif planted == "entity":
        catalogue["entities"].append(
            {"id": "invented", "name": "Invented", "currency": "EUR", "brings": "None."}
        )
        reported = [
            line
            for p in ids(access["personas"])
            for line in (f"{p} payments invented: no cell", f"{p} ledger invented: no cell")
        ]
    elif planted == "tag":
        tags["tags"].append(
            {
                "id": "invented-tag",
                "marks": "Invented.",
                "masking": "hash",
                "clear": [],
                "masked": [],
            }
        )
        reported = every(access, "{persona} invented-tag: no cell")
    elif planted == "matrix row":
        matrix["personas"]["nobody"] = copy.deepcopy(matrix["personas"]["proposer"])
        reported = ["nobody: not a persona"]
    else:
        row = matrix["personas"]["auditor"]
        row["products"]["group-totals"] = {"chicago": "clear"}
        row["products"]["payments"] = "clear"
        row["products"]["forecasts"] = "denied"
        row["products"]["ledger"]["invented"] = "denied"
        row["columns"]["invented-tag"] = "denied"
        reported = [
            "auditor forecasts: not a data product",
            "auditor payments: an entity-scoped product takes one per entity",
            "auditor ledger invented: not an entity",
            "auditor group-totals: a group product takes one cell",
            "auditor invented-tag: not a tag",
        ]
    assert gaps(matrix, catalogue, tags) == reported


def bigquery_inputs() -> tuple[set[tuple[str, str, str]], dict[str, set[tuple[str, str]]]]:
    return expected_dataset_grants(), {kind: expected_readers(kind) for kind in KINDS}


def test_a_bigquery_grant_the_matrix_needs_and_lacks_or_does_not_need_is_reported(
    visibility: Doc,
) -> None:
    viewer, editor = DATASET_ROLES["read"], DATASET_ROLES["edit"]
    grants, readers = bigquery_inputs()
    grants.discard(("auditor", "audit_log", viewer))
    grants.add(("proposer", "ledger", viewer))
    readers["masked"].discard(("individual-id", "agent-operator"))
    readers["clear"].add(("individual-id", "agent-operator"))
    assert bigquery_contradictions(visibility, grants, readers) == [
        f"auditor audit_log {viewer}: the matrix needs it, BigQuery lacks it",
        f"proposer ledger {viewer}: BigQuery grants it, no matrix cell needs it",
        "individual-id agent-operator clear: BigQuery grants it, no matrix cell needs it",
        "individual-id agent-operator masked: the matrix needs it, BigQuery lacks it",
    ]
    matrix = copy.deepcopy(visibility)
    set_cell(matrix, "metric-owner", "metric-definitions", None, "clear")
    set_cell(matrix, "group-controller", "group-totals", None, "denied")
    assert bigquery_contradictions(matrix, *bigquery_inputs()) == [
        f"metric-owner metric_definitions {viewer}: the matrix needs it, BigQuery lacks it",
        f"group-controller group_totals {viewer}: BigQuery grants it, no matrix cell needs it",
        f"metric-owner metric_definitions {editor}: BigQuery grants it, no matrix cell needs it",
    ]


def test_the_schema_refuses_a_cell_outside_its_kind() -> None:
    validator = Draft202012Validator(
        json.loads((MATRIX.parent / "visibility.schema.json").read_text())
    )
    row = {"products": {"payments": {"chicago": "clear"}}, "columns": {"individual-id": "masked"}}
    assert validator.is_valid({"personas": {"auditor": row}})
    for bad in (
        {"products": {"payments": {"chicago": "masked"}}, "columns": row["columns"]},
        {"products": {"group-totals": "masked"}, "columns": row["columns"]},
        {"products": row["products"], "columns": {"individual-id": "aggregate"}},
        {"products": row["products"], "columns": {"individual-id": "post"}},
        {"products": row["products"]},
        {**row, "writes": {}},
    ):
        assert not validator.is_valid({"personas": {"auditor": bad}}), bad
