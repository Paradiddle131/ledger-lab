"""What `make lint` reads, held by planting files under the repository's ruff settings.

Checked:
- an untidy file under `.claude/` (an agent's worktree) is left out of `ruff check` and
  `ruff format --check`, while one in the tree is reported;
- an untidy file planted at the path of each tracked Python file, in a copy holding only the
  tracked ruff settings and `.gitignore` files, is reported by `ruff check` (rule F401) and by
  `ruff format --check`: a top-level, lint or format `exclude`, or a per-file ignore covering F401
  (`ALL` among them), on a tracked path turns it red;
- no tracked Python file opts out of linting whole with a file-level `# ruff: noqa` or
  `# flake8: noqa`.

Not checked: a per-file ignore of rules other than F401, a line's `# noqa`, a `# fmt: off`
region, and settings given on the command line.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUFF = Path(sys.executable).with_name("ruff")
GIT = shutil.which("git") or "git"
UNTIDY = "import os\nx=1\n"
SETTINGS = ("pyproject.toml", "ruff.toml", ".ruff.toml", ".gitignore")
FILE_LEVEL_NOQA = re.compile(r"^\s*#\s*(?:ruff|flake8)\s*:\s*noqa\b", re.IGNORECASE | re.MULTILINE)


def ruff(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(RUFF), *args], capture_output=True, text=True, check=False, cwd=cwd)


def tracked(*patterns: str) -> list[str]:
    listed = subprocess.run(
        [GIT, "-C", str(REPO), "ls-files", "-z", "--", *patterns],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [name for name in listed.split("\0") if name]


def test_lint_and_format_leave_an_agents_worktree_out(tmp_path: Path) -> None:
    shutil.copy(REPO / "pyproject.toml", tmp_path)
    worktree = tmp_path / ".claude" / "worktrees" / "half-edited" / "tests"
    worktree.mkdir(parents=True)
    (worktree / "test_half.py").write_text(UNTIDY)
    (tmp_path / "in_tree.py").write_text(UNTIDY)
    for command in (["check", "--no-cache", "."], ["format", "--check", "--no-cache", "."]):
        result = ruff(tmp_path, *command)
        out = result.stdout + result.stderr
        assert result.returncode == 1, out
        assert "in_tree.py" in out, out
        assert ".claude" not in out, out


def test_lint_and_format_report_a_file_planted_at_each_tracked_python_path(
    tmp_path: Path,
) -> None:
    subprocess.run([GIT, "init", "--quiet", str(tmp_path)], capture_output=True, check=True)
    for name in tracked(*(f"**/{setting}" for setting in SETTINGS), *SETTINGS):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / name, tmp_path / name)
    planted = tracked("*.py", "*.pyi")
    assert planted
    for name in planted:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(UNTIDY)

    for command, code in (
        (["check", "--no-cache", "--output-format", "json", "."], "F401"),
        (["format", "--check", "--no-cache", "--output-format", "json", "."], "unformatted"),
    ):
        result = ruff(tmp_path, *command)
        reported = {
            Path(d["filename"]).resolve().relative_to(tmp_path.resolve()).as_posix()
            for d in json.loads(result.stdout)
            if d["code"] == code
        }
        assert sorted(set(planted) - reported) == [], (command, result.stderr)


def test_no_tracked_python_file_opts_out_of_linting_whole() -> None:
    opted_out = [
        name
        for name in tracked("*.py", "*.pyi")
        if FILE_LEVEL_NOQA.search((REPO / name).read_text())
    ]
    assert opted_out == []
