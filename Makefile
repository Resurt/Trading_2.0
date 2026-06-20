PYTHON ?= python
NPM ?= npm
TRADING_DATE ?= 2026-06-12
STRATEGY_ID ?= baseline
REPORT_WORKER_SMOKE_MICRO_SESSION_ID ?= 2026-06-12:weekday_main:1000
REPORT_WORKER_SMOKE_TIMEOUT ?= 30
LOOKBACK_DAYS ?= 90
LOOKAHEAD_DAYS ?= 365
INSTRUMENTS ?= SBER,GAZP
TIMEFRAMES ?= 5m,10m,15m
CORPORATE_ACTIONS_FILE ?= data/corporate_actions/sample_dividends.csv
CLASS_CODE ?= TQBR
DATA_SHADOW_MINUTES ?= 10
DATA_SHADOW_LOOKBACK_HOURS ?= 6

.PHONY: lint test up down logs frontend-build migrate migrate-down replay-smoke sandbox-smoke historical-backfill-dry-run historical-quality historical-replay historical-counterfactual historical-report-rebuild calibration-report corporate-actions-import instrument-resolve instrument-resolution-check dividend-sync dividend-sync-730d market-special-days market-special-days-future calibration-primary calibration-special-days historical-replay-clean data-shadow-smoke data-shadow-report data-shadow-readiness api-route-smoke docs-check analytics-smoke report-rebuild replay-day controlled-launch-acceptance launch-readiness observability-up report-worker-smoke celery-inspect

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m pytest

frontend-build:
	cd apps/frontend && $(NPM) run build

up:
	docker compose up -d --build
	$(PYTHON) scripts/run_api_route_smoke.py --json-output

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

historical-quality:
	$(PYTHON) scripts/run_historical_data_quality_report.py --lookback-days $(LOOKBACK_DAYS) --instruments $(INSTRUMENTS) --timeframes 1m,$(TIMEFRAMES) --json-output

historical-replay:
	$(PYTHON) scripts/run_historical_replay_from_db.py --lookback-days $(LOOKBACK_DAYS) --instruments $(INSTRUMENTS) --timeframes $(TIMEFRAMES) --strategy-id $(STRATEGY_ID) --json-output

historical-counterfactual:
	$(PYTHON) scripts/run_historical_counterfactual_rebuild.py --lookback-days $(LOOKBACK_DAYS) --strategy-id $(STRATEGY_ID) --instruments $(INSTRUMENTS) --timeframes $(TIMEFRAMES) --json-output

historical-report-rebuild:
	$(PYTHON) scripts/run_historical_report_rebuild.py --lookback-days $(LOOKBACK_DAYS) --strategy-id $(STRATEGY_ID) --json-output

calibration-report:
	$(PYTHON) scripts/run_calibration_report.py --lookback-days $(LOOKBACK_DAYS) --strategy-id $(STRATEGY_ID) --instruments $(INSTRUMENTS) --timeframes $(TIMEFRAMES) --json-output

corporate-actions-import:
	$(PYTHON) scripts/run_corporate_actions_import.py --file $(CORPORATE_ACTIONS_FILE) --source manual --json-output

instrument-resolve:
	$(PYTHON) scripts/run_tbank_instrument_resolve.py --instruments $(INSTRUMENTS) --class-code $(CLASS_CODE) --strict --json-output

instrument-resolution-check:
	$(PYTHON) scripts/run_launch_readiness.py --mode instrument-resolution --instruments $(INSTRUMENTS)

dividend-sync:
	$(PYTHON) scripts/run_tbank_dividend_sync.py --lookback-days $(LOOKBACK_DAYS) --lookahead-days $(LOOKAHEAD_DAYS) --instruments $(INSTRUMENTS) --json-output

dividend-sync-730d:
	$(PYTHON) scripts/run_tbank_dividend_sync.py --lookback-days 730 --lookahead-days $(LOOKAHEAD_DAYS) --instruments $(INSTRUMENTS) --json-output

market-special-days:
	$(PYTHON) scripts/run_market_special_day_classification.py --lookback-days $(LOOKBACK_DAYS) --instruments $(INSTRUMENTS) --json-output

market-special-days-future:
	$(PYTHON) scripts/run_market_special_day_classification.py --lookback-days $(LOOKBACK_DAYS) --include-future --lookahead-days $(LOOKAHEAD_DAYS) --instruments $(INSTRUMENTS) --require-dividend-sync --json-output

calibration-primary:
	$(PYTHON) scripts/run_calibration_report.py --lookback-days $(LOOKBACK_DAYS) --strategy-id $(STRATEGY_ID) --instruments $(INSTRUMENTS) --timeframes $(TIMEFRAMES) --calibration-scope primary_normal_days --require-special-day-classification --json-output

calibration-special-days:
	$(PYTHON) scripts/run_calibration_report.py --lookback-days $(LOOKBACK_DAYS) --strategy-id $(STRATEGY_ID) --instruments $(INSTRUMENTS) --timeframes $(TIMEFRAMES) --calibration-scope special_days_only --json-output

historical-replay-clean:
	$(PYTHON) scripts/run_historical_replay_from_db.py --lookback-days $(LOOKBACK_DAYS) --instruments $(INSTRUMENTS) --timeframes $(TIMEFRAMES) --strategy-id $(STRATEGY_ID) --require-special-day-classification --json-output

data-shadow-smoke:
	$(PYTHON) scripts/run_data_only_shadow_smoke.py --instruments $(INSTRUMENTS) --minutes $(DATA_SHADOW_MINUTES) --require-dividend-sync --json-output

data-shadow-report:
	$(PYTHON) scripts/run_data_shadow_summary_report.py --lookback-hours $(DATA_SHADOW_LOOKBACK_HOURS) --json-output

data-shadow-readiness:
	$(PYTHON) scripts/run_launch_readiness.py --mode data-shadow --instruments $(INSTRUMENTS) --shadow-minutes $(DATA_SHADOW_MINUTES)

api-route-smoke:
	$(PYTHON) scripts/run_api_route_smoke.py --json-output

docs-check:
	$(PYTHON) scripts/run_docs_consistency_check.py

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
