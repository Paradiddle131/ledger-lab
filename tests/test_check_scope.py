"""The linter leaves an agent's worktrees under `.claude/` out, and reads every tracked Python
file."""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUFF = Path(sys.executable).with_name("ruff")
GIT = shutil.which("git") or "git"
UNTIDY = "import os\nx=1\n"


def ruff(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(RUFF), *args], capture_output=True, text=True, check=False, cwd=cwd)


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


def test_ruff_check_reads_every_tracked_python_file() -> None:
    shown = ruff(REPO, "check", "--show-files", ".")
    assert shown.returncode == 0, shown.stdout + shown.stderr
    read = {Path(line).resolve() for line in shown.stdout.splitlines() if line.strip()}
    listed = subprocess.run(
        [GIT, "-C", str(REPO), "ls-files", "-z", "--", "*.py", "*.pyi"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tracked = {(REPO / name).resolve() for name in listed.split("\0") if name}
    assert tracked
    assert tracked <= read, sorted(str(p) for p in tracked - read)


def test_the_formatter_excludes_nothing_of_its_own() -> None:
    """`ruff format` reads what `ruff check` reads, less only an exclusion of its own."""
    ruff_settings = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["ruff"]
    assert "exclude" not in ruff_settings.get("format", {})
