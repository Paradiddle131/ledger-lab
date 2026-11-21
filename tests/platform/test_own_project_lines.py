"""The own-project conditions written in the agent rules, read and held to `gcp_project`.

The oracle is `platform/gcp_project.py`: a case is the lab's own environment exactly when
`lab_project_or_none(env)` answers a project. The cases are the lab variable missing, empty, a
space, a tab, a newline or a project id, each beside `GCP_PROJECT` missing, empty, a space or
another lab's id.

Checked, by those cases only:
- the known-condition line of `docs/agents/autopilot.md` that names the GCP credential holds one
  backticked `[ ... ]` check, followed by `` fails there``, and holds KNOWN_INSTRUCTION; the check
  has the shape SHELL_CHECK allows (`[ -n|-z "<expansion>" ]` tests joined by `&&`, each expansion
  one of the two variables, bare, with an empty `:-` default or with whitespace removed by
  `//[[:space:]]/`), and its exit status under `bash --noprofile --norc -c`, in an environment
  holding only `PATH` and the case's variables, is 0 exactly on the own-environment cases;
- the Queue rule's line holds QUEUE_BEFORE once, and the condition right after it, up to the next
  comma, read with the closed vocabulary of WORDS (clauses joined by `or`), is true exactly on the
  cases that are not the own environment;
- the Never line starts with NEVER_BEFORE and ends with NEVER_INSTRUCTION, and the condition right
  after NEVER_BEFORE, up to NEVER_AFTER, read the same way, is true exactly on those cases;
- WORDS reads each word as it says, for either variable: `unset` missing, `set` present (an
  empty value included), `empty` present and exactly empty, `non-empty` present and not empty (a
  space included), `blank` present and empty or only whitespace (as `str.strip` sees it); the
  earlier wording, EARLIER_WORDING, read with them, differs from the oracle on some case;
- the reader refuses the clauses in REFUSED_CLAUSES, and SHELL_CHECK refuses the checks in
  REFUSED_CHECKS; the run helper refuses one of them before bash starts, leaving no file behind;
- `AGENTS.md` has, under `## Guardrails`, a line starting `- **Own project only:**` holding each
  phrase of GUARDRAIL_PHRASES.

Not checked:
- the wording on those lines outside the pinned phrases and the extracted condition, and the rest
  of the guardrail line's prose;
- the profile's paragraph that defines the lab's own environment, and the one that gives a
  `needs-gcp` item back;
- whitespace beyond the ASCII cases above, where Python's `str.strip` and bash's `[[:space:]]`
  may differ;
- that the shell check is run, or run as the line says, by an agent or hook.
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
QUEUE_BEFORE = "swap `ready` and `agent:claimed` for `needs-gcp` (the step calls GCP while "
NEVER_BEFORE = "- Call GCP outside this lab's own environment: while "
NEVER_AFTER = ", where"
NEVER_INSTRUCTION = "label the item `needs-gcp` instead."
KNOWN_INSTRUCTION = "make no GCP call and label the item `needs-gcp`"
GUARDRAIL_PHRASES = (
    f"configures to set `{VARIABLE}` and no `{FOREIGN}`",
    "is made only in this lab's own environment",
    "no GCP call is made",
)

_EXPANSION = rf'"\$\{{(?:{VARIABLE}|{FOREIGN})(?::-|//\[\[:space:\]\]/)?\}}"'
_TEST = rf"\[ -[nz] {_EXPANSION} \]"
SHELL_CHECK = re.compile(rf"{_TEST}(?: && {_TEST})*")

_LAB = f'[ -n "${{{VARIABLE}//[[:space:]]/}}" ]'
_NO_FOREIGN = f'[ -z "${{{FOREIGN}:-}}" ]'
REFUSED_CHECKS = {
    "semicolon": f"{_LAB} && {_NO_FOREIGN}; true",
    "and command": f"{_LAB} && {_NO_FOREIGN} && true",
    "or command": f"{_LAB} || true",
    "command substitution": f'{_LAB} && [ -z "${{{FOREIGN}:-}}$(true)" ]',
    "backtick": f'{_LAB} && [ -z "`true`" ]',
    "another variable": f'{_LAB} && [ -z "${{HOME:-}}" ]',
    "newline and command": f"{_LAB} && {_NO_FOREIGN}\ntrue",
}

Env = Mapping[str, str]
WORDS: dict[str, Callable[[Env, str], bool]] = {
    "unset": lambda env, name: name not in env,
    "set": lambda env, name: name in env,
    "empty": lambda env, name: env.get(name) == "",
    "non-empty": lambda env, name: bool(env.get(name, "")),
    "blank": lambda env, name: name in env and not env[name].strip(),
}
_CLAUSE = re.compile(rf"(?:(?:a )?`({VARIABLE}|{FOREIGN})` is )?({'|'.join(WORDS)})")
REFUSED_CLAUSES = {
    "not set": f"`{VARIABLE}` is not set",
    "whitespace": f"`{VARIABLE}` is whitespace",
    "and": f"`{VARIABLE}` is unset and a `{FOREIGN}` is set",
    "mode": "the Digest says `Mode: offline`",
}

EARLIER_WORDING = f"`{VARIABLE}` is unset or empty or a `{FOREIGN}` is set"

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


def between(line: str, before: str, after: str) -> str:
    """The text right after the one `before` on the line, up to the first `after` past it."""
    assert line.count(before) == 1, f"expected {before!r} once, on: {line}"
    rest = line.split(before, 1)[1]
    assert after in rest, f"expected {after!r} after {before!r}, on: {line}"
    return rest.split(after, 1)[0]


def known_condition_check() -> str:
    lines = section(AUTOPILOT, "## Known conditions")
    named = [line for line in lines if "GCP credential" in line]
    assert len(named) == 1, f"expected one known condition naming the GCP credential: {named}"
    line = named[0]
    checks = [span for span in re.findall(r"`([^`]*)`", line) if span.startswith("[")]
    assert len(checks) == 1, f"expected one backticked `[ ... ]` check: {line}"
    assert f"`{checks[0]}` fails there" in line, (
        f"the check is not followed by `fails there`: {line}"
    )
    assert KNOWN_INSTRUCTION in line, f"expected {KNOWN_INSTRUCTION!r}, on: {line}"
    return checks[0]


def run_check(check: str, env: Env) -> subprocess.CompletedProcess[str]:
    """Runs `check` in bash with only PATH and `env`, after refusing a check SHELL_CHECK does not
    allow."""
    assert SHELL_CHECK.fullmatch(check), f"refusing to run a check of another shape: {check!r}"
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", check],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def read_condition(text: str, where: str) -> Callable[[Env], bool]:
    """`text`, clauses joined by ` or `, read with WORDS; a clause outside them fails."""
    predicates: list[Callable[[Env], bool]] = []
    subject: str | None = None
    for clause in text.split(" or "):
        read = _CLAUSE.fullmatch(clause)
        assert read, f"clause {clause!r} is outside the vocabulary, on: {where}"
        subject = read.group(1) or subject
        assert subject, f"clause {clause!r} names no variable, on: {where}"
        word, name = WORDS[read.group(2)], subject
        predicates.append(lambda env, word=word, name=name: word(env, name))
    return lambda env: any(predicate(env) for predicate in predicates)


def queue_condition() -> Callable[[Env], bool]:
    line = line_starting(AUTOPILOT.read_text().splitlines(), QUEUE_ANCHOR, "autopilot.md")
    return read_condition(between(line, QUEUE_BEFORE, ","), line)


def never_condition() -> Callable[[Env], bool]:
    line = line_starting(section(AUTOPILOT, "## Never"), NEVER_BEFORE, "autopilot.md, ## Never")
    assert line.endswith(NEVER_INSTRUCTION), f"expected the end {NEVER_INSTRUCTION!r}, on: {line}"
    return read_condition(between(line, NEVER_BEFORE, NEVER_AFTER), line)


def test_the_cases_hold_own_and_foreign_environments() -> None:
    """The oracle answers both ways over the cases, so the comparisons below can fail."""
    assert any(own(env) for env in CASES)
    assert not all(own(env) for env in CASES)


def test_the_known_condition_check_has_the_allowed_shape() -> None:
    check = known_condition_check()
    assert SHELL_CHECK.fullmatch(check), f"the known condition's check is not allowed: {check!r}"


@pytest.mark.parametrize("check", REFUSED_CHECKS.values(), ids=REFUSED_CHECKS.keys())
def test_the_allowed_shape_refuses_more_than_the_tests(check: str) -> None:
    assert not SHELL_CHECK.fullmatch(check)


def test_a_check_of_another_shape_is_refused_before_bash_runs(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    with pytest.raises(AssertionError, match="refusing to run"):
        run_check(f"{_LAB} && touch {marker}", {VARIABLE: "the-lab"})
    assert not marker.exists()


@pytest.mark.parametrize("env", CASES, ids=repr)
def test_the_known_condition_check_passes_only_in_the_own_environment(env: dict[str, str]) -> None:
    ran = run_check(known_condition_check(), env)
    assert ran.returncode in (0, 1), ran.stderr
    assert (ran.returncode == 0) == own(env)


@pytest.mark.parametrize(
    "condition", [queue_condition, never_condition], ids=["queue rule", "never line"]
)
@pytest.mark.parametrize("env", CASES, ids=repr)
def test_the_written_condition_is_the_foreign_environment(
    condition: Callable[[], Callable[[Env], bool]], env: dict[str, str]
) -> None:
    assert condition()(env) == (not own(env))


@pytest.mark.parametrize("text", REFUSED_CLAUSES.values(), ids=REFUSED_CLAUSES.keys())
def test_the_reader_refuses_a_clause_outside_the_vocabulary(text: str) -> None:
    with pytest.raises(AssertionError, match="outside the vocabulary"):
        read_condition(text, "a probe")


def test_the_guardrail_names_the_own_environment() -> None:
    guardrail = line_starting(
        section(AGENTS, "## Guardrails"), "- **Own project only:**", "AGENTS.md, ## Guardrails"
    )
    missing = [phrase for phrase in GUARDRAIL_PHRASES if phrase not in guardrail]
    assert missing == [], f"the own-project guardrail lacks {missing}"


def test_the_words_tell_the_earlier_wording_apart() -> None:
    """The earlier lines called a blank lab variable `empty` and an empty `GCP_PROJECT` `set`;
    read word for word, that wording misjudges some case, so the reader cannot pass it."""
    earlier = read_condition(EARLIER_WORDING, "the earlier wording")
    assert any(earlier(env) != (not own(env)) for env in CASES)
