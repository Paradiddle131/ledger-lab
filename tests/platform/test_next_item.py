"""The queue rule of `docs/agents/autopilot.md`, run by `platform/next_item.py` on fixture trackers.

Every tracker here is invented, offline: issue numbers in the nine hundreds, a fake `gh api` for
the live snapshot. Checked: the slice item first, in path order over epic order; the leftovers
issue of one area as one item, only when no slice item is pickable, before every other item; then
epic order; each skip label; live, expired and handed-off claims as the skill's `claim.sh` reads
them; open blockers from the body's section and from the relation, apart; an open PR that closes
the item; epics and the slice issue never picked; the Owner lane stopping the pick; the profile's
Queue section naming the command and the labels this module reads; the live snapshot paged by
hand through `gh api` REST calls.
"""

import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from next_item import (
    COMMAND,
    LEFTOVER,
    RULE_EPIC,
    RULE_LEFTOVER,
    RULE_NAMED,
    RULE_SLICE,
    SKIP_LABELS,
    SLICE,
    Comment,
    Issue,
    PullRequest,
    Tracker,
    TrackerError,
    blocked_by_section,
    build_tracker,
    closes,
    gh_get,
    main,
    origin_repo,
    pick,
    slice_path,
)

REPO = Path(__file__).resolve().parents[2]
PROFILE = REPO / "docs" / "agents" / "autopilot.md"
REPO_NAME = "example-owner/example-repo"
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)

ROADMAP = 900
GROUND_RULES = 901  # the first epic
ACCESS = 903
AGENT = 905
SLICE_ISSUE = 909

LEFTOVERS = 911  # `Review leftovers: platform`, under the first epic
GROUND_ITEM = 912  # an ordinary ready item under the first epic
ACCESS_STEP = 931  # a slice step under the access epic
AGENT_STEP = 951  # a slice step under the agent epic
AGENT_STEP_LATER = 952
OTHER = 990  # an issue outside every epic
CLOSED_STEP = 999  # a path step already closed: an empty path is written as this one alone


def ref(number: int) -> str:
    return "#" + str(number)


def path_body(numbers: Iterable[int]) -> str:
    rows = "\n".join(
        f"| {step} | {ref(n)} A step | a control | none |" for step, n in enumerate(numbers, 1)
    )
    return (
        "One question end to end.\n\n<details>\n"
        "<summary>The path, in dependency order</summary>\n\n"
        "| # | Step | Control it proves | Depends on |\n|---|---|---|---|\n"
        f"{rows}\n\nNotes:\n- a note naming {ref(OTHER)}\n</details>\n"
    )


def issue(number: int, *labels: str, title: str = "", body: str = "", **rest: Any) -> Issue:
    return Issue(number, title or f"Item {number}", labels=frozenset(labels), body=body, **rest)


def claimed(hours_ago: float, association: str = "OWNER", word: str = "claimed") -> Comment:
    at = NOW - timedelta(hours=hours_ago)
    return Comment(
        f"🤖 {word} {at:%Y-%m-%dT%H:%MZ} 1a2b3c4d https://example.invalid", at, association
    )


def handoff(hours_ago: float, association: str = "OWNER") -> Comment:
    at = NOW - timedelta(hours=hours_ago)
    return Comment(f"🤖 Handoff — {at:%Y-%m-%dT%H:%MZ} — **parked**", at, association)


def tracker(
    *items: Issue,
    path: Iterable[int] = (AGENT_STEP,),
    under: Mapping[int, Iterable[int]] | None = None,
    pulls: Iterable[PullRequest] = (),
    lane: str | None = None,
) -> Tracker:
    """A Roadmap of the three epics and the slice issue; `under` places items under epics (by
    default each fixture item under its usual epic)."""
    placed = {
        GROUND_RULES: [LEFTOVERS, GROUND_ITEM],
        ACCESS: [ACCESS_STEP],
        AGENT: [AGENT_STEP, AGENT_STEP_LATER],
        SLICE_ISSUE: [],
    }
    if under is not None:
        placed |= {epic: list(numbers) for epic, numbers in under.items()}
    issues = {
        ROADMAP: issue(ROADMAP, title="Roadmap"),
        GROUND_RULES: issue(GROUND_RULES, "platform", "epic", title="Ground rules"),
        ACCESS: issue(ACCESS, "access", "epic", title="Access"),
        AGENT: issue(AGENT, "agent", "epic", title="Agent"),
        SLICE_ISSUE: issue(
            SLICE_ISSUE,
            "agent",
            "epic",
            title="Thin slice: one question",
            body=path_body(tuple(path) or (CLOSED_STEP,)),
        ),
    }
    issues |= {item.number: item for item in items}
    epics = (GROUND_RULES, ACCESS, AGENT, SLICE_ISSUE)
    sub_issues = {epic: tuple(n for n in placed.get(epic, ()) if n in issues) for epic in epics}
    return Tracker(REPO_NAME, epics, sub_issues, issues, tuple(pulls), lane)


LEFTOVERS_ISSUE = issue(
    LEFTOVERS,
    "platform",
    LEFTOVER,
    "ready",
    title="Review leftovers: platform",
    body="- [ ] a finding\n- [ ] another finding\n",
)
GROUND = issue(GROUND_ITEM, "platform", "ready")
ACCESS_SLICE = issue(ACCESS_STEP, "access", SLICE, "ready")
AGENT_SLICE = issue(AGENT_STEP, "agent", SLICE, "ready")
AGENT_SLICE_LATER = issue(AGENT_STEP_LATER, "agent", SLICE, "ready")


def test_a_ready_slice_item_comes_before_a_ready_leftover_in_the_first_epic() -> None:
    chosen = pick(tracker(LEFTOVERS_ISSUE, GROUND, AGENT_SLICE), NOW)
    assert (chosen.number, chosen.rule) == (AGENT_STEP, RULE_SLICE)


def test_with_no_slice_item_pickable_the_leftovers_of_one_area_are_one_item() -> None:
    held = replace(AGENT_SLICE, labels=AGENT_SLICE.labels | {"needs-owner"})
    chosen = pick(tracker(LEFTOVERS_ISSUE, GROUND, held), NOW)
    assert (chosen.number, chosen.rule) == (LEFTOVERS, RULE_LEFTOVER)
    assert dict(chosen.skipped)[AGENT_STEP] == "labelled `needs-owner`"


def test_leftovers_come_before_other_items_even_from_a_later_epic() -> None:
    """The ordinary item sits first in the first epic; the leftovers issue in a later one."""
    leftovers = replace(LEFTOVERS_ISSUE, labels=frozenset({"agent", LEFTOVER, "ready"}))
    placed = {GROUND_RULES: [GROUND_ITEM], AGENT: [LEFTOVERS]}
    chosen = pick(tracker(leftovers, GROUND, path=(), under=placed), NOW)
    assert (chosen.number, chosen.rule) == (LEFTOVERS, RULE_LEFTOVER)


def test_leftovers_go_in_epic_order() -> None:
    later = issue(LEFTOVERS + 50, "agent", LEFTOVER, "ready", title="Review leftovers: agent")
    placed = {GROUND_RULES: [LEFTOVERS], AGENT: [later.number]}
    chosen = pick(tracker(later, LEFTOVERS_ISSUE, path=(), under=placed), NOW)
    assert chosen.number == LEFTOVERS


def test_with_nothing_on_the_path_or_left_over_the_first_sub_issue_in_epic_order() -> None:
    chosen = pick(tracker(GROUND, ACCESS_SLICE, path=()), NOW)
    assert (chosen.number, chosen.rule) == (GROUND_ITEM, RULE_EPIC)


def test_path_order_wins_over_epic_order() -> None:
    """The access epic comes first, but the path lists the agent step first."""
    chosen = pick(tracker(ACCESS_SLICE, AGENT_SLICE, path=(AGENT_STEP, ACCESS_STEP)), NOW)
    assert chosen.number == AGENT_STEP
    chosen = pick(tracker(ACCESS_SLICE, AGENT_SLICE, path=(ACCESS_STEP, AGENT_STEP)), NOW)
    assert chosen.number == ACCESS_STEP


def test_a_later_path_step_is_taken_when_an_earlier_one_is_not_pickable() -> None:
    first = replace(AGENT_SLICE, labels=frozenset({"agent", SLICE}))  # not ready
    chosen = pick(tracker(first, AGENT_SLICE_LATER, path=(AGENT_STEP, AGENT_STEP_LATER)), NOW)
    assert chosen.number == AGENT_STEP_LATER
    assert dict(chosen.skipped)[AGENT_STEP] == "not labelled `ready`"


@pytest.mark.parametrize("label", [*SKIP_LABELS, "blocked: upstream", "blocked: org"])
def test_each_skip_label_leaves_the_item(label: str) -> None:
    skipped = replace(AGENT_SLICE, labels=AGENT_SLICE.labels | {label})
    chosen = pick(tracker(skipped, AGENT_SLICE_LATER, path=(AGENT_STEP, AGENT_STEP_LATER)), NOW)
    assert chosen.number == AGENT_STEP_LATER
    assert dict(chosen.skipped)[AGENT_STEP] == f"labelled `{label}`"


def test_ready_with_research_is_pickable() -> None:
    research = replace(AGENT_SLICE, labels=AGENT_SLICE.labels | {"research"})
    assert pick(tracker(research), NOW).number == AGENT_STEP


def with_comments(*comments: Comment) -> Tracker:
    return tracker(
        replace(AGENT_SLICE, comments=comments),
        AGENT_SLICE_LATER,
        path=(AGENT_STEP, AGENT_STEP_LATER),
    )


@pytest.mark.parametrize(
    ("comments", "live"),
    [
        ((claimed(1),), True),
        ((claimed(2.9),), True),
        ((claimed(3),), False),
        ((claimed(4),), False),
        ((claimed(1), handoff(0.5)), False),
        ((claimed(5), claimed(1, word="still on it")), True),
        ((claimed(2), handoff(1.5), claimed(1)), True),
        ((claimed(1, association="NONE"),), False),
        ((claimed(1), handoff(0.5, association="CONTRIBUTOR")), True),
        ((Comment("a note: claimed 2026-01-10T11:00Z 1a2b3c4d", NOW, "OWNER"),), False),
    ],
    ids=[
        "an hour old",
        "just under three hours",
        "three hours",
        "expired",
        "handed off",
        "renewed",
        "claimed again after a handoff",
        "a claim that is no owner input",
        "a handoff that is no owner input",
        "a comment that only mentions a claim",
    ],
)
def test_a_live_claim_leaves_the_item(comments: tuple[Comment, ...], live: bool) -> None:
    chosen = pick(with_comments(*comments), NOW)
    assert chosen.number == (AGENT_STEP_LATER if live else AGENT_STEP)
    if live:
        assert dict(chosen.skipped)[AGENT_STEP].startswith("claimed at ")


BLOCKER = 980


def blocked_in_body(number: int) -> str:
    return (
        f"Do it.\n\n## Blocked by\n\n- {ref(number)} the step before\n\n<details>\n"
        f"<summary>Sources</summary>\n\nSee {ref(OTHER)}.\n</details>\n"
    )


@pytest.mark.parametrize("state", ["open", "closed"])
def test_a_blocker_in_the_body_section_holds_the_item_while_open(state: str) -> None:
    item = replace(AGENT_SLICE, body=blocked_in_body(BLOCKER))
    blocker = issue(BLOCKER, title="The step before", state=state)
    chosen = pick(
        tracker(item, blocker, AGENT_SLICE_LATER, path=(AGENT_STEP, AGENT_STEP_LATER)), NOW
    )
    assert chosen.number == (AGENT_STEP_LATER if state == "open" else AGENT_STEP)


@pytest.mark.parametrize("state", ["open", "closed"])
def test_a_blocker_in_the_relation_holds_the_item_while_open(state: str) -> None:
    item = replace(AGENT_SLICE, blocked_by=(BLOCKER,))
    blocker = issue(BLOCKER, state=state)
    chosen = pick(
        tracker(item, blocker, AGENT_SLICE_LATER, path=(AGENT_STEP, AGENT_STEP_LATER)), NOW
    )
    assert chosen.number == (AGENT_STEP_LATER if state == "open" else AGENT_STEP)


def test_a_blocker_the_snapshot_lacks_holds_the_item() -> None:
    item = replace(AGENT_SLICE, blocked_by=(BLOCKER,))
    chosen = pick(tracker(item), NOW)
    assert chosen.number is None
    assert "lacks" in dict(chosen.skipped)[AGENT_STEP]


def test_the_body_section_ends_at_the_next_block() -> None:
    """The reference inside `<details>` after the section is no blocker."""
    assert blocked_by_section(blocked_in_body(BLOCKER)) == (BLOCKER,)
    url = f"## Blocked by\n- https://github.com/{REPO_NAME}/issues/{BLOCKER}\n## Next\n{ref(OTHER)}"
    assert blocked_by_section(url) == (BLOCKER,)
    assert blocked_by_section("## Blocked by\n\n- none\n") == ()


@pytest.mark.parametrize(
    ("pr", "stacks"),
    [
        (
            PullRequest(
                970, "fix", f"Closes {ref(BLOCKER)}", "claude/x", frozenset({"owner-merges"})
            ),
            True,
        ),
        (
            PullRequest(
                970, "fix", f"Closes {ref(BLOCKER)}", "claude/x", frozenset({"needs-local-verify"})
            ),
            True,
        ),
        (PullRequest(970, "[speculative: a = b]", f"Fixes {ref(BLOCKER)}", "claude/x"), True),
        (PullRequest(970, "fix", f"Closes {ref(BLOCKER)}", "claude/x"), False),
    ],
    ids=["owner-merges", "needs-local-verify", "speculative", "not held"],
)
def test_an_open_blocker_with_a_held_pr_is_stacked_on(pr: PullRequest, stacks: bool) -> None:
    item = replace(AGENT_SLICE, blocked_by=(BLOCKER,))
    blocker = issue(BLOCKER)
    chosen = pick(tracker(item, blocker, pulls=[pr]), NOW)
    assert chosen.number == (AGENT_STEP if stacks else None)
    assert chosen.stack_on == ((970,) if stacks else ())


@pytest.mark.parametrize(
    ("body", "head", "closing"),
    [
        (f"Closes {ref(AGENT_STEP)}", "claude/elsewhere", True),
        (f"resolved: {ref(AGENT_STEP)}", "claude/elsewhere", True),
        (f"Fixes {REPO_NAME}{ref(AGENT_STEP)}", "claude/elsewhere", True),
        ("", f"claude/{AGENT_STEP}-the-step", True),
        (f"Fixes other-owner/other-repo{ref(AGENT_STEP)}", "claude/elsewhere", False),
        (f"Refs {ref(AGENT_STEP)}", "claude/elsewhere", False),
        ("", f"claude/{AGENT_STEP}0-another", False),
    ],
)
def test_an_open_pr_that_closes_the_item_leaves_it(body: str, head: str, closing: bool) -> None:
    pr = PullRequest(970, "a change", body, head)
    assert (AGENT_STEP in closes(pr, REPO_NAME)) == closing
    chosen = pick(
        tracker(AGENT_SLICE, AGENT_SLICE_LATER, path=(AGENT_STEP, AGENT_STEP_LATER), pulls=[pr]),
        NOW,
    )
    assert chosen.number == (AGENT_STEP_LATER if closing else AGENT_STEP)


def test_epics_and_the_slice_issue_are_never_picked() -> None:
    """Both labelled `ready` and `slice`, on the path, under an epic and named by the prompt."""
    t = tracker(path=(SLICE_ISSUE, AGENT), under={GROUND_RULES: [AGENT, SLICE_ISSUE]})
    labels = frozenset({"agent", "epic", SLICE, "ready"})
    issues = dict(t.issues) | {
        AGENT: replace(t.issues[AGENT], labels=labels),
        SLICE_ISSUE: replace(t.issues[SLICE_ISSUE], labels=labels),
    }
    t = replace(t, issues=issues)
    for named in (None, AGENT, SLICE_ISSUE):
        chosen = pick(t, NOW, named)
        assert chosen.number is None
        assert chosen.rule == "nothing pickable"
    assert dict(pick(t, NOW).skipped) == {SLICE_ISSUE: "an epic", AGENT: "an epic"}
    no_epic_label = replace(
        t,
        issues=issues
        | {SLICE_ISSUE: replace(issues[SLICE_ISSUE], labels=frozenset({SLICE, "ready"}))},
    )
    assert dict(pick(no_epic_label, NOW).skipped)[SLICE_ISSUE] == "the slice issue itself"


def test_an_issue_outside_every_epic_and_the_path_is_not_picked() -> None:
    assert pick(tracker(issue(OTHER, "ready")), NOW).number is None


def test_a_named_issue_comes_first_when_pickable() -> None:
    t = tracker(AGENT_SLICE, GROUND)
    assert (pick(t, NOW, GROUND_ITEM).number, pick(t, NOW, GROUND_ITEM).rule) == (
        GROUND_ITEM,
        RULE_NAMED,
    )
    held = replace(GROUND, labels=GROUND.labels | {"needs-gcp"})
    assert pick(tracker(AGENT_SLICE, held), NOW, GROUND_ITEM).number == AGENT_STEP


@pytest.mark.parametrize("lane", [None, "", "none", "None"])
def test_no_owner_lane_lets_the_pick_run(lane: str | None) -> None:
    assert pick(tracker(AGENT_SLICE, lane=lane), NOW).number == AGENT_STEP


def test_an_owner_lane_stops_the_pick() -> None:
    chosen = pick(tracker(AGENT_SLICE, lane="the agent epic — agent/**"), NOW)
    assert chosen.number is None
    assert "Owner lane" in chosen.rule


def test_the_path_is_read_in_row_order_from_the_live_shape() -> None:
    body = path_body([957, 9162, 958])
    assert slice_path(body) == (957, 9162, 958)


TABLE_HEAD = "<summary>The path</summary>\n\n| # | Step |\n|---|---|\n"


@pytest.mark.parametrize(
    "body",
    [
        "no table here",
        "<summary>The path</summary>\n\nno table\n",
        f"{TABLE_HEAD}| one | {ref(957)} a step |\n",
        f"{TABLE_HEAD}| 1 | a step without its issue |\n",
        f"{TABLE_HEAD}| 1 | {ref(957)} a |\n| 2 | {ref(957)} b |\n",
    ],
    ids=["no path", "no table", "a step that is no number", "a row with no issue", "twice"],
)
def test_a_path_that_cannot_be_read_is_an_error(body: str) -> None:
    with pytest.raises(TrackerError):
        slice_path(body)


def test_two_slice_issues_are_an_error() -> None:
    second = issue(
        SLICE_ISSUE + 1, "epic", title="Thin slice: another", body=path_body([AGENT_STEP])
    )
    with pytest.raises(TrackerError, match="more than one"):
        pick(tracker(AGENT_SLICE, second), NOW)


def test_no_slice_issue_falls_to_the_leftovers_and_says_so() -> None:
    t = tracker(LEFTOVERS_ISSUE, AGENT_SLICE)
    t = replace(t, issues={n: i for n, i in t.issues.items() if n != SLICE_ISSUE})
    chosen = pick(t, NOW)
    assert chosen.number == LEFTOVERS
    assert any("no open issue titled" in w for w in chosen.warnings)


def test_the_labels_and_the_path_disagreeing_are_warned() -> None:
    unlabelled = replace(AGENT_SLICE, labels=frozenset({"agent", "ready"}))
    off_path = replace(ACCESS_SLICE)
    chosen = pick(tracker(unlabelled, off_path, path=(AGENT_STEP,)), NOW)
    assert any(
        f"{ref(AGENT_STEP)} is on the slice path but not labelled" in w for w in chosen.warnings
    )
    assert any(f"{ref(ACCESS_STEP)} is labelled `slice` but not on" in w for w in chosen.warnings)
    # Neither is a slice item: the first pickable sub-issue in epic order is the access one.
    assert (chosen.number, chosen.rule) == (ACCESS_STEP, RULE_EPIC)


# The profile and the code stay tied.


def profile_section(heading: str) -> str:
    lines = PROFILE.read_text().splitlines()
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def test_the_queue_section_names_the_command_and_every_label_the_picker_reads() -> None:
    queue = profile_section("## Queue")
    assert f"`{COMMAND}`" in queue
    for label in (*SKIP_LABELS, SLICE, LEFTOVER, "ready", "epic"):
        assert f"`{label}`" in queue, label
    assert "`Thin slice`" in queue
    assert "`Review leftovers: <area>`" in queue
    assert (REPO / "platform" / "next_item.py").is_file()


def test_the_command_runs_the_picker() -> None:
    assert COMMAND.endswith(" platform/next_item.py")


# The live snapshot, from a fake `gh api`.


class FakeGitHub:
    """Answers the REST paths `build_tracker` reads, as GitHub pages them, and records each."""

    def __init__(self, routes: Mapping[str, list[dict[str, Any]] | dict[str, Any]]) -> None:
        self.routes = routes
        self.asked: list[str] = []

    def __call__(self, path: str) -> Any:
        self.asked.append(path)
        assert "graphql" not in path
        bare, _, query = path.partition("?")
        bare = "/".join(bare.split("/")[3:])  # past `repos/<owner>/<name>/`
        params = dict(part.split("=", 1) for part in query.split("&") if part)
        answer = self.routes.get(bare)
        if answer is None:
            raise TrackerError(f"no route {bare}")
        if isinstance(answer, dict):
            return answer
        page, size = int(params["page"]), int(params["per_page"])
        return answer[(page - 1) * size : page * size]


def raw(
    number: int, *labels: str, title: str = "", body: str = "", state: str = "open"
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title or f"Item {number}",
        "state": state,
        "labels": [{"name": label} for label in labels],
        "body": body,
    }


def live_routes() -> dict[str, Any]:
    filler = [raw(800 + i, "data") for i in range(100)]  # a full first page
    path = (AGENT_STEP, AGENT_STEP_LATER)
    open_issues = [
        raw(ROADMAP, title="Roadmap"),
        raw(GROUND_RULES, "epic", title="Ground rules"),
        raw(AGENT, "epic", title="Agent"),
        raw(SLICE_ISSUE, "epic", title="Thin slice: one question", body=path_body(path)),
        raw(OTHER, title="Autopilot digest", body="Mode: full\nOwner lane: none\n"),
        raw(LEFTOVERS, LEFTOVER, "ready", title="Review leftovers: platform"),
        raw(AGENT_STEP, SLICE, "ready", body=blocked_in_body(BLOCKER)),
        raw(AGENT_STEP_LATER, SLICE, "ready"),
        {**raw(960), "pull_request": {}},
        *filler,
    ]
    claim = claimed(1)
    return {
        "issues": open_issues,
        "pulls": [{**raw(961, title="a change", body="Refs nothing"), "head": {"ref": "claude/x"}}],
        f"issues/{ROADMAP}/sub_issues": [
            raw(GROUND_RULES, "epic"),
            raw(AGENT, "epic"),
            raw(SLICE_ISSUE, "epic"),
        ],
        f"issues/{GROUND_RULES}/sub_issues": [raw(LEFTOVERS, LEFTOVER, "ready")],
        f"issues/{AGENT}/sub_issues": [
            raw(AGENT_STEP, SLICE, "ready"),
            raw(AGENT_STEP_LATER, SLICE, "ready"),
        ],
        f"issues/{SLICE_ISSUE}/sub_issues": [],
        f"issues/{AGENT_STEP}/comments": [],
        f"issues/{AGENT_STEP}/dependencies/blocked_by": [],
        f"issues/{AGENT_STEP_LATER}/comments": [
            {
                "body": claim.body,
                "created_at": f"{claim.created_at:%Y-%m-%dT%H:%M:%SZ}",
                "author_association": "OWNER",
            }
        ],
        f"issues/{AGENT_STEP_LATER}/dependencies/blocked_by": [],
        f"issues/{LEFTOVERS}/comments": [],
        f"issues/{LEFTOVERS}/dependencies/blocked_by": [],
        f"issues/{BLOCKER}": raw(BLOCKER, state="closed"),
    }


def test_the_live_snapshot_is_read_page_by_page_and_picks_by_the_rule() -> None:
    github = FakeGitHub(live_routes())
    t = build_tracker(REPO_NAME, github)
    assert all(p.startswith(f"repos/{REPO_NAME}/") for p in github.asked)
    assert all(
        "per_page=100&page=" in p for p in github.asked if not p.endswith(f"/issues/{BLOCKER}")
    )
    assert f"repos/{REPO_NAME}/issues?state=open&per_page=100&page=2" in github.asked
    assert t.epics == (GROUND_RULES, AGENT, SLICE_ISSUE)
    assert t.sub_issues[AGENT] == (AGENT_STEP, AGENT_STEP_LATER)
    assert t.owner_lane == "none"
    assert 960 not in t.issues  # a PR in the issues listing is no issue
    assert t.issues[BLOCKER].state == "closed"  # the body's blocker, read on its own
    assert len(t.issues[AGENT_STEP_LATER].comments) == 1
    # The first step's blocker is closed, so it is picked; with it open, the later step is
    # claimed, so the leftovers issue is.
    assert pick(t, NOW).number == AGENT_STEP
    routes = live_routes() | {f"issues/{BLOCKER}": raw(BLOCKER)}
    chosen = pick(build_tracker(REPO_NAME, FakeGitHub(routes)), NOW)
    assert (chosen.number, chosen.rule) == (LEFTOVERS, RULE_LEFTOVER)


def test_the_cli_prints_the_pick_and_exits_by_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--explain"], get=FakeGitHub(live_routes())) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{ref(AGENT_STEP)} Item {AGENT_STEP} ({RULE_SLICE})")
    routes = live_routes()
    routes["issues"] = [r for r in routes["issues"] if r["number"] != ROADMAP]
    assert main([], get=FakeGitHub(routes)) == 1
    assert "Roadmap" in capsys.readouterr().err


def test_the_cli_says_none_when_nothing_is_pickable(capsys: pytest.CaptureFixture[str]) -> None:
    routes = live_routes()
    routes["issues"] = [
        {**r, "labels": [{"name": n["name"]} for n in r["labels"] if n["name"] != "ready"]}
        for r in routes["issues"]
    ]
    assert main([], get=FakeGitHub(routes)) == 3
    assert capsys.readouterr().out == "none: nothing pickable\n"


def test_gh_is_called_once_per_page_through_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert gh_get("repos/a/b/issues?state=open&per_page=100&page=1") == []
    assert seen[0][:2] == ["gh", "api"]
    assert not any(arg.startswith("--paginate") or "graphql" in arg for arg in seen[0])


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/{REPO_NAME}.git",
        f"https://github.com/{REPO_NAME}",
        f"git@github.com:{REPO_NAME}.git",
        f"http://local_proxy@127.0.0.1:45000/git/{REPO_NAME}\n",
    ],
)
def test_the_repository_comes_from_the_origin_url(url: str) -> None:
    assert origin_repo(url) == REPO_NAME
