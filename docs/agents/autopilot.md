# Autopilot profile — ledger-lab

Unattended cloud wakes on this repo follow the `cloud:autopilot` skill (if missing, read `cloud/skills/autopilot/SKILL.md` in the collection's clone, which `~/.claude/CLAUDE.md` names) with the slots below.
A wake with no `~/.claude/cloud-target` (the environment's setup didn't run) first runs `bash <clone>/cloud/setup.sh ledger-lab`.
The routine prompt: "Follow `docs/agents/autopilot.md` in ledger-lab and do one wake."
Standing permission: push the train branch, open and edit the train PR, issues and labels, comment, without asking.

## Delivery

`train claude/autopilot-train`: one open train PR that every wake lands on; `export AUTOPILOT_TRAIN=claude/autopilot-train` before the scripts.

## Digest

Open owner questions: the train PR body's `## Owner decisions`, under the steering lines `Mode:` and `Owner lane:` (SKILL.md § Steering); before the train PR exists, none.
`Mode: offline` means no GCP call and no fetch from a data source's host; no `Mode` line means `full`.

## Merge policy

The owner merges the train PR with a merge commit; a wake never merges.

## Queue

1. The open issue titled `Roadmap`: its sub-issues are the epics, in order.
2. Take the first open sub-issue, in epic order then sub-issue order, that is labelled `ready`, carries none of `needs-gcp`, `needs-owner`, `blocked` or `in-train`, has no live claim, and is pickable by the skill's step 4 (no open PR closes it, every `Blocked by` closed or `in-train`).
3. Claim it with `claim.sh` before any other write; exit 2 means another wake holds it: take the next.

One work item lands per wake; an item parked below does not count, so the wake takes the next.
Nothing takeable: say so in one line and end, without filing issues.

Labels: `ready` is the skill's `agent:ready`; `blocked` stands for its `blocked: *` (the reason is the last comment); `research` with `ready` is its `agent:research`; `agent:claimed` and `in-train` are applied by the scripts as the skill says.
Area labels: `data`, `access`, `semantics`, `agent`, `workflow`, `operations`, `platform`; kind labels: `epic`, `decision`, `research`, `question`, `control`.

**An item that turns out to need GCP or the owner:** commit nothing half-done; swap `ready` and `agent:claimed` for `needs-gcp` (this lab's own project is missing, or the step is a live GCP step) or `needs-owner` (a decision packet as the last comment); say why on the issue; `git stash -u`; take the next.

## Gates

`cloud` (every wake runs them and quotes the counts in the handoff):
- `make check`: ruff lint and format, pyright, pytest, the process-id audit, the owner check and the secret scan over tracked files.

No `local` gates: cloud is the only environment.

## Never

- Call GCP while `LEDGER_LAB_GCP_PROJECT` is unset, or with the session's `GCP_PROJECT` or injected credential (`AGENTS.md`, own project only); label the item `needs-gcp` instead.
- Commit a download, a sample whose licence is unconfirmed, or a person's name; commit the fetch script and its checksum.
- Call a paid model; wait for the free quota or report the gap.
- Edit a guardrail in `AGENTS.md`; post a conflict packet on the issue.

## Known conditions

- The `ledger-lab` environment sets `LEDGER_LAB_GCP_PROJECT`, `LEDGER_LAB_BQ_LOCATION` and `LEDGER_LAB_BILLING_EXPORT` (a view of the lab's own rows of the billing export) and no `GCP_PROJECT`; its injected `*.googleapis.com` credential is the lab's own agent service account.
