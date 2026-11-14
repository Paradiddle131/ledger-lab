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
| Annual filings of listed companies, from the regulator | an independent figure to tie out against: the revenue twenty listed vendors reported in their annual reports, each restated or amended value beside the original |

Every figure is a public record, fetched by a script that checks it against a recorded checksum; no data is committed but a sample whose licence allows it and a test needs (ECB rates of a fortnight of 2024, a day of 2023 and March 2026, some rows of the Dutch budget statements, some rows of Chicago's contracts and payments, some rows of the UK Department for Transport's spend, and a few rows of the regulator's filings).
The ledger is derived: an open-source ERP posts the real documents into journal lines, and every derived row says so.
Individuals among vendors are hashed before anything is written.

City of Chicago data comes from the City's data portal under its [Data Terms of Use](https://www.chicago.gov/city/en/narr/foia/data_disclaimer.html):

> This site provides applications using data that has been modified for use from its original source, www.cityofchicago.org, the official website of the City of Chicago. The City of Chicago makes no claims as to the content, accuracy, timeliness, or completeness of any of the data provided at this site. The data provided at this site is subject to change at any time. It is understood that the data provided at this site is being used at one's own risk.

The euro reference rates come from the ECB data API under the ECB's [copyright and disclaimer](https://www.ecb.europa.eu/home/disclaimer/html/index.en.html); Source: European Central Bank.
The ECB publishes them for information purposes only, and a figure translated with them is derived from them.

Legal-entity records come from the GLEIF golden copy under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) and the [LEI data terms of use](https://www.gleif.org/en/meta/lei-data-terms-of-use).
What lands here is a subset the lab derives from it (three countries, selected fields), not GLEIF's own data, and GLEIF does not endorse this project.
Records that may name a person (sole proprietors, partnerships that are no separate legal person, and records without a real legal-form code whose legal name is not evidence of an organisation) and person-like other names are left out; legal names of records with a real legal-form code land as the register records them, and a record without a real legal-form code never vouches that a name is an organisation.

Annual filings come from the U.S. Securities and Exchange Commission's [Financial Statement Data Sets](https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets): information on sec.gov is public and may be copied or further distributed ([privacy and dissemination](https://www.sec.gov/privacy)); Source: U.S. Securities and Exchange Commission (EDGAR).
What lands is the lab's subset of four quarterly files (twenty companies' revenue), not the SEC's own data, and the SEC does not endorse this project.
The fetch sends four requests, one at a time, under a fixed client name, within the SEC's [fair-access rules](https://www.sec.gov/os/accessing-edgar-data).

The UK Department for Transport's spending over £25,000 comes from its monthly files on [GOV.UK](https://www.gov.uk/government/collections/dft-departmental-spending-over-25000): Contains public sector information licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
What lands is the twelve files of one fiscal year in one shape, with every supplier who may be a person hashed and what describes or locates them left out.

To land a source, run its line below; a source that names people (Chicago's contracts and payments, the UK spend) also needs `LEDGER_LAB_HASH_KEY` set (see `ingest/individuals.py`) and the legal-entity register landed first, since a payee the register vouches for as an organisation lands in clear (see `ingest/entities.py`).
With one key, a name hashes the same in every landing; a new key changes every hash, and with it the `payment_key` of each payments-mart row whose payee is hashed, since the key hashes the row as landed.
A set's name lands every source whose name it prefixes (`<set>-<member>`) as one file, through the set's step, once every member has verified.
The landed tests need the key the files were landed with for Chicago, and the network for the UK spend, whose files they download again; the pages test reads each pinned UK month's GOV.UK page, and the filings' company-list test builds the vendor matching over the vendors' landed sources and register:

```bash
uv run python -m ingest.fetch gleif-legal-entities  # lands landing/gleif-legal-entities as Parquet
uv run python -m ingest.fetch chicago             # lands landing/chicago-contracts and landing/chicago-payments together as Parquet
uv run python -m ingest.fetch ecb-rates           # lands landing/ecb-rates as Parquet
uv run python -m ingest.fetch nl-government-budget  # lands landing/nl-government-budget as Parquet
uv run python -m ingest.fetch uk-transport-spend-2025  # lands April 2025 to March 2026 as one Parquet set
uv run python -m ingest.fetch sec-annual-filings  # lands the listed companies' annual-report revenue from four quarters as Parquet
uv run python -m ingest.quality <source>         # checks a landed file against its GX Core suite; a set's name checks each of its files
uv run pytest -m landed                          # runs every quality suite on what is landed and checks it for hashed names left in clear
uv run pytest -m pages                           # fails once GOV.UK lists another file or a change note for a pinned month
```

## Checks

Prerequisites: [uv](https://docs.astral.sh/uv/), GNU make, git, and a clone of the agent rules collection, which holds the process-id and owner checks (`make check LOADOUT=<clone>` when the Makefile does not find it).

```bash
uv sync
make check        # lint, types, tests, process-id and owner checks, secret scan, contract gate
make fresh-clone  # clone HEAD and prove it holds no secret, identifier or data
```

The contract gate (`make contract`) matches every data contract (`*.odcs.yaml`, today [`models/contracts/payments.odcs.yaml`](models/contracts/payments.odcs.yaml)) with the one on `origin/main` as this clone holds it, by contract id, and fails on a change that breaks a consumer, or when `origin/main` is not fetched (`git fetch origin main`); it runs offline after `uv sync`.
`make sync` also puts the Data Contract CLI's DuckDB engine (the lock's `contract` group) in uv's cache; the contract tests and `make contract-test`, which runs `datacontract test` over a local dbt build, use it offline on DuckDB 1.5 over a copy of the build in that version's storage format, and the tests skip, saying so, when it is not cached.

The dbt models build on DuckDB in the session by default, reading the landed files in place under `LEDGER_LAB_LANDING`; setting `LEDGER_LAB_GCP_PROJECT` (with `uv sync --group bigquery`) moves them to BigQuery:

```bash
uv run dbt build --project-dir models --profiles-dir models
```

Every model that reads a fetched source reads the file the fetch kit landed (`python -m ingest.fetch ecb-rates`, `python -m ingest.fetch nl-government-budget`, for the payments mart `chicago` and `uk-transport-spend-2025`, for the reconciliation of Chicago's payments to their contracts' awards `chicago`, and for the vendors' legal entities `chicago`, `uk-transport-spend-2025` and `gleif-legal-entities`), and the build fails on a missing file; the tests pass `--vars '{source_rows: sample}'` to read the committed samples of real rows instead (ECB rates of a fortnight of 2024, a day of 2023 and March 2026, Dutch budget rows chosen for their mess, and Chicago contract and payment rows and UK spend rows of companies, each landed through its landing step with a test hash key), and `uv run pytest -m landed` runs the translation and budget rules over the landed files.
The Dutch budget against outturn shows the figures as the file serves them: a zero the file may serve in place of a figure is flagged on its line (`has_doubtful_zero`), and the landed tests count the figures the file serves wrong.
The group totals sum each entity's spend by fiscal period, or by year where the source gives only a year, in euros at actual and at constant rates, each row naming what its figure is (`basis`: Chicago's direct vouchers, contract payments and yearly roll-ups, the UK department's payments over £25,000, the Dutch government's cash-expenditure outturn), and count what they leave out, by rule, beside them (`group_totals_left_out`).

The governed metrics are MetricFlow definitions in `semantics/metricflow/`, each with an owner (the persona who owns its glossary term), a glossary term and a role in its `meta`: a `headline` metric is one to read, an `input` only feeds one.
[`semantics/finance_metrics.yaml`](semantics/finance_metrics.yaml) maps the five promised finance metrics to the headline metrics that answer them:
spend against award, remaining commitment and what was paid over award (the City of Chicago's contracts the contract reconciliation holds to an award above 0, groupable by break class and filterable by end date; by `metric_time` they group contracts by approval cohort, the day of their first revision), the duplicate-payment rate by rows and by amount (Chicago's payments the publisher serves again), and constant-currency spend (the group totals at one day's rates, a figure only where the rows share one basis, so group it by `group_total__basis`; by year or coarser, never by month, since a whole year's figure has no month).
Days to pay is not definable from the public files, which serve one date per payment; the file says why.
A dbt build writes the semantic manifest `mf` reads (`models/target/semantic_manifest.json`).

The `mf` command checks for a newer release of itself on every run, and has no setting to stop it: it sends one HTTPS request to `pypi.org` for `dbt-metricflow`'s public release list (`/pypi/dbt-metricflow/json`, the client library's default user agent, nothing about the project or its data), waits at most one second, keeps the answer for an hour in the system's temporary folder (`update_checker_cache.pkl`), and carries on silently when the request fails.
To run it with no network call, use the runner beside the definitions, which turns that check off and ends the run on any connection or name lookup.
The build below reads the committed samples (`source_rows: sample`), so it needs no landing; its database goes to `target/ledger_lab.duckdb` under the folder you run from, which must exist, hence the `mkdir`:

```bash
mkdir -p target
uv run dbt build --project-dir models --profiles-dir models --vars '{source_rows: sample}'
DBT_PROJECT_DIR=models DBT_PROFILES_DIR=models uv run python -I semantics/offline/offline_mf.py validate-configs
DBT_PROJECT_DIR=models DBT_PROFILES_DIR=models uv run python -I semantics/offline/offline_mf.py query --metrics spend_against_award,remaining_commitment --group-by contract__break_class
DBT_PROJECT_DIR=models DBT_PROFILES_DIR=models uv run python -I semantics/offline/offline_mf.py query --metrics constant_currency_spend --group-by group_total__basis,metric_time__year
DBT_PROJECT_DIR=models DBT_PROFILES_DIR=models uv run mf query --metrics duplicate_payment_rate  # the plain command, update check included
```

`make reconcile-landed` runs the metrics over the landed files beside the reconciliation, each against its pinned value.

Each source is fetched by name from [`ingest/sources.yaml`](ingest/sources.yaml), which records its URL or pinned query, SHA-256, licence link, publisher and whether it names people; the file lands under `LEDGER_LAB_LANDING` (default `landing/`, outside git) only when its checksum matches, and the command exits non-zero otherwise:

```bash
uv run python -m ingest.fetch <source>
```

With the lab's project set, `LEDGER_LAB_LANDING=gs://<lab project>-landing` lands the same files as objects in the landing bucket, named as they are locally, the legal-entity register first as above; the kit refuses any other bucket or a folder inside it.
It sends no credential of its own, so it works only where the session's network proxy adds one to requests to Cloud Storage, as on the lab's cloud sessions.
One landing runs at a time under the lock object `.landing.lock`; a landing that was killed leaves it, and the next one refuses until it is deleted by hand once the landed files are checked.
The models and the landed tests read a local landing and stop on a bucket one; `pytest -m bucket` compares the bucket with one by listing it, reading no object:

```bash
LEDGER_LAB_LANDING=gs://<lab project>-landing uv run python -m ingest.fetch <source>
LEDGER_LAB_LANDING=<local landing> uv run pytest -m bucket  # same names, sizes, MD5 and CRC32C, nothing else in the bucket
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
The project's spend fences, one service account per persona, the agent account's right to mint their tokens, the landing bucket and the warehouse's access rules (its datasets, the agent's builder role on them, the personas' dataset and query grants, and the policy tags with their masking rules and readers, generated from `access/personas.yaml` and `access/columns.yaml`) live in `platform/gcp/` as OpenTofu definitions; `make fences-validate` installs the pinned OpenTofu and validates them offline, and only the owner applies them.
Applying takes the project, the billing account and the agent account's id as variables (`-var project=... -var billing_account=... -var agent_account_id=...`); the agent account itself is made by hand and only referenced.
Before applying, the owner enables the APIs the definitions use, which the agent cannot enable itself: the Service Account Credentials API, which minting a persona's token needs, the Data Catalog API, which holds the policy tags, and the BigQuery Data Policy API, which holds the masking rules (`gcloud services enable iamcredentials.googleapis.com datacatalog.googleapis.com bigquerydatapolicy.googleapis.com`).
The Data Lineage API (`datalineage.googleapis.com`) and the Dataplex API (`dataplex.googleapis.com`) stay off: lineage parsing and lineage metadata are billed.
Each persona account's id is its id in `access/personas.yaml`, so renaming or removing a persona there deletes that account at the next apply, and a rename creates a new one in its place.

The access review lists every identity that can touch the lab's data or its controls (each persona, the agent account and the kill-switch account) with its kind, owner, catalogue grants, the roles the definitions give it, its tagged-column reads, the identities it can act as and any conflicting pair of duties it holds, generated offline from the catalogues in `access/` and the definitions in `platform/gcp/`; it refuses to write a report when the definitions hold a grant it cannot attribute to one of those identities:

```bash
uv run python -m access.review              # writes build/access-review.md
uv run python -m access.review <path>       # or writes it to <path>
```

The report is derived, so it is never committed; `tests/access/test_review.py` generates it and checks its content.

## Licence

[MIT](LICENSE).
The data keeps its publishers' licences; each source's licence is confirmed before any sample of it enters the tree.
