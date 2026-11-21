# Autopilot profile — ledger-lab

Unattended cloud wakes on this repo follow the `cloud:autopilot` skill (if missing, read `cloud/skills/autopilot/SKILL.md` in the collection's clone, which `~/.claude/CLAUDE.md` names) with the slots below.
A wake with no `~/.claude/cloud-target` (the environment's setup didn't run) first runs `bash <clone>/cloud/setup.sh ledger-lab`.
The routine prompt: "Follow `docs/agents/autopilot.md` in ledger-lab and do one wake." (optionally "on #n").
Standing permission: push the item branches `claude/<n>-<slug>` and the existing branch of an open agent PR being resumed, open and edit their PRs, issues and labels, comment, and merge under the merge policy below, without asking.
Every item is one branch and one PR (a new item's branch is `claude/<n>-<slug>`; an open agent PR being resumed keeps its branch), reviewed by the `review` skill and merged by the wake once its gates and review records pass, unless the merge policy holds it.

## Merge policy

The wake merges a PR once its gates and review records pass on its head, unless the skill's holds or the one below hold it.
The owner keeps the guardrails and the spending: a PR whose diff adds, removes or changes any line of `AGENTS.md § Guardrails`, from its heading to its last item, or a PR that spends money (any paid service, data source, API or tier, GCP or not), gets the label `owner-merges` on the PR itself, and its issue gets the decision packet and `needs-owner`.
Every other PR is the wake's to merge once its gates and review records pass and the skill's holds allow it (an `owner-machine` item's PR carries `needs-local-verify`, § Queue), one that is costly to undo included (`AGENTS.md § Decisions while the owner is away`; this narrows the skill's own `owner-merges` examples).
`merge.sh` reads the PR's own labels and holds on `owner-merges`; `export AUTOPILOT_HOLD_LABELS=owner-merges` before the scripts keeps that hold with a copy of `merge.sh` that does not list the label itself.
A wake never removes `owner-merges`; only the owner's own merge lands such a PR.
The owner approved the guardrail hold in an attended session on 2026-11-18 and said agents merge the change that sets it; the spending hold keeps `AGENTS.md`'s rule that a choice that costs money waits.

## Review

The paths that set a review tier: `REVIEW.md` at the repo root, read from the default branch by the `review` skill's `tier.sh`.

## Digest

Steering lines (SKILL.md § Steering) at the top of the body of the open issue titled `Autopilot digest`, which the owner pins and only the owner edits: `Mode: full` and `Owner lane: none`.
`Mode: offline` means no GCP call and no fetch from a data source's host; no `Mode` line means `full`.
Open owner questions are the `needs-owner` issues.

## Queue

Run `uv run python platform/next_item.py` (add `--issue <n>` when the routine prompt names `#n`, `--explain` to also print the candidates it passed over before the pick, with their reasons): it reads the tracker live, only reading, and prints the item and the rule that chose it, or `none: <reason>` (exit 3); take that item, and name each `warning:` line it prints in the handoff as a `setup` item of **Went wrong:**.
Exit 1 means the tracker cannot be read as the rule needs (`error: <reason>`): the wake picks nothing, names the error in its handoff as a `setup` item of **Went wrong:**, and ends.
The rule it runs, which `tests/platform/test_next_item.py` proves on fixture trackers:

1. The open issue titled `Roadmap`: its sub-issues are the epics, in order. The open issue titled starting `Thin slice` holds the slice's path: the rows of its "The path" table, in row order, each a step number then `#<n>`.
2. Take the first pickable issue labelled `slice`, in path order.
3. Else the first pickable other sub-issue labelled `slice`, off the path (such as a leftover that blocks a step), in epic order then sub-issue order.
4. Else the first pickable issue labelled `leftover`, in epic order then sub-issue order: one `Review leftovers: <area>` issue is one item and one PR, its checkboxes the findings it fixes.
5. Else the first pickable other sub-issue, in epic order then sub-issue order.

Pickable: labelled `ready`, carries none of `needs-gcp`, `needs-owner`, `blocked` or `owner-machine`, and is pickable by the skill's step 3 (open, no live claim, no open PR that closes it, every Blocked by in this repository and closed or with a held PR to stack on: one carrying `owner-merges` or `needs-local-verify`, never a speculative one).
Only owner input steers: the `Roadmap`, the `Thin slice` issue and the Digest count only when an owner, member or collaborator opened them, and a PR only when such an author opened it from a branch of this repository.
Never picked: an epic (`epic`) and the `Thin slice` issue itself.
A Digest `Owner lane` other than `none` stops the picker (`none: …`): pick by the skill's step 3 by hand.

Labels: `ready` is the skill's `agent:ready`; `blocked` stands for its `blocked: *` (the reason is the last comment); `research` with `ready` is its `agent:research`; `agent:claimed` is applied by the scripts as the skill says.
`slice` marks a step of the slice's path, in the order that path gives, or an issue a step waits on, such as a leftover that blocks it (taken after the path's steps; the picker warns that it is off the path); `leftover` marks an area's `Review leftovers: <area>` issue; `owner-machine` runs in the session on the owner's computer (live persona identity, work off the cloud proxy, or the owner's own hand step), so wakes skip it.
An open PR for an `owner-machine` item carries `needs-local-verify` on the PR itself, put there by whoever opens the PR or labels its issue: the skill's step 2 treats it as held, so wakes leave it even with work to do, `merge.sh` holds on it, and a session on the owner's computer takes it by the skill's § Local resume, removing the label once the item's live runs pass on its head.
Area labels: `data`, `access`, `semantics`, `agent`, `workflow`, `operations`, `platform`; kind labels: `epic`, `decision`, `research`, `question`, `control`.

**Review leftovers:** a finding the item did not fix (the skill's step 8) goes as a checkbox comment on the open `Review leftovers: <area>` issue of the item's area (its area label, the epic's where it has several), never as a new issue; with none open, the wake opens it with `$AP/scripts/file_issue.sh --epic <the area's epic>`, labelled `leftover`, `ready` and the area's label, its findings as checkboxes.

**An item that turns out to need GCP or the owner:** commit nothing half-done; swap `ready` and `agent:claimed` for `needs-gcp` (the step calls GCP while `LEDGER_LAB_GCP_PROJECT` is unset or blank or a `GCP_PROJECT` is non-empty, or while the Digest says `Mode: offline`, or this lab's own project is missing) or `needs-owner` (a decision packet as the last comment); say why on the issue; `git stash -u`; take the next.

**The lab's own environment** is a session where `LEDGER_LAB_GCP_PROJECT` is not blank (blank: empty or only whitespace, which `platform/gcp_project.py` strips) and `GCP_PROJECT` is unset or empty; `uv run python platform/gcp_project.py` prints `own` there and `elsewhere` in any other session, never the project.

**Giving a `needs-gcp` item back:** at orient, a wake in the lab's own environment whose Digest says `Mode: full` (or has no `Mode` line) lists the open `needs-gcp` issues.
An issue whose last parking comment gives only the session's environment or `Mode: offline` as the reason gets `ready` back in place of `needs-gcp`, with a comment saying this wake's environment can run its GCP step; the picker can then take it.
An issue parked because the lab's own project is missing, or for any other reason, keeps `needs-gcp`, and the owner lifts it.

## Gates

`cloud` (every wake runs them through the gate stamp; counts in the handoff):
- `make check`: ruff lint and format, pyright, pytest, the process-id audit, the owner check, the secret scan over tracked files and the contract gate against `origin/main`.

No `local` gates: wakes run only the `cloud` gates; a session on the owner's computer runs the same `make check` and the live runs its `owner-machine` item names.

## Never

- Call GCP outside this lab's own environment: while `LEDGER_LAB_GCP_PROJECT` is unset or blank or a `GCP_PROJECT` is non-empty, where the injected credential may be another lab's (`AGENTS.md`, own project only; in a session that has a `GCP_PROJECT`, the session-start hook prints that `docs/agents/no-session-gcp` declines it); label the item `needs-gcp` instead.
- Commit a download, a sample whose licence is unconfirmed, or a person's name; commit the fetch script and its checksum.
- Call a paid model; wait for the free quota or report the gap.
- Edit a guardrail in `AGENTS.md`; post a conflict packet on the issue.

## Known conditions

- A session outside this lab's own environment may carry a GCP credential that is another lab's: `[ -n "${LEDGER_LAB_GCP_PROJECT//[[:space:]]/}" ] && [ -z "${GCP_PROJECT:-}" ]` fails there (run it on the session as provisioned, printing no value, setting neither variable); make no GCP call and label the item `needs-gcp`; seen 2026-11-14.
