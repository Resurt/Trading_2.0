"""Frontend-oriented report read models."""

from __future__ import annotations

from trading_common.db.models import CounterfactualResult, DailyReport, HourlyReport


def hourly_report_read_model(report: HourlyReport) -> dict[str, object]:
    return {
        "hourly_report_id": str(report.hourly_report_id),
        "run_id": str(report.run_id) if report.run_id is not None else None,
        "strategy_id": report.strategy_id,
        "instrument_id": report.instrument_id,
        "calendar_date": report.calendar_date.isoformat(),
        "trading_date": report.trading_date.isoformat(),
        "session_type": report.session_type,
        "session_phase": report.session_phase,
        "micro_session_id": report.micro_session_id,
        "started_at": report.started_at.isoformat(),
        "ended_at": report.ended_at.isoformat(),
        "realised_pnl": _optional_str(report.realised_pnl),
        "unrealised_pnl": _optional_str(report.unrealised_pnl),
        "commission": _optional_str(report.commission),
        "signal_count": report.signal_count,
        "blocked_count": report.blocked_count,
        "fill_ratio": _optional_str(report.fill_ratio),
        "payload": report.report_payload,
        "html": _payload_html(report.report_payload),
    }


def daily_report_read_model(report: DailyReport) -> dict[str, object]:
    return {
        "daily_report_id": str(report.daily_report_id),
        "calendar_date": report.calendar_date.isoformat(),
        "trading_date": report.trading_date.isoformat(),
        "strategy_id": report.strategy_id,
        "session_type": report.session_type,
        "instrument_id": report.instrument_id,
        "market_regime": report.market_regime,
        "realised_pnl": _optional_str(report.realised_pnl),
        "commission": _optional_str(report.commission),
        "signal_count": report.signal_count,
        "blocked_count": report.blocked_count,
        "fill_ratio": _optional_str(report.fill_ratio),
        "payload": report.report_payload,
        "html": _payload_html(report.report_payload),
    }


def counterfactual_read_model(result: CounterfactualResult) -> dict[str, object]:
    return {
        "counterfactual_result_id": str(result.counterfactual_result_id),
        "candidate_id": str(result.candidate_id) if result.candidate_id is not None else None,
        "order_intent_id": (
            str(result.order_intent_id) if result.order_intent_id is not None else None
        ),
        "source_event_type": result.source_event_type,
        "instrument_id": result.instrument_id,
        "strategy_id": result.strategy_id,
        "blocker_code": result.blocker_code,
        "cancel_reason_code": result.cancel_reason_code,
        "mfe_5m_bps": _optional_str(result.mfe_5m_bps),
        "mae_5m_bps": _optional_str(result.mae_5m_bps),
        "mfe_10m_bps": _optional_str(result.mfe_10m_bps),
        "mae_10m_bps": _optional_str(result.mae_10m_bps),
        "mfe_15m_bps": _optional_str(result.mfe_15m_bps),
        "mae_15m_bps": _optional_str(result.mae_15m_bps),
        "would_profit_5m": result.would_profit_5m,
        "would_profit_10m": result.would_profit_10m,
        "would_profit_15m": result.would_profit_15m,
        "payload": result.result_payload,
        "html": _payload_html(result.result_payload),
    }


def _optional_str(value: object | None) -> str | None:
    return str(value) if value is not None else None


def _payload_html(payload: dict[str, object]) -> str | None:
    html = payload.get("html_output")
    return html if isinstance(html, str) else None
