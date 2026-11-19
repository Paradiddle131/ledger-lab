"""`make test`, and so `make check`, reads the lab's cost before and after pytest in the lab's own
environment, and runs pytest alone elsewhere.

What is checked here:
- the commands `make -n check` prints, in an own, an unset, a blank and a foreign environment;
- what `make test` and `make -j check` do when run, over a stub `uv` first on PATH that logs each
  call and answers with a chosen exit code: the order of the cost reads and pytest, a failed read
  before (pytest does not run), a failed pytest (the read after still runs, make fails), a failed
  read after (make fails), and a decider that answers neither word (make stops).

No GCP call and no real test suite runs: `make -n` runs no recipe line (the Makefile has no
`$(MAKE)` line, which `-n` would run), and in the runs the stub stands in for every `uv` command;
only `platform/gcp_project.py`, which reads the environment and calls nothing, runs for real behind
it. The project id given is invented. `ids` and `portable` run stub guards from a stub rules clone.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COST = "uv run --no-sync python platform/cost_check.py"
PROJECT = "example-lab-project"

OWN = {"LEDGER_LAB_GCP_PROJECT": PROJECT}
ELSEWHERE = {
    "unset": {},
    "blank": {"LEDGER_LAB_GCP_PROJECT": "  "},
    "foreign": {"LEDGER_LAB_GCP_PROJECT": PROJECT, "GCP_PROJECT": "another-lab"},
    "foreign-alone": {"GCP_PROJECT": "another-lab"},
}

# The stub `uv`: logs its arguments; runs the real decider; answers the cost check with the next
# code of FAKE_COST_CODES (one per call, 0 once they run out) and pytest with FAKE_PYTEST_CODE.
FAKE_UV = f"""#!{sys.executable}
import os, subprocess, sys
args = sys.argv[1:]
line = " ".join(args)
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(line + "\\n")
if line == "run --no-sync python platform/gcp_project.py":
    if os.environ.get("FAKE_DECIDER"):
        print(os.environ["FAKE_DECIDER"])
        sys.exit(0)
    sys.exit(subprocess.run([sys.executable, "platform/gcp_project.py"]).returncode)
if line == "run --no-sync python platform/cost_check.py":
    counter = os.environ["FAKE_LOG"] + ".cost"
    done = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(done + 1))
    codes = os.environ.get("FAKE_COST_CODES", "").split()
    sys.exit(int(codes[done]) if done < len(codes) else 0)
if args[:2] == ["run", "pytest"]:
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


def stub_loadout(tmp_path: Path) -> Path:
    guards = tmp_path / "loadout" / "cloud" / "githooks"
    guards.mkdir(parents=True, exist_ok=True)
    (guards / "check-process-ids.py").write_text("")
    (guards / "check-portable.sh").write_text("")
    return tmp_path / "loadout"


def make(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    tool = shutil.which("make")
    assert tool is not None
    return subprocess.run(
        [tool, "--no-print-directory", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env=env,
        timeout=120,
    )


def calls(env: dict[str, str]) -> list[str]:
    """The cost reads and pytest runs the stub saw, in order."""
    log = Path(env["FAKE_LOG"])
    seen = log.read_text().splitlines() if log.exists() else []
    named = {"run --no-sync python platform/cost_check.py": "cost", "run pytest": "pytest"}
    return [named[line] for line in seen if line in named]


# The printed commands.


def pytest_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if "uv run pytest" in line]


def test_in_the_own_environment_the_cost_is_read_before_and_after_pytest(tmp_path: Path) -> None:
    result = make(environment(tmp_path, OWN), "-n", "check")
    assert result.returncode == 0, result.stderr
    (line,) = pytest_lines(result.stdout)
    before, rest = line.split("uv run pytest", 1)
    assert before.startswith(f"{COST} || exit 1; ")
    assert rest.startswith("; tests=$?; ")
    assert rest.endswith(f"{COST} || exit 1; exit $tests")
    assert result.stdout.count(COST) == 2


@pytest.mark.parametrize("lab", ELSEWHERE.values(), ids=ELSEWHERE.keys())
def test_elsewhere_check_runs_pytest_alone_and_reads_no_cost(
    tmp_path: Path, lab: dict[str, str]
) -> None:
    result = make(environment(tmp_path, lab), "-n", "check")
    assert result.returncode == 0, result.stderr
    (line,) = pytest_lines(result.stdout)
    assert line.startswith("LOADOUT='") and line.endswith("' uv run pytest")
    assert "cost_check" not in result.stdout


def test_the_project_id_is_not_printed(tmp_path: Path) -> None:
    result = make(environment(tmp_path, OWN), "-n", "check")
    assert PROJECT not in result.stdout + result.stderr


# What runs.


@pytest.mark.parametrize("target", [["test"], ["-j8", "check"], ["-j8", "-k", "check"]])
def test_in_the_own_environment_the_reads_bracket_pytest_when_run(
    tmp_path: Path, target: list[str]
) -> None:
    env = environment(tmp_path, OWN)
    result = make(env, f"LOADOUT={stub_loadout(tmp_path)}", *target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls(env) == ["cost", "pytest", "cost"]


@pytest.mark.parametrize("lab", ELSEWHERE.values(), ids=ELSEWHERE.keys())
def test_elsewhere_only_pytest_runs(tmp_path: Path, lab: dict[str, str]) -> None:
    env = environment(tmp_path, lab)
    result = make(env, f"LOADOUT={stub_loadout(tmp_path)}", "-j8", "check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls(env) == ["pytest"]


@pytest.mark.parametrize("target", [["test"], ["-j8", "-k", "check"]])
def test_a_failed_read_before_stops_pytest(tmp_path: Path, target: list[str]) -> None:
    env = environment(tmp_path, OWN, FAKE_COST_CODES="1")
    result = make(env, f"LOADOUT={stub_loadout(tmp_path)}", *target)
    assert result.returncode != 0
    assert calls(env) == ["cost"]


@pytest.mark.parametrize("target", [["test"], ["-j8", "-k", "check"]])
def test_a_failed_pytest_is_still_read_after_and_fails_make(
    tmp_path: Path, target: list[str]
) -> None:
    env = environment(tmp_path, OWN, FAKE_PYTEST_CODE="1")
    result = make(env, f"LOADOUT={stub_loadout(tmp_path)}", *target)
    assert result.returncode != 0
    assert calls(env) == ["cost", "pytest", "cost"]


@pytest.mark.parametrize("pytest_code", ["0", "1"])
def test_a_failed_read_after_fails_make(tmp_path: Path, pytest_code: str) -> None:
    env = environment(tmp_path, OWN, FAKE_COST_CODES="0 1", FAKE_PYTEST_CODE=pytest_code)
    result = make(env, f"LOADOUT={stub_loadout(tmp_path)}", "test")
    assert result.returncode != 0
    assert calls(env) == ["cost", "pytest", "cost"]


@pytest.mark.parametrize("answer", ["", "maybe"])
def test_a_decider_answering_neither_word_stops_make(tmp_path: Path, answer: str) -> None:
    env = environment(tmp_path, OWN, FAKE_DECIDER=answer or " ")
    result = make(env, f"LOADOUT={stub_loadout(tmp_path)}", "test")
    assert result.returncode != 0
    assert "answered neither own nor elsewhere" in result.stderr
    assert calls(env) == []
