# Every fast check runs through `make check`; each target below is one of them.
# LOADOUT is the rules collection's clone, which holds the process-id and owner checks; on a cloud
# session ~/.claude/CLAUDE.md names it, else a sibling clone, else pass LOADOUT=<path>.
LOADOUT ?= $(or $(shell sed -n 's/^The collection .* is cloned at `\([^`]*\)`.*/\1/p' $(HOME)/.claude/CLAUDE.md 2>/dev/null),../loadout)
GUARDS := $(LOADOUT)/cloud/githooks

.PHONY: check sync lint typecheck test ids portable scan stack-up stack-down cost-check fresh-clone tofu fences-validate

check: lint typecheck test ids portable scan
	@echo "make check: all passed"

sync:
	uv sync --locked

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

# The container stack: the shared database always, one more service per PROFILE=<name>.
STACK := docker compose -f platform/stack/compose.yaml

stack-up:
	$(STACK) $(if $(PROFILE),--profile $(PROFILE)) up -d --wait

stack-down:
	$(STACK) --profile '*' down --volumes

# The lab's month-to-date GCP cost: $0.00 by absence while LEDGER_LAB_GCP_PROJECT is unset.
cost-check:
	@uv run --no-sync python platform/cost_check.py

# Clone the committed HEAD and prove it holds no secret, identifier or data (also a test in `test`).
fresh-clone: sync
	@uv run python platform/fresh_clone.py

# The lab project's spend fences (platform/gcp): the pinned OpenTofu validates them with no backend
# and no credentials; only the owner plans and applies them.
TOFU := $(HOME)/.local/bin/tofu

tofu:
	@bash platform/gcp/install-tofu.sh

fences-validate: tofu
	$(TOFU) -chdir=platform/gcp fmt -check
	$(TOFU) -chdir=platform/gcp init -backend=false -input=false >/dev/null
	$(TOFU) -chdir=platform/gcp validate
