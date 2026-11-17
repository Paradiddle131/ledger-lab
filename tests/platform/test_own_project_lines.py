"""The own-project conditions written in the agent rules, read and held to `gcp_project`.

The oracle is `platform/gcp_project.py`: a case is the lab's own environment exactly when
`lab_project_or_none(env)` answers a project. The cases are the lab variable missing, empty, a
space, a tab, a newline or a project id, each beside `GCP_PROJECT` missing, empty, a space or
another lab's id.

Checked, by those cases only:
- the backticked `[ ... ]` check on the known-condition line of `docs/agents/autopilot.md` that
  names the GCP credential has the shape SHELL_CHECK allows (`[ -n|-z "<expansion>" ]` tests
  joined by `&&`, each expansion one of the two variables, bare, with an empty `:-` default or
  with whitespace removed by `//[[:space:]]/`), and its exit status under `bash --noprofile
  --norc -c`, in an environment holding only `PATH` and the case's variables, is 0 exactly on the
  own-environment cases;
- the condition after the first `while` up to the next comma, on the Queue rule's line and on the
  Never line, read with the closed vocabulary of WORDS (clauses joined by `or`), is true exactly
  on the cases that are not the own environment; a clause outside the vocabulary fails;
- `AGENTS.md` has, under `## Guardrails`, a line starting `- **Own project only:**` holding the
  phrase OWN_ENVIRONMENT.

Not checked:
- any other wording on those lines, or the rest of the guardrail line's prose;
- whitespace beyond the ASCII cases above, where Python's `str.strip` and bash's `[[:space:]]`
  may differ;
- that the shell check is run, or run as the line says, by any agent or hook.
"""

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from gcp_project import FOREIGN, VARIABLE, lab_project_or_none

REPO = Path(__file__).resolve().parents[2]
AUTOPILOT = REPO / "docs" / "agents" / "autopilot.md"
AGENTS = REPO / "AGENTS.md"
BASH = shutil.which("bash") or "bash"

QUEUE_ANCHOR = "**An item that turns out to need GCP or the owner:**"
NEVER_ANCHOR = "- Call GCP outside this lab's own environment"
OWN_ENVIRONMENT = f"configures to set `{VARIABLE}` and no `{FOREIGN}`"

_EXPANSION = rf'"\$\{{(?:{VARIABLE}|{FOREIGN})(?::-|//\[\[:space:\]\]/)?\}}"'
_TEST = rf"\[ -[nz] {_EXPANSION} \]"
SHELL_CHECK = re.compile(rf"{_TEST}(?: && {_TEST})*")

Env = Mapping[str, str]
WORDS: dict[tuple[str, str], Callable[[Env], bool]] = {
    (VARIABLE, "unset"): lambda env: VARIABLE not in env,
    (VARIABLE, "empty"): lambda env: VARIABLE in env and not env[VARIABLE].strip(),
    (VARIABLE, "set"): lambda env: bool(env.get(VARIABLE, "").strip()),
    (FOREIGN, "set"): lambda env: bool(env.get(FOREIGN, "")),
    (FOREIGN, "unset"): lambda env: not env.get(FOREIGN, ""),
    (FOREIGN, "empty"): lambda env: env.get(FOREIGN) == "",
}
_CLAUSE = re.compile(rf"(?:(?:a )?`({VARIABLE}|{FOREIGN})` is )?(unset|empty|set)")

LAB_VALUES: tuple[str | None, ...] = (None, "", " ", "\t", "\n", "the-lab")
FOREIGN_VALUES: tuple[str | None, ...] = (None, "", " ", "another-lab")


def case(lab: str | None, foreign: str | None) -> dict[str, str]:
    """The environment holding each variable whose value is not None (None: the variable is
    missing)."""
    env: dict[str, str] = {}
    if lab is not None:
        env[VARIABLE] = lab
    if foreign is not None:
        env[FOREIGN] = foreign
    return env


CASES = [case(lab, foreign) for lab in LAB_VALUES for foreign in FOREIGN_VALUES]


def own(env: Env) -> bool:
    return lab_project_or_none(env) is not None


def section(path: Path, heading: str) -> list[str]:
    """The lines under `heading` up to the next heading of the same level."""
    lines = path.read_text().splitlines()
    assert heading in lines, f"{path.relative_to(REPO)} has no `{heading}`"
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start:end]


def line_starting(lines: list[str], anchor: str, where: str) -> str:
    found = [line for line in lines if line.startswith(anchor)]
    assert len(found) == 1, f"{where}: expected one line starting {anchor!r}, found {len(found)}"
    return found[0]


def known_condition_check() -> str:
    lines = section(AUTOPILOT, "## Known conditions")
    named = [line for line in lines if "GCP credential" in line]
    assert len(named) == 1, f"expected one known condition naming the GCP credential: {named}"
    checks = [span for span in re.findall(r"`([^`]*)`", named[0]) if span.startswith("[")]
    assert len(checks) == 1, f"expected one backticked `[ ... ]` check: {named[0]}"
    return checks[0]


def condition(line: str) -> Callable[[Env], bool]:
    """The clause after the line's first `while`, up to the next comma, read with WORDS."""
    found = re.search(r"\bwhile ([^,]*),", line)
    assert found, f"no `while ...,` condition on: {line}"
    predicates: list[Callable[[Env], bool]] = []
    subject: str | None = None
    for clause in found.group(1).split(" or "):
        read = _CLAUSE.fullmatch(clause)
        assert read, f"clause {clause!r} is outside the vocabulary, on: {line}"
        subject = read.group(1) or subject
        assert subject, f"clause {clause!r} names no variable, on: {line}"
        predicates.append(WORDS[(subject, read.group(2))])
    return lambda env: any(predicate(env) for predicate in predicates)


def queue_line() -> str:
    lines = AUTOPILOT.read_text().splitlines()
    return line_starting(lines, QUEUE_ANCHOR, "docs/agents/autopilot.md")


def never_line() -> str:
    return line_starting(section(AUTOPILOT, "## Never"), NEVER_ANCHOR, "autopilot.md, ## Never")


def test_the_cases_hold_own_and_foreign_environments() -> None:
    """The oracle answers both ways over the cases, so the comparisons below can fail."""
    assert any(own(env) for env in CASES)
    assert not all(own(env) for env in CASES)


def test_the_known_condition_check_has_the_allowed_shape() -> None:
    check = known_condition_check()
    assert SHELL_CHECK.fullmatch(check), f"the known condition's check is not allowed: {check!r}"


@pytest.mark.parametrize("env", CASES, ids=repr)
def test_the_known_condition_check_passes_only_in_the_own_environment(env: dict[str, str]) -> None:
    check = known_condition_check()
    assert SHELL_CHECK.fullmatch(check), f"refusing to run a check of another shape: {check!r}"
    ran = subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", check],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode in (0, 1), ran.stderr
    assert (ran.returncode == 0) == own(env)


@pytest.mark.parametrize("line", [queue_line, never_line], ids=["queue rule", "never line"])
@pytest.mark.parametrize("env", CASES, ids=repr)
def test_the_written_condition_is_the_foreign_environment(
    line: Callable[[], str], env: dict[str, str]
) -> None:
    written = line()
    assert condition(written)(env) == (not own(env)), f"disagrees with gcp_project on: {written}"


def test_the_guardrail_names_the_own_environment() -> None:
    guardrail = line_starting(
        section(AGENTS, "## Guardrails"), "- **Own project only:**", "AGENTS.md, ## Guardrails"
    )
    assert OWN_ENVIRONMENT in guardrail
