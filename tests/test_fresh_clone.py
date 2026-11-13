"""A fresh clone of HEAD holds no secret, identifier or data, and the check catches each kind.

Planted values are built from pieces so this file's own lines never match what it plants.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from fresh_clone import check_clone

REPO = Path(__file__).resolve().parents[1]
GIT = shutil.which("git") or "git"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        [GIT, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        check=True,
    )


def repo_with(tmp_path: Path, name: str, content: str, *, force: bool = False) -> Path:
    """A repo holding this repo's .gitignore, a README, and one planted file, committed."""
    repo = tmp_path / "planted"
    repo.mkdir()
    git(repo, "init", "--quiet")
    shutil.copy(REPO / ".gitignore", repo / ".gitignore")
    (repo / "README.md").write_text("A clean file.\n")
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", ".gitignore", "README.md")
    git(repo, "add", *(["-f"] if force else []), name)
    git(repo, "commit", "--quiet", "-m", "plant")
    return repo


def test_a_fresh_clone_of_head_is_clean() -> None:
    assert check_clone(REPO) == []


AT = "@"
PLANTS = {
    "data file": ("data/payments.csv", "vendor,amount\nA,1\n", True, "excludes it"),
    "email": ("notes.md", "Ask person" + AT + "lab-mail.net.\n", False, "email address"),
    "project path": ("app.py", 'x = "projects/' + 'acme-ledger-prod/datasets"\n', False, "acme"),
    "project flag": ("run.sh", "bq --project_id=" + "acme-ledger-prod query\n", False, "acme"),
    "project setting": ("conf.yaml", "project: '" + "acme-ledger-prod'\n", False, "acme"),
    "service account": (
        "who.md",
        "runs as bot" + AT + "acme-ledger-prod.iam.gserviceaccount.com\n",
        False,
        "acme",
    ),
    "key file": ("sa.txt", '{"type": "' + 'service_account"}\n', False, "key material"),
    "private key": ("k.pem.txt", "-----BEGIN " + "PRIVATE KEY-----\n", False, "key material"),
    "secret": ("settings.py", 'k = "' + "AK" + "IA" + "Z7Q3M9XK2PL8V4RT" + '"\n', False, "secret"),
}


@pytest.mark.parametrize("kind", PLANTS)
def test_each_planted_kind_fails_the_check(tmp_path: Path, kind: str) -> None:
    name, content, force, expected = PLANTS[kind]
    found = check_clone(repo_with(tmp_path, name, content, force=force))
    assert any(expected in line and name in line for line in found), found


@pytest.mark.parametrize(
    "content",
    [
        "Write to someone" + AT + "example.com.\n",
        'project: "the-lab"\n',
        "bq --project_id=" + "another-lab query\n",
        'project: "${LEDGER_LAB_GCP_PROJECT}"\n',
    ],
)
def test_placeholders_pass(tmp_path: Path, content: str) -> None:
    assert check_clone(repo_with(tmp_path, "doc.md", content)) == []


def test_only_committed_files_are_checked(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "doc.md", "clean\n")
    (repo / "loose.md").write_text("Ask person" + AT + "lab-mail.net.\n")
    assert check_clone(repo) == []
