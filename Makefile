# Every fast check runs through `make check`; each target below is one of them.
# LOADOUT is the rules collection's clone, which holds the process-id and owner checks; on a cloud
# session ~/.claude/CLAUDE.md names it, else a sibling clone, else pass LOADOUT=<path>.
LOADOUT ?= $(or $(shell sed -n 's/^The collection .* is cloned at `\([^`]*\)`.*/\1/p' $(HOME)/.claude/CLAUDE.md 2>/dev/null),../loadout)
GUARDS := $(LOADOUT)/cloud/githooks

.PHONY: check sync lint typecheck test ids portable scan contract metric-versions contract-test warehouse-parity bigquery-personas bigquery-row-policies bigquery-thresholds bigquery-purposes bigquery-visibility bigquery-agent stack-up stack-down stack-test cost-check fresh-clone reconcile-landed reconcile-accept tofu fences-validate

check: lint typecheck test ids portable scan contract metric-versions
	@echo "make check: all passed"

# Also builds the one environment of each lock group that runs apart from the project's
# (platform/group_env.py, in uv's cache, once per lock), fetching what uv's cache lacks: the Data
# Contract CLI's DuckDB engine (`contract`), so the engine tests and `make contract-test` run
# offline; offline with no cache, they say so. And dbt-bigquery (`bigquery`), whose offline parse
# the metric versions gate reads; without it, the gate fails and says so.
sync:
	uv sync --locked
	@uv run --no-sync python platform/group_env.py contract datacontract 2>/dev/null \
		|| echo "sync: the contract engine is not cached and could not be fetched; its tests skip"
	@uv run --no-sync python platform/group_env.py bigquery dbt.adapters.bigquery \
		2>/dev/null || echo "sync: dbt-bigquery is not cached and could not be fetched; the metric versions gate fails"

lint: sync
	uv run ruff check .
	uv run ruff format --check .

typecheck: sync
	uv run pyright

# LOADOUT reaches pytest for the cross-check with the review skill's tier.sh
# (tests/test_review_routing.py), which skips, saying so, without it.
# After `sync`, the recipe asks `platform/gcp_project.py` whether this is the lab's own environment.
# It asks in the recipe's own shell, so the decider sees the environment pytest gets, variables
# given on make's command line included. By the answer it runs:
# - `own`: the cost read (`platform/cost_check.py`), pytest, then the cost read again, in that
#   order on one shell line. A failed read before stops pytest from running; the read after also
#   runs after a failed pytest; the line fails if either read or pytest fails.
# - `elsewhere` (the lab variable unset or blank, or GCP_PROJECT non-empty): pytest alone.
# - any other output, the empty output of a failed decider included: a message, a failure, no pytest.
# pytest runs its tests across one worker per CPU this process may use (`-n auto`, pytest-xdist),
# capped at one per gibibyte of available memory (tests/workers.py);
# `PYTEST_XDIST_AUTO_NUM_WORKERS=<n>` in the environment sets another count. A plain
# `uv run pytest` stays in one process.
# tests/platform/test_cost_read_around_tests.py checks these over a stub `uv`. Limits: an interrupt
# (Ctrl-C) stops the line, the read after included; pytest runs in other targets (`stack-test`,
# `reconcile-landed`, `bigquery-personas` and the like) have no cost read around them.
# The recipe holds no make variable, so no make-variable assignment from make's command line,
# MAKEFLAGS, `make -e`, `--eval` or `MAKEFILES`, global or for this target, rewrites its commands;
# the decider and both reads run Python with `-E`, so PYTHONPATH and the other PYTHON* variables
# do not reach them; a `SHELL` or `.SHELLFLAGS` other than make's own (`-c`, or `-ec` under
# `.POSIX`) stops the recipe; a failed pytest is caught without ending the line, so `.POSIX` does
# not skip the read after; LOADOUT reaches pytest exported for this target, never spliced into
# the shell line. These guard against a slip, not a deliberate evasion; not closed: `-o test`,
# `-i` or `.IGNORE`, `-n`, `-t`, another makefile that replaces this recipe, programs earlier on
# PATH (`uv`, `bq`, `gcloud`, `curl`), the environment pytest reads (`PYTEST_ADDOPTS`), and
# `uv`'s own `UV_*` variables.
test: export LOADOUT := $(LOADOUT)

test: sync
	$(if $(filter-out /bin/sh,$(SHELL))$(filter-out -c -ec,$(.SHELLFLAGS)),$(error test: SHELL or .SHELLFLAGS is not make's own))
	case "$$(uv run --no-sync python -E platform/gcp_project.py)" in \
	own) uv run --no-sync python -E platform/cost_check.py || exit 1; tests=0; uv run pytest -n auto || tests=$$?; uv run --no-sync python -E platform/cost_check.py || exit 1; exit $$tests ;; \
	elsewhere) uv run pytest -n auto ;; \
	*) echo "test: platform/gcp_project.py answered neither own nor elsewhere" >&2; exit 1 ;; \
	esac

ids:
	@test -f $(GUARDS)/check-process-ids.py || { echo "ids: no rules collection at $(LOADOUT); pass LOADOUT=<clone>" >&2; exit 1; }
	python3 $(GUARDS)/check-process-ids.py --all .

portable:
	@test -f $(GUARDS)/check-portable.sh || { echo "portable: no rules collection at $(LOADOUT); pass LOADOUT=<clone>" >&2; exit 1; }
	bash $(GUARDS)/check-portable.sh --all .

scan: sync
	git ls-files -z | xargs -0 uv run detect-secrets-hook
	@echo "scan: $$(git ls-files | wc -l) tracked files, no secret"

# The contracts against the ones on origin/main, read from this clone: fails on a change that
# breaks a consumer, and when origin/main is not fetched (models/contracts/gate.py).
contract: sync
	@uv run python models/contracts/gate.py

# The metrics against the ones on origin/main, read from this clone and each parsed by dbt: fails on
# a changed definition under the same version, a lost version or owner, and when origin/main is not
# fetched or not merged (semantics/versions.py).
metric-versions: sync
	@uv run python semantics/versions.py

# `datacontract test` over the local dbt build (models/target/ledger_lab.duckdb), run on the CLI's own
# DuckDB 1.5 engine over a copy in its storage format (models/contracts/engine.py).
contract-test: sync
	@uv run python models/contracts/engine.py

# The payments mart's `datacontract test` on BigQuery, then the BigQuery build compared with a local
# build over the same landing, by capped queries (models/contracts/warehouse.py,
# tests/models/bigquery_parity.py); GCP calls in the lab's own environment, kept out of `make check`.
warehouse-parity: sync
	@uv run python models/contracts/warehouse.py
	@uv run python -m tests.models.bigquery_parity $(or $(LEDGER_LAB_DUCKDB_PATH),models/target/ledger_lab.duckdb)

# Each human persona's query on BigQuery, as the persona's own service account, by a token minted by
# impersonation from this machine's login through the agent account (`LEDGER_LAB_AGENT_ACCOUNT`,
# access/bigquery.py); off the cloud proxy, which replaces the token, so `make check` runs it only over
# stubs, under the report options that add most, to hold that a failure names the persona alone.
bigquery-personas: sync
	uv run pytest -m bigquery_persona tests/access/test_bigquery_personas.py

# Each human persona's read of the payments product's governed copies on BigQuery, as the persona:
# exactly its granted entities' rows of its copy (clear or masked), none of the other, a refusal
# without a row grant (access/row_policies.py), and a tagged column of the clear copy refused
# without the tag's clear role (access/policy_tags.py).
bigquery-row-policies: sync
	uv run pytest -m bigquery_persona tests/access/test_row_policies.py tests/access/test_policy_tags.py

# Each aggregate reader's read of its product's aggregate views on BigQuery, as the persona: only
# the groups of at least the minimum group, no column finer than the payments view's grain, and a
# refusal without the grant (access/thresholds.py).
bigquery-thresholds: sync
	uv run pytest -m bigquery_persona tests/access/test_thresholds.py

# Each human persona's read of its purposes' views on BigQuery, as the persona: through each view
# exactly its entities' rows of the copy its tags give it, by count and digest, and none of the
# other copy; a refusal of every other purpose's views and, once the product grants are removed,
# of the views' sources; and each purpose dataset holding exactly its generated views, each with
# its generated query (access/purpose_views.py). `LEDGER_LAB_PRODUCT_GRANTS=kept` skips the
# sources' case while the product grants are still applied.
bigquery-purposes: sync
	uv run pytest -m bigquery_persona tests/access/test_purpose_views.py

# The visibility matrix acted out on BigQuery, as each persona: every cell the landed data holds
# something for observed through the persona's own session against the source read with full
# rights (the payments mart as the publisher reads it, the group totals through the audit purpose's
# views), and the cells on invented tables named and left to the local engine
# (tests/access/visibility_on_bigquery.py); `-rP` prints the cells left.
bigquery-visibility: sync
	uv run pytest -m bigquery_persona -rP tests/access/test_visibility.py::test_every_persona_sees_its_matrix

# The own agent's questions on BigQuery through its BigQuery mode (agent/bigquery.py), as the asking
# persona: the Chicago and the UK transport accountants asking the same question get different rows,
# each job run as the asker's own account (SESSION_USER() checked before the first query) and
# labelled with the agent, the conversation and the turn; a question without a persona mints no
# token; and a runaway join's job is refused by BigQuery at the bytes left of the turn, the turn
# stopped with no run.
# Run it between two `make cost-check` reads.
bigquery-agent: sync
	uv run pytest -m bigquery_persona -rA tests/agent/test_ask.py::test_two_personas_asking_the_same_question_get_different_rows tests/agent/test_ask.py::test_a_question_without_a_persona_gets_no_answer tests/agent/test_cost.py::test_runaway_join_stops_at_the_cap tests/agent/test_bigquery_mode.py::test_each_job_of_a_turn_carries_its_labels

# The container stack: the shared database always, one more service per PROFILE=<name>.
# Each checkout is its own Compose project (containers, network, volume) and the database takes a
# free loopback port unless LEDGER_LAB_DB_PORT names one, so checkouts sharing a daemon never meet.
# The database password comes from LEDGER_LAB_DB_PASSWORD, else from a file generated at the first
# start, ignored by git and deleted by `make stack-down` with the volume it unlocks.
STACK_ENV := $(CURDIR)/platform/stack/.env.generated
stack-up stack-down stack-test: export COMPOSE_PROJECT_NAME := ledger-lab-$(shell printf '%s' '$(CURDIR)' | cksum | cut -d' ' -f1)
stack-up stack-test: export COMPOSE_FILE := $(CURDIR)/platform/stack/compose.yaml
stack-up stack-test: export COMPOSE_ENV_FILES := $(STACK_ENV)

$(STACK_ENV):
	@umask 077 && printf 'LEDGER_LAB_DB_PASSWORD=%s\n' "$$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" > $@

stack-up: $(STACK_ENV)
	docker compose $(if $(PROFILE),--profile $(PROFILE)) up -d --wait
	@echo "stack-up: project $$COMPOSE_PROJECT_NAME, database at $$(docker compose port db 5432)"

# By project name alone, with no compose file to resolve (so no password needed): Compose finds the
# project's containers, under any profile, its network and its volumes by their labels.
stack-down:
	docker compose down --volumes --remove-orphans
	rm -f $(STACK_ENV)

# The live stack test against this checkout's running database; kept out of `make check`.
stack-test: sync stack-up
	uv run pytest -m stack

# The lab's month-to-date GCP cost and its spend fences, read live; $0.00 by absence, with no GCP
# call, while LEDGER_LAB_GCP_PROJECT is unset; no GCP call and a failure, not checked, in a session
# that carries another lab's GCP_PROJECT.
cost-check:
	@uv run --no-sync python -E platform/cost_check.py

# The reconciliation of Chicago's payments to their contracts' awards over the landed files: every
# class's count and dollars, and each break class's digest, must equal the reviewed baseline. Needs
# the Chicago set landed under LEDGER_LAB_LANDING; kept out of `make check`, which proves the
# mechanism on the committed cut. The governed metrics, which read the reconciliation, run over the
# same landed build here too, each against its pinned value (the full landing, every source).
reconcile-landed: sync
	uv run pytest -m landed tests/models/test_contract_reconciliation.py tests/semantics/test_metrics.py

# Accept today's reconciliation over the landed files: writes its per-contract list of breaks beside
# the landing (<landing>.reconciliation.csv, outside the repository), which a failing
# `reconcile-landed` then names contracts against, and prints the table and digests to pin in a
# reviewed change.
reconcile-accept: sync
	uv run python -m tests.models.reconciliation_state

# Clone the committed HEAD and prove it holds no secret, identifier or data (also a test in `test`).
fresh-clone: sync
	@uv run python platform/fresh_clone.py

# The lab project's definitions (platform/gcp): the pinned OpenTofu validates them with no backend
# and no credentials, then evaluates the access rules it generates from the catalogues for the
# `tofu` tests; only the owner plans and applies them.
TOFU := $(HOME)/.local/bin/tofu

tofu:
	@bash platform/gcp/install-tofu.sh

fences-validate: tofu sync
	$(TOFU) -chdir=platform/gcp fmt -check
	$(TOFU) -chdir=platform/gcp init -backend=false -input=false >/dev/null
	$(TOFU) -chdir=platform/gcp validate
	TOFU=$(TOFU) uv run pytest -m tofu
