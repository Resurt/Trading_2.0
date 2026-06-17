"""Instrument resolution and registry synchronization for trade-core startup."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
PLACEHOLDER_MARKERS = ("placeholder", "runtime-placeholder", "test-placeholder")


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
        response = await self.broker_gateway.resolve_instruments(
            InstrumentResolveRequest(tickers=tickers, class_code=class_code)
        )
        resolved = tuple(
            self._upsert_resolved_payload(payload)
            for payload in _instrument_payloads(response.data)
        )
        missing_tickers = sorted(set(tickers) - {instrument.ticker for instrument in resolved})
        if missing_tickers:
            msg = f"T-Bank instrument resolver did not return tickers: {', '.join(missing_tickers)}"
            raise RuntimeError(msg)
        if any(_has_placeholder_uid(instrument) for instrument in resolved):
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
                instrument_payload={"source": "historical_replay_local_registry"},
            )
            repository.upsert(registry)
            registered.append(
                InstrumentRef(
                    instrument_id=registry.instrument_id,
                    instrument_uid=registry.instrument_uid,
                    ticker=registry.ticker,
                    class_code=registry.class_code,
                )
            )
        self.session.flush()
        return tuple(registered)

    def _upsert_resolved_payload(self, payload: Mapping[str, object]) -> InstrumentRef:
        ticker = _required_str(payload, "ticker")
        instrument_uid = _required_str(payload, "instrument_uid")
        canonical_instrument_id = instrument_uid
        existing_by_ticker = self.session.execute(
            select(InstrumentRegistry).where(InstrumentRegistry.ticker == ticker)
        ).scalars().first()
        if existing_by_ticker is not None:
            existing_by_ticker.instrument_id = canonical_instrument_id
            existing_by_ticker.class_code = _optional_str(payload, "class_code") or "TQBR"
            existing_by_ticker.figi = _optional_str(payload, "figi")
            existing_by_ticker.instrument_uid = instrument_uid
            existing_by_ticker.name = _optional_str(payload, "name") or ticker
            existing_by_ticker.lot_size = _int_payload(payload, "lot_size", default=1)
            existing_by_ticker.min_price_increment = _decimal_payload(
                payload,
                "min_price_increment",
                default=Decimal("0.01"),
            )
            existing_by_ticker.currency = _optional_str(payload, "currency") or "RUB"
            existing_by_ticker.is_enabled = True
            existing_by_ticker.supports_morning = True
            existing_by_ticker.supports_evening = True
            existing_by_ticker.supports_weekend = _bool_payload(payload, "supports_weekend")
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
                lot_size=_int_payload(payload, "lot_size", default=1),
                min_price_increment=_decimal_payload(
                    payload,
                    "min_price_increment",
                    default=Decimal("0.01"),
                ),
                currency=_optional_str(payload, "currency") or "RUB",
                is_enabled=True,
                supports_morning=True,
                supports_evening=True,
                supports_weekend=_bool_payload(payload, "supports_weekend"),
                instrument_payload=dict(payload),
            )
            self.session.add(registry)
        self.session.flush()
        return InstrumentRef(
            instrument_id=registry.instrument_id,
            instrument_uid=registry.instrument_uid,
            ticker=registry.ticker,
            class_code=registry.class_code,
        )


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
    value = (instrument.instrument_uid or instrument.instrument_id).lower()
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        msg = f"resolved instrument payload is missing {key}"
        raise RuntimeError(msg)
    return value


def _optional_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _int_payload(payload: Mapping[str, object], key: str, *, default: int) -> int:
    value = payload.get(key)
    if value is None:
        return default
    return int(str(value))


def _decimal_payload(
    payload: Mapping[str, object],
    key: str,
    *,
    default: Decimal,
) -> Decimal:
    value = payload.get(key)
    if value is None:
        return default
    return Decimal(str(value))


def _bool_payload(payload: Mapping[str, object], key: str) -> bool:
    return bool(payload.get(key))
