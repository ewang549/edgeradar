# EdgeRadar developer shortcuts.
# Targets are intentionally thin wrappers so the README can say "run `make up`".
# Some targets are stubs until their phase lands (clearly labeled below).

.DEFAULT_GOAL := help
SHELL := /bin/bash

# Run a command inside the app container.
APP_EXEC := docker compose exec app

.PHONY: help up down logs ps console install lint test config-check ingest produce consume resolve weather evaluate backfill alert reset refresh notify dagster dbt dbt-test dashboard demo quality doctor data-quality

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## Build + start the stack (MinIO, bucket init, app); waits for health
	docker compose up -d --build
	@echo "Stack starting. MinIO console: http://localhost:9001 (user/pass from .env)"

down:  ## Stop and remove containers (keeps the data volume)
	docker compose down

logs:  ## Tail logs from all services
	docker compose logs -f

ps:  ## Show service status
	docker compose ps

console:  ## Print the MinIO console URL
	@echo "MinIO console: http://localhost:9001"

install:  ## Create local venv + install deps with uv (for running outside Docker)
	uv sync --extra dev

config-check:  ## Verify settings load (runs inside the app container)
	$(APP_EXEC) edgeradar config-check

doctor:  ## Diagnose the environment: deps, files, sample data, read-only guardrail
	$(APP_EXEC) edgeradar doctor

demo:  ## FASTEST OFFLINE DEMO: dry-run ingest -> resolve -> warehouse -> quality (no network)
	$(APP_EXEC) sh -c "rm -rf data/clean data/raw"
	$(APP_EXEC) edgeradar ingest --source all --dry-run
	$(APP_EXEC) edgeradar weather --dry-run
	$(APP_EXEC) edgeradar resolve
	$(MAKE) dbt
	$(APP_EXEC) edgeradar quality
	@echo ""
	@echo "Demo data is built from bundled sample responses (offline)."
	@echo "Open the product dashboard with:  make dashboard"

quality:  ## QUALITY GATE: ruff lint + format check, pytest, and dbt build/test
	$(APP_EXEC) ruff check .
	$(APP_EXEC) ruff format --check .
	$(APP_EXEC) pytest -q
	$(APP_EXEC) sh -c "mkdir -p data/warehouse && dbt build --exclude tag:eval --project-dir dbt --profiles-dir dbt"

data-quality:  ## Scan the lake and (re)write the data-quality / source-health report
	$(APP_EXEC) edgeradar quality

lint:  ## Ruff lint + format check (inside the app container)
	$(APP_EXEC) ruff check .
	$(APP_EXEC) ruff format --check .

test:  ## Run the test suite (inside the app container)
	$(APP_EXEC) pytest

ingest:  ## Batch ingest (Phase 1). Usage: make ingest SOURCE=manifold ARGS=--dry-run
	$(APP_EXEC) edgeradar ingest --source $(or $(SOURCE),all) $(ARGS)

produce:  ## Stream: publish quotes to the topic. Usage: make produce SOURCE=all ARGS=--dry-run
	$(APP_EXEC) edgeradar produce --source $(or $(SOURCE),all) $(ARGS)

consume:  ## Stream: drain topic -> clean Parquet (Phase 3)
	$(APP_EXEC) edgeradar consume $(ARGS)

resolve:  ## Entity resolution: group same-event markets -> event_map (Phase 4)
	$(APP_EXEC) edgeradar resolve $(ARGS)

weather:  ## Weather edge: NWS forecast vs Kalshi temp markets. Usage: make weather ARGS=--dry-run
	$(APP_EXEC) edgeradar weather $(ARGS)

evaluate:  ## Log signals + score vs outcomes; build eval dbt marts (Phase 6)
	$(APP_EXEC) edgeradar evaluate
	$(APP_EXEC) sh -c "dbt build --select tag:eval --project-dir dbt --profiles-dir dbt"

backfill:  ## Instant calibration: score already-settled Kalshi markets now (Phase 6)
	$(APP_EXEC) edgeradar backfill $(ARGS)

alert:  ## Fire Discord alerts for above-threshold signals (read-only). ARGS=--dry-run to preview
	$(APP_EXEC) edgeradar alert $(ARGS)

reset:  ## Wipe ALL local data (lake + warehouse + signal log) for a clean live-only start
	$(APP_EXEC) sh -c "rm -rf data/clean data/raw data/marts data/warehouse && mkdir -p data/warehouse"
	@echo "Wiped all local data. Run 'make refresh' to repopulate with live data only."

refresh:  ## ONE COMMAND: pull live data (all sources), rebuild warehouse, score signals
	# Start from a clean quote lake each run so stale/dry-run partitions can never
	# accumulate. The signal_log + auto-resolutions in data/marts are preserved.
	$(APP_EXEC) sh -c "rm -rf data/clean data/raw"
	$(APP_EXEC) edgeradar ingest --source all
	$(APP_EXEC) edgeradar weather
	$(APP_EXEC) edgeradar resolve
	$(MAKE) dbt
	$(MAKE) evaluate
	$(APP_EXEC) edgeradar quality
	@echo "Refresh complete. Open the dashboard with: make dashboard"

notify:  ## ONE COMMAND: refresh everything, then post signals to Discord
	$(MAKE) refresh
	$(APP_EXEC) edgeradar alert
	@echo "Sent any above-threshold signals to Discord."

dashboard:  ## Launch the Streamlit dashboard at http://localhost:8501 (read-only)
	$(APP_EXEC) sh -c "cd /app && streamlit run src/edgeradar/dashboard/app.py --server.address 0.0.0.0 --server.port 8501"

dagster:  ## Launch the Dagster UI (asset orchestration) at http://localhost:3000
	$(APP_EXEC) sh -c "cd /app && DAGSTER_HOME=/app/.dagster dagster dev -m edgeradar.orchestration.definitions -h 0.0.0.0 -p 3000"

dbt:  ## Build core dbt models + tests (excludes eval; run `make ingest`/`make resolve` first)
	$(APP_EXEC) sh -c "mkdir -p data/warehouse && dbt build --exclude tag:eval --project-dir dbt --profiles-dir dbt"

dbt-test:  ## Run only the dbt tests (core; excludes eval)
	$(APP_EXEC) sh -c "dbt test --exclude tag:eval --project-dir dbt --profiles-dir dbt"
