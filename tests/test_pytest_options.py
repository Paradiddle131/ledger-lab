"""pytest's own options in `pyproject.toml`, which decide what `make check` runs.

Under pytest-xdist the run's summary no longer counts the tests the `-m` expression deselects, so
what a plain run leaves out is held here instead: exactly the declared markers, each as one
`not <marker>` term, so a test leaves the plain run only by carrying a declared marker, and a
declared marker is never run by accident. A parametrize given an empty list fails collection.
"""

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OPTIONS = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]


def test_a_plain_run_leaves_out_exactly_the_declared_markers() -> None:
    addopts = OPTIONS["addopts"]
    assert addopts.count("-m") == 1
    terms = addopts[addopts.index("-m") + 1].split(" and ")
    declared = [marker.split(":", 1)[0].strip() for marker in OPTIONS["markers"]]
    assert terms == [f"not {marker}" for marker in declared]


def test_an_empty_parametrize_fails_collection() -> None:
    assert OPTIONS["empty_parameter_set_mark"] == "fail_at_collect"
