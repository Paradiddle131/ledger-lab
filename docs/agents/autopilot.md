# Autopilot profile — ledger-lab

Unattended cloud wakes on this repo follow the `cloud:autopilot` skill (if missing, read `cloud/skills/autopilot/SKILL.md` in the collection's clone, which `~/.claude/CLAUDE.md` names) with the slots below.
A wake with no `~/.claude/cloud-target` (the environment's setup didn't run) first runs `bash <clone>/cloud/setup.sh ledger-lab`.
The routine prompt: "Follow `docs/agents/autopilot.md` in ledger-lab and do one wake." (optionally "on #n").
Standing permission: push the item branches `claude/<n>-<slug>` and the existing branch of an open agent PR being resumed, open and edit their PRs, issues and labels, comment, and merge under the merge policy below, without asking.
In this lab's own environment (below, `Never`), a wake may also mint access tokens with the agent's credential, each for at most 3600 seconds and with the BigQuery scope only, for the persona service accounts that `platform/gcp/identities.tf` on the default branch declares, and use them only in the tests under `tests/access/` marked `warehouse`, which act as a persona through the `as_persona` fixture, run on a head whose review verdict has passed; a minted token carries the persona's own grants (`platform/gcp/warehouse.tf`, `platform/gcp/columns.tf`). The owner allowed this for the persona tests on BigQuery.
The session's tool-permission check may still refuse that call: the refusal is the answer, so the wake tries no other client or path, writes on the item that the call was refused and the refusal's reason, naming no account or project, and labels it `needs-owner`.
Every item is one branch and one PR (a new item's branch is `claude/<n>-<slug>`; an open agent PR being resumed keeps its branch), reviewed by the `review` skill and merged by the wake once its gates and review records pass, unless the merge policy holds it.

## Merge policy

The wake merges a PR once its gates and review records pass on its head, unless the skill's holds or the one below hold it.
The owner keeps what `AGENTS.md` holds for them: a PR that changes a line of `AGENTS.md § Guardrails`, is costly to undo or costs money gets the label `owner-merges` on the PR itself, and its issue gets the decision packet and `needs-owner`.
`merge.sh` reads the PR's own labels and holds on `owner-merges`; `export AUTOPILOT_HOLD_LABELS=owner-merges` before the scripts keeps that hold with a copy of `merge.sh` that does not list the label itself.
A wake never removes `owner-merges`; only the owner's own merge lands such a PR.
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

**An item that turns out to need GCP or the owner:** commit nothing half-done; swap `ready` and `agent:claimed` for `needs-gcp` (the step calls GCP while `LEDGER_LAB_GCP_PROJECT` is unset or empty or a `GCP_PROJECT` is set, or while the Digest says `Mode: offline`, or this lab's own project is missing) or `needs-owner` (a decision packet as the last comment); say why on the issue; `git stash -u`; take the next.

## Gates

`cloud` (every wake runs them through the gate stamp; counts in the handoff):
- `make check`: ruff lint and format, pyright, pytest, the process-id audit, the owner check, the secret scan over tracked files and the contract gate against `origin/main`.

No `local` gates: cloud is the only environment.

## Never

- Call GCP outside this lab's own environment: while `LEDGER_LAB_GCP_PROJECT` is unset or empty or a `GCP_PROJECT` is set, where the injected credential may be another lab's (`AGENTS.md`, own project only; in a session that has a `GCP_PROJECT`, the session-start hook prints that `docs/agents/no-session-gcp` declines it); label the item `needs-gcp` instead.
- Commit a download, a sample whose licence is unconfirmed, or a person's name; commit the fetch script and its checksum.
- Call a paid model; wait for the free quota or report the gap.
- Edit a guardrail in `AGENTS.md`; post a conflict packet on the issue.

## Known conditions

- The tool-permission check refused minting a persona token (`generateAccessToken`) in a wake, and again in a session where the owner had allowed it in chat, while `testIamPermissions` on one persona account showed the agent holds `iam.serviceAccounts.getAccessToken` there: the check refused the call, not IAM on that account; workaround: none in the session, so the item waits on `needs-owner` for the owner to allow the call where the check reads it; seen 2026-11-17.
- A session outside this lab's own environment may carry a GCP credential that is another lab's: `[ -n "${LEDGER_LAB_GCP_PROJECT:-}" ] && [ -z "${GCP_PROJECT:-}" ]` fails there (run it on the session as provisioned, printing no value, setting neither variable); make no GCP call and label the item `needs-gcp`; seen 2026-11-14.
