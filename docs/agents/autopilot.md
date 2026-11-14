# Autopilot profile — ledger-lab

Unattended cloud wakes on this repo follow the `cloud:autopilot` skill (if missing, read `cloud/skills/autopilot/SKILL.md` in the collection's clone, which `~/.claude/CLAUDE.md` names) with the slots below.
A wake with no `~/.claude/cloud-target` (the environment's setup didn't run) first runs `bash <clone>/cloud/setup.sh ledger-lab`.
The routine prompt: "Follow `docs/agents/autopilot.md` in ledger-lab and do one wake." (optionally "on #n").
Standing permission: push the item branches `claude/<n>-<slug>`, open and edit their PRs, issues and labels, comment, and merge under the merge policy below, without asking.
Every item is one branch `claude/<n>-<slug>` and one PR, reviewed by the `review` skill and merged by the wake once its gates and review records pass, unless the merge policy holds it.

## Merge policy

The wake merges a PR once its gates and review records pass on its head, unless the skill's holds or the one below hold it.
The owner keeps what `AGENTS.md` holds for them: a PR that changes a line of `AGENTS.md § Guardrails`, is costly to undo or costs money gets the label `owner-merges` on the PR itself, and its issue gets the decision packet and `needs-owner`.
`merge.sh` reads the PR's own labels and holds on `owner-merges`; `export AUTOPILOT_HOLD_LABELS=owner-merges` before the scripts keeps that hold with a copy of `merge.sh` that does not list the label itself.
A wake never removes `owner-merges`; only the owner's own merge lands such a PR.
PRs opened before this policy stay the owner's to merge.
The owner confirms this policy, and the merges it lets a wake make, by merging the change that sets it; that change's PR asks them to.

## Review

The paths that set a review tier: `REVIEW.md` at the repo root, read from the default branch by the `review` skill's `tier.sh`.

## Digest

Steering lines (SKILL.md § Steering) at the top of the body of the open issue titled `Autopilot digest`, which the owner pins and only the owner edits: `Mode: full` and `Owner lane: none`.
`Mode: offline` means no GCP call and no fetch from a data source's host; no `Mode` line means `full`.
Open owner questions are the `needs-owner` issues.

## Queue

1. The open issue titled `Roadmap`: its sub-issues are the epics, in order.
2. Take the first open sub-issue, in epic order then sub-issue order, that is labelled `ready`, carries none of `needs-gcp`, `needs-owner` or `blocked`, has no live claim, and is pickable by the skill's step 3.

Labels: `ready` is the skill's `agent:ready`; `blocked` stands for its `blocked: *` (the reason is the last comment); `research` with `ready` is its `agent:research`; `agent:claimed` is applied by the scripts as the skill says.
Area labels: `data`, `access`, `semantics`, `agent`, `workflow`, `operations`, `platform`; kind labels: `epic`, `decision`, `research`, `question`, `control`.

**An item that turns out to need GCP or the owner:** commit nothing half-done; swap `ready` and `agent:claimed` for `needs-gcp` (this lab's own project is missing, or the step is a live GCP step) or `needs-owner` (a decision packet as the last comment); say why on the issue; `git stash -u`; take the next.

## Gates

`cloud` (every wake runs them through the gate stamp; counts in the handoff):
- `make check`: ruff lint and format, pyright, pytest, the process-id audit, the owner check, the secret scan over tracked files and the contract gate against `origin/main`.

No `local` gates: cloud is the only environment.

## Never

- Call GCP while `LEDGER_LAB_GCP_PROJECT` is unset, or with the session's `GCP_PROJECT` or injected credential (`AGENTS.md`, own project only; in a session that has a `GCP_PROJECT`, the session-start hook prints that `docs/agents/no-session-gcp` declines it); label the item `needs-gcp` instead.
- Commit a download, a sample whose licence is unconfirmed, or a person's name; commit the fetch script and its checksum.
- Call a paid model; wait for the free quota or report the gap.
- Edit a guardrail in `AGENTS.md`; post a conflict packet on the issue.

## Known conditions

- Which environment a session is in: this lab's own environment sets `LEDGER_LAB_GCP_PROJECT` (with `LEDGER_LAB_BQ_LOCATION` and `LEDGER_LAB_BILLING_EXPORT`) and no `GCP_PROJECT`; check with `[ -n "${LEDGER_LAB_GCP_PROJECT:-}" ] && [ -z "${GCP_PROJECT:-}" ]`, printing no value.
  In any other session (`LEDGER_LAB_GCP_PROJECT` unset, or `GCP_PROJECT` set) the injected `*.googleapis.com` credential is not this lab's: make no GCP call and label the item `needs-gcp`.
  In the lab's own environment the GCP project is the one `LEDGER_LAB_GCP_PROJECT` names (`platform/gcp_project.py`); which account its injected credential is cannot be told without a GCP call, so this profile does not state it.
