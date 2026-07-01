"""Persistence adapter for normalized market data aggregates."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from sqlalchemy.orm import Session

from trade_core.market_data.calculators import MarketState
from trade_core.market_data.events import (
    Bar,
    Candle,
    MarketTrade,
    OrderBookSnapshot,
    TradingStatusTick,
)
from trade_core.session.models import SessionEventContext
from trading_common.db.models import (
    AuditEvent,
    MarketCandle,
    MarketMicrostructureSnapshot,
    MarketStatusSnapshot,
    MarketTradeSample,
    OrderBookSummary,
)
from trading_common.db.repositories import MarketDataRepository

ZERO = Decimal("0")
ONE = Decimal("1")


class MarketMicrostructureRejectReason(StrEnum):
    MISSING_BID_ASK = "missing_bid_ask"
    CROSSED_BOOK = "crossed_book"
    INVALID_SPREAD = "invalid_spread"
    INVALID_DEPTH = "invalid_depth"
    INVALID_IMBALANCE = "invalid_imbalance"
    OUTSIDE_SESSION_WINDOW = "outside_session_window"
    NON_CALIBRATION_SOURCE = "non_calibration_source"


class MarketMicrostructureRejectedError(ValueError):
    """Raised when a primary calibration row is unsafe to persist."""

    def __init__(self, reason: MarketMicrostructureRejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class SqlAlchemyMarketDataStore:
    """Store market data using the shared SQLAlchemy repository layer."""

    def __init__(self, session: Session) -> None:
        self._repository = MarketDataRepository(session)

    def save_candle(
        self,
        *,
        candle: Candle,
        context: SessionEventContext,
    ) -> MarketCandle:
        return self._repository.upsert_candle(
            MarketCandle(
                **context.as_db_values(),
                instrument_id=candle.instrument_id,
                timeframe=candle.timeframe.value,
                open_ts_utc=candle.open_ts_utc.astimezone(UTC),
                close_ts_utc=candle.close_ts_utc.astimezone(UTC),
                exchange_open_ts=candle.exchange_open_ts,
                exchange_close_ts=candle.exchange_close_ts,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
                volume_lots=candle.volume_lots,
                is_closed=candle.is_closed,
                source=candle.source,
                candle_payload=candle.payload,
            )
        )

    def save_bar(
        self,
        *,
        bar: Bar,
        context: SessionEventContext,
    ) -> MarketCandle:
        return self.save_candle(candle=bar.as_candle(), context=context)

    def save_status(
        self,
        *,
        tick: TradingStatusTick,
        context: SessionEventContext,
    ) -> MarketStatusSnapshot:
        return self._repository.save_status_snapshot(
            MarketStatusSnapshot(
                **context.as_db_values(),
                ts_utc=tick.received_ts.astimezone(UTC),
                exchange_ts=tick.exchange_ts,
                received_ts=tick.received_ts,
                instrument_id=tick.instrument_id,
                trading_status=tick.trading_status,
                api_trade_available=tick.api_trade_available,
                status_payload=tick.payload,
            )
        )

    def save_order_book_summary(
        self,
        *,
        order_book: OrderBookSnapshot,
        market_state: MarketState,
        context: SessionEventContext,
    ) -> OrderBookSummary:
        _validate_primary_microstructure(
            market_state=market_state,
            context=context,
            payload=order_book.payload,
        )
        return self._repository.save_order_book_summary(
            OrderBookSummary(
                **context.as_db_values(),
                ts_utc=order_book.received_ts.astimezone(UTC),
                exchange_ts=order_book.exchange_ts,
                received_ts=order_book.received_ts,
                instrument_id=order_book.instrument_id,
                depth_levels=order_book.depth,
                best_bid_price=market_state.best_bid.price if market_state.best_bid else None,
                best_bid_qty_lots=(
                    market_state.best_bid.quantity_lots if market_state.best_bid else None
                ),
                best_ask_price=market_state.best_ask.price if market_state.best_ask else None,
                best_ask_qty_lots=(
                    market_state.best_ask.quantity_lots if market_state.best_ask else None
                ),
                mid_price=market_state.mid_price,
                spread_abs=market_state.spread_abs,
                spread_bps=market_state.spread_bps,
                bid_depth_lots=market_state.bid_depth_lots,
                ask_depth_lots=market_state.ask_depth_lots,
                book_imbalance=market_state.book_imbalance,
                market_quality_score=market_state.market_quality_score,
                **_freshness_db_values(
                    exchange_ts=order_book.exchange_ts,
                    market_state=market_state,
                    missing_reason=_exchange_ts_missing_reason(
                        order_book.exchange_ts,
                        order_book.payload,
                    ),
                ),
                summary_payload=order_book.payload,
            )
        )

    def save_microstructure_snapshot(
        self,
        *,
        market_state: MarketState,
        context: SessionEventContext,
        ts_utc: datetime,
        exchange_ts: datetime | None = None,
        received_ts: datetime | None = None,
        source: str = "data_only_shadow",
        payload: dict[str, object] | None = None,
    ) -> MarketMicrostructureSnapshot:
        _validate_primary_microstructure(
            market_state=market_state,
            context=context,
            payload=payload or market_state.payload,
        )
        payload_values = payload or market_state.payload
        event_exchange_ts = (
            exchange_ts
            or _datetime_from_payload(payload_values, "exchange_ts")
            or _datetime_from_payload(payload_values, "order_book_ts")
        )
        event_ts = received_ts or _datetime_from_payload(payload_values, "received_ts") or ts_utc
        return self._repository.save_microstructure_snapshot(
            MarketMicrostructureSnapshot(
                **context.as_db_values(),
                ts_utc=ts_utc,
                exchange_ts=event_exchange_ts,
                received_ts=event_ts,
                instrument_id=market_state.instrument_id,
                best_bid=market_state.best_bid.price if market_state.best_bid else None,
                best_ask=market_state.best_ask.price if market_state.best_ask else None,
                mid_price=market_state.mid_price,
                spread_abs=market_state.spread_abs,
                spread_bps=market_state.spread_bps,
                bid_depth_lots=market_state.bid_depth_lots,
                ask_depth_lots=market_state.ask_depth_lots,
                book_imbalance=market_state.book_imbalance,
                market_quality_score=market_state.market_quality_score,
                feed_freshness_age_ms=market_state.feed_freshness.age_ms,
                **_freshness_db_values(
                    exchange_ts=event_exchange_ts,
                    market_state=market_state,
                    missing_reason=_exchange_ts_missing_reason(event_exchange_ts, payload_values),
                ),
                is_stale=market_state.feed_freshness.is_stale,
                source=source,
                snapshot_payload=payload_values or market_state.as_read_model(),
            )
        )

    def save_market_trade_sample(
        self,
        *,
        trade: MarketTrade,
        context: SessionEventContext,
        source: str = "market_trades_stream",
    ) -> MarketTradeSample:
        return self._repository.save_market_trade_sample(
            MarketTradeSample(
                **context.as_db_values(),
                exchange_ts=trade.exchange_ts,
                received_ts=trade.received_ts,
                instrument_id=trade.instrument_id,
                price=trade.price,
                quantity_lots=trade.quantity_lots,
                side=trade.side,
                source=str(trade.payload.get("source") or source),
                venue_type=_string_or_none(trade.payload.get("venue_type")),
                trade_id=trade.trade_id,
                include_in_calibration=bool(trade.payload.get("include_in_calibration", False)),
                payload=trade.payload,
            )
        )

    def save_microstructure_rejection_audit(
        self,
        *,
        market_state: MarketState,
        context: SessionEventContext,
        reason: MarketMicrostructureRejectReason,
        payload: dict[str, object],
    ) -> AuditEvent:
        now = datetime.now(tz=UTC)
        event = AuditEvent(
            audit_event_id=uuid4(),
            **context.as_db_values(),
            ts_utc=now,
            exchange_ts=None,
            received_ts=now,
            service="trade_core",
            actor="system",
            action="data_only_microstructure_row_rejected",
            entity_type="market_microstructure_snapshot",
            entity_id=market_state.instrument_id,
            severity="warning",
            correlation_id=str(uuid4()),
            audit_payload={
                "reason": reason.value,
                "instrument_id": market_state.instrument_id,
                "readonly_calls_only": True,
                "real_orders_disabled": True,
                "strategy_trading_disabled": True,
                **payload,
            },
        )
        self._repository.session.add(event)
        self._repository.session.flush()
        return event


def validate_primary_microstructure(
    *,
    market_state: MarketState,
    context: SessionEventContext,
    payload: dict[str, object] | None = None,
) -> MarketMicrostructureRejectReason | None:
    """Return the rejection reason for unsafe primary data-only rows, if any."""

    try:
        _validate_primary_microstructure(
            market_state=market_state,
            context=context,
            payload=payload or market_state.payload,
        )
    except MarketMicrostructureRejectedError as exc:
        return exc.reason
    return None


def _validate_primary_microstructure(
    *,
    market_state: MarketState,
    context: SessionEventContext,
    payload: dict[str, object] | None,
) -> None:
    if context.session_phase.value != "continuous_trading":
        raise MarketMicrostructureRejectedError(
            MarketMicrostructureRejectReason.OUTSIDE_SESSION_WINDOW
        )

    payload = payload or {}
    if (
        payload.get("include_in_calibration") is False
        or payload.get("calibration_allowed") is False
    ):
        raise MarketMicrostructureRejectedError(
            MarketMicrostructureRejectReason.NON_CALIBRATION_SOURCE
        )
    if payload.get("venue_type") in {"otc", "dealer", "indicative", "local"}:
        raise MarketMicrostructureRejectedError(
            MarketMicrostructureRejectReason.NON_CALIBRATION_SOURCE
        )
    if payload.get("quote_source") in {
        "broker_otc_order_book",
        "broker_indicative_quote",
        "local_fallback",
    }:
        raise MarketMicrostructureRejectedError(
            MarketMicrostructureRejectReason.NON_CALIBRATION_SOURCE
        )

    if market_state.best_bid is None or market_state.best_ask is None:
        raise MarketMicrostructureRejectedError(MarketMicrostructureRejectReason.MISSING_BID_ASK)
    if market_state.best_ask.price < market_state.best_bid.price:
        raise MarketMicrostructureRejectedError(MarketMicrostructureRejectReason.CROSSED_BOOK)
    if (
        market_state.mid_price is None
        or market_state.mid_price <= ZERO
        or market_state.spread_abs is None
        or market_state.spread_bps is None
        or market_state.spread_abs < ZERO
        or market_state.spread_bps < ZERO
    ):
        raise MarketMicrostructureRejectedError(MarketMicrostructureRejectReason.INVALID_SPREAD)
    if market_state.bid_depth_lots < ZERO or market_state.ask_depth_lots < ZERO:
        raise MarketMicrostructureRejectedError(MarketMicrostructureRejectReason.INVALID_DEPTH)
    if (
        market_state.book_imbalance is None
        or market_state.book_imbalance < -ONE
        or market_state.book_imbalance > ONE
    ):
        raise MarketMicrostructureRejectedError(MarketMicrostructureRejectReason.INVALID_IMBALANCE)


def _freshness_db_values(
    *,
    exchange_ts: datetime | None,
    market_state: MarketState,
    missing_reason: str | None,
) -> dict[str, object]:
    freshness = market_state.feed_freshness
    return {
        "exchange_age_ms": freshness.exchange_age_ms,
        "received_age_ms": freshness.received_age_ms,
        "stale_by_exchange_time": freshness.stale_by_exchange_time,
        "stale_by_received_time": freshness.stale_by_received_time,
        "freshness_basis": "exchange_ts" if exchange_ts is not None else "received_ts_only",
        "exchange_ts_missing_reason": None if exchange_ts is not None else missing_reason,
        "strict_dual_freshness_eligible": exchange_ts is not None,
    }


def _exchange_ts_missing_reason(
    exchange_ts: datetime | None,
    payload: dict[str, object] | None,
) -> str | None:
    if exchange_ts is not None:
        return None
    payload = payload or {}
    explicit = payload.get("exchange_ts_missing_reason")
    if isinstance(explicit, str) and explicit:
        return explicit[:96]
    return "source_payload_missing_exchange_ts"


def _datetime_from_payload(payload: dict[str, object] | None, key: str) -> datetime | None:
    if not payload:
        return None
    value = payload.get(key)
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, str) and value:
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
