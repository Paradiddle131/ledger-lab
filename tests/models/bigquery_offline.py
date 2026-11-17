"""Commands in the lock's `bigquery` group, run from uv's cache with no network: `uv run --isolated
--frozen --offline --only-group bigquery`, with `semantics/offline` on `PYTHONPATH`, whose
`sitecustomize` ends a Python process at its first connection or name lookup, and every proxy
variable pointed at a closed loopback port. The caller's `GCP_PROJECT` and `LEDGER_LAB_GCP_PROJECT`
are passed through unchanged.

`unavailable()` says why the group cannot run: its packages are not in uv's cache, or the adapter
does not import. Any other failure is the caller's to report.
"""

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# A port nothing listens on: a request through it is refused at once.
CLOSED = "http://127.0.0.1:9"
PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
# What uv prints when `--offline` finds a package missing from its cache.
NOT_CACHED = "Network connectivity is disabled"


def environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ) | {name: CLOSED for name in PROXIES}
    env |= {"NO_PROXY": "", "no_proxy": "", "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    env["PYTHONPATH"] = str(REPO / "semantics" / "offline")
    return env | (extra or {})


def run(*command: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    uv = os.environ.get("UV") or shutil.which("uv") or "uv"
    return subprocess.run(  # noqa: S603
        [uv, "run", "--isolated", "--frozen", "--offline", "--only-group", "bigquery", *command],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env=environment(extra),
    )


def unavailable() -> str | None:
    """Why dbt-bigquery cannot run here, or None when it can: the group is not in uv's cache, or
    the adapter's module does not import."""
    probe = run("python", "-c", "import dbt.adapters.bigquery")
    if probe.returncode == 0:
        return None
    if NOT_CACHED in probe.stderr:
        return "the lock's bigquery group is not in uv's cache: run `uv sync --group bigquery` once"
    if "ModuleNotFoundError" in probe.stderr or "ImportError" in probe.stderr:
        return "dbt-bigquery does not import in the lock's bigquery group"
    raise AssertionError(f"the bigquery group's probe failed: {probe.stderr.strip()[-500:]}")
