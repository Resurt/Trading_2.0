"""Read-model service for FastAPI BFF endpoints."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_api.schemas import (
    CounterfactualResponse,
    DailyReportResponse,
    HourlyReportResponse,
    MarketInstrumentOverview,
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
    CounterfactualResult,
    DailyReport,
    HourlyReport,
    InstrumentRegistry,
    MarketCandle,
    OrderBookSummary,
    OrderIntent,
    PositionSnapshot,
    SessionRun,
    SignalCandidate,
    StrategyConfig,
    StrategyStateEvent,
)

TERMINAL_ORDER_STATUSES = frozenset({"filled", "cancelled", "rejected"})


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
                recent_market_trades=[],
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

    def hourly_reports(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        limit: int = 50,
    ) -> list[HourlyReportResponse]:
        stmt = select(HourlyReport).order_by(HourlyReport.generated_at.desc()).limit(limit)
        if trading_date is not None:
            stmt = stmt.where(HourlyReport.trading_date == trading_date)
        if strategy_id is not None:
            stmt = stmt.where(HourlyReport.strategy_id == strategy_id)
        return [_hourly_report_response(report) for report in self._session.execute(stmt).scalars()]

    def daily_reports(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
        limit: int = 50,
    ) -> list[DailyReportResponse]:
        stmt = select(DailyReport).order_by(DailyReport.generated_at.desc()).limit(limit)
        if trading_date is not None:
            stmt = stmt.where(DailyReport.trading_date == trading_date)
        if strategy_id is not None:
            stmt = stmt.where(DailyReport.strategy_id == strategy_id)
        return [_daily_report_response(report) for report in self._session.execute(stmt).scalars()]

    def counterfactual_reports(
        self,
        *,
        trading_date: date | None = None,
        strategy_id: str | None = None,
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
        return [
            _counterfactual_response(result) for result in self._session.execute(stmt).scalars()
        ]

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
            blocker.candidate_id: blocker.reason_code
            for blocker in blockers
            if blocker.candidate_id is not None
        }


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


def _hourly_report_response(report: HourlyReport) -> HourlyReportResponse:
    return HourlyReportResponse(
        hourly_report_id=report.hourly_report_id,
        trading_date=report.trading_date,
        session_type=report.session_type,
        micro_session_id=report.micro_session_id,
        strategy_id=report.strategy_id,
        instrument_id=report.instrument_id,
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
        strategy_id=result.strategy_id,
        blocker_code=result.blocker_code,
        cancel_reason_code=result.cancel_reason_code,
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


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None
