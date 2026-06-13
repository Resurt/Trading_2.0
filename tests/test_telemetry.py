from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from trading_common import ServiceName
from trading_common.telemetry import (
    CANONICAL_LOG_FIELDS,
    REDACTED,
    JsonLogFormatter,
    LogContextFilter,
    RedactionFilter,
    bind_context,
    build_logging_dict_config,
    clear_context,
    log_event,
)


def test_log_event_enriches_json_with_contextvars_context() -> None:
    stream = io.StringIO()
    logger = _logger_for_stream("tests.telemetry.context", stream)
    candidate_id = uuid4()
    order_intent_id = uuid4()
    exchange_ts = datetime(2026, 6, 13, 7, 59, 59, tzinfo=UTC)

    with bind_context(
        candidate_id=candidate_id,
        instrument="MOEX:SBER",
        timeframe="5m",
        strategy_id="baseline",
        strategy_version="v1",
        session_type="weekday_morning",
        exchange_phase="continuous",
        micro_session_id="2026-06-13:weekday_morning:0700",
        order_intent_id=order_intent_id,
    ):
        log_event(
            logger=logger,
            event_type="signal_candidate_created",
            event_version="2",
            component="strategy.pipeline",
            exchange_ts=exchange_ts,
            latency_ms=12.5,
            stage_name="signal_precheck",
            expected_edge_bps="14.2",
        )

    payload = json.loads(stream.getvalue())

    assert set(CANONICAL_LOG_FIELDS).issubset(payload)
    assert payload["event_type"] == "signal_candidate_created"
    assert payload["event_version"] == "2"
    assert payload["service"] == "trade-core"
    assert payload["component"] == "strategy.pipeline"
    assert payload["exchange_ts"] == "2026-06-13T07:59:59+00:00"
    assert payload["candidate_id"] == str(candidate_id)
    assert payload["order_intent_id"] == str(order_intent_id)
    assert payload["instrument"] == "MOEX:SBER"
    assert payload["timeframe"] == "5m"
    assert payload["strategy_version"] == "v1"
    assert payload["exchange_phase"] == "continuous"
    assert payload["latency_ms"] == 12.5
    assert payload["payload"]["stage_name"] == "signal_precheck"
    assert payload["payload"]["expected_edge_bps"] == "14.2"


def test_redaction_filter_removes_tokens_headers_and_credentials() -> None:
    stream = io.StringIO()
    logger = _logger_for_stream("tests.telemetry.redaction", stream)

    log_event(
        logger=logger,
        event_type="broker_auth_diagnostic",
        authorization="Bearer extremely-secret-token",
        headers={"Authorization": "Bearer nested-secret-token"},
        nested={"password": "plain-secret", "safe": "visible"},
        raw_text="token=t.CUtCVmpYDwTcVJg2iw6I2-93fkWFHMeB38axeIC2ZG4PcOy",
    )

    raw = stream.getvalue()
    payload = json.loads(raw)

    assert "extremely-secret-token" not in raw
    assert "nested-secret-token" not in raw
    assert "plain-secret" not in raw
    assert "t.CUtCVmpYDwTcVJg2iw6I2-93fkWFHMeB38axeIC2ZG4PcOy" not in raw
    assert REDACTED in raw
    assert payload["payload"]["authorization"] == REDACTED
    assert payload["payload"]["headers"]["Authorization"] == REDACTED
    assert payload["payload"]["nested"]["password"] == REDACTED
    assert payload["payload"]["nested"]["safe"] == "visible"


def test_json_schema_contains_mandatory_fields_for_plain_technical_log() -> None:
    stream = io.StringIO()
    logger = _logger_for_stream("tests.telemetry.schema", stream)

    logger.info("health probe ok")

    payload = json.loads(stream.getvalue())

    assert set(CANONICAL_LOG_FIELDS).issubset(payload)
    assert payload["event_type"] == "technical_log"
    assert payload["event_version"] == "1"
    assert payload["payload"] == {}
    assert payload["exchange_ts"] is None
    assert payload["candidate_id"] is None
    assert payload["order_intent_id"] is None


def test_logging_dict_config_uses_stdout_json_with_redaction_and_context_filters() -> None:
    config = build_logging_dict_config(
        service=ServiceName.API,
        logger_name="tests.telemetry.dict_config",
        log_format="json",
    )

    handlers = config["handlers"]
    assert isinstance(handlers, dict)
    stdout = handlers["stdout"]
    assert isinstance(stdout, dict)
    assert stdout["stream"] == "ext://sys.stdout"
    assert stdout["formatter"] == "json"
    assert stdout["filters"] == ["redaction", "telemetry_context"]


def _logger_for_stream(name: str, stream: io.StringIO) -> logging.Logger:
    clear_context()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter())
    handler.addFilter(LogContextFilter(service=ServiceName.TRADE_CORE))
    handler.setFormatter(JsonLogFormatter(service=ServiceName.TRADE_CORE))

    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger
