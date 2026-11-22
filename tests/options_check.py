"""What a plain pytest run selects, checked from the configuration pytest itself read.

`tests/conftest.py` runs `problems` in `pytest_configure`, on every run whatever it selects, and
stops the run on any problem; under pytest-xdist the run's summary no longer counts the tests the
`-m` expression deselects, so this is where that expression is held. A plain run must read its
options from the repository's `pyproject.toml` (a `pytest.ini`, `tox.ini` or `setup.cfg` that pytest
would prefer is refused), and those options must hold:
- one marker expression that leaves out exactly the declared markers, each as a `not <marker>`
  term, in any order, and no other option that selects or drops tests (`-k`, `--deselect`,
  `--ignore`, a second `-m`);
- `testpaths` holding `tests` alone, no other setting that changes what is collected
  (`norecursedirs`, `python_files` and the like), and no conftest, at the repository's root or
  under `tests/`, whose code names `collect_ignore` or `collect_ignore_glob` in any form (a binding,
  an import, a string); a comment that names them does not count;
- `empty_parameter_set_mark = "fail_at_collect"`, so a parametrize given an empty list fails
  collection rather than skipping its test.
What it leaves out: a test skipped by `skipif` (it is still collected, and `-ra` names it); a
conftest's or plugin's collection hooks (`pytest_ignore_collect`, `pytest_collection_modifyitems`,
a module `pytest_plugins` names), which only a reading of the code shows; and options given on
the command line or in `PYTEST_ADDOPTS` (`-m`, `-k`, `-o`), the caller's choice. A `-c` naming
another file is refused like any other options file.
"""

import tomllib
from pathlib import Path

import pytest

PLAIN = ["-q", "--strict-markers", "--strict-config", "-ra"]
IGNORES = ("collect_ignore", "collect_ignore_glob")
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
    if options.get("testpaths") != ["tests"]:
        found.append(f'testpaths must be ["tests"], not {options.get("testpaths")}')
    if options.get("empty_parameter_set_mark") != "fail_at_collect":
        found.append('empty_parameter_set_mark is not "fail_at_collect"')
    return found


def pytest_configure(config: pytest.Config) -> None:
    """Registers the conftest check; `tests/conftest.py` loads this module as a plugin."""
    config.pluginmanager.register(ConftestIgnores(config.rootpath), "conftest collect_ignore check")


class ConftestIgnores:
    """Fails a run in which a conftest pytest loaded holds `collect_ignore` or
    `collect_ignore_glob`. pytest reads each as an attribute of the conftest module (`getattr`)
    while it collects the conftest's directory, so this asks each module the same way, however
    its code bound the name, and asks after collection too, since pytest loads a nested conftest
    only once collection reaches its directory: the tests the attribute left out are already
    gone, and the run ends with a usage error (exit status 4) instead of running the rest."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.conftests: dict[str, object] = {}
        self.seen: set[tuple[str, str]] = set()

    def pytest_plugin_registered(self, plugin: object, plugin_name: str) -> None:
        """pytest registers each conftest under its path; this is also replayed for each one
        registered before this plugin. A module deleting the attribute later is still named."""
        if Path(plugin_name).name == "conftest.py":
            self.conftests[plugin_name] = plugin
            self.seen |= {(plugin_name, name) for name in IGNORES if hasattr(plugin, name)}

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_finish(self) -> None:
        now = {(p, n) for p, conftest in self.conftests.items() for n in IGNORES if hasattr(conftest, n)}
        if found := sorted(self.seen | now):
            raise pytest.UsageError(
                "pytest's options: " + "; ".join(f"{self.shown(p)} sets {n}" for p, n in found)
            )

    def shown(self, path: str) -> str:
        return str(Path(path).relative_to(self.root)) if Path(path).is_relative_to(self.root) else path
