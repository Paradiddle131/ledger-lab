"""`make contract`: fail when the payments contract breaks a consumer of the one on `origin/main`.

The Data Contract CLI's `breaking` command compares the two from a consumer's point of view: a
change is breaking when data a consumer could have relied on may now arrive otherwise (a column
removed or renamed, any type changed, a column made nullable, a key or uniqueness dropped, an
accepted value added, a bound or quality rule loosened or removed), and not breaking when the
contract only promises more (an optional column added, a nullable column made required, an
accepted value removed) or changes words (a description, a classification).

The comparison reads `origin/main` from the local clone, so it needs no network. A clone that does
not hold `origin/main` cannot be compared, so the gate fails and says how to fetch it, rather than
passing a change it never saw. While `origin/main` holds no payments contract there is nothing
published to break, so the first contract passes, and the gate says so.

The CLI runs in its own locked environment (the `contract` dependency group), installed from the
lock on first use and read from uv's cache after that.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTRACT = Path("models/contracts/payments.odcs.yaml")
BASE = "origin/main"


class GateError(Exception):
    """The comparison cannot be made; the message says why and what to do."""


def in_contract_env(
    *command: str, env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command in the CLI's locked environment, never reaching for a newer one."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv is None:
        raise GateError("uv is missing: install it (https://docs.astral.sh/uv/)")
    return subprocess.run(  # noqa: S603
        [uv, "run", "--isolated", "--frozen", "--only-group", "contract", *command],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env=(env or dict(os.environ)) | {"COLUMNS": "200"},
    )


def datacontract(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the Data Contract CLI."""
    return in_contract_env("datacontract", *args, env=env)


def breaking(before: Path, after: Path) -> subprocess.CompletedProcess[str]:
    """The CLI's consumer-perspective comparison; a non-zero exit is a breaking change."""
    return datacontract("breaking", "--no-inline-references", str(before), str(after))


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )


def published(repo: Path, base: str = BASE, path: Path = CONTRACT) -> str | None:
    """The contract as `base` holds it, or None when `base` holds none; GateError without `base`."""
    if git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        raise GateError(
            f"{base} is not in this clone, so the contract cannot be compared with it:"
            f" run `git fetch origin main` and try again"
        )
    shown = git(repo, "show", f"{base}:{path.as_posix()}")
    return shown.stdout if shown.returncode == 0 else None


def check(repo: Path = REPO, base: str = BASE) -> tuple[bool, str]:
    """Whether the working tree's contract keeps every promise `base`'s made, and the report."""
    before = published(repo, base)
    if before is None:
        return True, f"contract: {base} holds no payments contract, so nothing published can break"
    after = repo / CONTRACT
    if not after.is_file():
        return False, f"contract: {CONTRACT} is gone, which breaks every consumer of {base}'s"
    with tempfile.TemporaryDirectory(prefix="contract-") as tmp:
        old = Path(tmp) / "published.odcs.yaml"
        old.write_text(before)
        result = breaking(old, after)
    if result.returncode == 0:
        return True, f"contract: no change breaks a consumer of {base}'s contract"
    return False, (
        f"contract: a change breaks consumers of {base}'s contract\n{result.stdout}{result.stderr}"
    )


def main() -> int:
    try:
        ok, report = check()
    except GateError as e:
        print(f"contract: {e}", file=sys.stderr)
        return 1
    print(report, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
