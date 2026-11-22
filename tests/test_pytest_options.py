"""`tests/options_check.py`, which `tests/conftest.py` runs on every pytest run: the repository's
options pass, each way of selecting other tests is named, and the hook stops a run on one."""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest
from tests.options_check import problems

REPO = Path(__file__).resolve().parents[1]


def copy_of_options(tmp_path: Path) -> Path:
    """A folder holding the repository's `pyproject.toml` and its conftest files."""
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    for conftest_file in (REPO / "tests").rglob("conftest.py"):
        target = tmp_path / conftest_file.relative_to(REPO)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(conftest_file, target)
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


def test_another_options_file_or_a_conftests_collect_ignore_is_named(tmp_path: Path) -> None:
    repo = copy_of_options(tmp_path)
    (repo / "pytest.ini").write_text("[pytest]\n")
    assert problems(repo, repo / "pytest.ini") == [
        f"pytest read its options from {repo / 'pytest.ini'}, not from {repo / 'pyproject.toml'}"
    ]
    with (repo / "tests" / "conftest.py").open("a") as file:
        file.write('\ncollect_ignore = ["x"]\n')
    assert problems(repo, repo / "pyproject.toml") == ["tests/conftest.py sets collect_ignore"]


def test_the_hook_stops_a_run_on_a_problem() -> None:
    with pytest.raises(pytest.UsageError, match="pytest's options: pytest read its options"):
        conftest.pytest_configure(SimpleNamespace(inipath=None))  # type: ignore[arg-type]
    conftest.pytest_configure(SimpleNamespace(inipath=REPO / "pyproject.toml"))  # type: ignore[arg-type]
