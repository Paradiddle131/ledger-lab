# Review

The paths whose change takes the sensitive review tier, and the lenses each adds, beside the floor the `review` skill always applies.
The list holds only the three areas whose defects using the system does not show: a person left in clear, a reader who sees more than their grant, and a step that spends money or reaches another project.
The floor is sensitive whatever this file says: agent instruction and rule files such as `AGENTS.md`, `CLAUDE.md` and this file, hook folders, and `docs/agents/**`; it adds no lenses.
The skill's `tier.sh` reads the list below from the default branch; with no `## Routine` list, prose documentation outside the floor and these paths is routine (gates only), and every other path is standard (one reviewer and a verifier).
Each item is backticked globs and its lenses only, a form every version of `tier.sh` parses; `## What each item guards` gives the reason for each, in the same order.

## Sensitive
- `ingest/individuals.py`, `ingest/entities.py`, `ingest/scrub.py`, `tests/ingest/test_individuals.py`, `tests/ingest/test_entities.py`, `tests/ingest/test_scrub.py`; lenses: leak-hunter, control-prober
- `access/**`, `tests/access/**`, `tests/test_access.py`, `tests/platform/test_access_rules.py`, `tests/platform/predefined_roles.json`, `platform/gcp/warehouse.tf`, `platform/gcp/identities.tf`, `platform/gcp/columns.tf`, `models/macros/mask.sql`, `models/models/marts/payments/payments_masked.sql`, `models/models/marts/payments/_payments_masked__models.yml`; lenses: guardrail-reviewer, leak-hunter, control-prober
- `platform/gcp/**`, `platform/fences.py`, `platform/gcp_project.py`, `models/profiles.yml`, `tests/platform/test_fences.py`, `tests/platform/test_gcp_project.py`, `tests/platform/test_own_project_lines.py`; lenses: guardrail-reviewer, control-prober

## What each item guards

1. Person hashing and scrubbing: the rule that a vendor or payee is a person unless the evidence says otherwise, the keyed hash, the free-text scrub, and their tests.
2. Access: the persona catalogue and its schema, the persona identities every access test acts as, the grants and the BigQuery datasets, accounts and policy tags generated from them, the column tags and the masked view their masked readers read, the visibility matrix, the aggregates and the views that serve aggregate readers, the purposes' views, the row access policies and the access review, with their tests.
3. Spend and project fences: the lab project's definitions (its budget, quotas, kill switch and landing bucket), the spend fences read offline, the module that names the lab's own project and the rule lines held to it, and the dbt profile that reads the same variable and caps every BigQuery job's billed bytes, with their tests.
