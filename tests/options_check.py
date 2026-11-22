"""What a plain pytest run selects, checked from the configuration pytest itself read.

`tests/conftest.py` runs `problems` in `pytest_configure`, on every run whatever it selects, and
stops the run on any problem; under pytest-xdist the run's summary no longer counts the tests the
`-m` expression deselects, so this is where that expression is held. A plain run must read its
options from the repository's `pyproject.toml` (a `pytest.ini`, `tox.ini` or `setup.cfg` that pytest
would prefer is refused), and those options must hold:
- one marker expression that leaves out exactly the declared markers, each as a `not <marker>`
  term, in any order, and no other option that selects or drops tests (`-k`, `--deselect`,
  `--ignore`, a second `-m`);
- no setting that changes what is collected (`norecursedirs`, `python_files`, `collect_ignore`
  and the like), and no `collect_ignore` in a conftest;
- `empty_parameter_set_mark = "fail_at_collect"`, so a parametrize given an empty list fails
  collection rather than skipping its test.
What it leaves out: a test skipped by `skipif` (it is still collected, and `-ra` names it), and
a run given its own options on the command line (`-m`, `-k`, `-o`, `-c`), the caller's choice.
"""

import tomllib
from pathlib import Path

PLAIN = ["-q", "--strict-markers", "--strict-config", "-ra"]
SETTINGS = {
    "addopts",
    "markers",
    "testpaths",
    "pythonpath",
    "xfail_strict",
    "empty_parameter_set_mark",
}


def problems(repo: Path, inipath: Path | None) -> list[str]:
    """Each way the configuration pytest read lets a plain run select other tests than the
    declared markers leave; empty when it holds."""
    expected = repo / "pyproject.toml"
    if inipath is None or inipath.resolve() != expected.resolve():
        return [f"pytest read its options from {inipath}, not from {expected}"]
    options = tomllib.loads(expected.read_text())["tool"]["pytest"]["ini_options"]
    found: list[str] = []
    addopts = options.get("addopts", [])
    if addopts[:-2] != PLAIN or addopts[-2:-1] != ["-m"]:
        found.append(f"addopts must be {PLAIN} and one -m expression, not {addopts}")
    else:
        declared = sorted(m.split(":", 1)[0].strip() for m in options.get("markers", []))
        terms = sorted(addopts[-1].split(" and "))
        if terms != [f"not {m}" for m in declared]:
            found.append("the -m expression does not leave out exactly the declared markers")
    if unknown := sorted(set(options) - SETTINGS):
        found.append(f"settings that may change what is collected: {', '.join(unknown)}")
    if options.get("empty_parameter_set_mark") != "fail_at_collect":
        found.append('empty_parameter_set_mark is not "fail_at_collect"')
    for conftest in sorted((repo / "tests").rglob("conftest.py")):
        if "collect_ignore" in conftest.read_text():
            found.append(f"{conftest.relative_to(repo)} sets collect_ignore")
    return found
