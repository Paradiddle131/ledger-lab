"""MetricFlow's command line (`mf`) over a dbt build on the local engine, offline.

`mf` reads the semantic manifest dbt writes at parse time from `<project>/target/`, whatever
`DBT_TARGET_PATH` says, so each build gets a project folder of its own beside it: the project and
profile files and that build's manifest. Every run goes through
`semantics/offline/offline_mf.py`, which turns the command's update check off and refuses any
network call, and reads the build's database through the build's environment; a run that
bypassed the runner would still refuse the network, through `semantics/offline/sitecustomize.py`.
"""

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.models.dbt_build import PROJECT, Build

NO_NETWORK = PROJECT.parent / "semantics" / "offline"
RUNNER = NO_NETWORK / "offline_mf.py"
MF = Path(sys.executable).with_name("mf")


def manifest(build: Build) -> dict[str, Any]:
    """The semantic manifest the build's parse wrote: semantic models, metrics, time spines."""
    return json.loads((build.cwd / "target" / "semantic_manifest.json").read_text())


def project(build: Build) -> Path:
    folder = build.cwd / "metricflow"
    if not folder.exists():
        (folder / "target").mkdir(parents=True)
        for name in ("dbt_project.yml", "profiles.yml"):
            shutil.copy(PROJECT / name, folder / name)
        shutil.copy(build.cwd / "target" / "semantic_manifest.json", folder / "target")
    return folder


def mf_env(build: Build) -> dict[str, str]:
    """The build's environment for a MetricFlow run: its project folder, the offline folder on the
    import path, so any Python process started with it refuses the network even without the
    runner, and a temporary folder of its own, so no cached update check from an earlier run can
    hide a request."""
    folder = project(build)
    temporary = folder / "tmp"
    temporary.mkdir(exist_ok=True)
    return build.env | {
        "DBT_PROJECT_DIR": str(folder),
        "DBT_PROFILES_DIR": str(folder),
        "PYTHONPATH": str(NO_NETWORK),
        "TMPDIR": str(temporary),
    }


def mf(build: Build, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        # Isolated (-I): the runner's own folder is not on the import path, so nothing here can
        # stand in for a module of MetricFlow's; the runner installs the refusal itself.
        [sys.executable, "-I", str(RUNNER), *args],
        env=mf_env(build),
        capture_output=True,
        text=True,
        check=False,
        cwd=build.cwd,
    )


def mf_query(
    build: Build, metrics: list[str], group_by: list[str] | None = None, where: str | None = None
) -> list[dict[str, str]]:
    """`mf query` of the metrics, one dict per row of its CSV output, values as `mf` writes them."""
    out = build.cwd / "metricflow" / "query.csv"
    args = ["query", "--metrics", ",".join(metrics), "--csv", str(out)]
    if group_by:
        args += ["--group-by", ",".join(group_by), "--order", ",".join(group_by)]
    if where:
        args += ["--where", where]
    result = mf(build, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    with out.open(newline="") as rows:
        return list(csv.DictReader(rows))
