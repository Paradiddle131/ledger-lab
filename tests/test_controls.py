"""The controls catalogue: shape, owners, layers, and no control claimed before its test exists."""

import ast
import subprocess
import sys
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
    counts = Counter(test for c in controls["controls"] for test in c["proven_by"])
    assert [t for t, n in counts.items() if n > 1] == []


def defined_tests(path: str) -> set[str]:
    source = REPO / path
    if not source.is_file():
        return set()
    tree = ast.parse(source.read_text())
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def test_an_implemented_control_has_its_proving_test(controls: dict[str, Any]) -> None:
    for control in controls["controls"]:
        for test in control["proven_by"]:
            path, name = test.split("::")
            exists = name in defined_tests(path)
            if control["status"] == "implemented":
                assert exists, f"{control['id']}: {test} does not exist"
                assert "awaits" not in control, f"{control['id']}: implemented, yet awaits"
            elif "awaits" not in control:
                assert not exists, f"{control['id']}: its test exists; mark it implemented"


def test_a_control_awaits_something_only_while_its_tests_exist(controls: dict[str, Any]) -> None:
    for control in controls["controls"]:
        if "awaits" in control:
            assert all(
                name in defined_tests(path)
                for path, name in (t.split("::") for t in control["proven_by"])
            ), f"{control['id']}: awaits, yet a proving test does not exist"


def test_a_control_awaits_a_parameter_its_tests_still_skip(controls: dict[str, Any]) -> None:
    """Each proving test of a control that awaits a parameter runs with it and skips, so the
    control cannot go on awaiting once that proof runs."""
    awaiting = [c for c in controls["controls"] if "awaits" in c]
    assert awaiting, "no control awaits a parameter, so this test proves nothing"
    nodes = [f"{t}[{c['awaits']['parameter']}]" for c in awaiting for t in c["proven_by"]]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-rA", "-p", "no:cacheprovider", *nodes],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-2000:]
    outcomes = [line.split()[0] for line in done.stdout.splitlines() if line.endswith(tuple(nodes))]
    outcomes += [line.split()[0] for line in done.stdout.splitlines() if line.startswith("SKIPPED")]
    assert outcomes == ["SKIPPED"] * len(nodes), done.stdout[-2000:]
