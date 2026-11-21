"""pytest's own options in `pyproject.toml`, which decide what `make check` runs.

Under pytest-xdist the run's summary no longer counts the tests the `-m` expression deselects, so
what a plain run leaves out is held here instead: exactly the declared markers, each as one
`not <marker>` term, and no other option or setting that selects, drops or skips collecting tests,
so a test leaves the plain run only by carrying a declared marker, and a declared marker is never
run by accident. A parametrize given an empty list fails collection.
"""

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OPTIONS = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]


# What a plain run's options may hold besides the one marker expression; any other option that
# selects or drops tests (`-k`, `--deselect`, `--ignore`, a second `-m`) fails the test below.
PLAIN = ["-q", "--strict-markers", "--strict-config", "-ra"]
# The settings pytest's options may hold; one that changes what is collected
# (`norecursedirs`, `python_files`, `collect_ignore` and the like) fails the test below.
SETTINGS = {
    "addopts",
    "markers",
    "testpaths",
    "pythonpath",
    "xfail_strict",
    "empty_parameter_set_mark",
}


def test_a_plain_run_leaves_out_exactly_the_declared_markers() -> None:
    addopts = OPTIONS["addopts"]
    assert addopts[:-2] == PLAIN and addopts[-2] == "-m"
    declared = [marker.split(":", 1)[0].strip() for marker in OPTIONS["markers"]]
    assert sorted(addopts[-1].split(" and ")) == sorted(f"not {marker}" for marker in declared)
    assert set(OPTIONS) <= SETTINGS
    conftests = [p.read_text() for p in (REPO / "tests").rglob("conftest.py")]
    assert not any("collect_ignore" in text for text in conftests)


def test_an_empty_parametrize_fails_collection() -> None:
    assert OPTIONS["empty_parameter_set_mark"] == "fail_at_collect"
