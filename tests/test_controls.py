"""The controls catalogue: shape, owners, layers, and no control claimed before its test exists."""

import ast
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def test_control_ids_are_unique(controls: dict[str, Any]) -> None:
    counts = Counter(c["id"] for c in controls["controls"])
    assert [i for i, n in counts.items() if n > 1] == []


def test_every_control_sits_on_a_known_layer_and_every_layer_has_one(
    controls: dict[str, Any],
) -> None:
    layers = controls["layers"]
    assert len(set(layers)) == len(layers)
    used = {c["layer"] for c in controls["controls"]}
    assert used - set(layers) == set(), "a control names an unknown layer"
    assert set(layers) - used == set(), "a layer has no control"


def test_every_owner_is_a_human_persona(controls: dict[str, Any], access: dict[str, Any]) -> None:
    humans = {p["id"] for p in access["personas"] if p["kind"] == "human"}
    for control in controls["controls"]:
        assert control["owner"] in humans, control["id"]


def test_proving_tests_are_distinct(controls: dict[str, Any]) -> None:
    counts = Counter(c["proven_by"] for c in controls["controls"])
    assert [t for t, n in counts.items() if n > 1] == []


def defined_tests(path: str) -> set[str]:
    source = REPO / path
    if not source.is_file():
        return set()
    tree = ast.parse(source.read_text())
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def test_an_implemented_control_has_its_proving_test(controls: dict[str, Any]) -> None:
    for control in controls["controls"]:
        path, name = control["proven_by"].split("::")
        exists = name in defined_tests(path)
        if control["status"] == "implemented":
            assert exists, f"{control['id']}: {control['proven_by']} does not exist"
        else:
            assert not exists, f"{control['id']}: its test exists; mark it implemented"
