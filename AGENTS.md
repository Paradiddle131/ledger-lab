# ledger-lab

A finance data platform on real public books; [`README.md`](README.md) says what it is, [`ARCHITECTURE.md`](ARCHITECTURE.md) holds the design and each choice's reason: read both before changing anything.

Ways of working: [ways](https://github.com/<owner>/<collection>/tree/main/ways) (on cloud, the `ways/` folder of the clone `~/.claude/CLAUDE.md` names); this file wins where it is more specific.

## Guardrails
The owner decides these; when evidence conflicts with one, post a conflict packet and keep working on the rest ([ways/guardrails.md](https://github.com/<owner>/<collection>/blob/main/ways/guardrails.md)). Everything else is a default: a choice in the architecture document.
- **$0.00:** every GCP step uses free-tier services only, in the regions their free tier names, with the cost read before and after; never raise a quota, cap or budget; any cost above zero stops GCP work and goes first in the handoff.
- **Own project only:** no GCP call (`gcloud`, `bq`, `gsutil`, a client library, `*.googleapis.com`) until the owner has created this lab's project and set `LEDGER_LAB_GCP_PROJECT`; the session's `GCP_PROJECT` and injected credential may belong to another lab and are never used here; work that needs GCP is labelled `needs-gcp`.
- **Free models only:** every model call uses a free model or free allowance; a spent free quota means wait or report the gap, never a paid model.
- **Public from the first commit:** no data, secret, key, project id, account name, email or owner login in any file; configuration comes from the environment; a fresh clone holds no secret, no identifier and no data.
- **Real data by script:** sources are fetched by a script that verifies a recorded checksum; downloads stay outside git (`.gitignore`).
- **Individuals hashed:** a vendor or payee who is a person is replaced by a keyed hash (key from the environment) before anything is written, landing included.
- **Licences first:** a source's licence is confirmed, with its link on the source's issue, before any sample of it is committed.
- **The repo says what it does:** README and docs describe the system and how to run it, never why the owner built it.

## Decisions while the owner is away
- A reversible choice is yours: put the options and your recommendation on its issue, take it, and land the outcome as an `ARCHITECTURE.md` line in the same PR.
- A choice that is costly to undo, costs money, or changes a guardrail waits: post a decision packet on its issue, label it `needs-owner`, take the next item.

## Work
- Unattended sessions pick, claim and land work by [`docs/agents/autopilot.md`](docs/agents/autopilot.md).
- Roadmap, progress, research and decisions live on the tracker as epics and sub-issues, never as files; `make ids` refuses a process id or a record file.
- A new top-level folder holds something real, and its `ARCHITECTURE.md` row changes in the same PR.
- The catalogues are data: change `access/personas.yaml` or `controls/catalogue.yaml` with its schema and tests; mark a control implemented in the PR that adds its proving test.

## Commands
- `uv sync`: the pinned Python and tools.
- `make check`: every fast gate (`lint`, `typecheck`, `test`, `ids`, `portable`, `scan` for secrets); each is also its own target.
