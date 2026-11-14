# Autopilot profile — ledger-lab

Unattended cloud wakes on this repo follow the `cloud:autopilot` skill (if missing, read `cloud/skills/autopilot/SKILL.md` in the collection's clone, which `~/.claude/CLAUDE.md` names) with the slots below.
A wake with no `~/.claude/cloud-target` (the environment's setup didn't run) first runs `bash <clone>/cloud/setup.sh ledger-lab`.
The routine prompt: "Follow `docs/agents/autopilot.md` in ledger-lab and do one wake." (optionally "on #n").
Standing permission: push `claude/<n>-<slug>` branches, open and edit their PRs, post their gates and review records, merge them under the merge policy below, edit issues and labels, and comment, without asking.
Every item is one branch `claude/<n>-<slug>` and one PR, reviewed by the `review` skill and merged by the wake once its gates and review records pass, unless the merge policy holds it.

## Merge policy

None beyond the skill's holds: the wake merges a PR once its gates and review records pass on its head.
The owner keeps what `AGENTS.md` holds for them: a PR that changes a line of `AGENTS.md § Guardrails`, is costly to undo or costs money carries `needs-owner` on the PR and its issue, with the decision packet on the issue, and only the owner merges it.

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

- Call GCP while `LEDGER_LAB_GCP_PROJECT` is unset, or with the session's `GCP_PROJECT` or injected credential (`AGENTS.md`, own project only; `docs/agents/no-session-gcp` declines it at session start); label the item `needs-gcp` instead.
- Commit a download, a sample whose licence is unconfirmed, or a person's name; commit the fetch script and its checksum.
- Call a paid model; wait for the free quota or report the gap.
- Edit a guardrail in `AGENTS.md`; post a conflict packet on the issue.

## Known conditions

- The `ledger-lab` environment sets `LEDGER_LAB_GCP_PROJECT`, `LEDGER_LAB_BQ_LOCATION` and `LEDGER_LAB_BILLING_EXPORT` (a view of the lab's own rows of the billing export) and no `GCP_PROJECT`; its injected `*.googleapis.com` credential is the lab's own agent service account.
