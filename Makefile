PYTHON ?= python
NPM ?= npm
TRADING_DATE ?= 2026-06-12
STRATEGY_ID ?= baseline
REPORT_WORKER_SMOKE_MICRO_SESSION_ID ?= 2026-06-12:weekday_main:1000
REPORT_WORKER_SMOKE_TIMEOUT ?= 30

.PHONY: lint test up down logs frontend-build migrate migrate-down replay-smoke sandbox-smoke historical-backfill-dry-run analytics-smoke report-rebuild replay-day controlled-launch-acceptance launch-readiness observability-up report-worker-smoke celery-inspect

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

historical-backfill-dry-run:
	$(PYTHON) scripts/run_historical_candle_backfill.py --instruments SBER,GAZP --lookback-days 90 --dry-run

analytics-smoke:
	$(PYTHON) scripts/run_logging_analytics_acceptance.py --date $(TRADING_DATE) --strategy-id $(STRATEGY_ID)

report-rebuild:
	$(PYTHON) scripts/run_report_rebuild.py --date $(TRADING_DATE) --strategy-id $(STRATEGY_ID)

replay-day:
	$(PYTHON) scripts/run_replay_day.py --date $(TRADING_DATE)

controlled-launch-acceptance:
	$(PYTHON) scripts/run_controlled_launch_acceptance.py --date $(TRADING_DATE) --strategy-id $(STRATEGY_ID)

launch-readiness:
	$(PYTHON) scripts/run_launch_readiness.py --mode local --date $(TRADING_DATE) --strategy-id $(STRATEGY_ID)

observability-up:
	docker compose up -d prometheus grafana loki fluent-bit

report-worker-smoke:
	$(PYTHON) scripts/run_report_worker_smoke.py --micro-session-id $(REPORT_WORKER_SMOKE_MICRO_SESSION_ID) --strategy-id $(STRATEGY_ID) --timeout-seconds $(REPORT_WORKER_SMOKE_TIMEOUT)

celery-inspect:
	docker compose exec -T report-worker celery -A report_worker.celery_app.celery_app inspect ping
