"""Readonly dashboard market feed independent from data-only collection."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

from trade_core.session.moex_calendar import MSK
from trading_api.market_quality import calculate_market_quality, calculate_spread_metrics
from trading_api.schemas import MarketInstrumentOverview, MarketOverviewResponse

GatewayFactory = Callable[[], Any]
_READONLY_BROKER_EXECUTOR: ThreadPoolExecutor | None = None


@dataclass(frozen=True, slots=True)
class DashboardMarketFeedConfig:
    """Runtime knobs for the readonly dashboard feed."""

    enabled: bool = True
    quote_refresh_seconds: float = 5.0
    selected_book_refresh_seconds: float = 3.0
    trades_refresh_seconds: float = 15.0
    session_refresh_seconds: float = 30.0
    max_instruments: int = 8
    last_price_max_exchange_age_seconds: float = 30.0
    order_book_max_exchange_age_seconds: float = 30.0
    trades_max_exchange_age_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> DashboardMarketFeedConfig:
        return cls(
            enabled=_env_bool("DASHBOARD_MARKET_FEED_ENABLED", True),
            quote_refresh_seconds=_env_float("DASHBOARD_QUOTE_REFRESH_SECONDS", 5.0),
            selected_book_refresh_seconds=_env_float(
                "DASHBOARD_SELECTED_BOOK_REFRESH_SECONDS", 3.0
            ),
            trades_refresh_seconds=_env_float("DASHBOARD_TRADES_REFRESH_SECONDS", 15.0),
            session_refresh_seconds=_env_float("DASHBOARD_SESSION_REFRESH_SECONDS", 30.0),
            max_instruments=max(1, _env_int("DASHBOARD_FEED_MAX_INSTRUMENTS", 8)),
            last_price_max_exchange_age_seconds=_env_float(
                "DASHBOARD_LAST_PRICE_MAX_EXCHANGE_AGE_SECONDS", 30.0
            ),
            order_book_max_exchange_age_seconds=_env_float(
                "DASHBOARD_ORDER_BOOK_MAX_EXCHANGE_AGE_SECONDS", 30.0
            ),
            trades_max_exchange_age_seconds=_env_float(
                "DASHBOARD_TRADES_MAX_EXCHANGE_AGE_SECONDS", 15.0
            ),
        )


@dataclass(slots=True)
class DashboardMarketFeedService:
    """Small in-memory BFF cache for operator display market data.

    The service intentionally does not write market_microstructure_snapshot or any
    trading entities. It only uses readonly broker unary calls and overlays those
    values onto the local read-model returned by BffReadService.
    """

    config: DashboardMarketFeedConfig = field(default_factory=DashboardMarketFeedConfig.from_env)
    _overview: MarketOverviewResponse | None = None
    _selected_details: dict[str, MarketInstrumentOverview] = field(default_factory=dict)
    _last_quote_refresh_at: datetime | None = None
    _last_details_refresh_at: dict[str, datetime] = field(default_factory=dict)
    _last_trades_refresh_at: dict[str, datetime] = field(default_factory=dict)
    _last_status_refresh_at: dict[str, datetime] = field(default_factory=dict)
    _refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _running: bool = False
    _selected_instrument: str | None = None
    _errors: list[str] = field(default_factory=list)
    _warnings: list[str] = field(default_factory=list)
    _broker_calls_paused_until: datetime | None = None
    _broker_calls_pause_reason: str | None = None

    def status(self) -> dict[str, object]:
        overview = self._overview
        selected = (
            self._selected_details.get(self._selected_instrument or "")
            if self._selected_instrument
            else None
        )
        first_live = next(
            (
                row
                for row in (overview.instruments if overview else [])
                if row.official_exchange_open or row.quote_status == "live"
            ),
            None,
        )
        session_row = selected or first_live or (overview.instruments[0] if overview else None)
        last_refresh = self._last_quote_refresh_at
        if selected is not None:
            refresh_values = [
                _ensure_utc(value)
                for value in (last_refresh, selected.order_book_ts)
                if value is not None
            ]
            last_refresh = max(refresh_values, default=last_refresh)
        market_open = bool(session_row.official_exchange_open) if session_row else False
        session_type, session_phase = _clock_session_context(market_open=market_open)
        return {
            "enabled": self.config.enabled,
            "running": self._running,
            "market_open": market_open,
            "session_type": session_type if session_row else "unknown",
            "session_phase": session_phase if session_row else "unknown",
            "venue_type": session_row.venue_type if session_row else "unknown",
            "next_session_at": None,
            "last_refresh_at": last_refresh.isoformat() if last_refresh is not None else None,
            "selected_instrument": self._selected_instrument,
            "quote_rows_count": len(overview.instruments) if overview else 0,
            "order_book_available": bool(
                selected and selected.order_book_source and not selected.order_book_stale
            ),
            "trade_tape_available": bool(selected and selected.recent_market_trades),
            "broker_calls_paused_until": (
                self._broker_calls_paused_until.isoformat()
                if self._broker_calls_paused_until is not None
                else None
            ),
            "broker_calls_pause_reason": self._broker_calls_pause_reason,
            "errors": list(self._errors[-5:]),
            "warnings": list(self._warnings[-8:]),
        }

    def pause_broker_calls(self, *, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        paused_until = datetime.now(tz=UTC) + timedelta(seconds=seconds)
        if (
            self._broker_calls_paused_until is None
            or paused_until > self._broker_calls_paused_until
        ):
            self._broker_calls_paused_until = paused_until
        self._broker_calls_pause_reason = reason
        self._record_warning(reason)

    def _broker_calls_paused(self) -> bool:
        paused_until = self._broker_calls_paused_until
        if paused_until is None:
            return False
        if datetime.now(tz=UTC) < paused_until:
            return True
        self._broker_calls_paused_until = None
        self._broker_calls_pause_reason = None
        self._clear_warning("start_preflight_pending")
        return False

    async def snapshot(
        self,
        *,
        base_overview: MarketOverviewResponse,
        refs: Sequence[Any],
        selected_instrument: str | None,
        gateway_factory: GatewayFactory,
        include_order_book: bool,
        include_trades: bool,
        force: bool = False,
    ) -> dict[str, object]:
        self._running = self.config.enabled
        selected_id = _canonical_moex_instrument(selected_instrument or "MOEX:SBER")
        self._selected_instrument = selected_id
        overview = _limit_overview(base_overview, self.config.max_instruments)

        if self._refresh_lock.locked():
            overview = _merge_overviews(overview, self._overview)
            return self._snapshot_payload(overview, selected_id)

        async with self._refresh_lock:
            return await self._refresh_snapshot(
                overview=overview,
                refs=refs,
                selected_id=selected_id,
                gateway_factory=gateway_factory,
                include_order_book=include_order_book,
                include_trades=include_trades,
                force=force,
            )

    async def _refresh_snapshot(
        self,
        *,
        overview: MarketOverviewResponse,
        refs: Sequence[Any],
        selected_id: str,
        gateway_factory: GatewayFactory,
        include_order_book: bool,
        include_trades: bool,
        force: bool,
    ) -> dict[str, object]:
        if self._broker_calls_paused():
            self._record_warning(self._broker_calls_pause_reason or "broker_calls_paused")
            overview = _merge_overviews(overview, self._overview)
            return self._snapshot_payload(overview, selected_id)
        if self.config.enabled:
            overview = await self._refresh_quotes_if_needed(
                overview=overview,
                refs=refs,
                gateway_factory=gateway_factory,
                force=force,
            )
            if include_order_book or include_trades:
                selected = await self._refresh_selected_if_needed(
                    overview=overview,
                    refs=refs,
                    selected_instrument=selected_id,
                    gateway_factory=gateway_factory,
                    include_order_book=include_order_book,
                    include_trades=include_trades,
                    force=force,
                )
                if selected is not None:
                    overview = _replace_instrument(overview, selected)
        else:
            self._record_warning("dashboard_market_feed_disabled")

        return self._snapshot_payload(overview, selected_id)

    def _snapshot_payload(
        self,
        overview: MarketOverviewResponse,
        selected_id: str,
    ) -> dict[str, object]:
        self._overview = overview
        selected_details = self._selected_details.get(selected_id) or next(
            (row for row in overview.instruments if row.instrument_id == selected_id),
            overview.instruments[0] if overview.instruments else None,
        )
        session_row = selected_details or (
            overview.instruments[0] if overview.instruments else None
        )
        return {
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "source": "dashboard_market_feed",
            "data_only_collection_required": False,
            "session": _session_payload(session_row),
            "quote_rows": [row.model_dump(mode="json") for row in overview.instruments],
            "market_overview": overview.model_dump(mode="json"),
            "selected_instrument": selected_id,
            "selected_details": (
                selected_details.model_dump(mode="json") if selected_details is not None else None
            ),
            "errors": list(self._errors[-5:]),
            "warnings": list(self._warnings[-8:]),
            "status": self.status(),
        }

    async def _refresh_quotes_if_needed(
        self,
        *,
        overview: MarketOverviewResponse,
        refs: Sequence[Any],
        gateway_factory: GatewayFactory,
        force: bool,
    ) -> MarketOverviewResponse:
        now = datetime.now(tz=UTC)
        if (
            not force
            and self._overview is not None
            and self._last_quote_refresh_at is not None
            and now - self._last_quote_refresh_at
            < timedelta(seconds=self.config.quote_refresh_seconds)
        ):
            return _merge_overviews(overview, self._overview)

        resolved_refs = _resolved_refs(refs, overview.instruments)
        if not resolved_refs:
            self._record_warning("dashboard_feed_no_resolved_instruments")
            return _merge_overviews(overview, self._overview)
        try:
            from trade_core.broker_gateway import LastPricesRequest

            gateway = gateway_factory()
            timeout_seconds = _env_float("DASHBOARD_LAST_PRICES_TIMEOUT_SECONDS", 3.0)
            response = await _run_readonly_broker_call(
                lambda: gateway.get_last_prices(
                    LastPricesRequest(instruments=tuple(resolved_refs))
                ),
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            self._record_error(_reason_from_exception(exc, "dashboard_last_prices_unavailable"))
            return _merge_overviews(overview, self._overview)

        prices = _prices_by_instrument(response.data, resolved_refs)
        self._clear_error("dashboard_last_prices_unavailable")
        refreshed: list[MarketInstrumentOverview] = []
        for instrument in overview.instruments:
            price_payload = prices.get(instrument.instrument_id)
            refreshed.append(
                _instrument_with_last_price(
                    instrument,
                    price_payload,
                    max_exchange_age_seconds=self.config.last_price_max_exchange_age_seconds,
                )
            )
        self._last_quote_refresh_at = now
        return MarketOverviewResponse(generated_at=now, instruments=refreshed)

    async def _refresh_selected_if_needed(
        self,
        *,
        overview: MarketOverviewResponse,
        refs: Sequence[Any],
        selected_instrument: str,
        gateway_factory: GatewayFactory,
        include_order_book: bool,
        include_trades: bool,
        force: bool,
    ) -> MarketInstrumentOverview | None:
        now = datetime.now(tz=UTC)
        selected = next(
            (row for row in overview.instruments if row.instrument_id == selected_instrument),
            None,
        )
        if selected is None:
            return None
        ref = next(
            (
                item
                for item in refs
                if _canonical_moex_instrument(str(getattr(item, "instrument_id", "")))
                == selected_instrument
            ),
            None,
        )
        if ref is None or not (getattr(ref, "instrument_uid", None) or getattr(ref, "figi", None)):
            self._record_warning("dashboard_feed_selected_instrument_unresolved")
            return selected

        last_details_at = self._last_details_refresh_at.get(selected_instrument)
        last_trades_at = self._last_trades_refresh_at.get(selected_instrument)
        last_status_at = self._last_status_refresh_at.get(selected_instrument)
        should_refresh_status = (
            force
            or last_status_at is None
            or now - last_status_at >= timedelta(seconds=self.config.session_refresh_seconds)
        )
        should_refresh_book = include_order_book and (
            force
            or last_details_at is None
            or now - last_details_at
            >= timedelta(seconds=self.config.selected_book_refresh_seconds)
        )
        should_refresh_trades = include_trades and (
            force
            or last_trades_at is None
            or now - last_trades_at >= timedelta(seconds=self.config.trades_refresh_seconds)
        )
        cached = self._selected_details.get(selected_instrument)
        current = _prefer_selected_details(selected, cached)
        if include_order_book and not should_refresh_book:
            max_age_ms = int(self.config.order_book_max_exchange_age_seconds * 1000)
            refresh_before_ms = max(0, max_age_ms - 1_000)
            cached_age_ms = (
                max(
                    0,
                    int(
                        (now - _ensure_utc(current.order_book_ts)).total_seconds()
                        * 1000
                    ),
                )
                if current.order_book_ts is not None
                else None
            )
            if (
                current.order_book_stale
                or cached_age_ms is None
                or cached_age_ms >= refresh_before_ms
            ):
                should_refresh_book = True
        if not should_refresh_book and not should_refresh_trades:
            return current

        try:
            gateway = gateway_factory()
        except Exception as exc:
            self._record_error(_reason_from_exception(exc, "dashboard_gateway_unavailable"))
            return current

        if should_refresh_status:
            status_payload = await self._get_trading_status(
                gateway=gateway,
                ref=ref,
                instrument=current,
            )
            self._last_status_refresh_at[selected_instrument] = now
            if status_payload is not None:
                current = current.model_copy(update=status_payload)

        trade_update = _visible_trade_tape_update(
            current.recent_market_trades,
            source=current.market_trades_source or "order_book_summary_payload",
        )
        current = current.model_copy(update=trade_update)
        recent_trades = cast(list[dict[str, object]], trade_update["recent_market_trades"])
        if should_refresh_trades and not _has_stream_trade_rows(current):
            fetched_trades = await self._get_last_trades(
                gateway=gateway,
                ref=ref,
                instrument=current,
            )
            self._last_trades_refresh_at[selected_instrument] = now
            trade_update = _visible_trade_tape_update(
                fetched_trades,
                source="tbank_get_last_trades",
            )
            current = current.model_copy(update=trade_update)
            recent_trades = cast(list[dict[str, object]], trade_update["recent_market_trades"])

        if should_refresh_book:
            payload = await self._get_order_book(
                gateway=gateway,
                ref=ref,
                instrument=current,
                recent_trades=recent_trades,
            )
            self._last_details_refresh_at[selected_instrument] = now
            if payload is not None:
                if (
                    not recent_trades
                    and trade_update.get("trade_tape_status") != "no_market_trades_samples"
                ):
                    payload = {**payload, **trade_update}
                updated = current.model_copy(update=payload)
                current = _preserve_fresh_selected_ladder(updated, current)
            elif recent_trades:
                trade_update = _visible_trade_tape_update(
                    recent_trades,
                    source=current.market_trades_source or "order_book_summary_payload",
                )
                current = current.model_copy(
                    update=trade_update
                )
            elif should_refresh_trades:
                current = current.model_copy(
                    update={
                        "recent_market_trades": [],
                        "market_trades_source": "no_market_trades_samples",
                        "market_trades_age_ms": None,
                        "trade_tape_status": "no_market_trades_samples",
                        "trade_tape_reason": "no_market_trades_samples",
                    }
                )
        elif recent_trades:
            trade_update = _visible_trade_tape_update(
                recent_trades,
                source=current.market_trades_source or "order_book_summary_payload",
            )
            current = current.model_copy(
                update=trade_update
            )

        if current.order_book_ts is not None:
            age_ms = max(
                0,
                int((now - current.order_book_ts.astimezone(UTC)).total_seconds() * 1000),
            )
            max_age_ms = int(self.config.order_book_max_exchange_age_seconds * 1000)
            if current.official_exchange_open and age_ms > max_age_ms:
                current = current.model_copy(
                    update={
                        "order_book_age_ms": age_ms,
                        "order_book_stale": True,
                        "warning": "selected_order_book_stale",
                    }
                )
                self._record_warning("selected_order_book_stale")
        self._selected_details[selected_instrument] = current
        return current

    async def _get_trading_status(
        self,
        *,
        gateway: Any,
        ref: Any,
        instrument: MarketInstrumentOverview,
    ) -> dict[str, object] | None:
        try:
            from trade_core.broker_gateway import TradingStatusRequest

            timeout_seconds = _env_float("DASHBOARD_TRADING_STATUS_TIMEOUT_SECONDS", 1.0)
            response = await _run_readonly_broker_call(
                lambda: gateway.get_trading_status(TradingStatusRequest(instrument=ref)),
                timeout_seconds=timeout_seconds,
            )
        except AttributeError:
            self._record_warning("dashboard_trading_status_not_implemented")
            return None
        except Exception as exc:
            self._record_warning(
                _reason_from_exception(exc, "dashboard_trading_status_unavailable")
            )
            return None
        raw_status = str(
            response.data.get("trading_status")
            or response.data.get("status")
            or instrument.broker_trading_status
            or "unknown"
        )
        normalized_status = raw_status.lower().removeprefix("security_trading_status_")
        api_trade_available = bool(
            response.data.get(
                "api_trade_available",
                normalized_status
                in {"normal", "normal_trading", "session_open", "trading", "open"},
            )
        )
        market_open = (
            api_trade_available
            and _status_is_open(normalized_status)
            and not instrument.official_exchange_closed
            and instrument.api_trade_available is not False
        )
        session_type, session_phase = _clock_session_context(market_open=market_open)
        venue_type = (
            "official_exchange"
            if market_open
            else "broker_otc"
            if instrument.last_price is not None
            else "unknown"
        )
        reason_code = (
            "market_open"
            if market_open
            else "official_exchange_closed"
            if instrument.official_exchange_closed
            else "market_closed_expected"
        )
        self._clear_warning("dashboard_trading_status_unavailable")
        return {
            "session_type": session_type,
            "official_exchange_open": market_open,
            "official_exchange_closed": not market_open,
            "venue_type": venue_type,
            "trading_mode": (
                "standard_exchange"
                if market_open
                else "broker_otc_only"
                if instrument.official_exchange_closed and instrument.last_price is not None
                else "exchange_closed"
            ),
            "broker_trading_status": normalized_status,
            "api_trade_available": api_trade_available,
            "quote_allowed_for_data_collection": market_open,
            "reason_code": reason_code,
            "quote_payload": {
                **instrument.quote_payload,
                "dashboard_trading_status_source": response.method_name,
                "broker_trading_status": normalized_status,
                "api_trade_available": api_trade_available,
                "session_phase": session_phase,
                "session_type": session_type,
            },
        }

    async def _get_last_trades(
        self,
        *,
        gateway: Any,
        ref: Any,
        instrument: MarketInstrumentOverview,
    ) -> list[dict[str, object]]:
        try:
            from trade_core.broker_gateway import LastTradesRequest

            to_ts = datetime.now(tz=UTC)
            from_ts = to_ts - timedelta(
                minutes=max(1, _env_int("DASHBOARD_TRADES_LOOKBACK_MINUTES", 30))
            )
            trade_source = "all"
            timeout_seconds = _env_float("DASHBOARD_TRADES_TIMEOUT_SECONDS", 1.0)
            response = await _run_readonly_broker_call(
                lambda: gateway.get_last_trades(
                    LastTradesRequest(
                        instrument=ref,
                        from_=from_ts,
                        to=to_ts,
                        trade_source=trade_source,
                    )
                ),
                timeout_seconds=timeout_seconds,
            )
        except AttributeError:
            self._record_warning("no_market_trades_feed_implemented")
            return []
        except Exception as exc:
            self._record_warning(_reason_from_exception(exc, "no_market_trades_samples"))
            return []
        raw_trades = response.data.get("trades")
        if not isinstance(raw_trades, list):
            return []
        limit = max(1, _env_int("DASHBOARD_TRADES_LIMIT", 20))
        venue_type = "official_exchange" if instrument.official_exchange_open else "broker_otc"
        source = "tbank_get_last_trades"
        normalized: list[dict[str, object]] = []
        for item in raw_trades:
            if isinstance(item, dict):
                normalized.append(
                    {
                        **item,
                        "instrument_id": instrument.instrument_id,
                        "source": source,
                        "venue_type": venue_type,
                        "include_in_calibration": instrument.official_exchange_open,
                    }
                )
        normalized.sort(key=_trade_sort_ts, reverse=True)
        normalized = normalized[:limit]
        return normalized

    async def _get_order_book(
        self,
        *,
        gateway: Any,
        ref: Any,
        instrument: MarketInstrumentOverview,
        recent_trades: list[dict[str, object]],
    ) -> dict[str, object] | None:
        from trade_core.broker_gateway import OrderBookRequest

        timeout_seconds = _env_float("DASHBOARD_ORDER_BOOK_TIMEOUT_SECONDS", 1.0)
        response: Any | None = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await _run_readonly_broker_call(
                    lambda: gateway.get_order_book(OrderBookRequest(instrument=ref, depth=10)),
                    timeout_seconds=timeout_seconds,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.2)
        if response is None:
            self._record_warning(
                _reason_from_exception(
                    last_error or RuntimeError("selected_order_book_unavailable"),
                    "selected_order_book_unavailable",
                )
            )
            return None
        payload = _order_book_overview_payload(
            response.data,
            official_exchange_open=instrument.official_exchange_open,
            official_exchange_closed=instrument.official_exchange_closed,
            recent_market_trades=recent_trades,
            max_exchange_age_seconds=self.config.order_book_max_exchange_age_seconds,
        )
        if payload is None:
            self._record_warning("no_order_book_samples")
        else:
            self._clear_warning("selected_order_book_unavailable")
            self._clear_warning("selected_order_book_stale")
            self._clear_warning("no_order_book_samples")
        return payload

    def _record_error(self, value: str) -> None:
        self._errors = _bounded_unique([value, *self._errors], limit=5)

    def _record_warning(self, value: str) -> None:
        self._warnings = _bounded_unique([value, *self._warnings], limit=8)

    def _clear_error(self, value: str) -> None:
        self._errors = [item for item in self._errors if item != value]

    def _clear_warning(self, value: str) -> None:
        self._warnings = [item for item in self._warnings if item != value]


def _instrument_with_last_price(
    instrument: MarketInstrumentOverview,
    payload: dict[str, object] | None,
    *,
    max_exchange_age_seconds: float,
) -> MarketInstrumentOverview:
    if payload is None:
        return instrument
    price = _decimal_or_none(payload.get("price"))
    if price is None:
        return instrument
    now = datetime.now(tz=UTC)
    exchange_ts = _datetime_or_none(payload.get("exchange_ts"))
    freshness = _freshness_payload(
        exchange_ts=exchange_ts,
        received_ts=now,
        max_exchange_age_seconds=max_exchange_age_seconds,
        received_snapshot_is_authoritative=False,
    )
    stale_by_exchange = bool(freshness["stale_by_exchange_time"])
    freshness_status = str(freshness["freshness_status"])
    existing_order_book_fresh = (
        instrument.order_book_source in {"live_order_book_mid", "live_exchange_order_book"}
        and instrument.order_book_stale is False
        and instrument.mid_price is not None
        and instrument.best_bid is not None
        and instrument.best_ask is not None
    )
    if existing_order_book_fresh and stale_by_exchange:
        return instrument
    quote_source = (
        "live_exchange_last_price"
        if instrument.official_exchange_open
        else "broker_quote_exchange_closed"
        if instrument.official_exchange_closed
        else "broker_indicative_quote"
    )
    venue_type = (
        "official_exchange"
        if instrument.official_exchange_open
        else "broker_otc"
        if instrument.official_exchange_closed
        else "broker_indicative"
    )
    session_type = (
        _clock_session_context(market_open=True)[0]
        if instrument.official_exchange_open
        else instrument.session_type
    )
    trading_mode = (
        "standard_exchange"
        if instrument.official_exchange_open
        else instrument.trading_mode
    )
    display_status = (
        "live"
        if instrument.official_exchange_open and not stale_by_exchange
        else "display_only"
        if not stale_by_exchange
        else "stale"
    )
    warning = instrument.warning
    if stale_by_exchange:
        warning = str(freshness["freshness_reason"])
    elif instrument.official_exchange_closed:
        warning = "broker_quote_not_for_calibration"
    return instrument.model_copy(
        update={
            "session_type": session_type,
            "trading_mode": trading_mode,
            "last_price": price,
            "last_price_at": exchange_ts or now,
            "last_price_ts": exchange_ts or now,
            "last_price_source": quote_source,
            "quote_source": quote_source,
            "venue_type": venue_type,
            "quote_allowed_for_display": True,
            "quote_allowed_for_data_collection": (
                instrument.official_exchange_open and not stale_by_exchange
            ),
            "quote_status": display_status,
            "is_price_stale": stale_by_exchange,
            "price_staleness_seconds": _age_seconds(freshness.get("exchange_age_ms")),
            "received_ts": now,
            "exchange_ts": exchange_ts,
            "received_age_ms": freshness["received_age_ms"],
            "exchange_age_ms": freshness["exchange_age_ms"],
            "stale_by_received_time": freshness["stale_by_received_time"],
            "stale_by_exchange_time": freshness["stale_by_exchange_time"],
            "freshness_status": freshness_status,
            "freshness_reason": freshness["freshness_reason"],
            "warning": warning,
            "quote_payload": {
                **instrument.quote_payload,
                "source": quote_source,
                "quote_source": quote_source,
                "venue_type": venue_type,
                "dashboard_live_feed": True,
                "include_in_calibration": (
                    instrument.official_exchange_open and not stale_by_exchange
                ),
                "quote_allowed_for_display": True,
                "quote_allowed_for_data_collection": (
                    instrument.official_exchange_open and not stale_by_exchange
                ),
                **freshness,
            },
        }
    )


def _order_book_overview_payload(
    payload: dict[str, object],
    *,
    official_exchange_open: bool,
    official_exchange_closed: bool,
    recent_market_trades: list[dict[str, object]] | None,
    max_exchange_age_seconds: float,
) -> dict[str, object] | None:
    bids = _price_levels(payload.get("bids"), reverse=True)
    asks = _price_levels(payload.get("asks"), reverse=False)
    if not bids or not asks:
        return None
    now = datetime.now(tz=UTC)
    exchange_ts = _datetime_or_none(payload.get("exchange_ts"))
    freshness = _freshness_payload(
        exchange_ts=exchange_ts,
        received_ts=now,
        max_exchange_age_seconds=max_exchange_age_seconds,
        received_snapshot_is_authoritative=True,
    )
    stale_by_exchange = bool(freshness["stale_by_exchange_time"])
    order_book_age_ms = cast(int | None, freshness["exchange_age_ms"])
    best_bid_price, best_bid_qty = bids[0]
    best_ask_price, best_ask_qty = asks[0]
    bid_depth = sum((qty for _, qty in bids[:5]), Decimal("0"))
    ask_depth = sum((qty for _, qty in asks[:5]), Decimal("0"))
    spread_metrics = calculate_spread_metrics(best_bid_price, best_ask_price)
    if spread_metrics.mid_price is None:
        return None
    depth_total = bid_depth + ask_depth
    imbalance = None if depth_total == 0 else (bid_depth - ask_depth) / depth_total
    venue_type = (
        "official_exchange"
        if official_exchange_open
        else "broker_otc"
        if official_exchange_closed
        else "broker_indicative"
    )
    quote_source = (
        "live_order_book_mid"
        if official_exchange_open
        else "broker_quote_exchange_closed"
        if official_exchange_closed
        else "broker_indicative_quote"
    )
    trades = recent_market_trades or []
    include_in_calibration = official_exchange_open and not stale_by_exchange
    quality_components = calculate_market_quality(
        spread_bps=spread_metrics.spread_bps,
        bid_depth_lots=bid_depth,
        ask_depth_lots=ask_depth,
        best_bid_qty_lots=best_bid_qty,
        best_ask_qty_lots=best_ask_qty,
        book_imbalance=imbalance,
        order_book_age_ms=order_book_age_ms,
        order_book_stale=stale_by_exchange,
        venue_type=venue_type,
        official_exchange_open=include_in_calibration,
        trades_count=len(trades),
    )
    trade_age_ms = _market_trades_age_ms(trades)
    trade_status = _trade_tape_status(trades, trade_age_ms)
    trade_reason = _trade_tape_reason(trades, trade_age_ms)
    display_status = (
        "live"
        if official_exchange_open and not stale_by_exchange
        else "display_only"
        if not stale_by_exchange
        else "stale"
    )
    warning = str(freshness["freshness_reason"]) if stale_by_exchange else None
    if official_exchange_closed and not stale_by_exchange:
        warning = "broker_quote_not_for_calibration"
    return {
        "venue_type": venue_type,
        "trading_mode": (
            "standard_exchange"
            if official_exchange_open
            else "broker_otc_only"
            if official_exchange_closed
            else "indicative_only"
        ),
        "quote_source": quote_source,
        "quote_allowed_for_data_collection": include_in_calibration,
        "quote_allowed_for_display": True,
        "last_price": spread_metrics.mid_price,
        "last_price_at": exchange_ts or now,
        "last_price_ts": exchange_ts or now,
        "last_price_source": quote_source,
        "is_price_stale": stale_by_exchange,
        "price_staleness_seconds": _age_seconds(order_book_age_ms),
        "received_ts": now,
        "exchange_ts": exchange_ts,
        "received_age_ms": freshness["received_age_ms"],
        "exchange_age_ms": freshness["exchange_age_ms"],
        "stale_by_received_time": freshness["stale_by_received_time"],
        "stale_by_exchange_time": freshness["stale_by_exchange_time"],
        "freshness_status": freshness["freshness_status"],
        "freshness_reason": freshness["freshness_reason"],
        "quote_status": display_status,
        "spread": spread_metrics.spread_abs,
        "spread_abs": spread_metrics.spread_abs,
        "spread_bps": spread_metrics.spread_bps,
        "spread_abs_rub": spread_metrics.spread_abs,
        "mid_price": spread_metrics.mid_price,
        "market_quality": quality_components.get("display_market_quality_score"),
        "market_quality_score": quality_components.get("display_market_quality_score"),
        "display_market_quality_score": quality_components.get("display_market_quality_score"),
        "calibration_market_quality_score": quality_components.get(
            "calibration_market_quality_score"
        ),
        "market_quality_label": quality_components.get("market_quality_label", "unknown"),
        "market_quality_components": quality_components,
        "best_bid": best_bid_price,
        "best_ask": best_ask_price,
        "bid_depth_lots": bid_depth,
        "ask_depth_lots": ask_depth,
        "book_imbalance": imbalance,
        "order_book_source": quote_source,
        "order_book_ts": exchange_ts or now,
        "order_book_age_ms": order_book_age_ms,
        "order_book_stale": stale_by_exchange,
        "recent_market_trades": trades,
        "market_trades_source": _market_trades_source(trades),
        "market_trades_age_ms": trade_age_ms,
        "trade_tape_status": trade_status,
        "trade_tape_reason": trade_reason,
        "warning": warning,
        "order_book_summary": {
            "source": quote_source,
            "venue_type": venue_type,
            "quote_allowed_for_data_collection": include_in_calibration,
            "include_in_calibration": include_in_calibration,
            "depth_levels": len(bids) + len(asks),
            "bids": [
                {"price": str(price), "quantity_lots": str(quantity)}
                for price, quantity in bids[:20]
            ],
            "asks": [
                {"price": str(price), "quantity_lots": str(quantity)}
                for price, quantity in asks[:20]
            ],
            "best_bid_qty_lots": str(best_bid_qty),
            "best_ask_qty_lots": str(best_ask_qty),
            "bid_depth_lots": str(bid_depth),
            "ask_depth_lots": str(ask_depth),
            "book_imbalance": str(imbalance) if imbalance is not None else None,
            "spread_abs_rub": str(spread_metrics.spread_abs),
            "spread_bps": str(spread_metrics.spread_bps),
            "market_quality_components": quality_components,
            "exchange_ts": exchange_ts.isoformat() if exchange_ts is not None else None,
            "received_ts": now.isoformat(),
            "age_seconds": _age_seconds(order_book_age_ms),
            "is_stale": stale_by_exchange,
            **freshness,
        },
        "quote_payload": {
            "source": quote_source,
            "quote_source": quote_source,
            "venue_type": venue_type,
            "dashboard_live_feed": True,
            "include_in_calibration": include_in_calibration,
            "order_book_stale": stale_by_exchange,
            "market_quality_components": quality_components,
            **freshness,
        },
    }


def _prices_by_instrument(
    data: dict[str, object],
    refs: Sequence[Any],
) -> dict[str, dict[str, object]]:
    ref_by_broker_id: dict[str, str] = {}
    for ref in refs:
        instrument_id = str(getattr(ref, "instrument_id", "") or "")
        for key in (getattr(ref, "instrument_uid", None), getattr(ref, "figi", None)):
            if key:
                ref_by_broker_id[str(key)] = instrument_id
    prices: dict[str, dict[str, object]] = {}
    raw_prices = data.get("prices")
    if not isinstance(raw_prices, list):
        return prices
    for item in raw_prices:
        if not isinstance(item, dict):
            continue
        broker_key = str(
            item.get("instrument_uid")
            or item.get("figi")
            or item.get("instrument_id")
            or ""
        )
        target = ref_by_broker_id.get(broker_key)
        if target:
            prices[target] = item
    return prices


def _resolved_refs(
    refs: Sequence[Any],
    instruments: Sequence[MarketInstrumentOverview],
) -> list[Any]:
    allowed = {item.instrument_id for item in instruments}
    return [
        ref
        for ref in refs
        if str(getattr(ref, "instrument_id", "")) in allowed
        and (getattr(ref, "instrument_uid", None) or getattr(ref, "figi", None))
    ]


def _merge_overviews(
    base: MarketOverviewResponse,
    cached: MarketOverviewResponse | None,
) -> MarketOverviewResponse:
    if cached is None:
        return base
    cached_by_id = {row.instrument_id: row for row in cached.instruments}
    rows = [
        _prefer_live_row(row, cached_by_id.get(row.instrument_id))
        for row in base.instruments
    ]
    return MarketOverviewResponse(
        generated_at=max(base.generated_at, cached.generated_at),
        instruments=rows,
    )


def _prefer_live_row(
    base: MarketInstrumentOverview,
    cached: MarketInstrumentOverview | None,
) -> MarketInstrumentOverview:
    if cached is None:
        return base
    if not _cached_row_safe_for_base_session(base, cached):
        return base
    cached_priority = _source_priority(cached.last_price_source)
    base_priority = _source_priority(base.last_price_source)
    if cached.last_price is None or cached_priority < base_priority:
        return base
    if not _row_display_fresh(cached) and base.last_price is not None:
        return base
    if cached_priority == base_priority and _row_data_timestamp(base) > _row_data_timestamp(cached):
        return base
    if cached_priority >= base_priority:
        return base.model_copy(
            update=cached.model_dump(
                exclude={
                    "instrument_id",
                    "ticker",
                    "class_code",
                    "board",
                    "exchange",
                    "session_type",
                    "broker_trading_status",
                    "api_trade_available",
                }
            )
        )
    return base


def _row_display_fresh(row: MarketInstrumentOverview) -> bool:
    if row.quote_status != "live":
        return False
    return not (row.is_price_stale or row.stale_by_received_time)


def _row_data_timestamp(row: MarketInstrumentOverview) -> datetime:
    candidates = [
        value
        for value in (
            row.received_ts,
            row.order_book_ts,
            row.last_price_at,
            row.exchange_ts,
        )
        if value is not None
    ]
    ensured = [_ensure_utc(value) for value in candidates if value is not None]
    return max(ensured, default=datetime.min.replace(tzinfo=UTC))


def _cached_row_safe_for_base_session(
    base: MarketInstrumentOverview,
    cached: MarketInstrumentOverview,
) -> bool:
    if base.official_exchange_open:
        return True
    cached_payload = cached.quote_payload if isinstance(cached.quote_payload, dict) else {}
    cached_book = (
        cached.order_book_summary if isinstance(cached.order_book_summary, dict) else {}
    )
    return not (
        cached.official_exchange_open
        or cached.quote_allowed_for_data_collection
        or cached_payload.get("include_in_calibration") is True
        or cached_book.get("include_in_calibration") is True
        or str(cached.quote_source).startswith("live")
        or str(cached.last_price_source).startswith("live")
    )


def _prefer_selected_details(
    base: MarketInstrumentOverview,
    cached: MarketInstrumentOverview | None,
) -> MarketInstrumentOverview:
    if cached is None:
        return base
    preferred = _prefer_live_row(base, cached)
    if not _cached_row_safe_for_base_session(base, cached):
        return preferred
    return _preserve_fresh_selected_ladder(preferred, cached)


def _preserve_fresh_selected_ladder(
    preferred: MarketInstrumentOverview,
    cached: MarketInstrumentOverview,
) -> MarketInstrumentOverview:
    if not _cached_row_safe_for_base_session(preferred, cached):
        return preferred
    if not _selected_ladder_display_fresh(cached):
        return preferred
    cached_levels = _order_book_level_count(cached)
    preferred_levels = _order_book_level_count(preferred)
    if cached_levels <= 0 or preferred_levels >= cached_levels:
        return preferred
    if cached_levels < 8 and preferred_levels > 0:
        return preferred
    return preferred.model_copy(update=_selected_ladder_fields(cached))


def _selected_ladder_display_fresh(row: MarketInstrumentOverview) -> bool:
    if row.order_book_source is None or row.order_book_stale:
        return False
    if row.order_book_ts is not None:
        return datetime.now(tz=UTC) - _ensure_utc(row.order_book_ts) <= timedelta(seconds=30)
    if row.order_book_age_ms is not None:
        return row.order_book_age_ms <= 30_000
    return False


def _order_book_level_count(row: MarketInstrumentOverview) -> int:
    summary = row.order_book_summary if isinstance(row.order_book_summary, dict) else {}
    bids = summary.get("bids")
    asks = summary.get("asks")
    bid_count = len(bids) if isinstance(bids, list) else 0
    ask_count = len(asks) if isinstance(asks, list) else 0
    depth_levels = _safe_int(summary.get("depth_levels"))
    return max(bid_count + ask_count, depth_levels or 0)


def _safe_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _selected_ladder_fields(row: MarketInstrumentOverview) -> dict[str, object]:
    return {
        "best_bid": row.best_bid,
        "best_ask": row.best_ask,
        "mid_price": row.mid_price,
        "spread": row.spread,
        "spread_abs": row.spread_abs,
        "spread_bps": row.spread_bps,
        "spread_abs_rub": row.spread_abs_rub,
        "spread_units_validated": row.spread_units_validated,
        "bid_depth_lots": row.bid_depth_lots,
        "ask_depth_lots": row.ask_depth_lots,
        "book_imbalance": row.book_imbalance,
        "market_quality": row.market_quality,
        "market_quality_score": row.market_quality_score,
        "display_market_quality_score": row.display_market_quality_score,
        "calibration_market_quality_score": row.calibration_market_quality_score,
        "market_quality_label": row.market_quality_label,
        "market_quality_components": row.market_quality_components,
        "order_book_source": row.order_book_source,
        "order_book_ts": row.order_book_ts,
        "order_book_age_ms": row.order_book_age_ms,
        "order_book_stale": row.order_book_stale,
        "order_book_summary": row.order_book_summary,
    }


def _replace_instrument(
    overview: MarketOverviewResponse,
    instrument: MarketInstrumentOverview,
) -> MarketOverviewResponse:
    return overview.model_copy(
        update={
            "generated_at": datetime.now(tz=UTC),
            "instruments": [
                instrument if row.instrument_id == instrument.instrument_id else row
                for row in overview.instruments
            ],
        }
    )


def _limit_overview(
    overview: MarketOverviewResponse,
    max_instruments: int,
) -> MarketOverviewResponse:
    if len(overview.instruments) <= max_instruments:
        return overview
    return overview.model_copy(update={"instruments": overview.instruments[:max_instruments]})


def _source_priority(source: str | None) -> int:
    if source in {"live_order_book_mid", "live_exchange_order_book"}:
        return 6
    if source == "live_exchange_last_price":
        return 5
    if source in {"broker_quote_exchange_closed", "broker_otc_order_book"}:
        return 4
    if source in {"broker_indicative_quote", "tbank_last_price"}:
        return 3
    if source == "latest_market_candle_close":
        return 1
    return 0


def _session_payload(instrument: MarketInstrumentOverview | None) -> dict[str, object]:
    if instrument is None:
        return {
            "market_open": False,
            "session_type": "unknown",
            "session_phase": "unknown",
            "venue_type": "unknown",
            "data_only_collection_allowed": False,
            "reason_code": "instrument_unavailable",
            "next_session_at": None,
        }
    reason = instrument.reason_code
    if instrument.official_exchange_open and reason in {"no_price_source_available", None}:
        reason = "market_open"
    session_type, session_phase = _clock_session_context(
        market_open=instrument.official_exchange_open
    )
    return {
        "market_open": instrument.official_exchange_open,
        "session_type": session_type,
        "session_phase": session_phase,
        "venue_type": instrument.venue_type,
        "data_only_collection_allowed": instrument.quote_allowed_for_data_collection,
        "reason_code": reason,
        "next_session_at": None,
    }


def _clock_session_context(*, market_open: bool) -> tuple[str, str]:
    now_msk = datetime.now(tz=UTC).astimezone(MSK)
    if market_open:
        return _clock_open_session_type(now_msk), "continuous_trading"
    return "closed", "closed"


def _clock_open_session_type(now_msk: datetime) -> str:
    if now_msk.weekday() >= 5:
        return "weekend"
    current_time = now_msk.time()
    if time(7, 0) <= current_time < time(10, 0):
        return "weekday_morning"
    if time(10, 0) <= current_time < time(19, 0):
        return "weekday_main"
    return "weekday_evening"


def _status_is_open(value: str) -> bool:
    return value in {"normal", "normal_trading", "session_open", "trading", "open"}


def _price_levels(value: object, *, reverse: bool) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(value, list):
        return []
    levels: list[tuple[Decimal, Decimal]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        price = _decimal_or_none(item.get("price"))
        qty = _decimal_or_none(item.get("quantity_lots") or item.get("quantity"))
        if price is not None and qty is not None:
            levels.append((price, qty))
    return sorted(levels, key=lambda item: item[0], reverse=reverse)


def _market_trades_age_ms(trades: Sequence[dict[str, object]]) -> int | None:
    newest: datetime | None = None
    for trade in trades:
        ts = _datetime_or_none(
            trade.get("exchange_ts")
            or trade.get("ts_utc")
            or trade.get("time")
            or trade.get("ts")
        )
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    if newest is None:
        return None
    return max(0, int((datetime.now(tz=UTC) - newest).total_seconds() * 1000))


def _trade_sort_ts(trade: dict[str, object]) -> datetime:
    return _datetime_or_none(
        trade.get("exchange_ts")
        or trade.get("ts_utc")
        or trade.get("time")
        or trade.get("ts")
    ) or datetime.min.replace(tzinfo=UTC)


def _market_trades_source(trades: Sequence[dict[str, object]]) -> str:
    if not trades:
        return "no_market_trades_samples"
    for trade in trades:
        source = trade.get("source")
        if isinstance(source, str) and source:
            if source.startswith("tbank_get_last_trades"):
                return "tbank_get_last_trades"
            return source
    return "market_trades_stream"


def _visible_trade_tape_update(
    trades: Sequence[dict[str, object]] | None,
    *,
    source: str,
    stale_source: str | None = None,
) -> dict[str, object]:
    normalized = [dict(item) for item in trades or [] if isinstance(item, dict)]
    age_ms = _market_trades_age_ms(normalized)
    status = _trade_tape_status(normalized, age_ms)
    reason = _trade_tape_reason(normalized, age_ms)
    if status == "live":
        return {
            "recent_market_trades": normalized,
            "market_trades_source": source,
            "market_trades_age_ms": age_ms,
            "trade_tape_status": status,
            "trade_tape_reason": reason,
        }
    return {
        "recent_market_trades": [],
        "market_trades_source": (
            "no_market_trades_samples"
            if not normalized
            else source or stale_source or "stale_market_trades_samples"
        ),
        "market_trades_age_ms": age_ms,
        "trade_tape_status": status,
        "trade_tape_reason": reason,
    }


def _has_stream_trade_rows(instrument: MarketInstrumentOverview) -> bool:
    if not instrument.recent_market_trades:
        return False
    source = instrument.market_trades_source or ""
    return source in {"market_trades_stream", "order_book_summary_payload"} or source.startswith(
        "market_trades_stream"
    )


def _freshness_payload(
    *,
    exchange_ts: datetime | None,
    received_ts: datetime,
    max_exchange_age_seconds: float,
    received_snapshot_is_authoritative: bool = False,
) -> dict[str, object]:
    received_ts = received_ts.astimezone(UTC)
    now = datetime.now(tz=UTC)
    received_age_ms = max(0, int((now - received_ts).total_seconds() * 1000))
    exchange_age_ms: int | None = None
    if exchange_ts is not None:
        exchange_ts = exchange_ts.astimezone(UTC)
        exchange_age_ms = max(0, int((now - exchange_ts).total_seconds() * 1000))
    max_age_ms = max_exchange_age_seconds * 1000
    stale_by_received_time = received_age_ms > max_age_ms
    stale_by_exchange_time = (
        exchange_ts is None
        or exchange_age_ms is None
        or exchange_age_ms > max_age_ms
    )
    if received_snapshot_is_authoritative and not stale_by_received_time:
        stale_by_exchange_time = False
        status = "fresh"
        reason = "fresh"
    elif exchange_ts is None:
        status = "unknown"
        reason = "missing_exchange_ts"
    elif stale_by_received_time:
        status = "stale"
        reason = "received_ts_too_old"
    elif stale_by_exchange_time:
        status = "stale"
        reason = "exchange_ts_too_old"
    else:
        status = "fresh"
        reason = "fresh"
    return {
        "received_ts": received_ts.isoformat(),
        "exchange_ts": exchange_ts.isoformat() if exchange_ts is not None else None,
        "received_age_ms": received_age_ms,
        "exchange_age_ms": exchange_age_ms,
        "stale_by_received_time": stale_by_received_time,
        "stale_by_exchange_time": stale_by_exchange_time,
        "freshness_status": status,
        "freshness_reason": reason,
    }


def _age_seconds(age_ms: object) -> int | None:
    if age_ms is None:
        return None
    try:
        return max(0, int(int(cast(Any, age_ms)) / 1000))
    except (TypeError, ValueError):
        return None


def _trade_tape_status(
    trades: Sequence[dict[str, object]],
    age_ms: int | None,
) -> str:
    if not trades:
        return "no_market_trades_samples"
    if age_ms is None:
        return "stale"
    max_age_ms = int(_env_float("DASHBOARD_TRADES_MAX_EXCHANGE_AGE_SECONDS", 15.0) * 1000)
    return "live" if age_ms <= max_age_ms else "stale"


def _trade_tape_reason(
    trades: Sequence[dict[str, object]],
    age_ms: int | None,
) -> str:
    if not trades:
        return "no_market_trades_samples"
    if age_ms is None:
        return "missing_trade_exchange_ts"
    max_age_ms = int(_env_float("DASHBOARD_TRADES_MAX_EXCHANGE_AGE_SECONDS", 15.0) * 1000)
    return "fresh" if age_ms <= max_age_ms else "trade_exchange_ts_too_old"


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _datetime_or_none(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_moex_instrument(value: str) -> str:
    stripped = value.strip().upper()
    if not stripped:
        return "MOEX:SBER"
    if ":" in stripped:
        return stripped
    return f"MOEX:{stripped}"


def _bounded_unique(values: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result[:limit]


def _reason_from_exception(exc: Exception, default: str) -> str:
    text = str(exc).strip()
    if text and " " not in text and len(text) <= 96:
        return text
    return default


async def _run_readonly_broker_call(
    factory: Callable[[], Any],
    *,
    timeout_seconds: float,
) -> Any:
    """Run readonly broker calls away from the FastAPI event loop."""

    def _invoke() -> Any:
        result = factory()
        if inspect.isawaitable(result):
            return asyncio.run(_await_any(result))
        return result

    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_readonly_broker_executor(), _invoke),
        timeout=timeout_seconds,
    )


async def _await_any(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _readonly_broker_executor() -> ThreadPoolExecutor:
    global _READONLY_BROKER_EXECUTOR
    if _READONLY_BROKER_EXECUTOR is None:
        max_workers = max(1, _env_int("BROKER_READONLY_MAX_CONCURRENCY", 4))
        _READONLY_BROKER_EXECUTOR = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="dashboard-broker-readonly",
        )
    return _READONLY_BROKER_EXECUTOR


__all__ = ["DashboardMarketFeedConfig", "DashboardMarketFeedService"]
