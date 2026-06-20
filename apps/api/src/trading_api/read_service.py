"""Read-model service for FastAPI BFF endpoints."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_api.schemas import (
    BlockerAnalyticsResponse,
    BlockerAnalyticsRow,
    CalibrationDiagnosticRunResponse,
    CalibrationObservatoryStatusResponse,
    CanceledOrderDiagnosticsResponse,
    CanceledOrderDiagnosticsRow,
    CandidateFunnelResponse,
    CandidateFunnelStage,
    CounterfactualResponse,
    DailyReportResponse,
    DataShadowStatusResponse,
    HourlyReportResponse,
    IntradayAnalyticsSnapshotResponse,
    JsonPayload,
    MarketInstrumentOverview,
    MarketMicrostructureSnapshotResponse,
    MarketMicrostructureSummaryResponse,
    MarketOverviewResponse,
    MarketRegimeSnapshotResponse,
    MoneyBalance,
    OrderResponse,
    PortfolioSummaryResponse,
    PositionResponse,
    RobotStatusResponse,
    RollingPerformanceCubeResponse,
    SessionSnapshotResponse,
    SignalResponse,
    StrategyConfigCandidateResponse,
    StrategyConfigResponse,
    StrategyConfigUpdateRequest,
)
from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CalibrationDiagnosticRun,
    CandidateStageResult,
    CounterfactualResult,
    DailyReport,
    FillEvent,
    HourlyReport,
    InstrumentRegistry,
    IntradaySessionAnalytics,
    MarketCandle,
    MarketMicrostructureSnapshot,
    MarketRegimeSnapshot,
    OrderBookSummary,
    OrderIntent,
    PositionSnapshot,
    RollingPerformanceCube,
    SessionRun,
    SignalCandidate,
    StrategyConfig,
    StrategyConfigCandidate,
    StrategyStateEvent,
)

TERMINAL_ORDER_STATUSES = frozenset({"filled", "cancelled", "rejected"})
DEFAULT_ANALYTICS_LIMIT = 50
DEFAULT_DASHBOARD_UNIVERSE = ("SBER", "GAZP", "LKOH", "YDEX", "TATN", "GMKN", "OZON", "VTBR")


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
        portfolio_summary = self.portfolio_summary()
        if portfolio_summary.balance.balance_degraded:
            degraded_flags.append("balance_unavailable")

        return RobotStatusResponse(
            balance=portfolio_summary.balance,
            active_instruments=active_instruments,
            active_timeframes=active_timeframes,
            strategy_state=latest_state,
            session_type=current_session.session_type,
            session_phase=current_session.session_phase,
            broker_trading_status=current_session.broker_trading_status,
            micro_session_id=current_session.micro_session_id,
            open_orders_count=self._open_orders_count(),
            active_positions_count=self._active_positions_count(),
            degraded_flags=degraded_flags,
            robot_control_state=robot_control_state,
        )

    def portfolio_summary(self) -> PortfolioSummaryResponse:
        latest_snapshot_ts = self._latest_position_snapshot_ts()
        if latest_snapshot_ts is None:
            return PortfolioSummaryResponse(
                balance=MoneyBalance(
                    balance_degraded=True,
                    balance_degraded_reason_code="broker_balance_unavailable",
                ),
                positions_count=0,
                source="position_snapshot_missing",
            )
        snapshots = self._position_snapshots_at(latest_snapshot_ts)
        if not snapshots:
            return PortfolioSummaryResponse(
                balance=MoneyBalance(
                    balance_degraded=True,
                    balance_degraded_reason_code="position_snapshot_empty",
                ),
                positions_count=0,
                source="position_snapshot_empty",
            )
        latest = snapshots[0]
        balance_payload = _payload_dict_value(latest.snapshot_payload, "broker_balance")
        if balance_payload:
            balance = _money_balance_from_payload(balance_payload, latest=latest)
            return PortfolioSummaryResponse(
                balance=balance,
                positions_count=len(snapshots),
                source=str(balance_payload.get("source", "broker_balance_payload")),
            )
        return PortfolioSummaryResponse(
            balance=_money_balance_from_positions(snapshots),
            positions_count=len(snapshots),
            source="position_snapshot_derived",
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
        latest_snapshot_ts = self._latest_position_snapshot_ts()
        if latest_snapshot_ts is None:
            return []
        snapshots = self._position_snapshots_at(latest_snapshot_ts)
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

    def open_orders(self, *, limit: int = 100) -> list[OrderResponse]:
        since = datetime.now(tz=UTC) - timedelta(days=30)
        broker_orders = list(
            self._session.execute(
                select(BrokerOrder)
                .where(
                    BrokerOrder.broker_status.not_in(TERMINAL_ORDER_STATUSES),
                    BrokerOrder.last_observed_at >= since,
                )
                .order_by(BrokerOrder.last_observed_at.desc())
                .limit(max(1, min(limit, 500)))
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
        instruments = _dashboard_universe_from_env()
        overviews = [
            MarketInstrumentOverview(
                **self._market_instrument_payload(instrument_id)
            )
            for instrument_id in instruments
        ]
        return MarketOverviewResponse(generated_at=datetime.now(tz=UTC), instruments=overviews)

    def _market_instrument_payload(self, instrument_id: str) -> JsonPayload:
        summary = self._latest_order_book_summary(instrument_id)
        candle = self._latest_market_candle(instrument_id)
        last_price = summary.mid_price if summary and summary.mid_price is not None else None
        last_price_at = summary.ts_utc if last_price is not None and summary is not None else None
        last_price_source = "live_order_book" if last_price is not None else None
        quote_status = "live" if last_price is not None else "unavailable"
        if last_price is None and candle is not None:
            last_price = candle.close_price
            last_price_at = candle.close_ts_utc
            last_price_source = "last_candle"
            quote_status = "last_close"

        order_book_summary: JsonPayload = {}
        recent_market_trades: list[JsonPayload] = []
        if summary is not None:
            recent_market_trades = _payload_list(summary.summary_payload, "recent_market_trades")
            order_book_summary = {
                "depth_levels": summary.depth_levels,
                "best_bid_qty_lots": _optional_str(summary.best_bid_qty_lots),
                "best_ask_qty_lots": _optional_str(summary.best_ask_qty_lots),
                "bid_depth_lots": str(summary.bid_depth_lots),
                "ask_depth_lots": str(summary.ask_depth_lots),
                "book_imbalance": _optional_str(summary.book_imbalance),
                "spread_bps": _optional_str(summary.spread_bps),
                "ts_utc": summary.ts_utc.isoformat(),
            }
        elif candle is not None:
            order_book_summary = {
                "last_candle_open": str(candle.open_price),
                "last_candle_high": str(candle.high_price),
                "last_candle_low": str(candle.low_price),
                "last_candle_close": str(candle.close_price),
                "last_candle_volume_lots": str(candle.volume_lots),
                "last_candle_close_ts": candle.close_ts_utc.isoformat(),
            }

        return {
            "instrument_id": instrument_id,
            "last_price": last_price,
            "last_price_at": last_price_at,
            "last_price_source": last_price_source,
            "quote_status": quote_status,
            "last_candle_timeframe": candle.timeframe if candle is not None else None,
            "spread": summary.spread_abs if summary is not None else None,
            "mid_price": summary.mid_price if summary is not None else None,
            "market_quality": summary.market_quality_score if summary is not None else None,
            "best_bid": summary.best_bid_price if summary is not None else None,
            "best_ask": summary.best_ask_price if summary is not None else None,
            "recent_market_trades": recent_market_trades,
            "order_book_summary": order_book_summary,
        }

    def _latest_order_book_summary(self, instrument_id: str) -> OrderBookSummary | None:
        return self._session.execute(
            select(OrderBookSummary)
            .where(OrderBookSummary.instrument_id == instrument_id)
            .order_by(OrderBookSummary.ts_utc.desc())
            .limit(1)
        ).scalars().first()

    def _latest_market_candle(self, instrument_id: str) -> MarketCandle | None:
        candle = self._session.execute(
            select(MarketCandle)
            .where(
                MarketCandle.instrument_id == instrument_id,
                MarketCandle.timeframe == "1m",
            )
            .order_by(MarketCandle.open_ts_utc.desc())
            .limit(1)
        ).scalars().first()
        if candle is not None:
            return candle
        return self._session.execute(
            select(MarketCandle)
            .where(MarketCandle.instrument_id == instrument_id)
            .order_by(MarketCandle.open_ts_utc.desc())
            .limit(1)
        ).scalars().first()

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

    def intraday_analytics_snapshot(
        self,
        *,
        trading_date: date | None = None,
        session_type: str | None = None,
        micro_session_id: str | None = None,
        mode: str = "all",
    ) -> IntradayAnalyticsSnapshotResponse:
        target_date = trading_date or datetime.now(tz=UTC).date()
        rows = self._intraday_rows(
            trading_date=target_date,
            session_type=session_type,
            micro_session_id=micro_session_id,
            mode=mode,
        )
        if not rows:
            from report_worker.analytics.calibration_observatory import (
                IntradayAnalyticsService,
            )

            service = IntradayAnalyticsService(self._session)
            if micro_session_id is not None:
                payload = service.build_for_micro_session(micro_session_id)
            elif session_type is not None:
                payload = service.build_for_session(target_date, session_type, mode=mode)
            else:
                payload = service.build_for_trading_date(target_date, mode=mode)
            return _intraday_snapshot_response(payload)
        return _intraday_snapshot_response(_intraday_payload_from_rows(rows))

    def calibration_observatory_status(self) -> CalibrationObservatoryStatusResponse:
        latest_diagnostic = self._session.execute(
            select(CalibrationDiagnosticRun).order_by(
                CalibrationDiagnosticRun.created_at.desc()
            )
        ).scalars().first()
        latest_cube_generated_at = self._session.execute(
            select(RollingPerformanceCube.generated_at).order_by(
                RollingPerformanceCube.generated_at.desc()
            )
        ).scalars().first()
        latest_regime_generated_at = self._session.execute(
            select(MarketRegimeSnapshot.generated_at).order_by(
                MarketRegimeSnapshot.generated_at.desc()
            )
        ).scalars().first()
        open_candidates = list(
            self._session.execute(
                select(StrategyConfigCandidate).where(
                    StrategyConfigCandidate.status == "draft"
                )
            ).scalars()
        )
        return CalibrationObservatoryStatusResponse(
            generated_at=datetime.now(tz=UTC),
            latest_diagnostic=(
                _diagnostic_run_response(latest_diagnostic)
                if latest_diagnostic is not None
                else None
            ),
            latest_cube_generated_at=latest_cube_generated_at,
            latest_regime_generated_at=latest_regime_generated_at,
            open_candidate_configs=len(open_candidates),
        )

    def calibration_diagnostics(
        self,
        *,
        limit: int = 50,
    ) -> list[CalibrationDiagnosticRunResponse]:
        rows = self._session.execute(
            select(CalibrationDiagnosticRun)
            .order_by(CalibrationDiagnosticRun.created_at.desc())
            .limit(limit)
        ).scalars()
        return [_diagnostic_run_response(row) for row in rows]

    def calibration_diagnostic(
        self,
        diagnostic_run_id: UUID,
    ) -> CalibrationDiagnosticRunResponse:
        row = self._session.get(CalibrationDiagnosticRun, diagnostic_run_id)
        if row is None:
            msg = f"Calibration diagnostic not found: {diagnostic_run_id}"
            raise LookupError(msg)
        return _diagnostic_run_response(row)

    def rolling_performance(
        self,
        *,
        window_name: str | None = None,
        instrument_id: str | None = None,
        session_type: str | None = None,
        timeframe: str | None = None,
        side: str | None = None,
        mode: str | None = None,
        contour_status: str | None = None,
        limit: int = 200,
    ) -> list[RollingPerformanceCubeResponse]:
        stmt = select(RollingPerformanceCube).order_by(
            RollingPerformanceCube.generated_at.desc()
        )
        if window_name is not None:
            stmt = stmt.where(RollingPerformanceCube.window_name == window_name)
        if instrument_id is not None:
            stmt = stmt.where(RollingPerformanceCube.instrument_id == instrument_id)
        if session_type is not None:
            stmt = stmt.where(RollingPerformanceCube.session_type == session_type)
        if timeframe is not None:
            stmt = stmt.where(RollingPerformanceCube.timeframe == timeframe)
        if side is not None:
            stmt = stmt.where(RollingPerformanceCube.side == side)
        if mode is not None:
            stmt = stmt.where(RollingPerformanceCube.mode == mode)
        if contour_status is not None:
            stmt = stmt.where(RollingPerformanceCube.contour_status == contour_status)
        rows = self._session.execute(stmt.limit(limit)).scalars()
        return [_rolling_cube_response(row) for row in rows]

    def market_regime_snapshots(
        self,
        *,
        instrument_id: str | None = None,
        session_type: str | None = None,
        market_regime: str | None = None,
        limit: int = 100,
    ) -> list[MarketRegimeSnapshotResponse]:
        stmt = select(MarketRegimeSnapshot).order_by(MarketRegimeSnapshot.generated_at.desc())
        if instrument_id is not None:
            stmt = stmt.where(MarketRegimeSnapshot.instrument_id == instrument_id)
        if session_type is not None:
            stmt = stmt.where(MarketRegimeSnapshot.session_type == session_type)
        if market_regime is not None:
            stmt = stmt.where(MarketRegimeSnapshot.market_regime == market_regime)
        rows = self._session.execute(stmt.limit(limit)).scalars()
        return [_market_regime_response(row) for row in rows]

    def config_candidates(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[StrategyConfigCandidateResponse]:
        stmt = select(StrategyConfigCandidate).order_by(
            StrategyConfigCandidate.created_at.desc()
        )
        if status is not None:
            stmt = stmt.where(StrategyConfigCandidate.status == status)
        rows = self._session.execute(stmt.limit(limit)).scalars()
        return [_config_candidate_response(row) for row in rows]

    def config_candidate(
        self,
        candidate_config_id: UUID,
    ) -> StrategyConfigCandidateResponse:
        row = self._config_candidate_row(candidate_config_id)
        return _config_candidate_response(row)

    def approve_config_candidate_for_shadow(
        self,
        candidate_config_id: UUID,
        *,
        approved_by: str,
    ) -> StrategyConfigCandidateResponse:
        row = self._config_candidate_row(candidate_config_id)
        if row.status not in {"draft", "rejected"}:
            msg = f"Candidate cannot be approved from status={row.status}"
            raise ValueError(msg)
        row.status = "approved_for_shadow"
        row.approved_by = approved_by
        row.approved_at = datetime.now(tz=UTC)
        row.validation_payload = {
            **row.validation_payload,
            "approval_changes_status_only": True,
            "runtime_config_changed": False,
        }
        self._session.flush()
        return _config_candidate_response(row)

    def reject_config_candidate(
        self,
        candidate_config_id: UUID,
        *,
        rejected_by: str,
        reason: str,
    ) -> StrategyConfigCandidateResponse:
        row = self._config_candidate_row(candidate_config_id)
        row.status = "rejected"
        row.rejection_reason = reason
        row.validation_payload = {
            **row.validation_payload,
            "rejected_by": rejected_by,
            "runtime_config_changed": False,
        }
        self._session.flush()
        return _config_candidate_response(row)

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
        return _dashboard_universe_from_env()

    def _active_timeframes(self) -> list[str]:
        configured = os.environ.get("TRADING_TIMEFRAMES", "")
        timeframes = [item.strip() for item in configured.split(",") if item.strip()]
        return timeframes or ["1m", "5m", "10m", "15m"]

    def _latest_position_snapshot_ts(self) -> datetime | None:
        return self._session.execute(select(func.max(PositionSnapshot.snapshot_ts))).scalar_one()

    def _position_snapshots_at(self, snapshot_ts: datetime) -> list[PositionSnapshot]:
        return list(
            self._session.execute(
                select(PositionSnapshot)
                .where(PositionSnapshot.snapshot_ts == snapshot_ts)
                .order_by(PositionSnapshot.instrument_id, PositionSnapshot.account_id)
                .limit(500)
            ).scalars()
        )

    def _latest_strategy_state(self) -> str:
        event = self._session.execute(
            select(StrategyStateEvent).order_by(StrategyStateEvent.ts_utc.desc()).limit(1)
        ).scalars().first()
        return event.new_state if event is not None else "unknown"

    def _open_orders_count(self) -> int:
        since = datetime.now(tz=UTC) - timedelta(days=30)
        return int(
            self._session.execute(
                select(func.count(BrokerOrder.broker_order_id)).where(
                    BrokerOrder.broker_status.not_in(TERMINAL_ORDER_STATUSES),
                    BrokerOrder.last_observed_at >= since,
                )
            ).scalar_one()
        )

    def _active_positions_count(self) -> int:
        latest_snapshot_ts = self._latest_position_snapshot_ts()
        if latest_snapshot_ts is None:
            return 0
        return int(
            self._session.execute(
                select(func.count(PositionSnapshot.position_snapshot_id)).where(
                    PositionSnapshot.snapshot_ts == latest_snapshot_ts,
                    PositionSnapshot.qty_lots != 0,
                )
            ).scalar_one()
        )

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

    def _intraday_rows(
        self,
        *,
        trading_date: date,
        session_type: str | None,
        micro_session_id: str | None,
        mode: str,
    ) -> list[IntradaySessionAnalytics]:
        stmt = select(IntradaySessionAnalytics).where(
            IntradaySessionAnalytics.trading_date == trading_date
        )
        if session_type is not None:
            stmt = stmt.where(IntradaySessionAnalytics.session_type == session_type)
        if micro_session_id is not None:
            stmt = stmt.where(IntradaySessionAnalytics.micro_session_id == micro_session_id)
        if mode != "all":
            stmt = stmt.where(IntradaySessionAnalytics.mode == mode)
        rows = list(
            self._session.execute(
                stmt.order_by(IntradaySessionAnalytics.generated_at.desc()).limit(500)
            ).scalars()
        )
        return _latest_intraday_rows(rows)

    def _config_candidate_row(self, candidate_config_id: UUID) -> StrategyConfigCandidate:
        row = self._session.get(StrategyConfigCandidate, candidate_config_id)
        if row is None:
            msg = f"Strategy config candidate not found: {candidate_config_id}"
            raise LookupError(msg)
        return row

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


def _intraday_snapshot_response(payload: JsonPayload) -> IntradayAnalyticsSnapshotResponse:
    trading_date_value = payload.get("trading_date")
    parsed_date = (
        date.fromisoformat(trading_date_value)
        if isinstance(trading_date_value, str)
        else trading_date_value
        if isinstance(trading_date_value, date)
        else None
    )
    generated_at = payload.get("generated_at")
    return IntradayAnalyticsSnapshotResponse(
        generated_at=_coerce_datetime(generated_at),
        trading_date=parsed_date,
        session_summaries=_payload_list_value(payload, "session_summaries"),
        instrument_summaries=_payload_list_value(payload, "instrument_summaries"),
        timeframe_summaries=_payload_list_value(payload, "timeframe_summaries"),
        side_summaries=_payload_list_value(payload, "side_summaries"),
        market_bias=str(payload.get("market_bias", "unknown")),
        market_activity=str(payload.get("market_activity", "unknown")),
        near_miss_count=int(payload.get("near_miss_count", 0)),
        spread_depth_imbalance_summary=_payload_dict_value(
            payload,
            "spread_depth_imbalance_summary",
        ),
        warnings=[str(item) for item in payload.get("warnings", [])],
        rows=_payload_list_value(payload, "rows"),
    )


def _intraday_payload_from_rows(rows: list[IntradaySessionAnalytics]) -> JsonPayload:
    row_payloads = [_intraday_row_payload(row) for row in rows]
    summary_rows = [row for row in row_payloads if row.get("instrument_id") is None]
    first = summary_rows[0] if summary_rows else row_payloads[0] if row_payloads else {}
    warnings = sorted({item for row in row_payloads for item in row.get("warnings", [])})
    return {
        "generated_at": first.get("generated_at", datetime.now(tz=UTC).isoformat()),
        "trading_date": first.get("trading_date"),
        "session_summaries": summary_rows,
        "instrument_summaries": _latest_payload_by(row_payloads, "instrument_id"),
        "timeframe_summaries": _latest_payload_by(row_payloads, "timeframe"),
        "side_summaries": _latest_payload_by(row_payloads, "side"),
        "market_bias": first.get("market_bias", "unknown"),
        "market_activity": first.get("market_activity", "unknown"),
        "near_miss_count": sum(int(row.get("near_miss_count", 0)) for row in summary_rows),
        "spread_depth_imbalance_summary": first.get("spread_depth_imbalance_summary", {}),
        "warnings": warnings,
        "rows": row_payloads,
    }


def _latest_intraday_rows(
    rows: list[IntradaySessionAnalytics],
) -> list[IntradaySessionAnalytics]:
    latest_by_scope: dict[
        tuple[str, str | None, str | None, str | None, str | None, str | None],
        IntradaySessionAnalytics,
    ] = {}
    for row in rows:
        key = (
            row.session_type,
            row.micro_session_id,
            row.hour_bucket.isoformat() if row.hour_bucket else None,
            row.instrument_id,
            row.timeframe,
            row.side,
        )
        previous = latest_by_scope.get(key)
        if previous is None or row.generated_at > previous.generated_at:
            latest_by_scope[key] = row
    return list(latest_by_scope.values())


def _intraday_row_payload(row: IntradaySessionAnalytics) -> JsonPayload:
    payload = dict(row.analytics_payload)
    spread_summary = payload.get("spread_depth_imbalance_summary")
    if not isinstance(spread_summary, dict):
        spread_summary = {
            "avg_spread_bps": _optional_str(row.avg_spread_bps),
            "p95_spread_bps": _optional_str(row.p95_spread_bps),
            "avg_depth": _optional_str(row.avg_depth),
            "avg_imbalance": _optional_str(row.avg_imbalance),
            "avg_market_quality": _optional_str(row.avg_market_quality),
        }
    warnings = payload.get("warnings", [])
    return {
        "intraday_analytics_id": str(row.intraday_analytics_id),
        "generated_at": row.generated_at.isoformat(),
        "trading_date": row.trading_date.isoformat(),
        "calendar_date": row.calendar_date.isoformat(),
        "session_type": row.session_type,
        "session_phase": row.session_phase,
        "micro_session_id": row.micro_session_id,
        "hour_bucket": row.hour_bucket.isoformat() if row.hour_bucket else None,
        "instrument_id": row.instrument_id,
        "timeframe": row.timeframe,
        "side": row.side,
        "mode": row.mode,
        "market_bias": row.market_bias,
        "market_activity": row.market_activity,
        "trend_strength": _optional_str(row.trend_strength),
        "candidate_count": row.candidate_count,
        "pseudo_order_count": row.pseudo_order_count,
        "real_order_count": row.real_order_count,
        "blocked_count": row.blocked_count,
        "near_miss_count": row.near_miss_count,
        "avg_spread_bps": _optional_str(row.avg_spread_bps),
        "p95_spread_bps": _optional_str(row.p95_spread_bps),
        "avg_depth": _optional_str(row.avg_depth),
        "avg_imbalance": _optional_str(row.avg_imbalance),
        "avg_market_quality": _optional_str(row.avg_market_quality),
        "stale_incidents": row.stale_incidents,
        "candle_lag_p95_seconds": _optional_str(row.candle_lag_p95_seconds),
        "gross_pnl_proxy": _optional_str(row.gross_pnl_proxy),
        "net_pnl_proxy": _optional_str(row.net_pnl_proxy),
        "no_trade_reason": payload.get("no_trade_reason"),
        "closest_to_entry": payload.get("closest_to_entry", []),
        "warnings": [str(item) for item in warnings] if isinstance(warnings, list) else [],
        "spread_depth_imbalance_summary": spread_summary,
        "payload": payload,
    }


def _diagnostic_run_response(row: CalibrationDiagnosticRun) -> CalibrationDiagnosticRunResponse:
    return CalibrationDiagnosticRunResponse(
        diagnostic_run_id=row.diagnostic_run_id,
        created_at=row.created_at,
        completed_at=row.completed_at,
        requested_by=row.requested_by,
        trigger_type=row.trigger_type,
        status=row.status,
        from_ts=row.from_ts,
        to_ts=row.to_ts,
        universe=row.universe,
        diagnosis=row.diagnosis,
        confidence=row.confidence,
        blocking_issues=row.blocking_issues,
        warnings=row.warnings,
        diagnostic_payload=row.diagnostic_payload,
    )


def _rolling_cube_response(row: RollingPerformanceCube) -> RollingPerformanceCubeResponse:
    return RollingPerformanceCubeResponse(
        cube_id=row.cube_id,
        generated_at=row.generated_at,
        window_start=row.window_start,
        window_end=row.window_end,
        window_name=row.window_name,
        instrument_id=row.instrument_id,
        session_type=row.session_type,
        timeframe=row.timeframe,
        side=row.side,
        mode=row.mode,
        candidate_count=row.candidate_count,
        approved_count=row.approved_count,
        blocked_count=row.blocked_count,
        pseudo_order_count=row.pseudo_order_count,
        real_order_count=row.real_order_count,
        gross_pnl_proxy=row.gross_pnl_proxy,
        net_pnl_proxy=row.net_pnl_proxy,
        avg_net_pnl_proxy=row.avg_net_pnl_proxy,
        win_proxy=row.win_proxy,
        avg_spread_bps=row.avg_spread_bps,
        p95_spread_bps=row.p95_spread_bps,
        avg_depth=row.avg_depth,
        p95_depth=row.p95_depth,
        avg_imbalance=row.avg_imbalance,
        avg_market_quality=row.avg_market_quality,
        stale_incidents=row.stale_incidents,
        stream_gap_count=row.stream_gap_count,
        active_days=row.active_days,
        last_signal_at=row.last_signal_at,
        sample_warning=row.sample_warning,
        confidence=row.confidence,
        contour_status=row.contour_status,
        cube_payload=row.cube_payload,
    )


def _market_regime_response(row: MarketRegimeSnapshot) -> MarketRegimeSnapshotResponse:
    return MarketRegimeSnapshotResponse(
        regime_snapshot_id=row.regime_snapshot_id,
        generated_at=row.generated_at,
        window_start=row.window_start,
        window_end=row.window_end,
        instrument_id=row.instrument_id,
        session_type=row.session_type,
        market_regime=row.market_regime,
        volume_score=row.volume_score,
        volatility_score=row.volatility_score,
        spread_score=row.spread_score,
        depth_score=row.depth_score,
        imbalance_score=row.imbalance_score,
        candidate_frequency_score=row.candidate_frequency_score,
        regime_payload=row.regime_payload,
    )


def _config_candidate_response(row: StrategyConfigCandidate) -> StrategyConfigCandidateResponse:
    return StrategyConfigCandidateResponse(
        candidate_config_id=row.candidate_config_id,
        created_at=row.created_at,
        source_diagnostic_run_id=row.source_diagnostic_run_id,
        base_strategy_id=row.base_strategy_id,
        proposed_strategy_id=row.proposed_strategy_id,
        status=row.status,
        proposed_by=row.proposed_by,
        approval_required=row.approval_required,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        proposal_payload=row.proposal_payload,
        validation_payload=row.validation_payload,
        caveats=row.caveats,
        rejection_reason=row.rejection_reason,
    )


def _latest_payload_by(rows: list[JsonPayload], key: str) -> list[JsonPayload]:
    grouped: dict[str, JsonPayload] = {}
    for row in rows:
        value = row.get(key)
        if value is not None:
            grouped[str(value)] = row
    return [grouped[item] for item in sorted(grouped)]


def _payload_list_value(payload: JsonPayload, key: str) -> list[JsonPayload]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _payload_dict_value(payload: JsonPayload, key: str) -> JsonPayload:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _dashboard_universe_from_env() -> list[str]:
    configured = os.environ.get("TRADING_INSTRUMENTS", "")
    instruments = [item.strip() for item in configured.split(",") if item.strip()]
    source = instruments or list(DEFAULT_DASHBOARD_UNIVERSE)
    return [_canonical_moex_instrument(item) for item in source]


def _canonical_moex_instrument(instrument_id: str) -> str:
    value = instrument_id.strip()
    if not value:
        return value
    if ":" in value:
        return value
    return f"MOEX:{value}"


def _money_balance_from_payload(
    payload: JsonPayload,
    *,
    latest: PositionSnapshot,
) -> MoneyBalance:
    currency = str(payload.get("balance_currency") or "RUB")
    available = _decimal_value(payload.get("available_cash_rub")) or Decimal("0")
    blocked = _decimal_value(payload.get("blocked_cash_rub")) or Decimal("0")
    total = _decimal_value(payload.get("total_portfolio_value_rub"))
    expected_yield = _decimal_value(payload.get("expected_yield_rub"))
    free_collateral = _decimal_value(payload.get("free_collateral_rub"))
    refreshed_at = _coerce_datetime(payload.get("last_balance_refresh_at") or latest.snapshot_ts)
    freshness = max(
        0,
        int((datetime.now(tz=UTC) - refreshed_at.astimezone(UTC)).total_seconds()),
    )
    return MoneyBalance(
        currency=currency,
        available=available,
        blocked=blocked,
        total_portfolio_value_rub=total,
        available_cash_rub=available,
        blocked_cash_rub=blocked,
        expected_yield_rub=expected_yield,
        free_collateral_rub=free_collateral,
        account_id_masked=_safe_masked_account(payload, latest.account_id),
        account_type=_optional_string(payload.get("account_type")),
        account_status=_optional_string(payload.get("account_status")),
        balance_currency=currency,
        last_balance_refresh_at=refreshed_at,
        balance_freshness_seconds=freshness,
        balance_degraded=False,
        balance_degraded_reason_code=None,
    )


def _money_balance_from_positions(snapshots: list[PositionSnapshot]) -> MoneyBalance:
    latest = snapshots[0]
    latest_ts = latest.snapshot_ts
    latest_rows = [snapshot for snapshot in snapshots if snapshot.snapshot_ts == latest_ts]
    total_exposure = sum(
        (snapshot.exposure or Decimal("0") for snapshot in latest_rows),
        Decimal("0"),
    )
    expected_yield = sum(
        (snapshot.unrealized_pnl or Decimal("0") for snapshot in latest_rows),
        Decimal("0"),
    )
    freshness = max(
        0,
        int((datetime.now(tz=UTC) - latest_ts.astimezone(UTC)).total_seconds()),
    )
    return MoneyBalance(
        currency="RUB",
        available=Decimal("0"),
        blocked=Decimal("0"),
        total_portfolio_value_rub=total_exposure,
        available_cash_rub=None,
        blocked_cash_rub=None,
        expected_yield_rub=expected_yield,
        account_id_masked=_mask_account_id(latest.account_id),
        balance_currency="RUB",
        last_balance_refresh_at=latest_ts,
        balance_freshness_seconds=freshness,
        balance_degraded=True,
        balance_degraded_reason_code="broker_balance_payload_unavailable",
    )


def _decimal_value(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _safe_masked_account(payload: JsonPayload, fallback_account_id: str) -> str | None:
    masked = payload.get("account_id_masked")
    if isinstance(masked, str) and masked and masked != fallback_account_id:
        return masked
    return _mask_account_id(fallback_account_id)


def _mask_account_id(account_id: str) -> str | None:
    if not account_id:
        return None
    if len(account_id) <= 6:
        return f"{account_id[:2]}***"
    return f"{account_id[:3]}***{account_id[-3:]}"


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    return datetime.now(tz=UTC)


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
