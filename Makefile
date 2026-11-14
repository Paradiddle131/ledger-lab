# Every fast check runs through `make check`; each target below is one of them.
# LOADOUT is the rules collection's clone, which holds the process-id and owner checks; on a cloud
# session ~/.claude/CLAUDE.md names it, else a sibling clone, else pass LOADOUT=<path>.
LOADOUT ?= $(or $(shell sed -n 's/^The collection .* is cloned at `\([^`]*\)`.*/\1/p' $(HOME)/.claude/CLAUDE.md 2>/dev/null),../loadout)
GUARDS := $(LOADOUT)/cloud/githooks

.PHONY: check sync lint typecheck test ids portable scan contract contract-test stack-up stack-down stack-test cost-check fresh-clone reconcile-landed tofu fences-validate

check: lint typecheck test ids portable scan contract
	@echo "make check: all passed"

# Also puts the Data Contract CLI's DuckDB engine (the lock's `contract` group) in uv's cache, so
# the engine tests and `make contract-test` run offline; offline with no cache, they say so.
sync:
	uv sync --locked
	@uv run --isolated --frozen --only-group contract python -c 'import datacontract' 2>/dev/null \
		|| echo "sync: the contract engine is not cached and could not be fetched; its tests skip"

lint: sync
	uv run ruff check .
	uv run ruff format --check .

typecheck: sync
	uv run pyright

test: sync
	uv run pytest

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

# `datacontract test` over the local dbt build (target/ledger_lab.duckdb), run on the CLI's own
# DuckDB 1.5 engine over a copy in its storage format (models/contracts/engine.py).
contract-test: sync
	@uv run python models/contracts/engine.py

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
# call, while LEDGER_LAB_GCP_PROJECT is unset.
cost-check:
	@uv run --no-sync python platform/cost_check.py

# The reconciliation of Chicago's payments to their contracts' awards over the landed files: every
# class's count and dollars must equal the reviewed baseline. Needs the Chicago set landed under
# LEDGER_LAB_LANDING; kept out of `make check`, which proves the mechanism on the committed cut.
reconcile-landed: sync
	uv run pytest -m landed tests/models/test_contract_reconciliation.py

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
