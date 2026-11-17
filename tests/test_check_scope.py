"""What `make lint` reads, held by planting files under the repository's ruff settings.

Checked, by these cases only:
- an untidy file under `.claude/` (an agent's worktree) is left out of `ruff check` and
  `ruff format --check`, while one in the tree is reported;
- an untidy file planted at the path of each tracked Python file, in a copy holding only the
  tracked files named in SETTINGS (`pyproject.toml`, `ruff.toml`, `.ruff.toml`, `.gitignore` and
  `.ignore`, at any level), is reported by `ruff check` (rule F401) and by `ruff format --check`;
  so a top-level, lint or format `exclude` written relative to the project root, a per-file
  ignore covering F401 (`ALL` among them), or a pattern in a tracked `.gitignore` or `.ignore`,
  that covers a tracked path, turns it red;
- the copy holds the checkout's `pyproject.toml` unchanged, and an untidy file planted there at
  a path the tracked root `.gitignore` ignores is reported by neither command, so the copy is a
  git work tree whose tracked `.gitignore` ruff honours;
- untidy text given on stdin under the checkout's own absolute path of each tracked Python file,
  with the checkout's root `pyproject.toml` as the only settings and `--force-exclude`, is
  reported by `ruff check` (rule F401) and by `ruff format --check`; so a top-level, lint or
  format `exclude`, or a per-file ignore covering F401, in that file, that names the checkout by
  its location (an absolute or `../` path) and covers a tracked path, turns it red;
- no tracked Python file opts out of linting whole with a file-level `# ruff: noqa` or
  `# flake8: noqa`.

Not checked:
- an `exclude` naming the checkout by its location in a settings file other than the root
  `pyproject.toml` (a nested `ruff.toml`, `.ruff.toml` or `pyproject.toml`);
- a per-file ignore of rules other than F401, a line's `# noqa`, a `# fmt: off` or
  `# yapf: disable` region, and settings given on the command line;
- a ruff settings file that is not tracked;
- ignore sources outside the copied tracked files, such as the checkout's `.git/info/exclude`
  or a global git excludes file.
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
SETTINGS = ("pyproject.toml", "ruff.toml", ".ruff.toml", ".gitignore", ".ignore")
IGNORED_PROBE = "data/planted_by_the_scope_test.py"
FILE_LEVEL_NOQA = re.compile(r"^\s*#\s*(?:ruff|flake8)\s*:\s*noqa\b", re.IGNORECASE | re.MULTILINE)


def ruff(cwd: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(RUFF), *args], input=stdin, capture_output=True, text=True, check=False, cwd=cwd
    )


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
    copied = tmp_path / "pyproject.toml"
    assert copied.is_file() and copied.read_bytes() == (REPO / "pyproject.toml").read_bytes()
    planted = tracked("*.py", "*.pyi")
    assert planted
    for name in planted:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(UNTIDY)
    # The probe path is ignored by a line of the tracked root `.gitignore`, and is not tracked.
    ignored_by = subprocess.run(
        [GIT, "-C", str(REPO), "check-ignore", "--no-index", "--verbose", IGNORED_PROBE],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert ignored_by.startswith(".gitignore:"), ignored_by
    assert IGNORED_PROBE not in planted
    (tmp_path / IGNORED_PROBE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / IGNORED_PROBE).write_text(UNTIDY)

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
        assert IGNORED_PROBE not in reported, command


def test_lint_and_format_report_untidy_text_at_each_tracked_python_path_in_the_checkout() -> None:
    # Nothing is written into the checkout: the text goes on stdin under the tracked file's own
    # absolute path, and `--config` keeps untracked settings files there out of the run.
    settings = ["--config", str(REPO / "pyproject.toml"), "--no-cache", "--force-exclude"]
    planted = tracked("*.py", "*.pyi")
    assert planted
    left_out = []
    for name in planted:
        path = str(REPO / name)
        linted = ruff(
            REPO,
            "check",
            *settings,
            "--output-format",
            "json",
            "--stdin-filename",
            path,
            "-",
            stdin=UNTIDY,
        )
        formatted = ruff(
            REPO, "format", "--check", *settings, "--stdin-filename", path, "-", stdin=UNTIDY
        )
        if "F401" not in {d["code"] for d in json.loads(linted.stdout)}:
            left_out.append(("check", name, linted.stderr))
        if formatted.returncode != 1:
            left_out.append(("format", name, formatted.stderr))
    assert left_out == []


def test_no_tracked_python_file_opts_out_of_linting_whole() -> None:
    opted_out = [
        name
        for name in tracked("*.py", "*.pyi")
        if FILE_LEVEL_NOQA.search((REPO / name).read_text())
    ]
    assert opted_out == []
