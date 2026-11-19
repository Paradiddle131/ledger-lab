"""The queue rule of `docs/agents/autopilot.md`, run by `platform/next_item.py` on fixture trackers.

Every tracker here is invented and offline, its issue numbers made up, with a fake `gh api` for
the live snapshot. Checked: the slice item first, in path order over epic order; the leftovers
issue of one area as one item, only when no slice item is pickable, before every other item; then
epic order; closed items and each skip label; live, expired and handed-off claims, read with the
skill's `claim.sh` patterns from any author; open blockers from the body's section and from the
relation, apart, and blockers in another repository; an open PR that closes the item, and only
owner-input PRs from this repository counted; stacking only on a held PR that is not speculative;
epics and the slice issue never picked; the Roadmap, the slice issue and the Digest read only from
owner-input issues; the Owner lane stopping the pick; the profile's Queue section naming the
command, the labels and the order; the live snapshot paged by hand through `gh api` REST calls.
"""

import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from next_item import (
    COMMAND,
    GH_TIMEOUT_S,
    LEFTOVER,
    RULE_EPIC,
    RULE_LEFTOVER,
    RULE_NAMED,
    RULE_SLICE,
    RULE_SLICE_OFF_PATH,
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
    owner_lane,
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
# The labels the profile names, written out here rather than read from the module under test.
SKIPPED = ("needs-gcp", "needs-owner", "blocked", "owner-machine")
HOLDS = ("owner-merges", "needs-local-verify")
FOREIGN_REPO = "other-owner/other-repo"


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
    rest.setdefault("author_association", "OWNER")
    return Issue(number, title or f"Item {number}", labels=frozenset(labels), body=body, **rest)


def pr(
    number: int,
    body: str = "",
    *labels: str,
    title: str = "a change",
    head: str = "claude/elsewhere",
    author: str = "OWNER",
    head_repo: str = REPO_NAME,
) -> PullRequest:
    return PullRequest(number, title, body, head, frozenset(labels), author, head_repo)


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
    access_item = issue(ACCESS_STEP, "access", "ready")
    chosen = pick(tracker(GROUND, access_item, path=()), NOW)
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


@pytest.mark.parametrize("label", [*SKIPPED, "blocked: upstream", "blocked: org"])
def test_each_skip_label_leaves_the_item(label: str) -> None:
    skipped = replace(AGENT_SLICE, labels=AGENT_SLICE.labels | {label})
    chosen = pick(tracker(skipped, AGENT_SLICE_LATER, path=(AGENT_STEP, AGENT_STEP_LATER)), NOW)
    assert chosen.number == AGENT_STEP_LATER
    assert dict(chosen.skipped)[AGENT_STEP] == f"labelled `{label}`"


def test_a_closed_item_still_labelled_ready_is_not_picked() -> None:
    """Closed sub-issues keep their `ready` label on the live tracker."""
    closed = replace(AGENT_SLICE, state="closed")
    chosen = pick(tracker(closed, AGENT_SLICE_LATER, path=(AGENT_STEP, AGENT_STEP_LATER)), NOW)
    assert chosen.number == AGENT_STEP_LATER
    assert dict(chosen.skipped)[AGENT_STEP] == "closed"
    done = replace(GROUND, state="closed")
    assert pick(tracker(done, path=()), NOW).number is None


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
        ((claimed(1, association="NONE"),), True),
        ((claimed(1), handoff(0.5, association="CONTRIBUTOR")), False),
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
        "a claim by any author counts, by claim.sh's patterns",
        "a handoff by any author ends it, by claim.sh's patterns",
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
    assert blocked_by_section(blocked_in_body(BLOCKER), REPO_NAME) == ((BLOCKER,), ())
    url = f"## Blocked by\n- https://github.com/{REPO_NAME}/issues/{BLOCKER}\n## Next\n{ref(OTHER)}"
    assert blocked_by_section(url, REPO_NAME) == ((BLOCKER,), ())
    short = f"## Blocked by\n- {REPO_NAME}{ref(BLOCKER)}\n"
    assert blocked_by_section(short, REPO_NAME) == ((BLOCKER,), ())
    assert blocked_by_section("## Blocked by\n\n- none\n", REPO_NAME) == ((), ())


@pytest.mark.parametrize(
    "line",
    [
        f"- {FOREIGN_REPO}{ref(BLOCKER)} upstream",
        f"- https://github.com/{FOREIGN_REPO}/issues/{BLOCKER}",
    ],
    ids=["short reference", "issue URL"],
)
def test_a_blocker_in_another_repository_in_the_body_holds_the_item(line: str) -> None:
    """Never read as this repository's issue of the same number, which here is closed."""
    body = f"Do it.\n\n## Blocked by\n\n{line}\n"
    assert blocked_by_section(body, REPO_NAME) == ((), (f"{FOREIGN_REPO}{ref(BLOCKER)}",))
    item = replace(AGENT_SLICE, body=body)
    chosen = pick(tracker(item, issue(BLOCKER, state="closed")), NOW)
    assert chosen.number is None
    assert dict(chosen.skipped)[AGENT_STEP] == (
        f"blocked by {FOREIGN_REPO}{ref(BLOCKER)}, in another repository"
    )


def test_a_blocker_in_another_repository_in_the_relation_holds_the_item() -> None:
    item = replace(AGENT_SLICE, foreign_blocked_by=(f"{FOREIGN_REPO}{ref(BLOCKER)}",))
    chosen = pick(tracker(item), NOW)
    assert chosen.number is None
    assert "in another repository" in dict(chosen.skipped)[AGENT_STEP]


@pytest.mark.parametrize(
    ("closing", "stacks"),
    [
        (pr(970, f"Closes {ref(BLOCKER)}", "owner-merges"), True),
        (pr(970, f"Closes {ref(BLOCKER)}", "needs-local-verify"), True),
        (pr(970, f"Fixes {ref(BLOCKER)}", title="[speculative: a = b]"), False),
        (pr(970, f"Fixes {ref(BLOCKER)}", "owner-merges", title="[speculative: a = b]"), False),
        (pr(970, f"Closes {ref(BLOCKER)}"), False),
        (pr(970, f"Closes {ref(BLOCKER)}", "owner-merges", author="NONE"), False),
        (pr(970, f"Closes {ref(BLOCKER)}", "owner-merges", head_repo="a-fork/example-repo"), False),
    ],
    ids=[
        "owner-merges",
        "needs-local-verify",
        "speculative, never stacked on",
        "speculative with a hold label, never stacked on",
        "not held",
        "held, by an outsider",
        "held, from a fork",
    ],
)
def test_an_open_blocker_with_a_held_pr_is_stacked_on(closing: PullRequest, stacks: bool) -> None:
    item = replace(AGENT_SLICE, blocked_by=(BLOCKER,))
    blocker = issue(BLOCKER)
    chosen = pick(tracker(item, blocker, pulls=[closing]), NOW)
    assert chosen.number == (AGENT_STEP if stacks else None)
    assert chosen.stack_on == ((970,) if stacks else ())


@pytest.mark.parametrize(
    ("body", "head", "closing"),
    [
        (f"Closes {ref(AGENT_STEP)}", "claude/elsewhere", True),
        (f"resolved: {ref(AGENT_STEP)}", "claude/elsewhere", True),
        (f"Fixes {REPO_NAME}{ref(AGENT_STEP)}", "claude/elsewhere", True),
        ("", f"claude/{AGENT_STEP}-the-step", True),
        (f"Closes https://github.com/{REPO_NAME}/issues/{AGENT_STEP}", "claude/elsewhere", True),
        (f"Fixes {FOREIGN_REPO}{ref(AGENT_STEP)}", "claude/elsewhere", False),
        (
            f"Closes https://github.com/{FOREIGN_REPO}/issues/{AGENT_STEP}",
            "claude/elsewhere",
            False,
        ),
        (f"Refs {ref(AGENT_STEP)}", "claude/elsewhere", False),
        ("", f"claude/{AGENT_STEP}0-another", False),
    ],
)
def test_an_open_pr_that_closes_the_item_leaves_it(body: str, head: str, closing: bool) -> None:
    change = pr(970, body, head=head)
    assert (AGENT_STEP in closes(change, REPO_NAME)) == closing
    chosen = pick(
        tracker(
            AGENT_SLICE, AGENT_SLICE_LATER, path=(AGENT_STEP, AGENT_STEP_LATER), pulls=[change]
        ),
        NOW,
    )
    assert chosen.number == (AGENT_STEP_LATER if closing else AGENT_STEP)


@pytest.mark.parametrize(
    ("change", "closing"),
    [
        (
            pr(
                970,
                f"Refs {ref(AGENT_STEP)}",
                title="[speculative: a = b]",
                head=f"claude/{AGENT_STEP}-a",
            ),
            False,
        ),
        (
            pr(
                970,
                f"Closes {ref(AGENT_STEP)}",
                title="[speculative: a = b]",
                head=f"claude/{AGENT_STEP}-a",
            ),
            True,
        ),
        (pr(970, f"Refs {ref(AGENT_STEP)}", title="a = b", head=f"claude/{AGENT_STEP}-a"), True),
    ],
    ids=["speculative on the item branch", "speculative with a closing keyword", "not speculative"],
)
def test_a_speculative_pr_closes_its_issue_only_by_a_closing_keyword(
    change: PullRequest, closing: bool
) -> None:
    """A speculative PR only refers to its issue: its `claude/<n>-…` branch closes nothing."""
    assert (AGENT_STEP in closes(change, REPO_NAME)) == closing
    chosen = pick(tracker(AGENT_SLICE, LEFTOVERS_ISSUE, pulls=[change]), NOW)
    assert chosen.number == (LEFTOVERS if closing else AGENT_STEP)


@pytest.mark.parametrize(
    "change",
    [
        pr(970, f"Closes {ref(AGENT_STEP)}", author="NONE"),
        pr(970, f"Closes {ref(AGENT_STEP)}", author="CONTRIBUTOR"),
        pr(970, f"Closes {ref(AGENT_STEP)}", head_repo="a-fork/example-repo"),
        pr(970, f"Closes {ref(AGENT_STEP)}", head_repo=""),
    ],
    ids=["an outsider", "a contributor", "from a fork", "from a deleted fork"],
)
def test_a_pr_that_is_no_owner_input_from_this_repository_does_not_count(
    change: PullRequest,
) -> None:
    chosen = pick(tracker(AGENT_SLICE, pulls=[change]), NOW)
    assert chosen.number == AGENT_STEP


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
    chosen = pick(tracker(AGENT_SLICE, held), NOW, GROUND_ITEM)
    assert (chosen.number, chosen.rule) == (AGENT_STEP, RULE_SLICE)
    assert f"the named {ref(GROUND_ITEM)} is not pickable: labelled `needs-gcp`" in chosen.warnings


@pytest.mark.parametrize("lane", [None, "", "none", "None"])
def test_no_owner_lane_lets_the_pick_run(lane: str | None) -> None:
    assert pick(tracker(AGENT_SLICE, lane=lane), NOW).number == AGENT_STEP


def test_an_owner_lane_stops_the_pick() -> None:
    chosen = pick(tracker(AGENT_SLICE, lane="the agent epic — agent/**"), NOW)
    assert chosen.number is None
    assert "Owner lane" in chosen.rule


DIGEST_HELP = (
    "\nThe steering lines above steer the wakes.\n"
    "- `Owner lane: <epics or issues> — <path globs>`: the owner's attended work.\n"
)


@pytest.mark.parametrize(
    ("body", "lane"),
    [
        ("Mode: full\nOwner lane: none\n" + DIGEST_HELP, "none"),
        (
            "Mode: full\nOwner lane: the agent epic — agent/**\n" + DIGEST_HELP,
            "the agent epic — agent/**",
        ),
        ("Mode: full\n" + DIGEST_HELP, None),
        ("", None),
    ],
    ids=["none", "a lane", "no lane line", "an empty body"],
)
def test_the_owner_lane_is_read_from_its_line(body: str, lane: str | None) -> None:
    assert owner_lane(body) == lane


@pytest.mark.parametrize(
    "body",
    [
        "Mode: full\n- Owner lane: none\n",
        "Mode: full\n**Owner lane:** none\n",
        "Mode: full\nowner lane: none\n",
        "Mode: full\nThe Owner lane is the agent epic\n",
        "Mode: full\nOwner lane: none\n\nMore.\n\nOwner lane: agent/**\n",
        "Mode: full\n\nLater: * Owner lane - agent/**\n> Owner lane: agent/**\n",
    ],
    ids=["a bullet", "bold", "lower case", "prose in the steering lines", "two lines", "quoted"],
)
def test_an_owner_lane_line_that_cannot_be_read_stops_the_pick(body: str) -> None:
    lane = owner_lane(body)
    assert lane is not None and lane.startswith("unreadable: ")
    chosen = pick(tracker(AGENT_SLICE, lane=lane), NOW)
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


@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", ""])
def test_a_slice_issue_no_owner_input_opened_is_not_read(association: str) -> None:
    """A stranger's `Thin slice` issue neither sets the path nor makes the owner's ambiguous."""
    stranger = issue(
        SLICE_ISSUE + 1,
        title="Thin slice: mine first",
        body=path_body([GROUND_ITEM]),
        author_association=association,
    )
    chosen = pick(tracker(AGENT_SLICE, GROUND, stranger), NOW)
    assert (chosen.number, chosen.rule) == (AGENT_STEP, RULE_SLICE)


def test_no_slice_issue_leaves_every_slice_item_off_the_path_and_says_so() -> None:
    t = tracker(LEFTOVERS_ISSUE, AGENT_SLICE)
    t = replace(t, issues={n: i for n, i in t.issues.items() if n != SLICE_ISSUE})
    chosen = pick(t, NOW)
    assert (chosen.number, chosen.rule) == (AGENT_STEP, RULE_SLICE_OFF_PATH)
    assert any("no open issue titled" in w for w in chosen.warnings)
    held = replace(AGENT_SLICE, labels=AGENT_SLICE.labels | {"needs-owner"})
    t = replace(t, issues=dict(t.issues) | {AGENT_STEP: held})
    assert pick(t, NOW).number == LEFTOVERS


def test_the_labels_and_the_path_disagreeing_are_warned() -> None:
    unlabelled = replace(AGENT_SLICE, labels=frozenset({"agent", "ready"}))
    off_path = replace(ACCESS_SLICE)
    chosen = pick(tracker(unlabelled, off_path, path=(AGENT_STEP,)), NOW)
    assert any(
        f"{ref(AGENT_STEP)} is on the slice path but not labelled" in w for w in chosen.warnings
    )
    assert any(f"{ref(ACCESS_STEP)} is labelled `slice` but not on" in w for w in chosen.warnings)
    # The labelled one off the path comes after the path's steps, before the other sub-issues.
    assert (chosen.number, chosen.rule) == (ACCESS_STEP, RULE_SLICE_OFF_PATH)


def test_a_slice_issue_off_the_path_that_blocks_a_step_comes_before_leftovers() -> None:
    """A leftover that blocks a path step gets `slice`: it is taken before every leftover and
    every ordinary item, though its epic comes after theirs."""
    blocker = issue(
        ACCESS_STEP, "access", LEFTOVER, SLICE, "ready", title="Review leftovers: access"
    )
    step = replace(AGENT_SLICE, body=blocked_in_body(ACCESS_STEP))
    chosen = pick(tracker(LEFTOVERS_ISSUE, GROUND, blocker, step, path=(AGENT_STEP,)), NOW)
    assert (chosen.number, chosen.rule) == (ACCESS_STEP, RULE_SLICE_OFF_PATH)
    assert dict(chosen.skipped)[AGENT_STEP] == f"blocked by {ref(ACCESS_STEP)}, open"
    assert any(f"{ref(ACCESS_STEP)} is labelled `slice` but not on" in w for w in chosen.warnings)


SECOND = 913  # a second item under the first epic


@pytest.mark.parametrize("order", [(GROUND_ITEM, SECOND), (SECOND, GROUND_ITEM)])
@pytest.mark.parametrize(
    ("labels", "rule"),
    [
        (("platform", "ready"), RULE_EPIC),
        (("platform", LEFTOVER, "ready"), RULE_LEFTOVER),
        (("platform", SLICE, "ready"), RULE_SLICE_OFF_PATH),
    ],
    ids=["other sub-issues", "leftovers", "slice items off the path"],
)
def test_two_candidates_of_one_tier_under_one_epic_go_in_sub_issue_order(
    order: tuple[int, int], labels: tuple[str, ...], rule: str
) -> None:
    items = [issue(n, *labels) for n in order]
    chosen = pick(tracker(*items, path=(), under={GROUND_RULES: list(order)}), NOW)
    assert (chosen.number, chosen.rule) == (order[0], rule)


# The profile and the code stay tied.


def profile_section(heading: str) -> str:
    lines = PROFILE.read_text().splitlines()
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def test_the_queue_section_names_the_command_and_the_labels_the_picker_reads() -> None:
    queue = profile_section("## Queue")
    assert f"`{COMMAND}`" in queue
    for label in (*SKIPPED, *HOLDS, "slice", "leftover", "ready", "epic"):
        assert f"`{label}`" in queue, label
    assert "`Thin slice`" in queue
    assert "`Review leftovers: <area>`" in queue
    assert (REPO / "platform" / "next_item.py").is_file()


def test_the_queue_steps_name_slice_then_leftover_then_the_other_sub_issues() -> None:
    steps = [line for line in profile_section("## Queue").splitlines() if re.match(r"\d+\. ", line)]
    assert [int(line.split(".")[0]) for line in steps] == list(range(1, len(steps) + 1))

    def step_naming(words: str) -> int:
        found = [i for i, line in enumerate(steps) if words in line]
        assert len(found) == 1, f"expected one Queue step naming {words!r}: {steps}"
        return found[0]

    roadmap = step_naming("titled `Roadmap`")
    slice_step = step_naming("labelled `slice`, in path order")
    leftover = step_naming("labelled `leftover`")
    other = step_naming("the first pickable other sub-issue, in epic order")
    off_path = step_naming("other sub-issue labelled `slice`, off the path")
    assert roadmap < slice_step < off_path < leftover < other
    assert "in path order" in steps[slice_step]
    for later in (off_path, leftover, other):
        assert steps[later].startswith(f"{later + 1}. Else ")
        assert "in epic order then sub-issue order" in steps[later]


def test_the_pickable_line_names_every_skip_label_and_the_stacking_rule() -> None:
    lines = profile_section("## Queue").splitlines()
    pickable = [line for line in lines if line.startswith("Pickable: ")]
    assert len(pickable) == 1
    for label in SKIPPED:
        assert f"`{label}`" in pickable[0], label
    assert (
        "one carrying `owner-merges` or `needs-local-verify`, never a speculative one"
        in (pickable[0])
    )
    assert "Blocked by in this repository and closed" in pickable[0]


OWNER_MACHINE_RULE = (
    "An open PR for an `owner-machine` item gets `owner-machine` on the PR itself too, from "
    "whoever opens it or labels its issue: the merge policy holds a PR carrying it, so wakes "
    "neither resume nor merge it (the skill's step 2 still answers owner input on it and "
    "retargets it once its base merges), and no blocked item stacks on it. A wake never removes "
    "it; the session on the owner's computer removes it only to merge that head itself, after "
    "the item's live runs pass on that head. The picker prints a `warning:` for each open PR "
    "that closes an `owner-machine` item without carrying the label, so a wake runs it before "
    "the skill's step 2 and puts the label on each PR a warning names."
)


def test_the_profile_holds_an_owner_machine_items_pr_by_its_own_label() -> None:
    """The rule line and the merge policy's two clauses, word for word: a reworded hold fails."""
    queue = profile_section("## Queue").splitlines()
    assert [line for line in queue if line.startswith("An open PR for an ")] == [OWNER_MACHINE_RULE]
    merge_policy = profile_section("## Merge policy")
    assert (
        "pass and no hold label is on it (an `owner-machine` item's PR gets `owner-machine`, "
        "§ Queue)" in merge_policy
    )
    assert 'export AUTOPILOT_HOLD_LABELS="owner-merges owner-machine"' in merge_policy
    assert "owner-machine" not in HOLDS  # held from merges, never a PR to stack on


@pytest.mark.parametrize(
    ("pr_labels", "issue_labels", "warned"),
    [
        ((), ("owner-machine",), True),
        (("owner-machine",), ("owner-machine",), False),
        (("needs-local-verify",), ("owner-machine",), True),
        ((), (), False),
    ],
)
def test_the_picker_warns_of_an_owner_machine_items_pr_without_the_label(
    pr_labels: tuple[str, ...], issue_labels: tuple[str, ...], warned: bool
) -> None:
    item = issue(BLOCKER, "access", *issue_labels)
    chosen = pick(
        tracker(AGENT_SLICE, item, pulls=[pr(970, f"Closes {ref(BLOCKER)}", *pr_labels)]), NOW
    )
    expected = (
        f"PR {ref(970)} closes `owner-machine` {ref(BLOCKER)} "
        "without carrying `owner-machine` itself"
    )
    assert any(w.startswith(expected) for w in chosen.warnings) is warned
    assert chosen.number == AGENT_STEP


def test_an_item_blocked_by_an_owner_machine_item_does_not_stack_on_its_held_pr() -> None:
    item = replace(AGENT_SLICE, blocked_by=(BLOCKER,))
    blocker = issue(BLOCKER, "ready", "owner-machine")
    closing = pr(970, f"Closes {ref(BLOCKER)}", "owner-machine")
    chosen = pick(tracker(item, blocker, pulls=[closing]), NOW)
    assert (chosen.number, chosen.stack_on) == (None, ())


def test_the_command_names_the_picker_script() -> None:
    assert COMMAND.endswith(" platform/next_item.py")


# The live snapshot, from a fake `gh api`.

HERE = "<this repository>"  # filled in by FakeGitHub with the repository each path names


class FakeGitHub:
    """Answers the REST paths `build_tracker` reads, as GitHub pages them, and records each. A
    `repository_url` or head repository of HERE becomes the repository the path names."""

    def __init__(self, routes: Mapping[str, list[dict[str, Any]] | dict[str, Any]]) -> None:
        self.routes = routes
        self.asked: list[str] = []

    def __call__(self, path: str) -> Any:
        self.asked.append(path)
        assert "graphql" not in path
        bare, _, query = path.partition("?")
        parts = bare.split("/")
        here, bare = "/".join(parts[1:3]), "/".join(parts[3:])  # past `repos/<owner>/<name>/`
        params = dict(part.split("=", 1) for part in query.split("&") if part)
        answer = self.routes.get(bare)
        if answer is None:
            raise TrackerError(f"no route {bare}")
        if isinstance(answer, dict):
            return self.fill(answer, here)
        page, size = int(params["page"]), int(params["per_page"])
        return [self.fill(item, here) for item in answer[(page - 1) * size : page * size]]

    @staticmethod
    def fill(item: dict[str, Any], here: str) -> dict[str, Any]:
        item = dict(item)
        if item.get("repository_url") == HERE:
            item["repository_url"] = f"https://api.github.com/repos/{here}"
        head = item.get("head")
        if isinstance(head, dict) and head.get("repo") == HERE:
            item["head"] = {**head, "repo": {"full_name": here}}
        return item


def raw(
    number: int,
    *labels: str,
    title: str = "",
    body: str = "",
    state: str = "open",
    author: str = "OWNER",
    repo: str = HERE,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title or f"Item {number}",
        "state": state,
        "labels": [{"name": label} for label in labels],
        "body": body,
        "author_association": author,
        "repository_url": repo if repo == HERE else f"https://api.github.com/repos/{repo}",
    }


def raw_pr(number: int, body: str, head: str, author: str = "OWNER", repo: Any = HERE) -> Any:
    return {
        **raw(number, title="a change", body=body, author=author),
        "head": {"ref": head, "repo": repo},
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
        "pulls": [raw_pr(961, "Refs nothing", "claude/x")],
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
    assert t.pulls[0].head_repo == REPO_NAME and t.pulls[0].author_association == "OWNER"
    # The first step's blocker is closed, so it is picked; with it open, the later step is
    # claimed, so the leftovers issue is.
    assert pick(t, NOW).number == AGENT_STEP
    routes = live_routes() | {f"issues/{BLOCKER}": raw(BLOCKER)}
    chosen = pick(build_tracker(REPO_NAME, FakeGitHub(routes)), NOW)
    assert (chosen.number, chosen.rule) == (LEFTOVERS, RULE_LEFTOVER)


def test_the_live_relation_blocker_is_read() -> None:
    """An open blocker only the relation names holds the first step."""
    routes = live_routes()
    routes[f"issues/{AGENT_STEP}/dependencies/blocked_by"] = [raw(BLOCKER + 1)]
    t = build_tracker(REPO_NAME, FakeGitHub(routes))
    assert t.issues[AGENT_STEP].blocked_by == (BLOCKER + 1,)
    assert t.issues[BLOCKER + 1].state == "open"
    chosen = pick(t, NOW)
    assert chosen.number == LEFTOVERS
    assert dict(chosen.skipped)[AGENT_STEP] == f"blocked by {ref(BLOCKER + 1)}, open"


def test_a_live_relation_blocker_in_another_repository_is_never_read_as_this_one() -> None:
    routes = live_routes()
    routes[f"issues/{AGENT_STEP}/dependencies/blocked_by"] = [
        raw(BLOCKER, state="closed", repo=FOREIGN_REPO)
    ]
    t = build_tracker(REPO_NAME, FakeGitHub(routes))
    assert t.issues[AGENT_STEP].blocked_by == ()
    assert t.issues[AGENT_STEP].foreign_blocked_by == (f"{FOREIGN_REPO}{ref(BLOCKER)}",)
    assert "in another repository" in dict(pick(t, NOW).skipped)[AGENT_STEP]


def test_a_live_sub_issue_in_another_repository_is_left_out() -> None:
    routes = live_routes()
    routes[f"issues/{AGENT}/sub_issues"] = [
        raw(AGENT_STEP_LATER, "ready", repo=FOREIGN_REPO),
        *routes[f"issues/{AGENT}/sub_issues"],
    ]
    t = build_tracker(REPO_NAME, FakeGitHub(routes))
    assert t.sub_issues[AGENT] == (AGENT_STEP, AGENT_STEP_LATER)
    assert t.issues[AGENT_STEP_LATER].labels == frozenset({SLICE, "ready"})


def test_live_prs_keep_their_author_and_head_repository() -> None:
    routes = live_routes()
    routes["pulls"] = [
        raw_pr(
            961,
            f"Closes {ref(AGENT_STEP)}",
            "patch-1",
            author="NONE",
            repo={"full_name": "a-fork/example-repo"},
        ),
        raw_pr(962, f"Closes {ref(AGENT_STEP)}", "patch-2", repo=None),
    ]
    t = build_tracker(REPO_NAME, FakeGitHub(routes))
    assert [(p.author_association, p.head_repo) for p in t.pulls] == [
        ("NONE", "a-fork/example-repo"),
        ("OWNER", ""),
    ]
    assert pick(t, NOW).number == AGENT_STEP
    routes["pulls"] = [raw_pr(963, f"Closes {ref(AGENT_STEP)}", "claude/x")]
    assert pick(build_tracker(REPO_NAME, FakeGitHub(routes)), NOW).number == LEFTOVERS


@pytest.mark.parametrize("title", ["Roadmap", "Autopilot digest", "Thin slice: a second"])
def test_two_owner_input_issues_of_one_steering_title_are_an_error(title: str) -> None:
    routes = live_routes()
    routes["issues"] = [
        *routes["issues"],
        raw(970, title=title, body=path_body([GROUND_ITEM]), author="MEMBER"),
    ]
    with pytest.raises(TrackerError, match="more than one open issue titled"):
        pick(build_tracker(REPO_NAME, FakeGitHub(routes)), NOW)


@pytest.mark.parametrize("author", ["NONE", "CONTRIBUTOR", "FIRST_TIMER", ""])
def test_steering_issues_by_anyone_else_are_not_read(author: str) -> None:
    """A stranger's Roadmap, Digest naming a lane and Thin slice change nothing."""
    routes = live_routes()
    routes["issues"] = [
        raw(970, title="Roadmap", author=author),
        raw(971, title="Autopilot digest", body="Owner lane: none\n", author=author),
        raw(972, title="Thin slice: mine", body=path_body([AGENT_STEP_LATER]), author=author),
        *routes["issues"],
    ]
    t = build_tracker(REPO_NAME, FakeGitHub(routes))
    assert t.epics == (GROUND_RULES, AGENT, SLICE_ISSUE)
    assert pick(t, NOW).number == AGENT_STEP
    # The owner's Digest alone names a lane; a stranger's `none` does not clear it.
    lane = "Mode: full\nOwner lane: agent/**\n"
    routes["issues"] = [
        r
        if r["title"] != "Autopilot digest" or r["author_association"] != "OWNER"
        else {**r, "body": lane}
        for r in routes["issues"]
    ]
    t = build_tracker(REPO_NAME, FakeGitHub(routes))
    assert t.owner_lane == "agent/**"
    assert pick(t, NOW).number is None


def test_a_roadmap_by_anyone_else_alone_is_an_error() -> None:
    routes = live_routes()
    routes["issues"] = [
        {**r, "author_association": "NONE"} if r["title"] == "Roadmap" else r
        for r in routes["issues"]
    ]
    with pytest.raises(TrackerError, match="no open issue titled `Roadmap` by owner input"):
        build_tracker(REPO_NAME, FakeGitHub(routes))


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


def test_gh_is_called_once_for_one_page_through_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert gh_get("repos/a/b/issues?state=open&per_page=100&page=1") == []
    assert len(seen) == 1
    assert seen[0][:2] == ["gh", "api"]
    assert seen[0][-1] == "repos/a/b/issues?state=open&per_page=100&page=1"
    assert not any(arg.startswith("--paginate") or "graphql" in arg for arg in seen[0])


def test_a_stalled_gh_call_ends_the_pick_as_an_unreadable_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounds: list[object] = []

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        bounds.append(options.get("timeout"))
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(TrackerError, match=f"no answer in {GH_TIMEOUT_S} s"):
        gh_get("repos/a/b/issues?state=open&per_page=100&page=1")
    assert bounds == [GH_TIMEOUT_S]
    assert 0 < GH_TIMEOUT_S <= 120


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/{REPO_NAME}.git",
        f"https://github.com/{REPO_NAME}",
        f"git@example.com:{REPO_NAME}.git",  # the ssh form, on a reserved example domain
        f"http://local_proxy@127.0.0.1:45000/git/{REPO_NAME}\n",
    ],
)
def test_the_repository_comes_from_the_origin_url(url: str) -> None:
    assert origin_repo(url) == REPO_NAME
