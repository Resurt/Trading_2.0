PYTHON ?= python
NPM ?= npm
TRADING_DATE ?= 2026-06-12
STRATEGY_ID ?= baseline

.PHONY: lint test up down logs frontend-build migrate migrate-down replay-smoke sandbox-smoke analytics-smoke report-rebuild replay-day observability-up

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m pytest

frontend-build:
	cd apps/frontend && $(NPM) run build

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

migrate:
	$(PYTHON) -m alembic upgrade head

migrate-down:
	$(PYTHON) -m alembic downgrade -1

replay-smoke:
	$(PYTHON) scripts/run_replay_harness.py

sandbox-smoke:
	$(PYTHON) scripts/run_sandbox_smoke.py --dry-run

analytics-smoke:
	$(PYTHON) scripts/run_logging_analytics_acceptance.py --date $(TRADING_DATE) --strategy-id $(STRATEGY_ID)

report-rebuild:
	$(PYTHON) scripts/run_report_rebuild.py --date $(TRADING_DATE) --strategy-id $(STRATEGY_ID)

replay-day:
	$(PYTHON) scripts/run_replay_day.py --date $(TRADING_DATE)

observability-up:
	docker compose up -d prometheus grafana loki fluent-bit
