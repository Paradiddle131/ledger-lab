"""What a plain pytest run selects, checked from the configuration and the conftests pytest read.

`tests/conftest.py` runs `problems` in `pytest_configure`, on every run whatever it selects, and
stops the run on any problem; under pytest-xdist the run's summary no longer counts the tests the
`-m` expression deselects, so this is where that expression is held. A plain run must read its
options from the repository's `pyproject.toml` (a `pytest.ini`, `tox.ini` or `setup.cfg` that pytest
would prefer is refused), and those options must hold:
- one marker expression that leaves out exactly the declared markers, each as a `not <marker>`
  term, in any order, and no other option that selects or drops tests (`-k`, `--deselect`,
  `--ignore`, a second `-m`);
- `testpaths` holding `tests` alone, and no other setting that changes what is collected
  (`norecursedirs`, `python_files` and the like);
- `empty_parameter_set_mark = "fail_at_collect"`, so a parametrize given an empty list fails
  collection rather than skipping its test.
`tests/conftest.py` then registers this module as a plugin, which fails a run in which any conftest
pytest loaded, at the root, under `tests/` or nested and loaded during collection, has a
`collect_ignore` or `collect_ignore_glob` attribute: it asks each conftest module for the
attribute as pytest does, so how its code bound the name does not matter, and a comment naming it
is no attribute.
What it leaves out: a test skipped by `skipif` (it is still collected, and `-ra` names it); code
that changes what is collected while the run goes, which only a reading of the code shows: a
conftest's or plugin's collection hooks (`pytest_ignore_collect`, `pytest_collection_modifyitems`,
a module `pytest_plugins` names), a conftest that sets the attribute during collection and deletes
it again before collection ends, or one that unregisters this plugin; and options given on the
command line or in `PYTEST_ADDOPTS` (`-m`, `-k`, `-o`, `--ignore`, `-p no:tests.options_check`),
the caller's choice. A `-c` naming another file is refused like any other options file.
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
    """Registers the conftest check, once this module is a plugin: `tests/conftest.py` registers
    it, and a child pytest outside the repository loads it with `-p tests.options_check`."""
    config.pluginmanager.register(ConftestIgnores(config.rootpath), "conftest collect_ignore check")


class ConftestIgnores:
    """Fails a run in which a conftest pytest loaded has `collect_ignore` or `collect_ignore_glob`.

    pytest reads each with `getattr` on the conftest module while it collects that conftest's
    directory, and loads a nested conftest only once collection reaches its directory, so the
    check asks each conftest with `hasattr` when pytest registers it and again once collection
    ends. By then the tests the attribute left out are gone; the check reports a collection error
    naming the conftest and the attribute and drops every collected test, so no test runs and the
    run exits non-zero: 2 from pytest, 1 under pytest-xdist."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.conftests: dict[str, object] = {}
        self.seen: set[tuple[str, str]] = set()

    def pytest_plugin_registered(self, plugin: object, plugin_name: str) -> None:
        """pytest registers each conftest under its path, and replays this hook for those it
        registered before this plugin; an attribute held here counts though it is deleted later."""
        if Path(plugin_name).name == "conftest.py":
            self.conftests[plugin_name] = plugin
            self.seen |= {(plugin_name, name) for name in IGNORES if hasattr(plugin, name)}

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Runs before pytest-xdist's own hook, which sends a worker's collected tests to the
        controller, so a worker sends none."""
        held = {(p, n) for p, mod in self.conftests.items() for n in IGNORES if hasattr(mod, n)}
        if found := sorted(self.seen | held):
            session.items.clear()
        for path, name in found:
            where = self.shown(path)
            session.ihook.pytest_collectreport(
                report=pytest.CollectReport(
                    where, "failed", f"pytest's options: {where} sets {name}", []
                )
            )

    def shown(self, path: str) -> str:
        """The conftest's path from the root directory, or whole when it is outside it."""
        conftest = Path(path)
        return str(conftest.relative_to(self.root)) if conftest.is_relative_to(self.root) else path
