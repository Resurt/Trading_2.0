"""Repository layer for database-backed domain aggregates."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_common.db.models import (
    BrokerOrder,
    InstrumentRegistry,
    MarketCandle,
    MarketStatusSnapshot,
    OrderBookSummary,
    OrderIntent,
    SessionRun,
    StrategyConfig,
    StrategyStateEvent,
)


class InstrumentRepository:
    """CRUD helpers for `instrument_registry`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, instrument_id: str) -> InstrumentRegistry | None:
        return self._session.get(InstrumentRegistry, instrument_id)

    def get_by_ticker(self, ticker: str) -> InstrumentRegistry | None:
        stmt = select(InstrumentRegistry).where(InstrumentRegistry.ticker == ticker)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_enabled(self) -> list[InstrumentRegistry]:
        stmt = (
            select(InstrumentRegistry)
            .where(InstrumentRegistry.is_enabled.is_(True))
            .order_by(InstrumentRegistry.ticker)
        )
        return list(self._session.execute(stmt).scalars())

    def upsert(self, instrument: InstrumentRegistry) -> InstrumentRegistry:
        existing = self.get(instrument.instrument_id)
        if existing is None:
            self._session.add(instrument)
            self._session.flush()
            return instrument

        existing.ticker = instrument.ticker
        existing.class_code = instrument.class_code
        existing.figi = instrument.figi
        existing.instrument_uid = instrument.instrument_uid
        existing.name = instrument.name
        existing.lot_size = instrument.lot_size
        existing.min_price_increment = instrument.min_price_increment
        existing.currency = instrument.currency
        existing.is_enabled = instrument.is_enabled
        existing.supports_morning = instrument.supports_morning
        existing.supports_evening = instrument.supports_evening
        existing.supports_weekend = instrument.supports_weekend
        existing.instrument_payload = instrument.instrument_payload
        self._session.flush()
        return existing


class StrategyConfigRepository:
    """CRUD helpers for versioned `strategy_config` rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_version(self, config: StrategyConfig) -> StrategyConfig:
        self._session.add(config)
        self._session.flush()
        return config

    def get_active(self, strategy_id: str, session_template: str) -> StrategyConfig | None:
        stmt = (
            select(StrategyConfig)
            .where(
                StrategyConfig.strategy_id == strategy_id,
                StrategyConfig.session_template == session_template,
                StrategyConfig.is_active.is_(True),
            )
            .order_by(StrategyConfig.version.desc())
        )
        return self._session.execute(stmt).scalars().first()

    def deactivate_previous(self, strategy_id: str, session_template: str) -> int:
        active_configs = self._session.execute(
            select(StrategyConfig).where(
                StrategyConfig.strategy_id == strategy_id,
                StrategyConfig.session_template == session_template,
                StrategyConfig.is_active.is_(True),
            )
        ).scalars()
        updated = 0
        for config in active_configs:
            config.is_active = False
            updated += 1
        self._session.flush()
        return updated


class SessionRunRepository:
    """CRUD helpers for logical micro-session runs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, run: SessionRun) -> SessionRun:
        self._session.add(run)
        self._session.flush()
        return run

    def get(self, run_id: UUID) -> SessionRun | None:
        return self._session.get(SessionRun, run_id)

    def get_by_micro_session_id(self, micro_session_id: str) -> SessionRun | None:
        stmt = select(SessionRun).where(SessionRun.micro_session_id == micro_session_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def close(
        self,
        run_id: UUID,
        *,
        ended_at: datetime,
        close_reason_code: str,
    ) -> SessionRun:
        run = self.get(run_id)
        if run is None:
            msg = f"SessionRun not found: {run_id}"
            raise LookupError(msg)
        run.status = "closed"
        run.ended_at = ended_at
        run.close_reason_code = close_reason_code
        self._session.flush()
        return run

    def mark_freeze(self, run_id: UUID, *, freeze_started_at: datetime) -> SessionRun:
        run = self.get(run_id)
        if run is None:
            msg = f"SessionRun not found: {run_id}"
            raise LookupError(msg)
        if run.freeze_started_at is None:
            run.freeze_started_at = freeze_started_at
        if run.status == "open":
            run.status = "freezing"
        self._session.flush()
        return run

    def request_report(self, run_id: UUID, *, requested_at: datetime) -> SessionRun:
        run = self.get(run_id)
        if run is None:
            msg = f"SessionRun not found: {run_id}"
            raise LookupError(msg)
        run.report_requested_at = requested_at
        self._session.flush()
        return run


class StrategyStateEventRepository:
    """Append-only helpers for `strategy_state_event` rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, event: StrategyStateEvent) -> StrategyStateEvent:
        self._session.add(event)
        self._session.flush()
        return event


class MarketDataRepository:
    """Persistence helpers for market candles and lightweight market snapshots."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_candle(
        self,
        *,
        instrument_id: str,
        timeframe: str,
        open_ts_utc: datetime,
    ) -> MarketCandle | None:
        stmt = select(MarketCandle).where(
            MarketCandle.instrument_id == instrument_id,
            MarketCandle.timeframe == timeframe,
            MarketCandle.open_ts_utc == open_ts_utc,
        )
        return self._session.execute(stmt).scalars().first()

    def upsert_candle(self, candle: MarketCandle) -> MarketCandle:
        existing = self.get_candle(
            instrument_id=candle.instrument_id,
            timeframe=candle.timeframe,
            open_ts_utc=candle.open_ts_utc,
        )
        if existing is None:
            self._session.add(candle)
            self._session.flush()
            return candle

        existing.close_ts_utc = candle.close_ts_utc
        existing.exchange_open_ts = candle.exchange_open_ts
        existing.exchange_close_ts = candle.exchange_close_ts
        existing.open_price = candle.open_price
        existing.high_price = candle.high_price
        existing.low_price = candle.low_price
        existing.close_price = candle.close_price
        existing.volume_lots = candle.volume_lots
        existing.is_closed = candle.is_closed
        existing.source = candle.source
        existing.candle_payload = candle.candle_payload
        existing.calendar_date = candle.calendar_date
        existing.trading_date = candle.trading_date
        existing.session_type = candle.session_type
        existing.session_phase = candle.session_phase
        existing.micro_session_id = candle.micro_session_id
        existing.broker_trading_status = candle.broker_trading_status
        self._session.flush()
        return existing

    def save_status_snapshot(self, snapshot: MarketStatusSnapshot) -> MarketStatusSnapshot:
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def save_order_book_summary(self, summary: OrderBookSummary) -> OrderBookSummary:
        self._session.add(summary)
        self._session.flush()
        return summary


class OrderRepository:
    """Order repositories with request id based idempotency."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_intent_by_request_order_id(self, request_order_id: UUID) -> OrderIntent | None:
        stmt = select(OrderIntent).where(OrderIntent.request_order_id == request_order_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def create_intent_idempotent(self, intent: OrderIntent) -> OrderIntent:
        existing = self.get_intent_by_request_order_id(intent.request_order_id)
        if existing is not None:
            return existing
        self._session.add(intent)
        self._session.flush()
        return intent

    def get_broker_order_by_request_order_id(self, request_order_id: UUID) -> BrokerOrder | None:
        stmt = select(BrokerOrder).where(BrokerOrder.request_order_id == request_order_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def upsert_broker_order_state(self, order: BrokerOrder) -> BrokerOrder:
        existing = self.get_broker_order_by_request_order_id(order.request_order_id)
        if existing is None:
            self._session.add(order)
            self._session.flush()
            return order

        if order.lifecycle_seq < existing.lifecycle_seq:
            return existing

        existing.exchange_order_id = order.exchange_order_id
        existing.broker_status = order.broker_status
        existing.lifecycle_seq = order.lifecycle_seq
        existing.posted_at = order.posted_at
        existing.cancelled_at = order.cancelled_at
        existing.rejected_at = order.rejected_at
        existing.reject_reason_code = order.reject_reason_code
        existing.broker_tracking_id = order.broker_tracking_id
        existing.last_observed_at = order.last_observed_at
        existing.broker_payload = order.broker_payload
        self._session.flush()
        return existing
