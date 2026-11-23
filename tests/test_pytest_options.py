"""`tests/options_check.py`, which `tests/conftest.py` runs on every pytest run: the repository's
options pass, each way of selecting other tests is named, the hook stops a run on one, and a
conftest holding `collect_ignore` fails a run however its code bound the name."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest, options_check
from tests.options_check import problems

REPO = Path(__file__).resolve().parents[1]


def copy_of_options(tmp_path: Path) -> Path:
    """A folder holding the repository's `pyproject.toml`."""
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def test_the_repositorys_options_pass() -> None:
    assert problems(REPO, REPO / "pyproject.toml") == []


@pytest.mark.parametrize(
    ("old", "new", "named"),
    [
        ("and not pages and", "and pages and", "-m expression"),
        (" and not bigquery_persona", "", "-m expression"),
        ('"-ra", "-m",', '"-ra", "-k", "not x", "-m",', "addopts"),
        ('"-ra", "-m",', '"-ra", "--deselect=tests/test_glossary.py", "-m",', "addopts"),
        ('"-ra", "-m",', '"-ra", "-mnot stack", "-m",', "addopts"),
        ('"-ra", "-m",', '"-ra", "-k",', "addopts"),
        ('testpaths = ["tests"]', 'testpaths = ["tests/platform"]', "testpaths"),
        ('testpaths = ["tests"]', 'testpaths = ["tests", "platform"]', "testpaths"),
        (
            'testpaths = ["tests"]',
            'testpaths = ["tests"]\nnorecursedirs = ["semantics"]',
            "settings",
        ),
        ('empty_parameter_set_mark = "fail_at_collect"', "", "empty_parameter_set_mark"),
    ],
    ids=[
        "a term inverted",
        "a term dropped",
        "-k",
        "--deselect",
        "a second -m",
        "-k for -m",
        "testpaths narrowed",
        "testpaths widened",
        "norecursedirs",
        "no empty mark",
    ],
)
def test_each_way_of_selecting_other_tests_is_named(
    tmp_path: Path, old: str, new: str, named: str
) -> None:
    repo = copy_of_options(tmp_path)
    text = (repo / "pyproject.toml").read_text()
    assert text.count(old) == 1, f"pyproject.toml no longer holds {old!r} once"
    (repo / "pyproject.toml").write_text(text.replace(old, new))
    (found,) = problems(repo, repo / "pyproject.toml")
    assert named in found


def test_another_options_file_is_named(tmp_path: Path) -> None:
    repo = copy_of_options(tmp_path)
    (repo / "pytest.ini").write_text("[pytest]\n")
    assert problems(repo, repo / "pytest.ini") == [
        f"pytest read its options from {repo / 'pytest.ini'}, not from {repo / 'pyproject.toml'}"
    ]


def test_the_hook_stops_a_run_on_a_problem() -> None:
    with pytest.raises(pytest.UsageError, match="pytest's options: pytest read its options"):
        conftest.pytest_configure(SimpleNamespace(inipath=None))  # type: ignore[arg-type]


def test_this_run_carries_the_conftest_check(request: pytest.FixtureRequest) -> None:
    manager = request.config.pluginmanager
    assert manager.is_registered(options_check)
    check = manager.get_plugin("conftest collect_ignore check")
    assert isinstance(check, options_check.ConftestIgnores)
    assert str(REPO / "tests" / "conftest.py") in check.conftests


DROPS = 'collect_ignore = ["test_dropped.py"]'
FORMS = {
    "assignment": DROPS,
    "glob": 'collect_ignore_glob = ["*dropped*"]',
    "unpacking": 'collect_ignore, _x = ["test_dropped.py"], 0',
    "walrus": '(collect_ignore := ["test_dropped.py"])',
    "globals update": 'globals().update(collect_ignore=["test_dropped.py"])',
    "module attribute": f"import sys\nsys.modules[__name__].{DROPS}",
    "import": "from ignores import collect_ignore",
    "star import": "from ignores import *",
    "built name": 'globals()["collect_" + "ignore"] = ["test_dropped.py"]',
    "f-string": 'part = "ignore"\nglobals()[f"collect_{part}"] = ["test_dropped.py"]',
    "exec": f"exec({DROPS!r})",
    "module getattr": (
        "def __getattr__(name):\n"
        '    if name == "collect_ignore":\n'
        '        return ["test_dropped.py"]\n'
        "    raise AttributeError(name)"
    ),
    "deleted once collected": (
        f"import sys\n\n{DROPS}\n\n\ndef pytest_collection_modifyitems():\n"
        "    del sys.modules[__name__].collect_ignore"
    ),
    "set while collecting": (
        f"import sys\n\n\ndef pytest_collectstart(collector):\n    sys.modules[__name__].{DROPS}"
    ),
}


def child_pytest(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """pytest run in `project`, with this repository importable so `-p tests.options_check` loads
    the check `tests/conftest.py` registers, and no `PYTEST_` variable of this run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        capture_output=True,
        text=True,
        cwd=project,
        env={**env, "PYTHONPATH": str(REPO)},
        timeout=120,
        check=False,
    )


def project_with(root: Path, folder: str, conftest_code: str) -> Path:
    """A project of three tests, `test_dropped.py` in `folder` beside a conftest holding
    `conftest_code` and a module `ignores` it can import `collect_ignore` from."""
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "test_kept.py").write_text("def test_kept():\n    pass\n")
    where = root / folder
    where.mkdir(parents=True, exist_ok=True)
    (where / "test_dropped.py").write_text("def test_dropped():\n    pass\n")
    (where / "test_beside.py").write_text("def test_beside():\n    pass\n")
    (where / "ignores.py").write_text(DROPS + "\n")
    (where / "conftest.py").write_text(conftest_code + "\n")
    return root


@pytest.mark.parametrize("code", FORMS.values(), ids=FORMS.keys())
def test_each_way_of_binding_collect_ignore_fails_the_run(tmp_path: Path, code: str) -> None:
    project = project_with(tmp_path, ".", code)
    unchecked = child_pytest(project)
    assert unchecked.returncode == 0, unchecked.stdout[-2000:]
    assert "2 passed" in unchecked.stdout, "the form does not make pytest leave a test out"
    checked = child_pytest(project, "-p", "tests.options_check")
    assert checked.returncode == pytest.ExitCode.INTERRUPTED, checked.stdout[-2000:]
    name = "collect_ignore_glob" if "_glob" in code else "collect_ignore"
    assert f"pytest's options: conftest.py sets {name}" in checked.stdout
    assert " passed" not in checked.stdout


@pytest.mark.parametrize("workers", [[], ["-n", "2"]], ids=["one process", "xdist"])
def test_a_nested_conftest_loaded_while_collecting_fails_the_run(
    tmp_path: Path, workers: list[str]
) -> None:
    project = project_with(tmp_path, "tests/sub", DROPS)
    unchecked = child_pytest(project, *workers)
    assert unchecked.returncode == 0, unchecked.stdout[-2000:]
    assert "2 passed" in unchecked.stdout, "the nested conftest does not leave a test out"
    checked = child_pytest(project, "-p", "tests.options_check", *workers)
    assert checked.returncode == (1 if workers else 2), checked.stdout[-2000:]
    assert "pytest's options: tests/sub/conftest.py sets collect_ignore" in checked.stdout
    assert " passed" not in checked.stdout


def test_a_comment_naming_collect_ignore_does_not_stop_a_run(tmp_path: Path) -> None:
    project = project_with(tmp_path, "tests/sub", f"# {DROPS}")
    checked = child_pytest(project, "-p", "tests.options_check", "-n", "2")
    assert checked.returncode == 0, checked.stdout[-2000:]
    assert "3 passed" in checked.stdout
