"""Acceptance checks for logging/analytics calibration readiness."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_common.db.models import (
    AuditEvent,
    BlockerEvent,
    BrokerOrder,
    CounterfactualResult,
    DailyReport,
    FillEvent,
    OrderIntent,
    SessionRun,
    SignalCandidate,
)

JsonPayload = dict[str, object]
TERMINAL_CANDIDATE_STATUSES = frozenset(
    {"blocked", "rejected", "cancelled", "canceled", "filled", "exited", "stopped"}
)
TERMINAL_INTENT_STATUSES = frozenset({"blocked", "rejected", "cancelled", "canceled", "filled"})
TERMINAL_BROKER_STATUSES = frozenset({"rejected", "cancelled", "canceled", "filled"})
RAW_SECRET_VALUE_PATTERN = re.compile(r"(?i)(bearer\s+[a-z0-9._-]+|t\.[a-z0-9_-]{20,})")
CREDENTIAL_KEY_PATTERN = re.compile(r"(?i)(authorization|token|password|secret|credential)")
REDACTED_VALUES = frozenset({"[redacted]", "redacted", "***", "<redacted>", "", "none", "null"})


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    """One machine-readable acceptance result."""

    code: str
    passed: bool
    details: JsonPayload

    def as_payload(self) -> JsonPayload:
        return {
            "code": self.code,
            "passed": self.passed,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    """Full logging/analytics acceptance report."""

    trading_date: date
    strategy_id: str
    checks: tuple[AcceptanceCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_payload(self) -> JsonPayload:
        return {
            "trading_date": self.trading_date.isoformat(),
            "strategy_id": self.strategy_id,
            "passed": self.passed,
            "checks": [check.as_payload() for check in self.checks],
        }


class AnalyticsAcceptanceChecker:
    """Validate the domain journal and report marts used for calibration."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def run(self, *, trading_date: date, strategy_id: str) -> AcceptanceReport:
        checks = (
            self._candidate_terminal_outcomes(trading_date=trading_date, strategy_id=strategy_id),
            self._blocker_measurements(trading_date=trading_date, strategy_id=strategy_id),
            self._broker_order_correlation(trading_date=trading_date),
            self._canceled_orders_have_counterfactuals(
                trading_date=trading_date,
                strategy_id=strategy_id,
            ),
            self._daily_report_shape(trading_date=trading_date, strategy_id=strategy_id),
            self._hourly_rollover_without_restart(
                trading_date=trading_date,
                strategy_id=strategy_id,
            ),
            self._stream_reconnect_gap_recovery(trading_date=trading_date),
            self._weekend_session_present(strategy_id=strategy_id),
            self._no_raw_secrets_in_audit_logs(trading_date=trading_date),
        )
        return AcceptanceReport(
            trading_date=trading_date,
            strategy_id=strategy_id,
            checks=checks,
        )

    def _candidate_terminal_outcomes(
        self,
        *,
        trading_date: date,
        strategy_id: str,
    ) -> AcceptanceCheck:
        candidates = self._list(
            select(SignalCandidate).where(
                SignalCandidate.trading_date == trading_date,
                SignalCandidate.strategy_id == strategy_id,
            )
        )
        final_blocker_ids = {
            blocker.candidate_id
            for blocker in self._list(
                select(BlockerEvent).where(
                    BlockerEvent.trading_date == trading_date,
                    BlockerEvent.strategy_id == strategy_id,
                    BlockerEvent.is_final_blocker.is_(True),
                )
            )
            if blocker.candidate_id is not None
        }
        intents_by_candidate = _group_by_candidate(
            self._list(
                select(OrderIntent).where(
                    OrderIntent.trading_date == trading_date,
                    OrderIntent.strategy_id == strategy_id,
                )
            )
        )
        broker_orders_by_candidate = _group_by_candidate(
            self._list(select(BrokerOrder).where(BrokerOrder.trading_date == trading_date))
        )
        fills_by_candidate = _group_by_candidate(
            self._list(select(FillEvent).where(FillEvent.trading_date == trading_date))
        )

        missing: list[str] = []
        for candidate in candidates:
            if candidate.candidate_status in TERMINAL_CANDIDATE_STATUSES:
                continue
            if candidate.candidate_id in final_blocker_ids:
                continue
            if any(
                intent.status in TERMINAL_INTENT_STATUSES
                or intent.cancel_reason_code
                or intent.reject_reason_code
                for intent in intents_by_candidate.get(candidate.candidate_id, ())
            ):
                continue
            if any(
                order.broker_status in TERMINAL_BROKER_STATUSES
                for order in broker_orders_by_candidate.get(candidate.candidate_id, ())
            ):
                continue
            if fills_by_candidate.get(candidate.candidate_id):
                continue
            missing.append(str(candidate.candidate_id))
        return AcceptanceCheck(
            code="candidate_terminal_outcome",
            passed=not missing and bool(candidates),
            details={"candidate_count": len(candidates), "missing_candidate_ids": missing},
        )

    def _blocker_measurements(
        self,
        *,
        trading_date: date,
        strategy_id: str,
    ) -> AcceptanceCheck:
        blockers = self._list(
            select(BlockerEvent).where(
                BlockerEvent.trading_date == trading_date,
                BlockerEvent.strategy_id == strategy_id,
                BlockerEvent.passed.is_(False),
            )
        )
        missing = [
            str(blocker.blocker_event_id)
            for blocker in blockers
            if blocker.measured_value is None or blocker.threshold_value is None
        ]
        return AcceptanceCheck(
            code="blocker_measured_threshold",
            passed=not missing and bool(blockers),
            details={"blocker_count": len(blockers), "missing_blocker_event_ids": missing},
        )

    def _broker_order_correlation(self, *, trading_date: date) -> AcceptanceCheck:
        orders = self._list(select(BrokerOrder).where(BrokerOrder.trading_date == trading_date))
        missing = [
            str(order.broker_order_id)
            for order in orders
            if order.request_order_id is None
            or not order.exchange_order_id
            or not (order.tracking_id or order.broker_tracking_id)
        ]
        return AcceptanceCheck(
            code="broker_order_correlation",
            passed=not missing and bool(orders),
            details={"broker_order_count": len(orders), "missing_broker_order_ids": missing},
        )

    def _canceled_orders_have_counterfactuals(
        self,
        *,
        trading_date: date,
        strategy_id: str,
    ) -> AcceptanceCheck:
        canceled_intents = self._list(
            select(OrderIntent).where(
                OrderIntent.trading_date == trading_date,
                OrderIntent.strategy_id == strategy_id,
                OrderIntent.cancel_reason_code.is_not(None),
            )
        )
        counterfactuals = self._list(
            select(CounterfactualResult).where(
                CounterfactualResult.trading_date == trading_date,
                CounterfactualResult.strategy_id == strategy_id,
                CounterfactualResult.cancel_reason_code.is_not(None),
            )
        )
        by_intent_id = {result.order_intent_id for result in counterfactuals}
        missing = [
            str(intent.order_intent_id)
            for intent in canceled_intents
            if intent.order_intent_id not in by_intent_id
        ]
        return AcceptanceCheck(
            code="canceled_order_counterfactual",
            passed=not missing and bool(canceled_intents),
            details={
                "canceled_order_count": len(canceled_intents),
                "missing_order_intent_ids": missing,
            },
        )

    def _daily_report_shape(self, *, trading_date: date, strategy_id: str) -> AcceptanceCheck:
        reports = self._list(
            select(DailyReport).where(
                DailyReport.trading_date == trading_date,
                DailyReport.strategy_id == strategy_id,
            )
        )
        valid = [
            report
            for report in reports
            if report.market_regime
            and _payload_has_non_empty(report.report_payload, "blocker_ranking")
            and _payload_has_non_empty(report.report_payload, "funnel")
            and (report.pnl_net is not None or report.realised_pnl is not None)
        ]
        return AcceptanceCheck(
            code="daily_report_calibration_shape",
            passed=bool(valid),
            details={
                "daily_report_count": len(reports),
                "valid_report_count": len(valid),
                "required_payload_keys": ["blocker_ranking", "funnel"],
            },
        )

    def _hourly_rollover_without_restart(
        self,
        *,
        trading_date: date,
        strategy_id: str,
    ) -> AcceptanceCheck:
        runs = self._list(
            select(SessionRun)
            .where(
                SessionRun.trading_date == trading_date,
                SessionRun.strategy_id == strategy_id,
            )
            .order_by(SessionRun.started_at)
        )
        rollover_found = False
        restart_detected = False
        for previous, current in zip(runs, runs[1:], strict=False):
            previous_instance = previous.run_payload.get("trade_core_instance_id")
            current_instance = current.run_payload.get("trade_core_instance_id")
            if previous.ended_at == current.started_at:
                rollover_found = True
                restart_detected = (
                    restart_detected
                    or previous_instance != current_instance
                    or bool(previous.run_payload.get("physical_restart"))
                    or bool(current.run_payload.get("physical_restart"))
                )
        return AcceptanceCheck(
            code="hourly_rollover_no_trade_core_restart",
            passed=rollover_found and not restart_detected,
            details={"session_run_count": len(runs), "restart_detected": restart_detected},
        )

    def _stream_reconnect_gap_recovery(self, *, trading_date: date) -> AcceptanceCheck:
        actions = {
            event.action
            for event in self._list(
                select(AuditEvent).where(AuditEvent.trading_date == trading_date)
            )
        }
        return AcceptanceCheck(
            code="stream_reconnect_gap_recovery",
            passed={"stream_reconnect", "gap_recovery_completed"}.issubset(actions),
            details={"actions": sorted(actions)},
        )

    def _weekend_session_present(self, *, strategy_id: str) -> AcceptanceCheck:
        weekend_runs = self._list(
            select(SessionRun).where(
                SessionRun.strategy_id == strategy_id,
                SessionRun.session_type == "weekend",
            )
        )
        return AcceptanceCheck(
            code="weekend_session_scenario",
            passed=bool(weekend_runs),
            details={"weekend_session_count": len(weekend_runs)},
        )

    def _no_raw_secrets_in_audit_logs(self, *, trading_date: date) -> AcceptanceCheck:
        audit_events = self._list(select(AuditEvent).where(AuditEvent.trading_date == trading_date))
        leaking_ids: list[str] = []
        for event in audit_events:
            if RAW_SECRET_VALUE_PATTERN.search(f"{event.action} {event.correlation_id or ''}"):
                leaking_ids.append(str(event.audit_event_id))
                continue
            if _payload_contains_raw_secret(event.audit_payload):
                leaking_ids.append(str(event.audit_event_id))
        return AcceptanceCheck(
            code="no_raw_secrets_in_logs",
            passed=not leaking_ids,
            details={
                "audit_event_count": len(audit_events),
                "leaking_audit_event_ids": leaking_ids,
            },
        )

    def _list(self, statement: Any) -> list[Any]:
        return list(self._session.execute(statement).scalars())


def _group_by_candidate(rows: list[Any]) -> dict[UUID, list[Any]]:
    grouped: dict[UUID, list[Any]] = {}
    for row in rows:
        candidate_id = getattr(row, "candidate_id", None)
        if isinstance(candidate_id, UUID):
            grouped.setdefault(candidate_id, []).append(row)
    return grouped


def _payload_has_non_empty(payload: JsonPayload, key: str) -> bool:
    value = payload.get(key)
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    if isinstance(value, (str, int, float, Decimal)):
        return bool(value)
    return value is not None


def _payload_contains_raw_secret(payload: object) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if CREDENTIAL_KEY_PATTERN.search(str(key)) and not _is_redacted(value):
                return True
            if _payload_contains_raw_secret(value):
                return True
        return False
    if isinstance(payload, (list, tuple, set)):
        return any(_payload_contains_raw_secret(value) for value in payload)
    if isinstance(payload, str):
        return RAW_SECRET_VALUE_PATTERN.search(payload) is not None
    return False


def _is_redacted(value: object) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in REDACTED_VALUES
