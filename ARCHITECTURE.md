# Architecture

The current design, and each major choice with its reason and what it rejected.
Rewritten in place: it describes the system as it is now.

## Components and seams

Real public documents are fetched, landed and loaded into the warehouse; an open-source ERP posts them into journal lines; layered models turn both into certified data products; governed metrics sit on top; one agent answers from them and another drafts entries a named human approves.
A control plane (identity, tool policy, approval, guardrails, audit, traces, evals, cost caps) spans every agent call.

| Component | Runs on | Seam | State |
|---|---|---|---|
| `ingest/`: download scripts with checksums, landing | the session, then Cloud Storage | a landing path from the environment | not built yet |
| Warehouse | BigQuery | the project and datasets from the environment | not built yet |
| `models/`: layered dbt models, contracts, quality checks, reconciliations | BigQuery | dbt profiles from the environment | not built yet |
| ERP: posting engine and write target | Odoo in containers | the ERP's own API | not built yet |
| Orchestrator | Airflow in containers | one DAG per pipeline calling the pieces above | not built yet |
| `access/`: personas, their grants, policies, visibility tests | BigQuery row, column and masking rules | [`access/personas.yaml`](access/personas.yaml) | persona set and mapping built as data; identities and policies not built yet |
| `semantics/`: governed metrics, glossary, verified queries | the platform's catalog, or a stand-in | metric definitions in the tree | not built yet |
| `agent/`: the governed data agent | the native data agent, own agent beside it | one question-to-answer interface | not built yet |
| `workflow/`: proposals and human approval | containers | one proposal-to-approval interface | not built yet |
| `controls/`: the controls catalogue | the tree | [`controls/catalogue.yaml`](controls/catalogue.yaml) | catalogue built as data; no control implemented yet |
| `platform/`: GCP definitions, container stack, cost fences | GCP free tier and the session | configuration from the environment | not built yet |

The catalogues are YAML validated by a JSON Schema beside each file, and `tests/` checks their cross-references: every grant names a known persona, entity and product, every control's owner is a human persona, and a control is marked implemented only once its proving test exists.

## Choices

- **Real public data, not generated data:** real records carry the revisions, negative awards, missing identifiers and revised figures that generated data leaves out, and those are what the controls must survive; rejected: generated data (clean by construction, so it proves nothing about mess), a well-known marketplace dataset (its non-commercial licence would bind everything derived from it).
- **Three public entities as the row boundary: City of Chicago (USD), UK Department for Transport (GBP), Dutch central government (EUR):** Chicago brings the only public contract to voucher to payment chain, the UK a second file layout and an April to March year, the Netherlands budget against outturn; together they give three currencies and three fiscal calendars to translate and consolidate; rejected: one entity (no row boundary, no translation), a private company's ledger (none is public).
- **Reference data from its publishers:** ECB reference rates translate currency, the GLEIF register matches vendor names to legal entities, and annual filings from the securities regulator give an independent figure to tie out against; rejected: a public warehouse copy of the filings (stale for years).
- **An open-source ERP (Odoo) as posting engine and write target:** it posts the real documents into journal lines by real accounting rules, loaded as SAP-shaped ledger tables, and is the system the acting agent drafts into; rejected: hand-written posting rules (accounting the lab invents), a vendor ERP (licensed and not free).
- **Native GCP services only where free, a stand-in behind the same seam otherwise:** BigQuery enforces row, column and masking rules itself, which is the point; the platform's catalog, lineage and native data agent are used only once they prove free; rejected: stand-ins for everything (they prove nothing about the real platform's enforcement), paid managed services (the lab spends $0.00).
- **Personas as service identities that tests impersonate, with a mapping table in place of a group directory:** a test acts as a persona and asserts what the warehouse returns, and the persona to entity to data product table in `access/` plays the directory's role; rejected: a group directory (needs an organisation and an administrator the lab does not have), personal accounts (need real people and email addresses in a public repo).
- **Orchestration in plain containers, not Kubernetes:** Airflow, the ERP and their shared database fit one cloud session, and a cluster would teach nothing this lab is for; rejected: a managed cluster (not free), a local cluster (spends the session's memory on plumbing).
- **The native data agent first, an own agent as the comparison:** the native agent runs on the asking person's credentials, so the warehouse's rules apply unchanged; an own agent over the same metrics measures right answers, confident wrong answers, cost and auditability against it; rejected: an own agent only (rebuilds what the platform owns), the native agent only (no yardstick).
- **Catalogues as data checked by tests:** the persona mapping and the controls catalogue are read by the tests that will prove them, so a claim and its proof cannot drift apart; rejected: tables in prose (nothing checks them).
- **Secret scan with detect-secrets pinned in the lock:** a plain clone runs it after `uv sync`; rejected: a scanner binary installed outside the project (absent in a plain clone).
- **The roadmap and decisions on the tracker:** epics with sub-issues hold the plan and progress, a decision is weighed on its issue, and only its outcome lands here; rejected: plan and decision files (appended to, they go stale and pull the tree into its own history).
