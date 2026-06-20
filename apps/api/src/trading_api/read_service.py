"""Read-model service for FastAPI BFF endpoints."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_api.schemas import (
    BlockerAnalyticsResponse,
    BlockerAnalyticsRow,
    CanceledOrderDiagnosticsResponse,
    CanceledOrderDiagnosticsRow,
    CandidateFunnelResponse,
    CandidateFunnelStage,
    CounterfactualResponse,
    DailyReportResponse,
    DataShadowStatusResponse,
    HourlyReportResponse,
    JsonPayload,
    MarketInstrumentOverview,
    MarketMicrostructureSnapshotResponse,
    MarketMicrostructureSummaryResponse,
    MarketOverviewResponse,
    MoneyBalance,
    OrderResponse,
    PositionResponse,
    RobotStatusResponse,
    SessionSnapshotResponse,
    SignalResponse,
    StrategyConfigResponse,
    StrategyConfigUpdateRequest,
)
from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    CounterfactualResult,
    DailyReport,
    FillEvent,
    HourlyReport,
    InstrumentRegistry,
    MarketCandle,
    MarketMicrostructureSnapshot,
    OrderBookSummary,
    OrderIntent,
    PositionSnapshot,
    SessionRun,
    SignalCandidate,
    StrategyConfig,
    StrategyStateEvent,
)

TERMINAL_ORDER_STATUSES = frozenset({"filled", "cancelled", "rejected"})
DEFAULT_ANALYTICS_LIMIT = 50


@dataclass(slots=True)
class _BlockerStats:
    blocker_code: str
    blocker_family: str | None = None
    count: int = 0
    terminal_count: int = 0
    candidate_ids: set[UUID] = field(default_factory=set)
    measured_total: Decimal = Decimal("0")
    measured_count: int = 0
    threshold_total: Decimal = Decimal("0")
    threshold_count: int = 0
    missed_pnl_gross: Decimal = Decimal("0")
    missed_pnl_net: Decimal = Decimal("0")
    avoided_loss: Decimal = Decimal("0")
    counterfactual_count: int = 0
    profitable_15m_count: int = 0
    explanation_payload: JsonPayload = field(default_factory=dict)


@dataclass(slots=True)
class _CancelStats:
    cancel_reason_code: str
    count: int = 0
    missed_pnl_gross: Decimal = Decimal("0")
    missed_pnl_net: Decimal = Decimal("0")
    avoided_loss: Decimal = Decimal("0")
    would_profit_5m_count: int = 0
    would_profit_10m_count: int = 0
    would_profit_15m_count: int = 0
    explanation_payload: JsonPayload = field(default_factory=dict)


class BffReadService:
    """Read side for API routes, isolated from route handlers."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def robot_status(self, *, robot_control_state: str) -> RobotStatusResponse:
        current_session = self.current_session()
        open_orders = self.open_orders()
        positions = self.positions()
        latest_state = self._latest_strategy_state()
        active_instruments = self._active_instruments()
        active_timeframes = self._active_timeframes()
        degraded_flags: list[str] = []
        if current_session.session_type == "unknown":
            degraded_flags.append("session_unavailable")
        if not active_instruments:
            degraded_flags.append("no_active_instruments")
        if latest_state == "unknown":
            degraded_flags.append("strategy_state_unavailable")
        degraded_flags.append("balance_unavailable")

        return RobotStatusResponse(
            balance=MoneyBalance(),
            active_instruments=active_instruments,
            active_timeframes=active_timeframes,
            strategy_state=latest_state,
            session_type=current_session.session_type,
            session_phase=current_session.session_phase,
            broker_trading_status=current_session.broker_trading_status,
            micro_session_id=current_session.micro_session_id,
            open_orders_count=len(open_orders),
            active_positions_count=sum(1 for position in positions if position.qty_lots != 0),
            degraded_flags=degraded_flags,
            robot_control_state=robot_control_state,
        )

    def current_session(self) -> SessionSnapshotResponse:
        run = self._session.execute(
            select(SessionRun).order_by(SessionRun.started_at.desc())
        ).scalars().first()
        if run is None:
            return SessionSnapshotResponse()
        return SessionSnapshotResponse(
            calendar_date=run.calendar_date,
            trading_date=run.trading_date,
            session_type=run.session_type,
            session_phase=run.session_phase,
            micro_session_id=run.micro_session_id,
            broker_trading_status=run.broker_trading_status,
            observed_at=run.ended_at or run.started_at,
        )

    def positions(self) -> list[PositionResponse]:
        snapshots = list(
            self._session.execute(
                select(PositionSnapshot).order_by(PositionSnapshot.snapshot_ts.desc())
            ).scalars()
        )
        latest_by_key: dict[tuple[str, str], PositionSnapshot] = {}
        for snapshot in snapshots:
            key = (snapshot.instrument_id, snapshot.account_id)
            latest_by_key.setdefault(key, snapshot)
        return [
            PositionResponse(
                instrument_id=snapshot.instrument_id,
                account_id=snapshot.account_id,
                position_side=snapshot.position_side,
                qty_lots=snapshot.qty_lots,
                avg_price=snapshot.avg_price,
                market_price=snapshot.market_price,
                unrealized_pnl=snapshot.unrealized_pnl,
                realised_pnl=snapshot.realised_pnl,
                snapshot_ts=snapshot.snapshot_ts,
            )
            for snapshot in latest_by_key.values()
        ]

    def open_orders(self) -> list[OrderResponse]:
        broker_orders = list(
            self._session.execute(
                select(BrokerOrder)
                .where(BrokerOrder.broker_status.not_in(TERMINAL_ORDER_STATUSES))
                .order_by(BrokerOrder.last_observed_at.desc())
            ).scalars()
        )
        responses: list[OrderResponse] = []
        for broker_order in broker_orders:
            intent = (
                self._session.get(OrderIntent, broker_order.order_intent_id)
                if broker_order.order_intent_id is not None
                else None
            )
            responses.append(_order_response(broker_order=broker_order, intent=intent))
        return responses

    def current_signals(self, *, limit: int = 20) -> list[SignalResponse]:
        current_session = self.current_session()
        stmt = select(SignalCandidate).order_by(SignalCandidate.ts_utc.desc()).limit(limit)
        if current_session.micro_session_id is not None:
            stmt = (
                select(SignalCandidate)
                .where(SignalCandidate.micro_session_id == current_session.micro_session_id)
                .order_by(SignalCandidate.ts_utc.desc())
                .limit(limit)
            )
        candidates = list(self._session.execute(stmt).scalars())
        final_blockers = self._final_blockers(candidate.candidate_id for candidate in candidates)
        return [
            SignalResponse(
                candidate_id=candidate.candidate_id,
                instrument_id=candidate.instrument_id,
                strategy_id=candidate.strategy_id,
                timeframe=candidate.timeframe,
                side=candidate.side,
                signal_type=candidate.signal_type,
                candidate_status=candidate.candidate_status,
                expected_edge_bps=candidate.expected_edge_bps,
                expected_holding_minutes=candidate.expected_holding_minutes,
                final_blocker_code=final_blockers.get(candidate.candidate_id),
                payload=candidate.signal_payload,
            )
            for candidate in candidates
        ]

    def market_overview(self) -> MarketOverviewResponse:
        summaries = list(
            self._session.execute(
                select(OrderBookSummary).order_by(OrderBookSummary.ts_utc.desc())
            ).scalars()
        )
        latest_by_instrument: dict[str, OrderBookSummary] = {}
        for summary in summaries:
            latest_by_instrument.setdefault(summary.instrument_id, summary)

        instruments = [
            MarketInstrumentOverview(
                instrument_id=summary.instrument_id,
                spread=summary.spread_abs,
                mid_price=summary.mid_price,
                market_quality=summary.market_quality_score,
                best_bid=summary.best_bid_price,
                best_ask=summary.best_ask_price,
                recent_market_trades=_payload_list(summary.summary_payload, "recent_market_trades"),
                order_book_summary={
                    "depth_levels": summary.depth_levels,
                    "best_bid_qty_lots": _optional_str(summary.best_bid_qty_lots),
                    "best_ask_qty_lots": _optional_str(summary.best_ask_qty_lots),
                    "bid_depth_lots": str(summary.bid_depth_lots),
                    "ask_depth_lots": str(summary.ask_depth_lots),
                    "book_imbalance": _optional_str(summary.book_imbalance),
                    "spread_bps": _optional_str(summary.spread_bps),
                    "ts_utc": summary.ts_utc.isoformat(),
                },
            )
            for summary in latest_by_instrument.values()
        ]
        return MarketOverviewResponse(generated_at=datetime.now(tz=UTC), instruments=instruments)

    def latest_microstructure(
        self,
        *,
        instrument_id: str | None = None,
        limit: int = 20,
    ) -> list[MarketMicrostructureSnapshotResponse]:
        stmt = select(MarketMicrostructureSnapshot).order_by(
            MarketMicrostructureSnapshot.ts_utc.desc()
        )
        if instrument_id:
            stmt = stmt.where(MarketMicrostructureSnapshot.instrument_id == instrument_id)
        rows = self._session.execute(stmt.limit(max(1, min(limit, 200)))).scalars()
        return [_microstructure_snapshot_response(row) for row in rows]

    def microstructure_summary(
        self,
        *,
        lookback_minutes: int = 60,
        instrument_id: str | None = None,
    ) -> MarketMicrostructureSummaryResponse:
        since = datetime.now(tz=UTC) - timedelta(minutes=max(1, lookback_minutes))
        stmt = select(MarketMicrostructureSnapshot).where(
            MarketMicrostructureSnapshot.ts_utc >= since
        )
        if instrument_id:
            stmt = stmt.where(MarketMicrostructureSnapshot.instrument_id == instrument_id)
        rows = list(self._session.execute(stmt).scalars())
        spread_values = [row.spread_bps for row in rows if row.spread_bps is not None]
        bid_depth_values = [
            row.bid_depth_lots for row in rows if row.bid_depth_lots is not None
        ]
        ask_depth_values = [
            row.ask_depth_lots for row in rows if row.ask_depth_lots is not None
        ]
        imbalance_values = [
            row.book_imbalance for row in rows if row.book_imbalance is not None
        ]
        quality_values = [
            row.market_quality_score
            for row in rows
            if row.market_quality_score is not None
        ]
        sessions: dict[str, int] = {}
        for row in rows:
            sessions[row.session_type] = sessions.get(row.session_type, 0) + 1
        latest_ts = max((row.ts_utc for row in rows), default=None)
        return MarketMicrostructureSummaryResponse(
            generated_at=datetime.now(tz=UTC),
            lookback_minutes=lookback_minutes,
            instrument_id=instrument_id,
            snapshots_count=len(rows),
            avg_spread_bps=_decimal_avg(spread_values),
            p95_spread_bps=_decimal_percentile(spread_values, 0.95),
            avg_bid_depth_lots=_decimal_avg(bid_depth_values),
            avg_ask_depth_lots=_decimal_avg(ask_depth_values),
            avg_book_imbalance=_decimal_avg(imbalance_values),
            avg_market_quality_score=_decimal_avg(quality_values),
            stale_incidents=sum(1 for row in rows if row.is_stale),
            latest_ts_utc=latest_ts,
            sessions=sessions,
        )

    def data_shadow_status(self) -> DataShadowStatusResponse:
        summary = self.microstructure_summary(lookback_minutes=60)
        latest = self.latest_microstructure(limit=1)
        enabled = _bool_env(os.environ.get("TRADING_DATA_ONLY_SHADOW"))
        last_message_age_seconds: Decimal | None = None
        if summary.latest_ts_utc is not None:
            age_seconds = max(
                Decimal("0"),
                Decimal(str((datetime.now(tz=UTC) - summary.latest_ts_utc).total_seconds())),
            )
            last_message_age_seconds = age_seconds.quantize(Decimal("0.001"))
        return DataShadowStatusResponse(
            enabled=enabled,
            strategy_trading_disabled=enabled,
            real_orders_disabled=True,
            stream_alive=last_message_age_seconds is not None
            and last_message_age_seconds <= Decimal("30"),
            last_message_age_seconds=last_message_age_seconds,
            candles_received=None,
            order_book_snapshots=summary.snapshots_count,
            market_microstructure_snapshots=summary.snapshots_count,
            avg_spread_bps=summary.avg_spread_bps,
            p95_spread_bps=summary.p95_spread_bps,
            avg_market_quality_score=summary.avg_market_quality_score,
            current_session=latest[0].session_type if latest else None,
            warning=(
                "Strategy trading disabled: data-only shadow mode"
                if enabled
                else "Data-only shadow mode is disabled"
            ),
        )

    def hourly_reports(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        blocker_code: str | None = None,
        limit: int = 50,
    ) -> list[HourlyReportResponse]:
        stmt = select(HourlyReport).order_by(HourlyReport.generated_at.desc())
        if trading_date is not None:
            stmt = stmt.where(HourlyReport.trading_date == trading_date)
        if strategy_id is not None:
            stmt = stmt.where(HourlyReport.strategy_id == strategy_id)
        if instrument_id is not None:
            stmt = stmt.where(HourlyReport.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(HourlyReport.timeframe == timeframe)
        if session_type is not None:
            stmt = stmt.where(HourlyReport.session_type == session_type)
        if blocker_code is None:
            stmt = stmt.limit(limit)
        reports = [
            _hourly_report_response(report) for report in self._session.execute(stmt).scalars()
        ]
        if blocker_code is not None:
            reports = [
                report for report in reports if _payload_mentions(report.payload, blocker_code)
            ][:limit]
        return reports

    def daily_reports(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        blocker_code: str | None = None,
        limit: int = 50,
    ) -> list[DailyReportResponse]:
        stmt = select(DailyReport).order_by(DailyReport.generated_at.desc())
        if trading_date is not None:
            stmt = stmt.where(DailyReport.trading_date == trading_date)
        if strategy_id is not None:
            stmt = stmt.where(DailyReport.strategy_id == strategy_id)
        if instrument_id is not None:
            stmt = stmt.where(DailyReport.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(DailyReport.timeframe == timeframe)
        if session_type is not None:
            stmt = stmt.where(DailyReport.session_type == session_type)
        if blocker_code is None:
            stmt = stmt.limit(limit)
        reports = [
            _daily_report_response(report) for report in self._session.execute(stmt).scalars()
        ]
        if blocker_code is not None:
            reports = [
                report for report in reports if _payload_mentions(report.payload, blocker_code)
            ][:limit]
        return reports

    def counterfactual_reports(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        blocker_code: str | None = None,
        limit: int = 100,
    ) -> list[CounterfactualResponse]:
        stmt = (
            select(CounterfactualResult)
            .order_by(CounterfactualResult.generated_at.desc())
            .limit(limit)
        )
        if trading_date is not None:
            stmt = stmt.where(CounterfactualResult.trading_date == trading_date)
        if strategy_id is not None:
            stmt = stmt.where(CounterfactualResult.strategy_id == strategy_id)
        if instrument_id is not None:
            stmt = stmt.where(CounterfactualResult.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(CounterfactualResult.timeframe == timeframe)
        if session_type is not None:
            stmt = stmt.where(CounterfactualResult.session_type == session_type)
        if blocker_code is not None:
            stmt = stmt.where(CounterfactualResult.blocker_code == blocker_code)
        return [
            _counterfactual_response(result) for result in self._session.execute(stmt).scalars()
        ]

    def blocker_analytics(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        blocker_code: str | None = None,
        strategy_version: int | None = None,
        limit: int = DEFAULT_ANALYTICS_LIMIT,
    ) -> BlockerAnalyticsResponse:
        blockers = self._blockers(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            blocker_code=blocker_code,
        )
        stats_by_code: dict[str, _BlockerStats] = {}
        for blocker in blockers:
            candidate = (
                self._session.get(SignalCandidate, blocker.candidate_id)
                if blocker.candidate_id is not None
                else None
            )
            if strategy_version is not None and (
                candidate is None or candidate.strategy_version != strategy_version
            ):
                continue
            code = _blocker_code(blocker)
            stats = stats_by_code.setdefault(
                code,
                _BlockerStats(
                    blocker_code=code,
                    blocker_family=blocker.blocker_family,
                ),
            )
            stats.count += 1
            if blocker.is_final_blocker:
                stats.terminal_count += 1
            if blocker.candidate_id is not None:
                stats.candidate_ids.add(blocker.candidate_id)
            if blocker.blocker_family and stats.blocker_family is None:
                stats.blocker_family = blocker.blocker_family
            if blocker.measured_value is not None:
                stats.measured_total += blocker.measured_value
                stats.measured_count += 1
            if blocker.threshold_value is not None:
                stats.threshold_total += blocker.threshold_value
                stats.threshold_count += 1
            if not stats.explanation_payload:
                stats.explanation_payload = blocker.explanation_payload or blocker.reason_payload

        for result in self._counterfactuals(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            blocker_code=blocker_code,
        ):
            if result.blocker_code is None:
                continue
            stats = stats_by_code.setdefault(
                result.blocker_code,
                _BlockerStats(blocker_code=result.blocker_code),
            )
            stats.counterfactual_count += 1
            stats.missed_pnl_gross += result.pnl_gross or Decimal("0")
            stats.missed_pnl_net += result.pnl_net or Decimal("0")
            if result.pnl_net is not None and result.pnl_net < 0:
                stats.avoided_loss += abs(result.pnl_net)
            if result.would_profit_15m:
                stats.profitable_15m_count += 1

        rows = sorted(
            (_blocker_row(stats) for stats in stats_by_code.values()),
            key=lambda row: (row.count, row.missed_pnl_net or Decimal("0")),
            reverse=True,
        )[:limit]
        return BlockerAnalyticsResponse(
            generated_at=datetime.now(tz=UTC),
            filters=_analytics_filters(
                trading_date=trading_date,
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                session_type=session_type,
                blocker_code=blocker_code,
                strategy_version=strategy_version,
            ),
            rows=rows,
        )

    def candidate_funnel(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        blocker_code: str | None = None,
        strategy_version: int | None = None,
    ) -> CandidateFunnelResponse:
        candidates = self._candidates(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            strategy_version=strategy_version,
        )
        candidate_ids = {candidate.candidate_id for candidate in candidates}
        blocked_ids = {
            blocker.candidate_id
            for blocker in self._blockers(
                trading_date=trading_date,
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                session_type=session_type,
                blocker_code=blocker_code,
            )
            if blocker.candidate_id is not None and blocker.is_final_blocker
        }
        if blocker_code is not None:
            candidate_ids &= blocked_ids
            candidates = [
                candidate for candidate in candidates if candidate.candidate_id in candidate_ids
            ]

        stage_results = self._stage_results(candidate_ids)
        passed_gate_ids = {
            stage.candidate_id for stage in stage_results if stage.passed and stage.candidate_id
        }
        intents = self._order_intents(candidate_ids=candidate_ids)
        intent_ids = {intent.order_intent_id for intent in intents}
        intent_candidate_ids = {
            intent.candidate_id for intent in intents if intent.candidate_id is not None
        }
        posted_candidate_ids = self._posted_candidate_ids(candidate_ids, intent_ids)
        filled_candidate_ids = self._filled_candidate_ids(candidate_ids, intent_ids)
        exited_ids = {
            candidate.candidate_id
            for candidate in candidates
            if candidate.candidate_status in {"exited", "closed"}
        } | filled_candidate_ids

        created_count = len(candidates)
        stages: list[tuple[str, int, JsonPayload]] = [
            ("created", created_count, {}),
            ("passed_gates", len(passed_gate_ids), {"stage_result_count": len(stage_results)}),
            ("blocked", len(blocked_ids & candidate_ids), {"terminal_blocker": True}),
            ("order_intent", len(intent_candidate_ids), {}),
            ("posted", len(posted_candidate_ids), {}),
            ("filled", len(filled_candidate_ids), {}),
            ("exited", len(exited_ids), {}),
        ]
        return CandidateFunnelResponse(
            generated_at=datetime.now(tz=UTC),
            filters=_analytics_filters(
                trading_date=trading_date,
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                session_type=session_type,
                blocker_code=blocker_code,
                strategy_version=strategy_version,
            ),
            stages=[
                CandidateFunnelStage(
                    stage_name=name,
                    count=count,
                    percentage_of_created=_ratio_decimal(count, created_count),
                    payload=payload,
                )
                for name, count, payload in stages
            ],
            totals={
                "candidate_count": created_count,
                "blocked_candidate_count": len(blocked_ids & candidate_ids),
                "order_intent_count": len(intents),
            },
        )

    def canceled_order_diagnostics(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        strategy_version: int | None = None,
        limit: int = DEFAULT_ANALYTICS_LIMIT,
    ) -> CanceledOrderDiagnosticsResponse:
        stats_by_reason: dict[str, _CancelStats] = {}
        stmt = select(OrderIntent).where(OrderIntent.cancel_reason_code.is_not(None))
        if trading_date is not None:
            stmt = stmt.where(OrderIntent.trading_date == trading_date)
        if strategy_id is not None:
            stmt = stmt.where(OrderIntent.strategy_id == strategy_id)
        if instrument_id is not None:
            stmt = stmt.where(OrderIntent.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(OrderIntent.timeframe == timeframe)
        if session_type is not None:
            stmt = stmt.where(OrderIntent.session_type == session_type)
        if strategy_version is not None:
            stmt = stmt.where(OrderIntent.strategy_version == strategy_version)
        intents = list(self._session.execute(stmt).scalars())
        for intent in intents:
            if intent.cancel_reason_code is None:
                continue
            stats = stats_by_reason.setdefault(
                intent.cancel_reason_code,
                _CancelStats(cancel_reason_code=intent.cancel_reason_code),
            )
            stats.count += 1
            if not stats.explanation_payload:
                stats.explanation_payload = intent.intent_payload

        for result in self._counterfactuals(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
        ):
            if result.cancel_reason_code is None:
                continue
            stats = stats_by_reason.setdefault(
                result.cancel_reason_code,
                _CancelStats(cancel_reason_code=result.cancel_reason_code),
            )
            stats.missed_pnl_gross += result.pnl_gross or Decimal("0")
            stats.missed_pnl_net += result.pnl_net or Decimal("0")
            if result.pnl_net is not None and result.pnl_net < 0:
                stats.avoided_loss += abs(result.pnl_net)
            if result.would_profit_5m:
                stats.would_profit_5m_count += 1
            if result.would_profit_10m:
                stats.would_profit_10m_count += 1
            if result.would_profit_15m:
                stats.would_profit_15m_count += 1
            if not stats.explanation_payload:
                stats.explanation_payload = result.result_payload

        rows = sorted(
            (_cancel_row(stats) for stats in stats_by_reason.values()),
            key=lambda row: (row.count, row.missed_pnl_net or Decimal("0")),
            reverse=True,
        )[:limit]
        return CanceledOrderDiagnosticsResponse(
            generated_at=datetime.now(tz=UTC),
            filters=_analytics_filters(
                trading_date=trading_date,
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                session_type=session_type,
                strategy_version=strategy_version,
            ),
            rows=rows,
        )

    def get_strategy_config(
        self,
        *,
        strategy_id: str,
        session_template: str,
    ) -> StrategyConfigResponse:
        config = self._session.execute(
            select(StrategyConfig)
            .where(
                StrategyConfig.strategy_id == strategy_id,
                StrategyConfig.session_template == session_template,
                StrategyConfig.is_active.is_(True),
            )
            .order_by(StrategyConfig.version.desc())
        ).scalars().first()
        if config is None:
            return StrategyConfigResponse(
                strategy_id=strategy_id,
                version=0,
                session_template=session_template,
                is_active=False,
            )
        return _strategy_config_response(config)

    def update_strategy_config(
        self,
        request: StrategyConfigUpdateRequest,
    ) -> StrategyConfigResponse:
        active_configs = list(
            self._session.execute(
                select(StrategyConfig).where(
                    StrategyConfig.strategy_id == request.strategy_id,
                    StrategyConfig.session_template == request.session_template,
                    StrategyConfig.is_active.is_(True),
                )
            ).scalars()
        )
        next_version = max((config.version for config in active_configs), default=0) + 1
        for config in active_configs:
            config.is_active = False
            config.valid_to = datetime.now(tz=UTC)
        new_config = StrategyConfig(
            strategy_id=request.strategy_id,
            version=next_version,
            session_template=request.session_template,
            is_active=True,
            valid_from=datetime.now(tz=UTC),
            valid_to=None,
            config_payload={
                **request.config_payload,
                "updated_by": request.actor,
            },
            risk_limits=request.risk_limits,
        )
        self._session.add(new_config)
        self._session.flush()
        return _strategy_config_response(new_config)

    def _active_instruments(self) -> list[str]:
        instruments = list(
            self._session.execute(
                select(InstrumentRegistry)
                .where(InstrumentRegistry.is_enabled.is_(True))
                .order_by(InstrumentRegistry.instrument_id)
            ).scalars()
        )
        if instruments:
            return [instrument.instrument_id for instrument in instruments]
        candle_instruments = self._session.execute(
            select(MarketCandle.instrument_id).distinct().order_by(MarketCandle.instrument_id)
        ).scalars()
        return list(candle_instruments)

    def _active_timeframes(self) -> list[str]:
        timeframes = self._session.execute(
            select(MarketCandle.timeframe).distinct().order_by(MarketCandle.timeframe)
        ).scalars()
        return list(timeframes)

    def _latest_strategy_state(self) -> str:
        event = self._session.execute(
            select(StrategyStateEvent).order_by(StrategyStateEvent.ts_utc.desc())
        ).scalars().first()
        return event.new_state if event is not None else "unknown"

    def _final_blockers(self, candidate_ids: Iterable[UUID]) -> dict[UUID, str]:
        ids = tuple(candidate_ids)
        if not ids:
            return {}
        blockers = list(
            self._session.execute(
                select(BlockerEvent).where(
                    BlockerEvent.candidate_id.in_(ids),
                    BlockerEvent.is_final_blocker.is_(True),
                )
            ).scalars()
        )
        return {
            blocker.candidate_id: _blocker_code(blocker)
            for blocker in blockers
            if blocker.candidate_id is not None
        }

    def _candidates(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        strategy_version: int | None = None,
    ) -> list[SignalCandidate]:
        stmt = select(SignalCandidate).order_by(SignalCandidate.ts_utc.desc())
        if trading_date is not None:
            stmt = stmt.where(SignalCandidate.trading_date == trading_date)
        if strategy_id is not None:
            stmt = stmt.where(SignalCandidate.strategy_id == strategy_id)
        if instrument_id is not None:
            stmt = stmt.where(SignalCandidate.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(SignalCandidate.timeframe == timeframe)
        if session_type is not None:
            stmt = stmt.where(SignalCandidate.session_type == session_type)
        if strategy_version is not None:
            stmt = stmt.where(SignalCandidate.strategy_version == strategy_version)
        return list(self._session.execute(stmt).scalars())

    def _blockers(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        blocker_code: str | None = None,
    ) -> list[BlockerEvent]:
        stmt = select(BlockerEvent).order_by(BlockerEvent.ts_utc.desc())
        if trading_date is not None:
            stmt = stmt.where(BlockerEvent.trading_date == trading_date)
        if strategy_id is not None:
            stmt = stmt.where(BlockerEvent.strategy_id == strategy_id)
        if instrument_id is not None:
            stmt = stmt.where(BlockerEvent.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(BlockerEvent.timeframe == timeframe)
        if session_type is not None:
            stmt = stmt.where(BlockerEvent.session_type == session_type)
        if blocker_code is not None:
            stmt = stmt.where(
                (BlockerEvent.blocker_code == blocker_code)
                | (BlockerEvent.reason_code == blocker_code)
            )
        return list(self._session.execute(stmt).scalars())

    def _counterfactuals(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        blocker_code: str | None = None,
    ) -> list[CounterfactualResult]:
        stmt = select(CounterfactualResult).order_by(CounterfactualResult.generated_at.desc())
        if trading_date is not None:
            stmt = stmt.where(CounterfactualResult.trading_date == trading_date)
        if strategy_id is not None:
            stmt = stmt.where(CounterfactualResult.strategy_id == strategy_id)
        if instrument_id is not None:
            stmt = stmt.where(CounterfactualResult.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(CounterfactualResult.timeframe == timeframe)
        if session_type is not None:
            stmt = stmt.where(CounterfactualResult.session_type == session_type)
        if blocker_code is not None:
            stmt = stmt.where(CounterfactualResult.blocker_code == blocker_code)
        return list(self._session.execute(stmt).scalars())

    def _stage_results(self, candidate_ids: set[UUID]) -> list[CandidateStageResult]:
        if not candidate_ids:
            return []
        return list(
            self._session.execute(
                select(CandidateStageResult).where(
                    CandidateStageResult.candidate_id.in_(candidate_ids)
                )
            ).scalars()
        )

    def _order_intents(self, *, candidate_ids: set[UUID]) -> list[OrderIntent]:
        if not candidate_ids:
            return []
        return list(
            self._session.execute(
                select(OrderIntent).where(OrderIntent.candidate_id.in_(candidate_ids))
            ).scalars()
        )

    def _posted_candidate_ids(
        self,
        candidate_ids: set[UUID],
        intent_ids: set[UUID],
    ) -> set[UUID]:
        if not candidate_ids and not intent_ids:
            return set()
        stmt = select(BrokerOrder)
        if candidate_ids and intent_ids:
            stmt = stmt.where(
                (BrokerOrder.candidate_id.in_(candidate_ids))
                | (BrokerOrder.order_intent_id.in_(intent_ids))
            )
        elif candidate_ids:
            stmt = stmt.where(BrokerOrder.candidate_id.in_(candidate_ids))
        else:
            stmt = stmt.where(BrokerOrder.order_intent_id.in_(intent_ids))
        broker_orders = list(self._session.execute(stmt).scalars())
        posted_ids: set[UUID] = set()
        intent_candidate_by_id = {
            intent.order_intent_id: intent.candidate_id
            for intent in self._session.execute(
                select(OrderIntent).where(OrderIntent.order_intent_id.in_(intent_ids))
            ).scalars()
            if intent.candidate_id is not None
        }
        for order in broker_orders:
            if order.candidate_id is not None:
                posted_ids.add(order.candidate_id)
            elif order.order_intent_id in intent_candidate_by_id:
                posted_ids.add(intent_candidate_by_id[order.order_intent_id])
        return posted_ids

    def _filled_candidate_ids(
        self,
        candidate_ids: set[UUID],
        intent_ids: set[UUID],
    ) -> set[UUID]:
        if not candidate_ids and not intent_ids:
            return set()
        stmt = select(FillEvent)
        if candidate_ids and intent_ids:
            stmt = stmt.where(
                (FillEvent.candidate_id.in_(candidate_ids))
                | (FillEvent.order_intent_id.in_(intent_ids))
            )
        elif candidate_ids:
            stmt = stmt.where(FillEvent.candidate_id.in_(candidate_ids))
        else:
            stmt = stmt.where(FillEvent.order_intent_id.in_(intent_ids))
        fills = list(self._session.execute(stmt).scalars())
        filled_ids: set[UUID] = set()
        intent_candidate_by_id = {
            intent.order_intent_id: intent.candidate_id
            for intent in self._session.execute(
                select(OrderIntent).where(OrderIntent.order_intent_id.in_(intent_ids))
            ).scalars()
            if intent.candidate_id is not None
        }
        for fill in fills:
            if fill.candidate_id is not None:
                filled_ids.add(fill.candidate_id)
            elif fill.order_intent_id in intent_candidate_by_id:
                filled_ids.add(intent_candidate_by_id[fill.order_intent_id])
        return filled_ids


def _order_response(*, broker_order: BrokerOrder, intent: OrderIntent | None) -> OrderResponse:
    return OrderResponse(
        order_intent_id=broker_order.order_intent_id,
        request_order_id=broker_order.request_order_id,
        exchange_order_id=broker_order.exchange_order_id,
        instrument_id=intent.instrument_id if intent is not None else None,
        side=intent.side if intent is not None else None,
        order_type=intent.order_type if intent is not None else None,
        lot_qty=intent.lot_qty if intent is not None else None,
        intended_price=intent.intended_price if intent is not None else None,
        broker_status=broker_order.broker_status,
        cancel_reason_code=intent.cancel_reason_code if intent is not None else None,
        reject_reason_code=broker_order.reject_reason_code
        or (intent.reject_reason_code if intent is not None else None),
        last_observed_at=broker_order.last_observed_at,
    )


def _blocker_code(blocker: BlockerEvent) -> str:
    return blocker.blocker_code or blocker.reason_code


def _blocker_row(stats: _BlockerStats) -> BlockerAnalyticsRow:
    return BlockerAnalyticsRow(
        blocker_code=stats.blocker_code,
        blocker_family=stats.blocker_family,
        count=stats.count,
        terminal_count=stats.terminal_count,
        candidate_count=len(stats.candidate_ids),
        measured_value_avg=_decimal_average(stats.measured_total, stats.measured_count),
        threshold_value_avg=_decimal_average(stats.threshold_total, stats.threshold_count),
        missed_pnl_gross=stats.missed_pnl_gross,
        missed_pnl_net=stats.missed_pnl_net,
        avoided_loss=stats.avoided_loss,
        false_positive_rate=_ratio_decimal(
            stats.profitable_15m_count,
            stats.counterfactual_count,
        ),
        explanation_payload=stats.explanation_payload,
    )


def _cancel_row(stats: _CancelStats) -> CanceledOrderDiagnosticsRow:
    return CanceledOrderDiagnosticsRow(
        cancel_reason_code=stats.cancel_reason_code,
        count=stats.count,
        missed_pnl_gross=stats.missed_pnl_gross,
        missed_pnl_net=stats.missed_pnl_net,
        avoided_loss=stats.avoided_loss,
        would_profit_5m_count=stats.would_profit_5m_count,
        would_profit_10m_count=stats.would_profit_10m_count,
        would_profit_15m_count=stats.would_profit_15m_count,
        explanation_payload=stats.explanation_payload,
    )


def _decimal_average(total: Decimal, count: int) -> Decimal | None:
    if count == 0:
        return None
    return total / Decimal(count)


def _ratio_decimal(numerator: int, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


def _analytics_filters(
    *,
    trading_date: date | None = None,
    strategy_id: str | None = None,
    instrument_id: str | None = None,
    timeframe: str | None = None,
    session_type: str | None = None,
    blocker_code: str | None = None,
    strategy_version: int | None = None,
) -> JsonPayload:
    return {
        key: value
        for key, value in {
            "trading_date": trading_date.isoformat() if trading_date is not None else None,
            "strategy_id": strategy_id,
            "instrument_id": instrument_id,
            "timeframe": timeframe,
            "session_type": session_type,
            "blocker_code": blocker_code,
            "strategy_version": strategy_version,
        }.items()
        if value is not None
    }


def _payload_mentions(payload: JsonPayload, value: str) -> bool:
    return value in str(payload)


def _payload_list(payload: JsonPayload, key: str) -> list[JsonPayload]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _hourly_report_response(report: HourlyReport) -> HourlyReportResponse:
    return HourlyReportResponse(
        hourly_report_id=report.hourly_report_id,
        trading_date=report.trading_date,
        session_type=report.session_type,
        micro_session_id=report.micro_session_id,
        strategy_id=report.strategy_id,
        instrument_id=report.instrument_id,
        timeframe=report.timeframe,
        realised_pnl=report.realised_pnl,
        commission=report.commission,
        signal_count=report.signal_count,
        blocked_count=report.blocked_count,
        fill_ratio=report.fill_ratio,
        payload=report.report_payload,
    )


def _daily_report_response(report: DailyReport) -> DailyReportResponse:
    return DailyReportResponse(
        daily_report_id=report.daily_report_id,
        trading_date=report.trading_date,
        strategy_id=report.strategy_id,
        market_regime=report.market_regime,
        session_type=report.session_type,
        instrument_id=report.instrument_id,
        timeframe=report.timeframe,
        realised_pnl=report.realised_pnl,
        commission=report.commission,
        signal_count=report.signal_count,
        blocked_count=report.blocked_count,
        fill_ratio=report.fill_ratio,
        payload=report.report_payload,
    )


def _counterfactual_response(result: CounterfactualResult) -> CounterfactualResponse:
    return CounterfactualResponse(
        counterfactual_result_id=result.counterfactual_result_id,
        trading_date=result.trading_date,
        candidate_id=result.candidate_id,
        order_intent_id=result.order_intent_id,
        source_event_type=result.source_event_type,
        instrument_id=result.instrument_id,
        timeframe=result.timeframe,
        strategy_id=result.strategy_id,
        blocker_code=result.blocker_code,
        cancel_reason_code=result.cancel_reason_code,
        pnl_gross=result.pnl_gross,
        pnl_net=result.pnl_net,
        slippage_bp=result.slippage_bp,
        mfe_5m_bps=result.mfe_5m_bps,
        mae_5m_bps=result.mae_5m_bps,
        mfe_10m_bps=result.mfe_10m_bps,
        mae_10m_bps=result.mae_10m_bps,
        mfe_15m_bps=result.mfe_15m_bps,
        mae_15m_bps=result.mae_15m_bps,
        would_profit_5m=result.would_profit_5m,
        would_profit_10m=result.would_profit_10m,
        would_profit_15m=result.would_profit_15m,
        payload=result.result_payload,
    )


def _strategy_config_response(config: StrategyConfig) -> StrategyConfigResponse:
    return StrategyConfigResponse(
        strategy_config_id=config.strategy_config_id,
        strategy_id=config.strategy_id,
        version=config.version,
        session_template=config.session_template,
        is_active=config.is_active,
        valid_from=config.valid_from,
        valid_to=config.valid_to,
        config_payload=config.config_payload,
        risk_limits=config.risk_limits,
    )


def _microstructure_snapshot_response(
    snapshot: MarketMicrostructureSnapshot,
) -> MarketMicrostructureSnapshotResponse:
    return MarketMicrostructureSnapshotResponse(
        snapshot_id=snapshot.snapshot_id,
        ts_utc=snapshot.ts_utc,
        exchange_ts=snapshot.exchange_ts,
        received_ts=snapshot.received_ts,
        instrument_id=snapshot.instrument_id,
        session_type=snapshot.session_type,
        session_phase=snapshot.session_phase,
        micro_session_id=snapshot.micro_session_id,
        broker_trading_status=snapshot.broker_trading_status,
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
        mid_price=snapshot.mid_price,
        spread_abs=snapshot.spread_abs,
        spread_bps=snapshot.spread_bps,
        bid_depth_lots=snapshot.bid_depth_lots,
        ask_depth_lots=snapshot.ask_depth_lots,
        book_imbalance=snapshot.book_imbalance,
        market_quality_score=snapshot.market_quality_score,
        feed_freshness_age_ms=snapshot.feed_freshness_age_ms,
        is_stale=snapshot.is_stale,
        source=snapshot.source,
        payload=snapshot.snapshot_payload,
    )


def _decimal_avg(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(Decimal("0.0001"))


def _decimal_percentile(values: list[Decimal], percentile: float) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index].quantize(Decimal("0.0001"))


def _bool_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None
