# Review

The paths whose change takes the sensitive review tier, and the lenses each adds, beside the floor the `review` skill always applies.
The skill's `tier.sh` reads the list below from the default branch; with no `## Routine` list, prose documentation outside these paths is routine and every other path is standard.
Each item is backticked globs and its lenses only, the form `tier.sh` parses; `## What each item guards` gives the reason for each, in the same order.

## Sensitive
- `ingest/individuals.py`, `ingest/entities.py`, `ingest/scrub.py`, `tests/ingest/test_individuals.py`, `tests/ingest/test_entities.py`, `tests/ingest/test_scrub.py`; lenses: leak-hunter, control-prober
- `ingest/**`, `tests/ingest/**`, `models/tests/**/*hashed*.sql`; lenses: leak-hunter
- `models/models/staging/**`, `models/models/intermediate/**`, `models/models/marts/**`, `models/models/reconciliations/**`, `models/macros/vendor_match.sql`, `models/contracts/payments.odcs.yaml`, `tests/models/**`; lenses: leak-hunter
- `models/seeds/**`, `tests/models/*_sample.*`; lenses: leak-hunter, guardrail-reviewer
- `ingest/fetch.py`, `ingest/bucket.py`, `ingest/sources.yaml`, `ingest/sources.schema.json`, `tests/ingest/test_fetch.py`, `tests/ingest/test_bucket.py`; lenses: control-prober, guardrail-reviewer
- `access/personas.yaml`, `access/schema.json`, `controls/**`, `tests/test_access.py`, `tests/test_controls.py`; lenses: guardrail-reviewer, control-prober, claims-auditor
- `access/**`, `tests/access/**`, `tests/platform/test_access_rules.py`, `tests/platform/predefined_roles.json`; lenses: guardrail-reviewer, leak-hunter, control-prober
- `platform/gcp/**`; lenses: guardrail-reviewer, control-prober
- `platform/cost_check.py`, `platform/fences.py`, `platform/gcp_project.py`, `tests/platform/test_cost_check.py`, `tests/platform/test_fences.py`, `tests/platform/test_gcp_project.py`; lenses: guardrail-reviewer, control-prober
- `platform/fresh_clone.py`, `tests/test_fresh_clone.py`, `tests/test_secrets.py`, `.gitignore`, `.process-ids`, `Makefile`, `semantics/offline/**`; lenses: guardrail-reviewer, control-prober
- `agent/evals/**`, `tests/agent/test_adversarial_cases.py`; lenses: control-prober

## What each item guards

1. Hashing: the rule that a vendor or payee is a person unless the evidence says otherwise, the keyed hash, the free-text scrub, and their tests.
2. Landing: every source's step that writes landed rows, the quality suites that may only count a column holding a name, the landed leak hunts, and the dbt tests that no identifier sits in clear beside a hashed name.
3. The models that carry landed payees, suppliers and document numbers, hashed or in clear, into the data products, the payments contract that names the personal columns, and their tests.
4. Committed samples of real rows: each needs its licence confirmed, and none may carry a person in clear or a hash made with a real key.
5. The fetch-and-verify kit: the recorded sources and checksums, the download that refuses bytes it cannot verify, and the bucket landing.
6. The persona and controls catalogues: who holds which duty and grant, and a control marked implemented only beside its proving test.
7. Access and identity definitions: the column tags, visibility matrix, aggregates, access review, the persona identities and the BigQuery grants generated from them.
8. The lab project's definitions: its grants, budget, quotas and kill switch.
9. Cost and fence checks: the month-to-date cost, the spend fences read offline, and the one place that names the lab's own project.
10. The guards of a public repo and its gates: the fresh-clone and secret scans, the ignore rules that keep data out of git, the process-id exemptions, the `make check` targets, and the refusal of network calls around the metric tools.
11. The agent's frozen adversarial cases and the test that scores them.
