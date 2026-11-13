"""The finance glossary: shape, unique ids, known owners and references that resolve."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

SCHEMA = Path(__file__).resolve().parents[1] / "semantics/glossary.schema.json"

# The vocabulary the models, metrics and agents cite from the start; more terms may join.
REQUIRED = {
    "procure-to-pay",
    "record-to-report",
    "voucher",
    "award",
    "commitment",
    "accrual",
    "reversing-accrual",
    "posting-date",
    "fiscal-calendar",
    "open-and-closed-period",
    "outturn",
    "budget-moment",
    "reference-rate",
    "constant-currency",
    "trial-balance",
    "three-way-match",
    "intercompany",
    "restatement",
    "legal-entity-identifier",
    "segregation-of-duties",
    "maker-checker",
}


def problems(doc: dict[str, Any]) -> list[str]:
    """Every reason a glossary is unusable: schema errors, then repeated ids."""
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))
    found = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(doc)
    ]
    counts = Counter(t.get("id") for t in doc.get("terms", []))
    found += [f"duplicate id: {i}" for i, n in counts.items() if n > 1]
    return found


def test_the_glossary_has_no_problem(glossary: dict[str, Any]) -> None:
    assert problems(glossary) == []


def test_the_glossary_holds_every_required_term(glossary: dict[str, Any]) -> None:
    assert REQUIRED - {t["id"] for t in glossary["terms"]} == set()


def test_every_owner_is_a_known_human_persona(
    glossary: dict[str, Any], access: dict[str, Any]
) -> None:
    humans = {p["id"] for p in access["personas"] if p["kind"] == "human"}
    for term in glossary["terms"]:
        if "owner" in term:
            assert term["owner"] in humans, term["id"]


def test_every_see_also_names_another_term(glossary: dict[str, Any]) -> None:
    ids = {t["id"] for t in glossary["terms"]}
    for term in glossary["terms"]:
        for other in term.get("see_also", []):
            assert other in ids - {term["id"]}, (term["id"], other)


GOOD = {"id": "voucher", "term": "Voucher", "definition": "An approved claim to pay."}


@pytest.mark.parametrize(
    ("terms", "expected"),
    [
        ([GOOD, GOOD], "duplicate id: voucher"),
        ([{"id": "voucher", "term": "Voucher"}], "'definition' is a required property"),
        ([GOOD | {"definition": ""}], "does not match"),
        ([GOOD | {"definition": "Two\nlines."}], "does not match"),
    ],
)
def test_a_duplicate_id_or_a_missing_definition_is_refused(
    terms: list[dict[str, Any]], expected: str
) -> None:
    assert any(expected in p for p in problems({"terms": terms}))
