"""`make contract-test`: run `datacontract test` over the local dbt build.

The CLI's DuckDB engine needs DuckDB 1.5, which conflicts with the project's 2.0 and cannot open
the database dbt builds, so the engine runs in the lock's `contract` group's one environment
(platform/group_env.py), built from uv's cache with no network, never fetched here (`make sync`
builds it, fetching into uv's cache what it lacks), over a copy of the build written in
1.5's storage format: `models/target/ledger_lab.duckdb`, where the profile's local output writes by
default, becomes `target/ledger_lab.cli.duckdb` under the repository's root, the file the contract's
`local` server names. The test therefore runs on the older engine over a copy, not
on the project's own engine, until the CLI supports DuckDB 2.0.
"""

import os
import subprocess
import sys
from pathlib import Path

import duckdb

# The lock groups' environments (platform/group_env.py), found from this file wherever it runs.
sys.path.append(str(Path(__file__).resolve().parents[2] / "platform"))
import group_env  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "models/contracts/payments.odcs.yaml"
BUILD = REPO / "models/target/ledger_lab.duckdb"
COPY = REPO / "target/ledger_lab.cli.duckdb"
GROUP = "contract"


def for_the_cli(database: Path, target: Path) -> Path:
    """A copy of a dbt build in DuckDB 1.5's storage format, which the CLI's engine reads."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with duckdb.connect() as db:
        db.execute("attach ? as build (read_only)", [str(database)])
        db.execute("attach ? as cli (storage_version 'v1.5.0')", [str(target)])
        db.execute("copy from database build to cli")
    return target


def in_engine_env(
    *command: str, env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command in the engine's locked environment, built from uv's cache only; UnbuiltError
    when it cannot be."""
    folder = group_env.environment(GROUP, repo=REPO)
    return subprocess.run(  # noqa: S603
        group_env.command(folder, *command),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env=group_env.run_environment(folder, env or dict(os.environ)) | {"COLUMNS": "200"},
    )


def unavailable() -> str | None:
    """Why the engine's environment cannot be built offline, or None when it can."""
    try:
        probe = in_engine_env("python", "-c", "import datacontract, duckdb")
    except group_env.UnbuiltError:
        probe = None
    if probe is not None and probe.returncode == 0:
        return None
    return (
        "the Data Contract CLI's DuckDB engine is not in uv's cache: run `make sync` with network"
    )


def main() -> int:
    reason = unavailable()
    if reason:
        print(f"contract-test: {reason}", file=sys.stderr)
        return 1
    if not BUILD.is_file():
        print(f"contract-test: no build at {BUILD.relative_to(REPO)}: run dbt build first")
        return 1
    for_the_cli(BUILD, COPY)
    run = in_engine_env("datacontract", "test", str(CONTRACT))
    print(run.stdout + run.stderr)
    return run.returncode


if __name__ == "__main__":
    sys.exit(main())
