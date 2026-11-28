"""The visibility matrix's BigQuery engine (`tests/access/visibility_on_bigquery.py`), offline.

`make bigquery-visibility`'s pytest run, in a child process with no network, no gcloud and no
inherited variable, over a stand-in for the lab's BigQuery (`tests/access/bigquery_stub.py`) that
serves the dbt build of the samples as the catalogues and `platform/gcp` set BigQuery up: the
engine proves exactly the cells the local engine proves over the same build and leaves exactly the
ones it proves on invented tables only; one planted fault per kind of cell turns exactly the cells
it touches red; and the report names no project, account or token. Beside it, the engine's own
pieces: its masking SQL, its typed cells and its refusal of what it cannot read.
"""

import json
import re
import shlex
import subprocess
import sys
from collections import Counter
from itertools import takewhile
from pathlib import Path
from typing import Any

import pytest

from tests.access.test_visibility import (
    PERSONAS,
    Catalogues,
    LocalEngine,
    act_out,
    live_proven,
    verdict,
)
from tests.access.visibility_on_bigquery import masked_sql, typed, where
from tests.conftest import REPO

PROJECT = "the-lab"
Doc = dict[str, Any]


def recipe_pytest_arguments() -> list[str]:
    """The pytest arguments `make bigquery-visibility` runs, read from its one recipe line."""
    lines = (REPO / "Makefile").read_text().splitlines()
    target = lines.index(next(line for line in lines if line.startswith("bigquery-visibility:")))
    (command,) = takewhile(lambda line: line.startswith("\t"), lines[target + 1 :])
    uv, run, *arguments = shlex.split(command)
    assert [uv, run, arguments[0]] == ["uv", "run", "pytest"], command
    return arguments[1:]


@pytest.fixture(scope="module")
def sample_tally(
    access: Doc,
    columns: Doc,
    aggregates: Doc,
    purposes: Doc,
    visibility: Doc,
    warehouse: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict[str, int]]:
    """Each persona's cells the local engine proves over the samples' build, by kind."""
    local = LocalEngine(
        warehouse,
        Catalogues(access, columns, aggregates, purposes),
        tmp_path_factory.mktemp("local"),
    )
    try:
        failures, tally = verdict(act_out(local, visibility, PERSONAS))
    finally:
        local.close()
    assert failures == []
    return {p: dict(sorted(t.items())) for p, t in tally.items()}


def run_over_stub(
    tmp_path: Path, warehouse: Path, proven: dict[str, dict[str, int]], fault: str = ""
) -> tuple[int, str, dict[str, int]]:
    """`make bigquery-visibility`'s pytest run over the stand-in, with the fault planted; its exit
    status, its report and the reads the stand-in served, by kind."""
    bin_dir, config, gcloud_log = tmp_path / "bin", tmp_path / "gcloud-config", tmp_path / "ran"
    bin_dir.mkdir()
    config.mkdir()
    (bin_dir / "gcloud").write_text(
        f'#!/bin/sh\necho "$@" >> {shlex.quote(str(gcloud_log))}\nexit 1\n'
    )
    (bin_dir / "gcloud").chmod(0o755)
    served = tmp_path / "served.json"
    env = {
        "PATH": str(bin_dir),
        "CLOUDSDK_CONFIG": str(config),
        "LEDGER_LAB_GCP_PROJECT": PROJECT,
        "LEDGER_LAB_AGENT_ACCOUNT": "the-agent",
        "STUB_WAREHOUSE": str(warehouse),
        "STUB_PROVEN": json.dumps(proven),
        "STUB_FAULT": fault,
        "STUB_LOG": str(served),
    }
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *recipe_pytest_arguments(),
            "-p",
            "tests.access.bigquery_stub",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        timeout=600,
        check=False,
    )
    report = run.stdout + run.stderr
    assert not gcloud_log.exists(), "the child ran gcloud"
    for word in (PROJECT, "the-agent", "stub-token", "iam.gserviceaccount.com"):
        assert word not in report, word
    return run.returncode, report, json.loads(served.read_text()) if served.exists() else {}


def test_the_bigquery_engine_proves_the_local_engines_cells_and_leaves_its_invented_ones(
    tmp_path: Path, warehouse: Path, sample_tally: dict[str, dict[str, int]]
) -> None:
    code, report, served = run_over_stub(tmp_path, warehouse, sample_tally)
    assert code == pytest.ExitCode.OK, report[-3000:]
    assert "1 passed" in report, report[-3000:]
    proven = sum(n for t in sample_tally.values() for k, n in t.items() if "invented" not in k)
    left = sum(n for t in sample_tally.values() for k, n in t.items() if "invented" in k)
    assert f"{proven} cells proven on BigQuery, {left} left to the local engine:" in report
    for persona in PERSONAS:
        assert f"  {persona}: " in report, persona
    # Each of the engine's ways of reading a large table was taken, and no read billed past the
    # stand-in's cap.
    assert served.get("over the cap") and served.get("distinct in parts"), served
    assert served.get("aggregation threshold") and served.get("metadata reads"), served


# Each fault, and the cells of the one persona it touches, which alone go red.
FAULTS = {
    "leak": ("accountant-chicago", {"payments uk-transport"}),
    "unmasked": ("accountant-chicago", {"tag individual-id"}),
    "stray-view": ("group-controller", {"tables"}),
    "changed": ("payables-clerk", {"payments chicago"}),
    "no-threshold": ("metric-owner", {"payments chicago", "payments uk-transport"}),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_a_persona_seeing_a_cell_it_should_not_turns_exactly_that_cell_red(
    tmp_path: Path, warehouse: Path, sample_tally: dict[str, dict[str, int]], fault: str
) -> None:
    code, report, _ = run_over_stub(tmp_path, warehouse, sample_tally, fault)
    assert code == pytest.ExitCode.TESTS_FAILED, report[-3000:]
    (line,) = [line for line in report.splitlines() if "observations unlike the matrix: " in line]
    failures = line.split("observations unlike the matrix: ", 1)[1].split("; ")
    persona, places = FAULTS[fault]
    found = {re.sub(r": expected .*", "", f) for f in failures}
    assert found == {f"{persona} {p}" for p in places}, failures
    assert all(": expected " in f and ", observed " in f for f in failures), failures


def test_the_live_tally_is_the_landed_builds_with_the_invented_cells_left() -> None:
    proven = live_proven()
    assert set(proven) == set(PERSONAS)
    counted = Counter[str]()
    for kinds in proven.values():
        counted.update(kinds)
    assert sum(n for k, n in counted.items() if k.endswith("left to the local engine")) == 81
    assert sum(n for k, n in counted.items() if not k.endswith("left to the local engine")) == 39


def test_the_masking_sql_is_the_python_rules() -> None:
    """The same SQL the engine holds to `test_visibility.mask` live, here only its text."""
    assert masked_sql("hash", "c") == "TO_BASE64(SHA256(c))"
    assert masked_sql("nullify", "c") == "CAST(NULL AS STRING)"
    assert masked_sql("last-four", "c") == (
        "CASE WHEN CHAR_LENGTH(c) > 4 THEN CONCAT('XXXXX', SUBSTR(c, -4))"
        " ELSE TO_BASE64(SHA256(c)) END"
    )


def test_a_cell_is_typed_by_its_answers_schema() -> None:
    from datetime import date
    from decimal import Decimal

    assert typed("INTEGER", "12") == 12
    assert typed("NUMERIC", "1.50") == Decimal("1.5")
    assert typed("BOOLEAN", "false") is False
    assert typed("DATE", "2024-01-31") == date(2024, 1, 31)
    assert typed("STRING", "12") == "12"
    assert typed("INTEGER", None) is None


def test_an_entity_filter_outside_its_shape_is_refused() -> None:
    from access.bigquery import PersonaQueryError

    assert where({"uk-transport", "chicago"}) == " WHERE entity_id IN ('chicago', 'uk-transport')"
    assert where(set()) == " WHERE entity_id IN (NULL)"
    assert where(None) == ""
    with pytest.raises(PersonaQueryError):
        where({"chi'cago"})


def test_the_live_test_carries_its_marker_and_the_target_runs_it_alone() -> None:
    arguments = recipe_pytest_arguments()
    assert arguments[:2] == ["-m", "bigquery_persona"]
    assert arguments[-1] == "tests/access/test_visibility.py::test_every_persona_sees_its_matrix"
    from tests.access import test_visibility

    params = test_visibility.engine._fixture_function_marker.params or ()
    (bigquery,) = [p for p in params if getattr(p, "values", None) == ("bigquery",)]
    assert "bigquery_persona" in {m.name for m in getattr(bigquery, "marks", ())}
