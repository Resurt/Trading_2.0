"""DB-backed portfolio and position service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import (
    BrokerGateway,
    InstrumentRef,
    PortfolioRequest,
    PositionsRequest,
    RequestMetadata,
)
from trade_core.session import SessionEventContext
from trade_core.strategy import PortfolioSnapshot
from trading_common import TradingMetrics
from trading_common.db.models import PositionSnapshot
from trading_common.telemetry import get_logger, log_event

SessionContextProvider = Callable[[str], SessionEventContext]
JsonPayload = dict[str, Any]
LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PositionRecord:
    """SDK-neutral broker position prepared for DB snapshots and risk gates."""

    instrument_id: str
    position_side: str
    qty_lots: int
    avg_price: Decimal | None = None
    market_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realised_pnl: Decimal | None = None
    exposure: Decimal | None = None
    short_available: bool = True
    payload: JsonPayload | None = None

    @property
    def signed_lots(self) -> int:
        if self.position_side == "short":
            return -abs(self.qty_lots)
        if self.position_side == "long":
            return abs(self.qty_lots)
        return 0


@dataclass(frozen=True, slots=True)
class PositionRefreshResult:
    """Result of one broker position refresh."""

    account_id: str
    snapshot_ts: datetime
    positions: tuple[PositionRecord, ...]
    snapshots: tuple[PositionSnapshot, ...]
    portfolio: PortfolioSnapshot
    short_allowed_by_account: bool
    short_allowed_by_instrument: Mapping[str, bool]

    def signed_lots_for(self, instrument_id: str) -> int:
        return sum(
            item.signed_lots
            for item in self.positions
            if item.instrument_id == instrument_id
        )

    def short_allowed_for(self, instrument_id: str) -> bool:
        return self.short_allowed_by_instrument.get(instrument_id, self.short_allowed_by_account)


@dataclass(frozen=True, slots=True)
class PositionValidationResult:
    """Pre-entry validation result consumed by risk engine."""

    allowed: bool
    reason_code: str | None
    portfolio: PortfolioSnapshot
    refresh: PositionRefreshResult
    local_position_lots: int | None
    broker_position_lots: int | None
    snapshot_age_ms: int | None


class PositionService:
    """Refresh broker positions and keep `position_snapshot` as the local read model."""

    def __init__(
        self,
        *,
        broker_gateway: BrokerGateway,
        session: Session,
        session_context_provider: SessionContextProvider,
        tracked_instruments: tuple[InstrumentRef, ...] = (),
        metrics: TradingMetrics | None = None,
        freshness_seconds: int = 30,
    ) -> None:
        self._broker_gateway = broker_gateway
        self._session = session
        self._session_context_provider = session_context_provider
        self._tracked_instruments = tracked_instruments
        self._metrics = metrics
        self._freshness_seconds = freshness_seconds

    async def refresh_positions(
        self,
        account_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> PositionRefreshResult:
        """Fetch broker state and write normalized `position_snapshot` rows."""

        observed_at = _ensure_utc(now or datetime.now(tz=UTC))
        positions_response = await self._broker_gateway.get_positions(
            PositionsRequest(account_id=account_id),
            metadata=RequestMetadata(account_id=account_id),
        )
        portfolio_response = await self._broker_gateway.get_portfolio(
            PortfolioRequest(account_id=account_id),
            metadata=RequestMetadata(account_id=account_id),
        )
        positions = _merge_positions(
            positions_response.data,
            portfolio_response.data,
            tracked_instruments=self._tracked_instruments,
        )
        snapshots = tuple(
            self._write_position_snapshot(
                account_id=account_id,
                position=position,
                snapshot_ts=observed_at,
                reason=reason,
            )
            for position in positions
        )
        portfolio = _portfolio_from_positions(positions)
        self._session.flush()
        self._update_metrics(portfolio)
        log_event(
            logger=LOGGER,
            event_type="position_snapshot_written",
            component="position.service",
            account_id_present=True,
            position_count=len(positions),
            snapshot_reason=reason,
        )
        return PositionRefreshResult(
            account_id=account_id,
            snapshot_ts=observed_at,
            positions=positions,
            snapshots=snapshots,
            portfolio=portfolio,
            short_allowed_by_account=_short_allowed_by_account(portfolio_response.data),
            short_allowed_by_instrument={
                position.instrument_id: position.short_available for position in positions
            },
        )

    async def validate_before_entry(
        self,
        *,
        account_id: str,
        instrument_id: str,
        now: datetime | None = None,
        max_age_seconds: int | None = None,
    ) -> PositionValidationResult:
        """Compare fresh local snapshot with broker state before an entry decision."""

        observed_at = _ensure_utc(now or datetime.now(tz=UTC))
        latest = self.latest_snapshot(account_id=account_id, instrument_id=instrument_id)
        snapshot_age_ms = _snapshot_age_ms(latest, observed_at)
        allowed_age_ms = (max_age_seconds or self._freshness_seconds) * 1000
        if latest is None or snapshot_age_ms is None or snapshot_age_ms > allowed_age_ms:
            refresh = await self.refresh_positions(
                account_id,
                reason="pre_entry_stale_position_refresh",
                now=observed_at,
            )
            return PositionValidationResult(
                allowed=False,
                reason_code="position_state_stale",
                portfolio=_with_position_validation(
                    refresh.portfolio,
                    fresh=False,
                    matched=False,
                    reason_code="position_state_stale",
                    snapshot_age_ms=snapshot_age_ms,
                    local_position_lots=_signed_lots_from_snapshot(latest),
                    broker_position_lots=refresh.signed_lots_for(instrument_id),
                ),
                refresh=refresh,
                local_position_lots=_signed_lots_from_snapshot(latest),
                broker_position_lots=refresh.signed_lots_for(instrument_id),
                snapshot_age_ms=snapshot_age_ms,
            )

        local_lots = _signed_lots_from_snapshot(latest)
        refresh = await self.refresh_positions(
            account_id,
            reason="pre_entry_position_reconciliation",
            now=observed_at,
        )
        broker_lots = refresh.signed_lots_for(instrument_id)
        matched = local_lots == broker_lots
        reason_code = None if matched else "position_reconciliation_mismatch"
        if not matched and self._metrics is not None:
            self._metrics.inc_reconciliation_mismatch(result="positions")
        return PositionValidationResult(
            allowed=matched,
            reason_code=reason_code,
            portfolio=_with_position_validation(
                refresh.portfolio,
                fresh=True,
                matched=matched,
                reason_code=reason_code,
                snapshot_age_ms=snapshot_age_ms,
                local_position_lots=local_lots,
                broker_position_lots=broker_lots,
            ),
            refresh=refresh,
            local_position_lots=local_lots,
            broker_position_lots=broker_lots,
            snapshot_age_ms=snapshot_age_ms,
        )

    def latest_snapshot(
        self,
        *,
        account_id: str,
        instrument_id: str,
    ) -> PositionSnapshot | None:
        stmt = (
            select(PositionSnapshot)
            .where(
                PositionSnapshot.account_id == account_id,
                PositionSnapshot.instrument_id == instrument_id,
            )
            .order_by(PositionSnapshot.snapshot_ts.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def portfolio_from_latest(self, *, account_id: str) -> PortfolioSnapshot:
        positions: list[PositionRecord] = []
        for instrument in self._tracked_instruments:
            snapshot = self.latest_snapshot(
                account_id=account_id,
                instrument_id=instrument.instrument_id,
            )
            if snapshot is None:
                continue
            positions.append(_record_from_snapshot(snapshot))
        return _portfolio_from_positions(tuple(positions))

    def _write_position_snapshot(
        self,
        *,
        account_id: str,
        position: PositionRecord,
        snapshot_ts: datetime,
        reason: str,
    ) -> PositionSnapshot:
        context = self._session_context_provider(position.instrument_id)
        existing = self._session.execute(
            select(PositionSnapshot).where(
                PositionSnapshot.micro_session_id == context.micro_session_id,
                PositionSnapshot.instrument_id == position.instrument_id,
                PositionSnapshot.account_id == account_id,
                PositionSnapshot.snapshot_ts == snapshot_ts,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.position_side = position.position_side
            existing.qty_lots = abs(position.qty_lots)
            existing.avg_price = position.avg_price
            existing.market_price = position.market_price
            existing.unrealized_pnl = position.unrealized_pnl
            existing.realised_pnl = position.realised_pnl
            existing.exposure = position.exposure
            existing.snapshot_reason = reason
            existing.snapshot_payload = position.payload or {}
            return existing

        snapshot = PositionSnapshot(
            **context.as_db_values(),
            snapshot_ts=snapshot_ts,
            instrument_id=position.instrument_id,
            account_id=account_id,
            position_side=position.position_side,
            qty_lots=abs(position.qty_lots),
            avg_price=position.avg_price,
            market_price=position.market_price,
            unrealized_pnl=position.unrealized_pnl,
            realised_pnl=position.realised_pnl,
            exposure=position.exposure,
            snapshot_reason=reason,
            snapshot_payload=position.payload or {},
        )
        self._session.add(snapshot)
        return snapshot

    def _update_metrics(self, portfolio: PortfolioSnapshot) -> None:
        if self._metrics is None:
            return
        self._metrics.set_active_positions(portfolio.long_position_lots, instrument="long")
        self._metrics.set_active_positions(portfolio.short_position_lots, instrument="short")
        self._metrics.set_active_positions(
            portfolio.long_position_lots + portfolio.short_position_lots,
            instrument="all",
        )


def _merge_positions(
    positions_payload: Mapping[str, Any],
    portfolio_payload: Mapping[str, Any],
    *,
    tracked_instruments: tuple[InstrumentRef, ...],
) -> tuple[PositionRecord, ...]:
    by_instrument_side: dict[tuple[str, str], PositionRecord] = {}
    aliases = _instrument_aliases(tracked_instruments)
    for raw in _iter_position_payloads(positions_payload):
        record = _record_from_payload(raw, aliases=aliases)
        if record is not None:
            by_instrument_side[(record.instrument_id, record.position_side)] = record
    for raw in _iter_position_payloads(portfolio_payload):
        record = _record_from_payload(raw, aliases=aliases)
        if record is not None:
            by_instrument_side[(record.instrument_id, record.position_side)] = record
    for instrument in tracked_instruments:
        has_position = any(key[0] == instrument.instrument_id for key in by_instrument_side)
        if not has_position:
            by_instrument_side[(instrument.instrument_id, "flat")] = PositionRecord(
                instrument_id=instrument.instrument_id,
                position_side="flat",
                qty_lots=0,
                exposure=Decimal("0"),
                payload={"source": "tracked_instrument_flat_fill"},
            )
    return tuple(by_instrument_side[key] for key in sorted(by_instrument_side))


def _instrument_aliases(tracked_instruments: tuple[InstrumentRef, ...]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for instrument in tracked_instruments:
        for value in (
            instrument.instrument_id,
            instrument.instrument_uid,
            instrument.ticker,
            (
                f"{instrument.class_code}:{instrument.ticker}"
                if instrument.class_code and instrument.ticker
                else None
            ),
        ):
            if value:
                aliases[value] = instrument.instrument_id
    return aliases


def _iter_position_payloads(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    positions = payload.get("positions", ())
    if not isinstance(positions, list | tuple):
        return ()
    return tuple(item for item in positions if isinstance(item, Mapping))


def _record_from_payload(
    payload: Mapping[str, Any],
    *,
    aliases: Mapping[str, str],
) -> PositionRecord | None:
    raw_instrument_id = _payload_str(payload, "instrument_id") or _payload_str(
        payload,
        "instrument_uid",
    )
    if not raw_instrument_id:
        return None
    instrument_id = aliases.get(raw_instrument_id, raw_instrument_id)
    signed_qty = _decimal_from_payload(payload.get("qty_lots"))
    side = _payload_str(payload, "position_side") or _side_from_quantity(signed_qty)
    qty_lots = int(abs(signed_qty))
    exposure = _decimal_or_none(payload.get("exposure"))
    if exposure is None:
        market_price = _decimal_or_none(payload.get("market_price"))
        exposure = abs(signed_qty) * market_price if market_price is not None else None
    return PositionRecord(
        instrument_id=instrument_id,
        position_side=side,
        qty_lots=qty_lots,
        avg_price=_decimal_or_none(payload.get("avg_price")),
        market_price=_decimal_or_none(payload.get("market_price")),
        unrealized_pnl=_decimal_or_none(payload.get("unrealized_pnl")),
        realised_pnl=_decimal_or_none(payload.get("realised_pnl")),
        exposure=exposure,
        short_available=bool(payload.get("short_available", True)),
        payload=dict(payload),
    )


def _record_from_snapshot(snapshot: PositionSnapshot) -> PositionRecord:
    return PositionRecord(
        instrument_id=snapshot.instrument_id,
        position_side=snapshot.position_side,
        qty_lots=snapshot.qty_lots,
        avg_price=snapshot.avg_price,
        market_price=snapshot.market_price,
        unrealized_pnl=snapshot.unrealized_pnl,
        realised_pnl=snapshot.realised_pnl,
        exposure=snapshot.exposure,
        payload=dict(snapshot.snapshot_payload),
    )


def _portfolio_from_positions(positions: tuple[PositionRecord, ...]) -> PortfolioSnapshot:
    long_lots = sum(position.qty_lots for position in positions if position.position_side == "long")
    short_lots = sum(
        position.qty_lots
        for position in positions
        if position.position_side == "short"
    )
    gross_exposure = sum(
        (
            abs(position.exposure) if position.exposure is not None else Decimal("0")
            for position in positions
        ),
        Decimal("0"),
    )
    net_exposure = sum(
        (_signed_exposure(position) for position in positions),
        Decimal("0"),
    )
    return PortfolioSnapshot(
        open_position_lots=long_lots - short_lots,
        long_position_lots=long_lots,
        short_position_lots=short_lots,
        gross_exposure_rub=gross_exposure,
        net_exposure_rub=net_exposure,
    )


def _with_position_validation(
    portfolio: PortfolioSnapshot,
    *,
    fresh: bool,
    matched: bool,
    reason_code: str | None,
    snapshot_age_ms: int | None,
    local_position_lots: int | None,
    broker_position_lots: int | None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        open_position_lots=portfolio.open_position_lots,
        open_order_count=portfolio.open_order_count,
        long_position_lots=portfolio.long_position_lots,
        short_position_lots=portfolio.short_position_lots,
        gross_exposure_rub=portfolio.gross_exposure_rub,
        net_exposure_rub=portfolio.net_exposure_rub,
        position_state_fresh=fresh,
        position_reconciliation_matched=matched,
        position_state_age_ms=snapshot_age_ms,
        local_position_lots=local_position_lots,
        broker_position_lots=broker_position_lots,
        position_reason_code=reason_code,
    )


def _short_allowed_by_account(payload: Mapping[str, Any]) -> bool:
    value = payload.get("short_allowed_by_account")
    if value is None:
        value = payload.get("margin_trading_enabled")
    return bool(True if value is None else value)


def _signed_lots_from_snapshot(snapshot: PositionSnapshot | None) -> int | None:
    if snapshot is None:
        return None
    if snapshot.position_side == "short":
        return -abs(snapshot.qty_lots)
    if snapshot.position_side == "long":
        return abs(snapshot.qty_lots)
    return 0


def _snapshot_age_ms(snapshot: PositionSnapshot | None, now: datetime) -> int | None:
    if snapshot is None:
        return None
    return int((_ensure_utc(now) - _ensure_utc(snapshot.snapshot_ts)).total_seconds() * 1000)


def _signed_exposure(position: PositionRecord) -> Decimal:
    exposure = position.exposure or Decimal("0")
    if position.position_side == "short":
        return -abs(exposure)
    if position.position_side == "long":
        return abs(exposure)
    return Decimal("0")


def _payload_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return str(value) if value is not None and str(value) else None


def _decimal_from_payload(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value)
    return None if text.lower() == "none" else Decimal(text)


def _side_from_quantity(quantity: Decimal) -> str:
    if quantity < 0:
        return "short"
    if quantity > 0:
        return "long"
    return "flat"


def _ensure_utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC) if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
