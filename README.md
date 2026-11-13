# ledger-lab

A finance data platform built on real public books.
It turns public purchase-to-pay records into certified data products, gives each persona access that a test can prove, puts a governed data agent in front of them, and adds an agent that proposes journal entries while a named human approves.

[`ARCHITECTURE.md`](ARCHITECTURE.md) holds the design and the reason for each choice.

## What runs where

- **GCP free tier, native first:** BigQuery for the warehouse and its row, column and masking rules, Cloud Storage for landed files, Cloud Run for services, and other managed services only where they are free.
- **Containers:** a stand-in for each service that is not free, behind the same seam, so the swap is configuration; the orchestrator and the open-source ERP run here too.
- **Cloud sessions:** everything is developed and checked in Claude Code cloud sessions; nothing needs a local machine.
- **Models:** free models only.

## Data

| Source | Brings |
|---|---|
| City of Chicago contracts and payments | the contract to voucher to payment chain, in US dollars |
| UK Department for Transport spend | a second entity, in pounds, on an April to March year |
| Dutch central government budget and outturn | a third entity, in euros, with several budget moments a year |
| European Central Bank reference rates | currency translation |
| GLEIF legal-entity register | matching vendor names to legal entities |
| Annual filings of listed companies, from the regulator | an independent figure to tie out against |

Every figure is a public record, fetched by a script that checks it against a recorded checksum; no data is committed.
The ledger is derived: an open-source ERP posts the real documents into journal lines, and every derived row says so.
Individuals among vendors are hashed before anything is written.

City of Chicago data comes from the City's data portal under its [Data Terms of Use](https://www.chicago.gov/city/en/narr/foia/data_disclaimer.html):

> This site provides applications using data that has been modified for use from its original source, www.cityofchicago.org, the official website of the City of Chicago. The City of Chicago makes no claims as to the content, accuracy, timeliness, or completeness of any of the data provided at this site. The data provided at this site is subject to change at any time. It is understood that the data provided at this site is being used at one's own risk.

Legal-entity records come from the GLEIF golden copy under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) and the [LEI data terms of use](https://www.gleif.org/en/meta/lei-data-terms-of-use).
What lands here is a subset the lab derives from it (three countries, selected fields), not GLEIF's own data, and GLEIF does not endorse this project.

To land a source, set `LEDGER_LAB_HASH_KEY` (see `ingest/individuals.py`) and run:

```bash
uv run python -m ingest.fetch chicago-contracts   # lands landing/chicago-contracts as Parquet
uv run python -m ingest.fetch gleif-legal-entities  # lands landing/gleif-legal-entities as Parquet
uv run pytest -m landed                          # profiles what is landed
```

## Checks

Prerequisites: [uv](https://docs.astral.sh/uv/), GNU make, git, and a clone of the agent rules collection, which holds the process-id and owner checks (`make check LOADOUT=<clone>` when the Makefile does not find it).

```bash
uv sync
make check        # lint, types, tests, process-id and owner checks, secret scan
make fresh-clone  # clone HEAD and prove it holds no secret, identifier or data
```

The dbt models build on DuckDB in the session by default; setting `LEDGER_LAB_GCP_PROJECT` (with `uv sync --group bigquery`) moves them to BigQuery:

```bash
uv run dbt build --project-dir models --profiles-dir models
```

Each source is fetched by name from [`ingest/sources.yaml`](ingest/sources.yaml), which records its URL or pinned query, SHA-256, licence link, publisher and whether it names people; the file lands under `LEDGER_LAB_LANDING` (default `landing/`, outside git) only when its checksum matches, and the command exits non-zero otherwise:

```bash
uv run python -m ingest.fetch <source>
```

The container stack needs Docker with Compose; the shared database always starts, every other service only under its profile:

```bash
make stack-up                 # the shared database, one database per planned service
make stack-up PROFILE=<name>  # plus one service
make stack-test               # start the stack and check the running database
make stack-down               # stop everything and drop its volumes
```

The database password comes from `LEDGER_LAB_DB_PASSWORD` when it is set, else from a file `make stack-up` generates (ignored by git, deleted by `make stack-down`); `make stack-up` prints the loopback port it took (`LEDGER_LAB_DB_PORT` fixes one).
After changing the password or the database list, run `make stack-down` first, because the server keeps what its volume was created with.

The lab's GCP project comes only from `LEDGER_LAB_GCP_PROJECT`; `make cost-check` prints its month-to-date cost (with `LEDGER_LAB_BILLING_EXPORT` naming the billing export's `dataset.table`) and each spend fence's state, read live, and fails above $0.00 or on a loose fence; it prints $0.00 without any GCP call while no project is set.
The project's spend fences, one service account per persona, the agent account's right to mint their tokens and the landing bucket live in `platform/gcp/` as OpenTofu definitions; `make fences-validate` installs the pinned OpenTofu and validates them offline, and only the owner applies them.
Applying takes the project, the billing account and the agent account's id as variables (`-var project=... -var billing_account=... -var agent_account_id=...`); the agent account itself is made by hand and only referenced.
Before applying, the owner enables the Service Account Credentials API (`gcloud services enable iamcredentials.googleapis.com`), which minting a persona's token needs and which the agent cannot enable itself.
Each persona account's id is its id in `access/personas.yaml`, so renaming or removing a persona there deletes that account at the next apply, and a rename creates a new one in its place.

## Licence

[MIT](LICENSE).
The data keeps its publishers' licences; each source's licence is confirmed before any sample of it enters the tree.
