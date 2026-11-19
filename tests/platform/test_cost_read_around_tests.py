"""`make test`, and so `make check`, reads the lab's cost before and after pytest in the lab's own
environment, and runs pytest alone elsewhere.

Each test runs make over a stub `uv`, first on PATH, that logs each call and answers with a chosen
exit code. At its pytest call the stub asks `platform/gcp_project.py`, in pytest's own environment,
whether it sees the lab's project, and logs the answer. What is checked:
- under each of `make test`, `make -j8 check` and `make -j8 -k check`: `sync` runs before the
  decider; in the own environment the cost read, pytest and the cost read again run in that order;
  elsewhere pytest runs alone; a failed read before stops pytest; a failed pytest is still read
  after; a failed read after fails make; each of five decider answers other than exactly `own` or
  `elsewhere` stops make before pytest; a failed pytest elsewhere fails make;
- with each lab variable given on make's command line, in both directions, the cost is read exactly
  when the pytest the stub saw has the lab's project;
- the invented project id is not in make's output, in each of these runs.

No GCP call and no real test suite runs: the stub stands in for every `uv` command, and only
`platform/gcp_project.py`, which reads the environment and calls nothing, runs for real behind it.
`ids` and `portable` run empty guards from a stub rules clone.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROJECT = "example-lab-project"

OWN = {"LEDGER_LAB_GCP_PROJECT": PROJECT}
FOREIGN_OVER_OWN = {"LEDGER_LAB_GCP_PROJECT": PROJECT, "GCP_PROJECT": "another-lab"}
ELSEWHERE = {
    "unset": {},
    "blank": {"LEDGER_LAB_GCP_PROJECT": "  "},
    "foreign": FOREIGN_OVER_OWN,
    "foreign-alone": {"GCP_PROJECT": "another-lab"},
}
INVOCATIONS = [["test"], ["-j8", "check"], ["-j8", "-k", "check"]]
INVOCATION_IDS = ["test", "j8-check", "j8-k-check"]
BRACKETED = ["cost", "pytest:own", "cost"]

# The stub `uv`. It logs `sync` for `uv sync`, `decider`, `cost` and `pytest:<own|elsewhere>` (what
# pytest's environment holds), and nothing for any other call. The decider is the real script unless
# FAKE_DECIDER gives its output; the cost read exits with the next code of FAKE_COST_CODES (0 once
# they run out), pytest with FAKE_PYTEST_CODE.
FAKE_UV = f"""#!{sys.executable}
import os, subprocess, sys
args = sys.argv[1:]
line = " ".join(args)
def log(event):
    with open(os.environ["FAKE_LOG"], "a") as file:
        file.write(event + "\\n")
if args[:1] == ["sync"]:
    log("sync")
elif line == "run --no-sync python platform/gcp_project.py":
    log("decider")
    if "FAKE_DECIDER" in os.environ:
        print(os.environ["FAKE_DECIDER"])
        sys.exit(0)
    sys.exit(subprocess.run([sys.executable, "platform/gcp_project.py"]).returncode)
elif line == "run --no-sync python platform/cost_check.py":
    log("cost")
    counter = os.environ["FAKE_LOG"] + ".cost"
    done = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(done + 1))
    codes = os.environ.get("FAKE_COST_CODES", "").split()
    sys.exit(int(codes[done]) if done < len(codes) else 0)
elif args[:2] == ["run", "pytest"]:
    sys.path.insert(0, "platform")
    from gcp_project import lab_project_or_none
    log("pytest:" + ("own" if lab_project_or_none() is not None else "elsewhere"))
    sys.exit(int(os.environ.get("FAKE_PYTEST_CODE", "0")))
sys.exit(0)
"""


def environment(tmp_path: Path, lab: dict[str, str], **fake: str) -> dict[str, str]:
    """This process's environment without the lab and foreign variables or make's own, plus `lab`
    and a stub `uv` first on PATH."""
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    uv = stubs / "uv"
    uv.write_text(FAKE_UV)
    uv.chmod(0o755)
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("LEDGER_LAB_")
        and k not in ("GCP_PROJECT", "MAKEFLAGS", "MFLAGS", "MAKELEVEL")
    }
    base["PATH"] = f"{stubs}{os.pathsep}{base['PATH']}"
    base["FAKE_LOG"] = str(tmp_path / "uv.log")
    return base | lab | fake


def run(
    tmp_path: Path, lab: dict[str, str], *args: str, **fake: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """make with `args` over the stub; its result and the stub's events, in order."""
    env = environment(tmp_path, lab, **fake)
    guards = tmp_path / "loadout" / "cloud" / "githooks"
    guards.mkdir(parents=True, exist_ok=True)
    (guards / "check-process-ids.py").write_text("")
    (guards / "check-portable.sh").write_text("")
    tool = shutil.which("make")
    assert tool is not None
    result = subprocess.run(
        [tool, "--no-print-directory", f"LOADOUT={tmp_path / 'loadout'}", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env=env,
        timeout=120,
    )
    assert PROJECT not in result.stdout + result.stderr
    log = Path(env["FAKE_LOG"])
    events = log.read_text().splitlines() if log.exists() else []
    assert "decider" in events, result.stdout + result.stderr
    assert "sync" in events[: events.index("decider")]
    return result, [e for e in events if e not in ("sync", "decider")]


@pytest.mark.parametrize("target", INVOCATIONS, ids=INVOCATION_IDS)
def test_in_the_own_environment_the_reads_bracket_pytest(tmp_path: Path, target: list[str]) -> None:
    result, events = run(tmp_path, OWN, *target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events == BRACKETED


@pytest.mark.parametrize("target", INVOCATIONS, ids=INVOCATION_IDS)
@pytest.mark.parametrize("lab", ELSEWHERE.values(), ids=ELSEWHERE.keys())
def test_elsewhere_pytest_runs_alone(
    tmp_path: Path, lab: dict[str, str], target: list[str]
) -> None:
    result, events = run(tmp_path, lab, *target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events == ["pytest:elsewhere"]


@pytest.mark.parametrize("target", INVOCATIONS, ids=INVOCATION_IDS)
def test_elsewhere_a_failed_pytest_fails_make(tmp_path: Path, target: list[str]) -> None:
    result, events = run(tmp_path, {}, *target, FAKE_PYTEST_CODE="1")
    assert result.returncode != 0
    assert events == ["pytest:elsewhere"]


@pytest.mark.parametrize("target", INVOCATIONS, ids=INVOCATION_IDS)
def test_a_failed_read_before_stops_pytest(tmp_path: Path, target: list[str]) -> None:
    result, events = run(tmp_path, OWN, *target, FAKE_COST_CODES="1")
    assert result.returncode != 0
    assert events == ["cost"]


@pytest.mark.parametrize("target", INVOCATIONS, ids=INVOCATION_IDS)
def test_a_failed_pytest_is_still_read_after_and_fails_make(
    tmp_path: Path, target: list[str]
) -> None:
    result, events = run(tmp_path, OWN, *target, FAKE_PYTEST_CODE="1")
    assert result.returncode != 0
    assert events == BRACKETED


@pytest.mark.parametrize("target", INVOCATIONS, ids=INVOCATION_IDS)
@pytest.mark.parametrize("pytest_code", ["0", "1"])
def test_a_failed_read_after_fails_make(
    tmp_path: Path, pytest_code: str, target: list[str]
) -> None:
    result, events = run(
        tmp_path, OWN, *target, FAKE_COST_CODES="0 1", FAKE_PYTEST_CODE=pytest_code
    )
    assert result.returncode != 0
    assert events == BRACKETED


@pytest.mark.parametrize("target", INVOCATIONS, ids=INVOCATION_IDS)
@pytest.mark.parametrize("answer", ["", "maybe", "own maybe", "elsewhere own", " own"])
def test_a_decider_answering_neither_word_stops_make(
    tmp_path: Path, answer: str, target: list[str]
) -> None:
    result, events = run(tmp_path, OWN, *target, FAKE_DECIDER=answer)
    assert result.returncode != 0
    assert "answered neither own nor elsewhere" in result.stderr
    assert events == []


# Each lab variable given on make's command line, over an environment that says the opposite: the
# decider must see what pytest sees.
COMMAND_LINE = {
    "lab-set-from-none": ({}, f"LEDGER_LAB_GCP_PROJECT={PROJECT}", "own"),
    "foreign-emptied": (FOREIGN_OVER_OWN, "GCP_PROJECT=", "own"),
    "foreign-set-over-own": (OWN, "GCP_PROJECT=another-lab", "elsewhere"),
    "lab-emptied-over-own": (OWN, "LEDGER_LAB_GCP_PROJECT=", "elsewhere"),
}


@pytest.mark.parametrize("target", INVOCATIONS, ids=INVOCATION_IDS)
@pytest.mark.parametrize(
    ("lab", "assignment", "seen"), COMMAND_LINE.values(), ids=COMMAND_LINE.keys()
)
def test_a_variable_on_makes_command_line_reaches_the_decider_as_it_reaches_pytest(
    tmp_path: Path, lab: dict[str, str], assignment: str, seen: str, target: list[str]
) -> None:
    result, events = run(tmp_path, lab, assignment, *target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events == (BRACKETED if seen == "own" else ["pytest:elsewhere"])
