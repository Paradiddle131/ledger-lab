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

The container stack needs Docker with Compose; the shared database always starts, every other service only under its profile:

```bash
export LEDGER_LAB_DB_PASSWORD=<any local password>
make stack-up                 # the shared database
make stack-up PROFILE=<name>  # plus one service
make stack-down               # stop everything and drop its volumes
```

The lab's GCP project comes only from `LEDGER_LAB_GCP_PROJECT`; `make cost-check` prints its month-to-date cost (with `LEDGER_LAB_BILLING_EXPORT` naming the billing export's `dataset.table`), and $0.00 without any GCP call while no project is set.

## Licence

[MIT](LICENSE).
The data keeps its publishers' licences; each source's licence is confirmed before any sample of it enters the tree.
