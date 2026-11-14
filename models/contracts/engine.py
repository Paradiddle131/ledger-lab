"""`make contract-test`: run `datacontract test` over the local dbt build.

The CLI's DuckDB engine needs DuckDB 1.5, which conflicts with the project's 2.0 and cannot open
the database dbt builds, so the engine runs in a throwaway environment from the lock's `contract`
group, never fetched here (`make sync` puts it in uv's cache), over a copy of the build written in
1.5's storage format: `target/ledger_lab.duckdb` becomes `target/ledger_lab.cli.duckdb`, the file
the contract's `local` server names. The test therefore runs on the older engine over a copy, not
on the project's own engine, until the CLI supports DuckDB 2.0.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "models/contracts/payments.odcs.yaml"
BUILD = REPO / "target/ledger_lab.duckdb"
COPY = REPO / "target/ledger_lab.cli.duckdb"


def for_the_cli(database: Path, target: Path) -> Path:
    """A copy of a dbt build in DuckDB 1.5's storage format, which the CLI's engine reads."""
    target.unlink(missing_ok=True)
    with duckdb.connect() as db:
        db.execute("attach ? as build (read_only)", [str(database)])
        db.execute("attach ? as cli (storage_version 'v1.5.0')", [str(target)])
        db.execute("copy from database build to cli")
    return target


def in_engine_env(
    *command: str, env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command in the engine's locked environment, from uv's cache only."""
    uv = os.environ.get("UV") or shutil.which("uv") or "uv"
    return subprocess.run(  # noqa: S603
        [uv, "run", "--isolated", "--frozen", "--offline", "--only-group", "contract", *command],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env=(env or dict(os.environ)) | {"COLUMNS": "200"},
    )


def unavailable() -> str | None:
    """Why the engine's environment cannot be built offline, or None when it can."""
    probe = in_engine_env("python", "-c", "import datacontract, duckdb")
    if probe.returncode == 0:
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
