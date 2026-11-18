"""The next item an unattended wake takes, by the queue rule of `docs/agents/autopilot.md`.

`pick(tracker, now)` is pure: it reads a snapshot of the tracker and answers the item and the rule
that chose it, or no item and the reason. The order:
1. an issue the routine prompt names, when it is pickable;
2. the first pickable issue labelled `slice`, in the order of the rows of the path table in the
   body of the open issue titled starting `Thin slice`;
3. else the first pickable issue labelled `leftover`, in epic order then sub-issue order;
4. else the first pickable other sub-issue, in epic order then sub-issue order.
The epics are the sub-issues of the open issue titled `Roadmap`, in order. An epic (label `epic`)
and the slice issue itself are never picked.

Pickable, as the autopilot skill's step 3 says, with the profile's labels: open; labelled `ready`
(`ready` with `research` too); none of SKIP_LABELS nor a label starting `blocked:`; no live claim;
no open PR that closes it; and every issue it is blocked by, under its body's `## Blocked by`
section or through GitHub's blocked-by relation, closed, or open with a held PR that closes it, to
stack on. A claim is what the skill's `claim.sh` reads: the latest comment whose body starts
`claimed <time> <wake id>` or `still on it <time> <wake id>` (after an optional `🤖 `), live while
its time is under three hours old and no comment starting `handoff` (after an optional `🤖 ` or
`## `, any case) follows it; only owner input counts (OWNER, MEMBER or COLLABORATOR). A PR closes
an issue when its body says so with one of GitHub's closing keywords, or when its head branch is
the item branch `claude/<n>-<slug>`. A held PR carries `owner-merges` or `needs-local-verify` or is
titled `[speculative: ...]`.

A Digest that names an Owner lane (anything but `none`) stops the pick: this module does not read
the lane's maps and globs, so a wake then picks by the skill's step 3 by hand.

The CLI (COMMAND) builds the snapshot live through `gh api` REST calls, paged by hand with
`per_page=100&page=<n>`, for the repository `git remote get-url origin` names; it prints the pick,
exiting 0, or `none: <reason>`, exiting 3; a tracker it cannot read exits 1. It only reads.
"""

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

COMMAND = "uv run python platform/next_item.py"

ROADMAP_TITLE = "Roadmap"
SLICE_TITLE_PREFIX = "Thin slice"
DIGEST_TITLE = "Autopilot digest"
READY = "ready"
EPIC = "epic"
SLICE = "slice"
LEFTOVER = "leftover"
SKIP_LABELS = ("needs-gcp", "needs-owner", "blocked", "owner-machine")
HOLD_LABELS = ("owner-merges", "needs-local-verify")
OWNER_INPUT = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
CLAIM_LIFE = timedelta(hours=3)

RULE_NAMED = "named by the prompt"
RULE_SLICE = "the first pickable slice item in path order"
RULE_LEFTOVER = "no slice item pickable: the first pickable leftovers issue in epic order"
RULE_EPIC = "no slice item or leftovers issue pickable: the first pickable sub-issue in epic order"

_CLAIM = re.compile(r"(?:🤖 )?(?:claimed|still on it) ([0-9T:Z-]+) [0-9a-f]+")
_HANDOFF = re.compile(r"(?:🤖 |## )?handoff", re.IGNORECASE)
_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+(?:([\w.-]+/[\w.-]+))?#(\d+)\b", re.IGNORECASE
)
_HEADING = re.compile(r"#{1,6}\s")
_PATH_ROW = re.compile(r"\|\s*(\d+)\s*\|\s*#(\d+)\b")


@dataclass(frozen=True)
class Comment:
    body: str
    created_at: datetime
    author_association: str


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str = "open"
    labels: frozenset[str] = frozenset()
    body: str = ""
    # GitHub's blocked-by relation; the body's `## Blocked by` section is read from `body`.
    blocked_by: tuple[int, ...] = ()
    comments: tuple[Comment, ...] = ()


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str = ""
    head: str = ""
    labels: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Tracker:
    """What the pick reads. `issues` holds every open issue and every issue a candidate is blocked
    by, open or closed; a blocker it lacks makes its candidate unpickable."""

    repo: str
    epics: tuple[int, ...]
    sub_issues: Mapping[int, tuple[int, ...]]
    issues: Mapping[int, Issue]
    pulls: tuple[PullRequest, ...] = ()
    owner_lane: str | None = None


@dataclass(frozen=True)
class Pick:
    """The item and the rule that chose it, or no item (`number` None) and the reason in `rule`."""

    number: int | None
    rule: str
    stack_on: tuple[int, ...] = ()
    skipped: tuple[tuple[int, str], ...] = ()
    warnings: tuple[str, ...] = ()


class TrackerError(RuntimeError):
    """The tracker cannot be read as the queue rule needs it."""


def slice_issue(tracker: Tracker) -> Issue | None:
    """The open issue titled starting `Thin slice`; more than one is an error."""
    found = [
        issue
        for issue in tracker.issues.values()
        if issue.state == "open" and issue.title.startswith(SLICE_TITLE_PREFIX)
    ]
    if len(found) > 1:
        numbers = ", ".join(f"#{issue.number}" for issue in found)
        raise TrackerError(f"more than one open issue titled `{SLICE_TITLE_PREFIX}…`: {numbers}")
    return found[0] if found else None


def slice_path(body: str) -> tuple[int, ...]:
    """The issue numbers of the path table's rows, in row order: the first table after the line
    that names `The path`, each data row a step number then a cell starting `#<n>`."""
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if "The path" in line), None)
    if start is None:
        raise TrackerError("the slice issue's body names no `The path` table")
    rows: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("|"):
            rows.append(stripped)
        elif rows:
            break
    if len(rows) < 3:
        raise TrackerError("the slice issue's path table has no rows")
    numbers: list[int] = []
    for row in rows[2:]:  # the header and its separator first
        read = _PATH_ROW.match(row)
        if not read:
            raise TrackerError(f"a path row is not `| <step> | #<n> …`: {row}")
        number = int(read.group(2))
        if number in numbers:
            raise TrackerError(f"the path lists #{number} twice")
        numbers.append(number)
    return tuple(numbers)


def blocked_by_section(body: str) -> tuple[int, ...]:
    """The issues the body's `## Blocked by` section references, up to the next heading, HTML
    block or rule."""
    numbers: list[int] = []
    inside = False
    for line in body.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"##\s+Blocked by\s*", stripped, re.IGNORECASE):
            inside = True
            continue
        if inside and (
            _HEADING.match(stripped) or stripped.startswith("<") or stripped.startswith("---")
        ):
            inside = False
        if inside:
            numbers += [int(n) for n in re.findall(r"(?<![\w/])#(\d+)\b", stripped)]
            numbers += [int(n) for n in re.findall(r"/issues/(\d+)\b", stripped)]
    return tuple(dict.fromkeys(numbers))


def closes(pr: PullRequest, repo: str) -> frozenset[int]:
    """The issues of `repo` an open PR closes: a closing keyword in its body, or its head branch
    `claude/<n>-<slug>`."""
    numbers = {
        int(number)
        for other, number in _CLOSING.findall(pr.body)
        if not other or other.lower() == repo.lower()
    }
    branch = re.match(r"claude/(\d+)-", pr.head)
    if branch:
        numbers.add(int(branch.group(1)))
    return frozenset(numbers)


def held(pr: PullRequest) -> bool:
    return bool(pr.labels & set(HOLD_LABELS)) or pr.title.startswith("[speculative:")


def live_claim(issue: Issue, now: datetime) -> datetime | None:
    """The time of the issue's live claim, or None."""
    claim: datetime | None = None
    for comment in sorted(issue.comments, key=lambda c: c.created_at):
        if comment.author_association not in OWNER_INPUT:
            continue
        read = _CLAIM.match(comment.body)
        if read:
            try:
                claim = datetime.fromisoformat(read.group(1))
            except ValueError:
                claim = comment.created_at
            if claim.tzinfo is None:
                claim = claim.replace(tzinfo=UTC)
        elif _HANDOFF.match(comment.body):
            claim = None
    if claim is not None and now - claim < CLAIM_LIFE:
        return claim
    return None


def _skip_label(labels: frozenset[str]) -> str | None:
    for label in sorted(labels):
        if label in SKIP_LABELS or label.startswith("blocked:"):
            return label
    return None


def unpickable(
    number: int, tracker: Tracker, now: datetime, slice_number: int | None
) -> tuple[str | None, tuple[int, ...]]:
    """Why the issue is not pickable (None when it is), and the held PRs it would stack on."""
    issue = tracker.issues.get(number)
    if issue is None:
        return "not among the open issues", ()
    if issue.state != "open":
        return "closed", ()
    if EPIC in issue.labels:
        return "an epic", ()
    if number == slice_number:
        return "the slice issue itself", ()
    if READY not in issue.labels:
        return f"not labelled `{READY}`", ()
    label = _skip_label(issue.labels)
    if label:
        return f"labelled `{label}`", ()
    claim = live_claim(issue, now)
    if claim:
        return f"claimed at {claim:%Y-%m-%dT%H:%MZ}, under three hours ago", ()
    closing = {pr.number: closes(pr, tracker.repo) for pr in tracker.pulls}
    open_prs = [pr for pr in tracker.pulls if number in closing[pr.number]]
    if open_prs:
        return f"an open PR closes it: #{open_prs[0].number}", ()
    stack: list[int] = []
    for blocker in dict.fromkeys((*blocked_by_section(issue.body), *issue.blocked_by)):
        if blocker == number:
            continue
        known = tracker.issues.get(blocker)
        if known is None:
            return f"blocked by #{blocker}, whose state the snapshot lacks", ()
        if known.state == "open":
            holding = [pr for pr in tracker.pulls if blocker in closing[pr.number] and held(pr)]
            if not holding:
                return f"blocked by #{blocker}, open", ()
            stack.append(holding[0].number)
    return None, tuple(stack)


def _sub_issues(tracker: Tracker) -> Iterator[int]:
    for epic in tracker.epics:
        yield from tracker.sub_issues.get(epic, ())


def pick(tracker: Tracker, now: datetime, named: int | None = None) -> Pick:
    """The next item by the queue rule; see the module's docstring."""
    lane = (tracker.owner_lane or "").strip()
    if lane and lane.lower() != "none":
        return Pick(
            None,
            "the Digest names an Owner lane, which this picker does not read: pick by the "
            "skill's step 3 by hand",
        )
    the_slice = slice_issue(tracker)
    slice_number = the_slice.number if the_slice else None
    path = slice_path(the_slice.body) if the_slice else ()
    warnings: list[str] = []
    if the_slice is None:
        warnings.append(f"no open issue titled `{SLICE_TITLE_PREFIX}…`: no slice path")
    labelled = sorted(
        n for n, issue in tracker.issues.items() if issue.state == "open" and SLICE in issue.labels
    )
    for n in labelled:
        if n not in path:
            warnings.append(f"#{n} is labelled `{SLICE}` but not on the slice path")
    skipped: dict[int, str] = {}

    def first(candidates: Iterable[int], wanted: Callable[[Issue], bool], rule: str) -> Pick | None:
        for n in candidates:
            if n in skipped:
                continue
            issue = tracker.issues.get(n)
            if issue is not None and not wanted(issue):
                continue
            reason, stack = unpickable(n, tracker, now, slice_number)
            if reason is None:
                return Pick(n, rule, stack, tuple(skipped.items()), tuple(warnings))
            skipped[n] = reason
        return None

    if named is not None:
        chosen = first([named], lambda _: True, RULE_NAMED)
        if chosen:
            return chosen
        warnings.append(f"the named #{named} is not pickable: {skipped[named]}")
    for n in path:
        issue = tracker.issues.get(n)
        if issue is not None and issue.state == "open" and SLICE not in issue.labels:
            warnings.append(f"#{n} is on the slice path but not labelled `{SLICE}`")
    chosen = (
        first(path, lambda issue: SLICE in issue.labels, RULE_SLICE)
        or first(_sub_issues(tracker), lambda issue: LEFTOVER in issue.labels, RULE_LEFTOVER)
        or first(_sub_issues(tracker), lambda _: True, RULE_EPIC)
    )
    if chosen:
        return chosen
    return Pick(None, "nothing pickable", (), tuple(skipped.items()), tuple(warnings))


# The live snapshot, through `gh api` REST calls only.

Get = Callable[[str], Any]


def origin_repo(url: str) -> str:
    """`<owner>/<name>` from an origin URL: https, ssh or the cloud proxy's form."""
    cleaned = re.sub(r"\.git$", "", url.strip().rstrip("/"))
    read = re.search(r"[:/]([^/:]+/[^/:]+)$", cleaned)
    if not read:
        raise TrackerError("the origin remote names no `<owner>/<name>`")
    return read.group(1)


def gh_get(path: str) -> Any:
    ran = subprocess.run(  # noqa: S603
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if ran.returncode != 0:
        raise TrackerError(f"gh api {path} failed: {ran.stderr.strip()[:300]}")
    return json.loads(ran.stdout)


def pages(get: Get, path: str) -> list[dict[str, Any]]:
    """Every item of a listing, page by page, until a page holds fewer than a hundred."""
    joiner = "&" if "?" in path else "?"
    items: list[dict[str, Any]] = []
    for page in range(1, 101):
        answer = get(f"{path}{joiner}per_page=100&page={page}")
        if not isinstance(answer, list):
            raise TrackerError(f"{path} answered no list")
        items += answer
        if len(answer) < 100:
            return items
    raise TrackerError(f"{path} has more than a hundred pages")


def _issue(raw: Mapping[str, Any]) -> Issue:
    return Issue(
        number=int(raw["number"]),
        title=str(raw.get("title") or ""),
        state=str(raw.get("state") or ""),
        labels=frozenset(str(label["name"]) for label in raw.get("labels") or ()),
        body=str(raw.get("body") or ""),
    )


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def build_tracker(repo: str, get: Get, named: int | None = None) -> Tracker:
    """The snapshot `pick` reads, read live: every open issue, the Roadmap's epics and their
    sub-issues in order, the open PRs, the Digest's Owner lane, and, for each candidate that
    labels alone leave pickable, its comments, its blocked-by relation and its blockers' states."""
    base = f"repos/{repo}"
    issues: dict[int, Issue] = {}
    for raw in pages(get, f"{base}/issues?state=open"):
        if "pull_request" not in raw:
            issue = _issue(raw)
            issues[issue.number] = issue
    pulls = tuple(
        PullRequest(
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            head=str((raw.get("head") or {}).get("ref") or ""),
            labels=frozenset(str(label["name"]) for label in raw.get("labels") or ()),
        )
        for raw in pages(get, f"{base}/pulls?state=open")
    )
    roadmaps = [i for i in issues.values() if i.title == ROADMAP_TITLE]
    if len(roadmaps) != 1:
        raise TrackerError(
            f"expected one open issue titled `{ROADMAP_TITLE}`, found {len(roadmaps)}"
        )

    def children(number: int) -> tuple[int, ...]:
        found: list[int] = []
        for raw in pages(get, f"{base}/issues/{number}/sub_issues"):
            child = _issue(raw)
            issues.setdefault(child.number, child)
            found.append(child.number)
        return tuple(found)

    epics = children(roadmaps[0].number)
    sub_issues = {epic: children(epic) for epic in epics}
    digests = [i for i in issues.values() if i.title == DIGEST_TITLE]
    lane = None
    if digests:
        read = re.search(r"^Owner lane:(.*)$", digests[0].body, re.MULTILINE)
        lane = read.group(1).strip() if read else None

    labels_only = Tracker(repo, epics, sub_issues, issues)
    the_slice = slice_issue(labels_only)
    path = slice_path(the_slice.body) if the_slice else ()
    candidates = dict.fromkeys(
        n
        for n in (*([named] if named is not None else []), *path, *_sub_issues(labels_only))
        if n in issues
        and issues[n].state == "open"
        and READY in issues[n].labels
        and EPIC not in issues[n].labels
        and _skip_label(issues[n].labels) is None
    )

    def details(issue: Issue) -> tuple[Issue, list[Issue]]:
        number = issue.number
        comments = tuple(
            Comment(
                body=str(raw.get("body") or ""),
                created_at=_time(str(raw["created_at"])),
                author_association=str(raw.get("author_association") or ""),
            )
            for raw in pages(get, f"{base}/issues/{number}/comments")
        )
        relation = pages(get, f"{base}/issues/{number}/dependencies/blocked_by")
        blockers = [_issue(raw) for raw in relation]
        detailed = Issue(
            issue.number,
            issue.title,
            issue.state,
            issue.labels,
            issue.body,
            tuple(b.number for b in blockers),
            comments,
        )
        return detailed, blockers

    with ThreadPoolExecutor(max_workers=8) as pool:
        for detailed, blockers in pool.map(details, [issues[n] for n in candidates]):
            issues[detailed.number] = detailed
            for blocker in blockers:
                issues.setdefault(blocker.number, blocker)
    missing = {
        blocker
        for n in candidates
        for blocker in blocked_by_section(issues[n].body)
        if blocker not in issues
    }
    for blocker in sorted(missing):
        # One that cannot be read is left out: its candidate is then unpickable, saying so.
        with suppress(TrackerError):
            issues[blocker] = _issue(get(f"{base}/issues/{blocker}"))
    return Tracker(repo, epics, sub_issues, issues, pulls, lane)


def main(argv: Sequence[str] | None = None, get: Get = gh_get) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--issue", type=int, help="the issue the routine prompt names")
    parser.add_argument("--explain", action="store_true", help="also print each skipped issue")
    args = parser.parse_args(argv)
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        tracker = build_tracker(origin_repo(url), get, args.issue)
        chosen = pick(tracker, datetime.now(UTC), args.issue)
    except (TrackerError, subprocess.CalledProcessError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for warning in chosen.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.explain:
        for number, reason in chosen.skipped:
            print(f"skipped #{number}: {reason}")
    if chosen.number is None:
        print(f"none: {chosen.rule}")
        return 3
    title = tracker.issues[chosen.number].title
    stack = "".join(f"; stack on PR #{n}" for n in chosen.stack_on)
    print(f"#{chosen.number} {title} ({chosen.rule}{stack})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
