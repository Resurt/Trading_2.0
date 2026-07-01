"""Instrument resolution and registry synchronization for trade-core startup."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import BrokerGateway, InstrumentRef, InstrumentResolveRequest
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.models import InstrumentRegistry
from trading_common.db.repositories import InstrumentRepository
from trading_common.telemetry import get_logger, log_event

LOGGER = get_logger(__name__)
PLACEHOLDER_MARKERS = (
    "placeholder",
    "runtime-placeholder",
    "test-placeholder",
    "safe-noop",
    "safe_noop",
)
UNRESOLVED_INSTRUMENT_ERROR_CODE = "instrument_not_resolved_for_broker_call"
REAL_BROKER_MODES = {
    RuntimeMode.SANDBOX,
    RuntimeMode.SHADOW,
    RuntimeMode.PRODUCTION,
}


@dataclass(slots=True)
class InstrumentResolverService:
    """Resolve configured tickers to production-safe broker instrument IDs."""

    broker_gateway: BrokerGateway
    session: Session
    launch_policy: LaunchModePolicy
    exchange: str = "MOEX"

    async def resolve_startup_instruments(
        self,
        requested: tuple[InstrumentRef, ...],
    ) -> tuple[InstrumentRef, ...]:
        if self.launch_policy.mode is RuntimeMode.HISTORICAL_REPLAY:
            return self._register_local_instruments(requested)

        tickers = tuple(_ticker_for(instrument) for instrument in requested)
        class_code = requested[0].class_code if requested and requested[0].class_code else "TQBR"
        try:
            response = await self.broker_gateway.resolve_instruments(
                InstrumentResolveRequest(tickers=tickers, class_code=class_code)
            )
        except Exception as exc:
            cached = self._resolved_from_registry(tickers)
            if len(cached) == len(tickers) and all(
                is_broker_resolved_instrument(instrument) for instrument in cached
            ):
                log_event(
                    logger=LOGGER,
                    event_type="instrument_registry_cached_fallback",
                    component="runtime.instruments",
                    launch_mode=self.launch_policy.mode.value,
                    tickers=list(tickers),
                    instrument_count=len(cached),
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
                return cached
            self._mark_failed_tickers(
                list(tickers),
                class_code=class_code,
                error_code="instrument_resolver_broker_unavailable",
                error_message=str(exc),
            )
            msg = "T-Bank instrument resolver unavailable and cached registry is incomplete"
            raise RuntimeError(msg) from exc
        resolved = tuple(
            self._upsert_resolved_payload(payload, exchange=self.exchange)
            for payload in _instrument_payloads(response.data)
        )
        missing_tickers = sorted(set(tickers) - {instrument.ticker for instrument in resolved})
        if missing_tickers:
            self._mark_failed_tickers(
                missing_tickers,
                class_code=class_code,
                error_code="instrument_resolver_missing_ticker",
                error_message=(
                    "T-Bank instrument resolver did not return requested tickers: "
                    + ", ".join(missing_tickers)
                ),
            )
            msg = f"T-Bank instrument resolver did not return tickers: {', '.join(missing_tickers)}"
            raise RuntimeError(msg)
        if any(not is_broker_resolved_instrument(instrument) for instrument in resolved):
            msg = "production-like runtime refuses placeholder instrument_uid"
            raise RuntimeError(msg)

        self.session.flush()
        log_event(
            logger=LOGGER,
            event_type="instrument_registry_resolved",
            component="runtime.instruments",
            launch_mode=self.launch_policy.mode.value,
            tickers=list(tickers),
            instrument_count=len(resolved),
        )
        return resolved

    def _register_local_instruments(
        self,
        requested: tuple[InstrumentRef, ...],
    ) -> tuple[InstrumentRef, ...]:
        repository = InstrumentRepository(self.session)
        registered: list[InstrumentRef] = []
        for instrument in requested:
            ticker = _ticker_for(instrument)
            existing = repository.get_by_ticker(ticker)
            if existing is not None and existing.is_enabled:
                registered.append(
                    InstrumentRef(
                        instrument_id=existing.instrument_id,
                        instrument_uid=existing.instrument_uid,
                        figi=existing.figi,
                        ticker=existing.ticker,
                        class_code=existing.class_code,
                        lot_size=existing.lot_size,
                        min_price_increment=existing.min_price_increment,
                    )
                )
                continue
            registry = InstrumentRegistry(
                instrument_id=instrument.instrument_id,
                ticker=ticker,
                class_code=instrument.class_code or "TQBR",
                figi=None,
                instrument_uid=instrument.instrument_uid,
                name=ticker,
                lot_size=1,
                min_price_increment=Decimal("0.01"),
                currency="RUB",
                is_enabled=True,
                supports_morning=True,
                supports_evening=True,
                supports_weekend=False,
                source="seed",
                resolved_at=None,
                resolution_status="unresolved",
                resolution_error_code=None,
                resolution_error_message=None,
                broker_payload=None,
                instrument_payload={"source": "historical_replay_local_registry"},
            )
            repository.upsert(registry)
            registered.append(
                InstrumentRef(
                    instrument_id=registry.instrument_id,
                    instrument_uid=registry.instrument_uid,
                    figi=registry.figi,
                    ticker=registry.ticker,
                    class_code=registry.class_code,
                    lot_size=registry.lot_size,
                    min_price_increment=registry.min_price_increment,
                )
            )
        self.session.flush()
        return tuple(registered)

    def _resolved_from_registry(self, tickers: tuple[str, ...]) -> tuple[InstrumentRef, ...]:
        if not tickers:
            return ()
        rows = self.session.execute(
            select(InstrumentRegistry).where(InstrumentRegistry.ticker.in_(tickers))
        ).scalars()
        by_ticker = {row.ticker: row for row in rows if row.is_enabled}
        resolved: list[InstrumentRef] = []
        for ticker in tickers:
            row = by_ticker.get(ticker)
            if row is None or not is_broker_resolved_instrument(row):
                return ()
            resolved.append(
                InstrumentRef(
                    instrument_id=row.instrument_id,
                    instrument_uid=row.instrument_uid,
                    figi=row.figi,
                    ticker=row.ticker,
                    class_code=row.class_code,
                    lot_size=row.lot_size,
                    min_price_increment=row.min_price_increment,
                )
            )
        return tuple(resolved)

    def _upsert_resolved_payload(
        self,
        payload: Mapping[str, object],
        *,
        exchange: str,
    ) -> InstrumentRef:
        ticker = _required_str(payload, "ticker")
        instrument_uid = _required_str(payload, "instrument_uid")
        canonical_instrument_id = f"{exchange}:{ticker}"
        existing_by_ticker = self.session.execute(
            select(InstrumentRegistry).where(InstrumentRegistry.ticker == ticker)
        ).scalars().first()
        if existing_by_ticker is not None:
            existing_by_ticker.instrument_id = (
                existing_by_ticker.instrument_id or canonical_instrument_id
            )
            existing_by_ticker.class_code = _optional_str(payload, "class_code") or "TQBR"
            existing_by_ticker.figi = _optional_str(payload, "figi")
            existing_by_ticker.instrument_uid = instrument_uid
            existing_by_ticker.name = _optional_str(payload, "name") or ticker
            existing_by_ticker.lot_size = _required_int_payload(payload, "lot_size")
            existing_by_ticker.min_price_increment = _required_decimal_payload(
                payload,
                "min_price_increment",
            )
            existing_by_ticker.currency = _optional_str(payload, "currency") or "RUB"
            existing_by_ticker.is_enabled = True
            existing_by_ticker.supports_morning = True
            existing_by_ticker.supports_evening = True
            existing_by_ticker.supports_weekend = _bool_payload(payload, "supports_weekend")
            existing_by_ticker.source = "tbank_resolved"
            existing_by_ticker.resolved_at = datetime.now(tz=UTC)
            existing_by_ticker.resolution_status = "resolved"
            existing_by_ticker.resolution_error_code = None
            existing_by_ticker.resolution_error_message = None
            existing_by_ticker.broker_payload = dict(payload)
            existing_by_ticker.instrument_payload = dict(payload)
            registry = existing_by_ticker
        else:
            registry = InstrumentRegistry(
                instrument_id=canonical_instrument_id,
                ticker=ticker,
                class_code=_optional_str(payload, "class_code") or "TQBR",
                figi=_optional_str(payload, "figi"),
                instrument_uid=instrument_uid,
                name=_optional_str(payload, "name") or ticker,
                lot_size=_required_int_payload(payload, "lot_size"),
                min_price_increment=_required_decimal_payload(
                    payload,
                    "min_price_increment",
                ),
                currency=_optional_str(payload, "currency") or "RUB",
                is_enabled=True,
                supports_morning=True,
                supports_evening=True,
                supports_weekend=_bool_payload(payload, "supports_weekend"),
                source="tbank_resolved",
                resolved_at=datetime.now(tz=UTC),
                resolution_status="resolved",
                resolution_error_code=None,
                resolution_error_message=None,
                broker_payload=dict(payload),
                instrument_payload=dict(payload),
            )
            self.session.add(registry)
        self.session.flush()
        return InstrumentRef(
            instrument_id=registry.instrument_id,
            instrument_uid=registry.instrument_uid,
            figi=registry.figi,
            ticker=registry.ticker,
            class_code=registry.class_code,
            lot_size=registry.lot_size,
            min_price_increment=registry.min_price_increment,
        )

    def _mark_failed_tickers(
        self,
        tickers: list[str],
        *,
        class_code: str,
        error_code: str,
        error_message: str,
    ) -> None:
        for ticker in tickers:
            existing = self.session.execute(
                select(InstrumentRegistry).where(InstrumentRegistry.ticker == ticker)
            ).scalars().first()
            if existing is None:
                existing = InstrumentRegistry(
                    instrument_id=f"{self.exchange}:{ticker}",
                    ticker=ticker,
                    class_code=class_code,
                    figi=None,
                    instrument_uid=None,
                    name=ticker,
                    lot_size=1,
                    min_price_increment=Decimal("0.01"),
                    currency="RUB",
                    is_enabled=True,
                    supports_morning=True,
                    supports_evening=True,
                    supports_weekend=False,
                    source="seed",
                    resolved_at=None,
                    resolution_status="failed",
                    resolution_error_code=error_code,
                    resolution_error_message=error_message,
                    broker_payload=None,
                    instrument_payload={},
                )
                self.session.add(existing)
            else:
                existing.resolution_status = "failed"
                existing.resolution_error_code = error_code
                existing.resolution_error_message = error_message
        self.session.flush()


def is_broker_resolved_instrument(value: InstrumentRef | InstrumentRegistry) -> bool:
    """Return whether an instrument is safe to pass to real T-Bank broker calls."""

    instrument_id = str(getattr(value, "instrument_id", "") or "")
    instrument_uid = str(getattr(value, "instrument_uid", "") or "")
    figi = str(getattr(value, "figi", "") or "")
    source = str(getattr(value, "source", "") or "")
    resolution_status = str(getattr(value, "resolution_status", "") or "")
    has_broker_identity = bool(instrument_uid or figi)
    has_required_metadata = _has_required_market_metadata(value)
    if _looks_internal_moex_id(instrument_uid) or _looks_internal_moex_id(figi):
        return False
    if _has_placeholder_value(instrument_id) or _has_placeholder_value(instrument_uid):
        return False
    if isinstance(value, InstrumentRegistry):
        return has_broker_identity and has_required_metadata and (
            source == "tbank_resolved" or resolution_status == "resolved"
        )
    return has_broker_identity and has_required_metadata and not _has_placeholder_uid(value)


def assert_resolved_for_broker_call(
    instrument: InstrumentRef | InstrumentRegistry,
    *,
    mode: RuntimeMode,
    operation_name: str,
) -> None:
    """Fail fast when a production-like broker call would use a seed/internal ID."""

    if mode not in REAL_BROKER_MODES:
        return
    if is_broker_resolved_instrument(instrument):
        return
    instrument_id = str(getattr(instrument, "instrument_id", "") or "")
    ticker = str(getattr(instrument, "ticker", "") or "")
    msg = (
        f"{operation_name} requires resolved T-Bank instrument_uid or figi "
        f"for {ticker or instrument_id}"
    )
    raise RuntimeError(
        f"{UNRESOLVED_INSTRUMENT_ERROR_CODE}: {msg}"
    )


def broker_identity_for(instrument: InstrumentRef | InstrumentRegistry) -> str | None:
    """Return the broker-facing identity without falling back to internal MOEX IDs."""

    instrument_uid = str(getattr(instrument, "instrument_uid", "") or "")
    if instrument_uid and not _looks_internal_moex_id(instrument_uid):
        return instrument_uid
    figi = str(getattr(instrument, "figi", "") or "")
    if figi and not _looks_internal_moex_id(figi):
        return figi
    return None


def _instrument_payloads(payload: Mapping[str, Any]) -> tuple[Mapping[str, object], ...]:
    raw_items = payload.get("instruments", ())
    if not isinstance(raw_items, list | tuple):
        return ()
    return tuple(item for item in raw_items if isinstance(item, Mapping))


def _ticker_for(instrument: InstrumentRef) -> str:
    if instrument.ticker:
        return instrument.ticker.upper()
    return instrument.instrument_id.rsplit(":", 1)[-1].upper()


def _has_placeholder_uid(instrument: InstrumentRef) -> bool:
    values = (
        instrument.instrument_id,
        instrument.instrument_uid or "",
        instrument.figi or "",
    )
    return any(_has_placeholder_value(value) for value in values)


def _has_placeholder_value(value: str) -> bool:
    lower = value.lower()
    return any(marker in lower for marker in PLACEHOLDER_MARKERS)


def _looks_internal_moex_id(value: str) -> bool:
    return value.upper().startswith("MOEX:")


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        msg = f"resolved instrument payload is missing {key}"
        raise RuntimeError(msg)
    return value


def _optional_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _has_required_market_metadata(value: InstrumentRef | InstrumentRegistry) -> bool:
    lot_size = getattr(value, "lot_size", None)
    tick = getattr(value, "min_price_increment", None)
    try:
        lot_size_value = int(lot_size) if lot_size is not None else 0
        tick_value = Decimal(str(tick)) if tick is not None else Decimal("0")
    except (ValueError, ArithmeticError):
        return False
    return lot_size_value > 0 and tick_value > Decimal("0")


def _required_int_payload(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if value is None:
        msg = f"resolved instrument payload is missing {key}"
        raise RuntimeError(msg)
    parsed = int(str(value))
    if parsed <= 0:
        msg = f"resolved instrument payload has invalid {key}"
        raise RuntimeError(msg)
    return parsed


def _required_decimal_payload(payload: Mapping[str, object], key: str) -> Decimal:
    value = payload.get(key)
    if value is None:
        msg = f"resolved instrument payload is missing {key}"
        raise RuntimeError(msg)
    parsed = Decimal(str(value))
    if parsed <= Decimal("0"):
        msg = f"resolved instrument payload has invalid {key}"
        raise RuntimeError(msg)
    return parsed


def _bool_payload(payload: Mapping[str, object], key: str) -> bool:
    return bool(payload.get(key))
