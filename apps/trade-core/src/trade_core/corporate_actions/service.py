"""Corporate action import and special-day classification."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from trading_common import ServiceName
from trading_common.db.models import (
    AuditEvent,
    InstrumentRegistry,
    MarketCandle,
    MarketSpecialDay,
)
from trading_common.db.models import (
    CorporateActionEvent as CorporateActionEventRow,
)

JsonPayload = dict[str, Any]
DEFAULT_SPECIAL_DAY_POLICY = "shadow_only"


@dataclass(frozen=True, slots=True)
class CorporateActionImportConfig:
    """Import defaults for manual CSV/JSON corporate action files."""

    source: str = "manual"
    confidence: str = "manual_unverified"


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    """SDK-neutral corporate action fact before ORM persistence."""

    instrument_id: str
    action_type: str
    ticker: str | None = None
    declared_date: date | None = None
    ex_date: date | None = None
    registry_close_date: date | None = None
    payment_date: date | None = None
    amount_per_share: Decimal | None = None
    currency: str | None = None
    source: str = "manual"
    confidence: str = "manual_unverified"
    action_payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SpecialDayFlags:
    """Condensed flags consumed by replay, risk, and calibration."""

    special_day_type: str | None = None
    corporate_action_flag: bool = False
    dividend_gap_day: bool = False
    abnormal_gap_day: bool = False
    future_dividend_risk_window: bool = False
    excluded_from_primary_calibration: bool = False
    trade_policy: str = "allow"
    source: str | None = None
    linked_corporate_action_id: str | None = None
    days_to_ex_date: int | None = None
    days_to_record_date: int | None = None
    corporate_action_source: str | None = None

    def as_payload(self) -> JsonPayload:
        return {
            "special_day_type": self.special_day_type,
            "corporate_action_flag": self.corporate_action_flag,
            "dividend_gap_day": self.dividend_gap_day,
            "abnormal_gap_day": self.abnormal_gap_day,
            "future_dividend_risk_window": self.future_dividend_risk_window,
            "excluded_from_primary_calibration": self.excluded_from_primary_calibration,
            "special_day_trade_policy": self.trade_policy,
            "special_day_source": self.source,
            "linked_corporate_action_id": self.linked_corporate_action_id,
            "dividend_event_id": self.linked_corporate_action_id,
            "days_to_ex_date": self.days_to_ex_date,
            "days_to_record_date": self.days_to_record_date,
            "corporate_action_source": self.corporate_action_source or self.source,
        }


@dataclass(frozen=True, slots=True)
class MarketSpecialDayResult:
    """Summary for one special-day classification run."""

    special_days_created: int
    dividend_gap_days: int
    abnormal_gap_days: int
    future_risk_windows_created: int
    excluded_from_primary_calibration: int
    instruments: tuple[str, ...]
    from_date: date
    to_date: date
    classification_status: str = "completed"

    def as_payload(self) -> JsonPayload:
        return {
            "special_days_created": self.special_days_created,
            "dividend_gap_days": self.dividend_gap_days,
            "abnormal_gap_days": self.abnormal_gap_days,
            "future_risk_windows_created": self.future_risk_windows_created,
            "excluded_from_primary_calibration": self.excluded_from_primary_calibration,
            "instruments": list(self.instruments),
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "classification_status": self.classification_status,
            "source": "market_special_day_classification",
        }


class CorporateActionService:
    """Import and query corporate action facts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def import_csv(
        self,
        path: Path,
        *,
        config: CorporateActionImportConfig,
    ) -> tuple[CorporateActionEventRow, ...]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [
                self.upsert_event(_event_from_mapping(row, config=config))
                for row in reader
            ]
        self._session.flush()
        return tuple(rows)

    def import_json(
        self,
        path: Path,
        *,
        config: CorporateActionImportConfig,
    ) -> tuple[CorporateActionEventRow, ...]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else payload.get("items", [])
        if not isinstance(items, list):
            msg = "corporate action JSON must be a list or an object with items"
            raise ValueError(msg)
        rows = [
            self.upsert_event(_event_from_mapping(item, config=config))
            for item in items
            if isinstance(item, dict)
        ]
        self._session.flush()
        return tuple(rows)

    def upsert_event(self, event: CorporateActionEvent) -> CorporateActionEventRow:
        existing = self._session.execute(
            select(CorporateActionEventRow).where(
                CorporateActionEventRow.instrument_id == event.instrument_id,
                CorporateActionEventRow.action_type == event.action_type,
                CorporateActionEventRow.ex_date == event.ex_date,
                CorporateActionEventRow.amount_per_share == event.amount_per_share,
                CorporateActionEventRow.source == event.source,
            )
        ).scalars().first()
        if existing is not None:
            existing.ticker = event.ticker
            existing.declared_date = event.declared_date
            existing.registry_close_date = event.registry_close_date
            existing.payment_date = event.payment_date
            existing.currency = event.currency
            existing.confidence = event.confidence
            existing.action_payload = event.action_payload
            existing.updated_at = datetime.now(tz=UTC)
            return existing
        row = CorporateActionEventRow(
            instrument_id=event.instrument_id,
            ticker=event.ticker,
            action_type=event.action_type,
            declared_date=event.declared_date,
            ex_date=event.ex_date,
            registry_close_date=event.registry_close_date,
            payment_date=event.payment_date,
            amount_per_share=event.amount_per_share,
            currency=event.currency,
            source=event.source,
            confidence=event.confidence,
            action_payload=event.action_payload,
        )
        self._session.add(row)
        return row

    def list_events(
        self,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        instruments: tuple[str, ...] = (),
        source: str | None = None,
        action_type: str | None = None,
    ) -> list[CorporateActionEventRow]:
        instrument_ids = self.resolve_instrument_ids(instruments)
        stmt = select(CorporateActionEventRow).order_by(
            CorporateActionEventRow.ex_date,
            CorporateActionEventRow.instrument_id,
        )
        if from_date is not None:
            stmt = stmt.where(CorporateActionEventRow.ex_date >= from_date)
        if to_date is not None:
            stmt = stmt.where(CorporateActionEventRow.ex_date <= to_date)
        if instrument_ids:
            stmt = stmt.where(CorporateActionEventRow.instrument_id.in_(instrument_ids))
        if source is not None:
            stmt = stmt.where(CorporateActionEventRow.source == source)
        if action_type is not None:
            stmt = stmt.where(CorporateActionEventRow.action_type == action_type)
        return list(self._session.execute(stmt).scalars())

    def list_special_days(
        self,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        instruments: tuple[str, ...] = (),
    ) -> list[MarketSpecialDay]:
        instrument_ids = self.resolve_instrument_ids(instruments)
        stmt = select(MarketSpecialDay).order_by(
            MarketSpecialDay.trading_date,
            MarketSpecialDay.instrument_id,
            MarketSpecialDay.special_day_type,
        )
        if from_date is not None:
            stmt = stmt.where(MarketSpecialDay.trading_date >= from_date)
        if to_date is not None:
            stmt = stmt.where(MarketSpecialDay.trading_date <= to_date)
        if instrument_ids:
            stmt = stmt.where(MarketSpecialDay.instrument_id.in_(instrument_ids))
        return list(self._session.execute(stmt).scalars())

    def read_special_day_flags(
        self,
        *,
        trading_date: date,
        instrument_id: str,
    ) -> SpecialDayFlags:
        rows = list(
            self._session.execute(
                select(MarketSpecialDay).where(
                    MarketSpecialDay.trading_date == trading_date,
                    MarketSpecialDay.instrument_id == instrument_id,
                )
            ).scalars()
        )
        return flags_from_rows(rows)

    def resolve_instrument_ids(self, instruments: tuple[str, ...]) -> tuple[str, ...]:
        if not instruments:
            return tuple(
                str(value)
                for value in self._session.execute(
                    select(InstrumentRegistry.instrument_id).order_by(
                        InstrumentRegistry.instrument_id
                    )
                ).scalars()
            )
        resolved: list[str] = []
        for raw in instruments:
            value = raw.strip()
            if not value:
                continue
            registry = self._session.execute(
                select(InstrumentRegistry).where(InstrumentRegistry.ticker == value.upper())
            ).scalars().first()
            if registry is not None:
                resolved.append(registry.instrument_id)
            elif ":" in value:
                resolved.append(value)
            else:
                resolved.append(f"MOEX:{value.upper()}")
        return tuple(dict.fromkeys(resolved))

    def api_imported_dividend_events_exist(
        self,
        *,
        from_date: date,
        to_date: date,
        instruments: tuple[str, ...] = (),
    ) -> bool:
        return bool(
            self.list_events(
                from_date=from_date,
                to_date=to_date,
                instruments=instruments,
                source="api_import",
                action_type="dividend",
            )
        )


class MarketSpecialDayClassifier:
    """Classify dividend/corporate-action and abnormal-gap trading days."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._corporate_actions = CorporateActionService(session)

    def classify(
        self,
        *,
        from_date: date,
        to_date: date,
        instruments: tuple[str, ...],
        gap_threshold_bps: Decimal = Decimal("150"),
        dividend_gap_threshold_bps: Decimal = Decimal("50"),
        force_rebuild: bool = False,
        include_future: bool = False,
        lookahead_days: int = 365,
    ) -> MarketSpecialDayResult:
        instrument_ids = self._corporate_actions.resolve_instrument_ids(instruments)
        effective_to_date = to_date + timedelta(days=lookahead_days) if include_future else to_date
        if force_rebuild:
            stmt = delete(MarketSpecialDay).where(
                MarketSpecialDay.trading_date >= from_date,
                MarketSpecialDay.trading_date <= effective_to_date,
            )
            if instrument_ids:
                stmt = stmt.where(MarketSpecialDay.instrument_id.in_(instrument_ids))
            self._session.execute(stmt)

        created = 0
        dividend_days = 0
        abnormal_days = 0
        future_windows = 0
        excluded = 0

        actions = self._corporate_actions.list_events(
            from_date=from_date,
            to_date=effective_to_date,
            instruments=instrument_ids,
        )
        for action in actions:
            if action.ex_date is None:
                continue
            gap = _open_gap_for(self._session, action.instrument_id, action.ex_date)
            expected_dividend_bps = _expected_dividend_bps(
                amount=action.amount_per_share,
                previous_close=gap.previous_close,
            )
            is_future = (
                include_future
                and gap.session_open_price is None
                and action.ex_date > to_date
            )
            day_type = _special_day_type_for_action(action, is_future=is_future)
            reason_code = _special_day_reason_code_for_action(action, is_future=is_future)
            row, was_created = _upsert_special_day(
                self._session,
                trading_date=action.ex_date,
                calendar_date=action.ex_date,
                instrument_id=action.instrument_id,
                ticker=action.ticker,
                special_day_type=day_type,
                session_type=gap.session_type,
                reason_code=reason_code,
                source=action.source,
                linked_corporate_action_id=action.corporate_action_id,
                open_gap_bps=gap.open_gap_bps,
                previous_close=gap.previous_close,
                session_open_price=gap.session_open_price,
                expected_dividend_bps=expected_dividend_bps,
                detected_gap_bps=gap.open_gap_bps,
                severity="warning",
                exclude_from_primary_calibration=True,
                trade_policy=DEFAULT_SPECIAL_DAY_POLICY,
                payload={
                    "corporate_action_id": str(action.corporate_action_id),
                    "action_type": action.action_type,
                    "confidence": action.confidence,
                    "source": action.source,
                    "dividend_gap_threshold_bps": str(dividend_gap_threshold_bps),
                    "raw_corporate_action_payload": dict(action.action_payload),
                },
            )
            created += int(was_created)
            dividend_days += int(row.special_day_type == "dividend_gap_day")
            future_windows += int(row.special_day_type == "future_dividend_risk_window")
            excluded += int(row.exclude_from_primary_calibration)

        for instrument_id in instrument_ids:
            for trading_day in _dates(from_date, to_date):
                gap = _open_gap_for(self._session, instrument_id, trading_day)
                if gap.open_gap_bps is None or abs(gap.open_gap_bps) < gap_threshold_bps:
                    continue
                row, was_created = _upsert_special_day(
                    self._session,
                    trading_date=trading_day,
                    calendar_date=trading_day,
                    instrument_id=instrument_id,
                    ticker=_ticker_for(self._session, instrument_id),
                    special_day_type="abnormal_gap_day",
                    session_type=gap.session_type,
                    reason_code="abnormal_open_gap",
                    source="historical_gap_detector",
                    linked_corporate_action_id=None,
                    open_gap_bps=gap.open_gap_bps,
                    previous_close=gap.previous_close,
                    session_open_price=gap.session_open_price,
                    expected_dividend_bps=None,
                    detected_gap_bps=gap.open_gap_bps,
                    severity="warning",
                    exclude_from_primary_calibration=True,
                    trade_policy=DEFAULT_SPECIAL_DAY_POLICY,
                    payload={"gap_threshold_bps": str(gap_threshold_bps)},
                )
                created += int(was_created)
                abnormal_days += 1
                excluded += int(row.exclude_from_primary_calibration)

        result = MarketSpecialDayResult(
            special_days_created=created,
            dividend_gap_days=dividend_days,
            abnormal_gap_days=abnormal_days,
            future_risk_windows_created=future_windows,
            excluded_from_primary_calibration=excluded,
            instruments=instrument_ids,
            from_date=from_date,
            to_date=effective_to_date,
        )
        self._session.add(_classification_audit_event(result))
        self._session.flush()
        return result


@dataclass(frozen=True, slots=True)
class _OpenGap:
    previous_close: Decimal | None
    session_open_price: Decimal | None
    open_gap_bps: Decimal | None
    session_type: str | None


def flags_from_rows(rows: list[MarketSpecialDay]) -> SpecialDayFlags:
    if not rows:
        return SpecialDayFlags()
    types = {row.special_day_type for row in rows}
    policy = (
        "shadow_only"
        if any(row.trade_policy == "shadow_only" for row in rows)
        else rows[0].trade_policy
    )
    linked_ids = {
        str(row.linked_corporate_action_id)
        for row in rows
        if row.linked_corporate_action_id is not None
    }
    sources = {row.source for row in rows if row.source}
    today = datetime.now(tz=UTC).date()
    nearest_ex_date = min(
        (row.trading_date for row in rows if row.trading_date >= today),
        default=None,
    )
    nearest_record_date = min(
        (
            date.fromisoformat(str(value))
            for row in rows
            for value in (_payload_value(row.special_day_payload, "record_date"),)
            if isinstance(value, str)
        ),
        default=None,
    )
    return SpecialDayFlags(
        special_day_type=",".join(sorted(types)),
        corporate_action_flag=bool(
            types
            & {"corporate_action_day", "dividend_gap_day", "future_dividend_risk_window"}
        ),
        dividend_gap_day="dividend_gap_day" in types,
        abnormal_gap_day="abnormal_gap_day" in types,
        future_dividend_risk_window="future_dividend_risk_window" in types,
        excluded_from_primary_calibration=any(
            row.exclude_from_primary_calibration for row in rows
        ),
        trade_policy=policy,
        source=",".join(sorted(sources)),
        linked_corporate_action_id=sorted(linked_ids)[0] if linked_ids else None,
        days_to_ex_date=(
            (nearest_ex_date - today).days if nearest_ex_date is not None else None
        ),
        days_to_record_date=(
            (nearest_record_date - today).days if nearest_record_date is not None else None
        ),
        corporate_action_source=",".join(sorted(sources)),
    )


def special_day_classification_exists(
    session: Session,
    *,
    from_date: date,
    to_date: date,
    instruments: tuple[str, ...] = (),
) -> bool:
    service = CorporateActionService(session)
    instrument_ids = service.resolve_instrument_ids(instruments)
    row_stmt = select(MarketSpecialDay.special_day_id).where(
        MarketSpecialDay.trading_date >= from_date,
        MarketSpecialDay.trading_date <= to_date,
    )
    if instrument_ids:
        row_stmt = row_stmt.where(MarketSpecialDay.instrument_id.in_(instrument_ids))
    if session.execute(row_stmt).first() is not None:
        return True
    audit_stmt = select(AuditEvent.audit_event_id).where(
        AuditEvent.trading_date >= from_date,
        AuditEvent.trading_date <= to_date,
        AuditEvent.action == "market_special_day_classification_completed",
    )
    if instrument_ids:
        audit_stmt = audit_stmt.where(AuditEvent.audit_payload["instruments"].is_not(None))
    return session.execute(audit_stmt).first() is not None


def _event_from_mapping(
    mapping: dict[str, Any],
    *,
    config: CorporateActionImportConfig,
) -> CorporateActionEvent:
    ticker = _optional_str(mapping.get("ticker"))
    instrument_id = _optional_str(mapping.get("instrument_id"))
    if instrument_id is None and ticker is not None:
        instrument_id = f"MOEX:{ticker.upper()}"
    if instrument_id is None:
        msg = "corporate action row requires ticker or instrument_id"
        raise ValueError(msg)
    source = _optional_str(mapping.get("source")) or config.source
    confidence = _optional_str(mapping.get("confidence")) or config.confidence
    action_type = (_optional_str(mapping.get("action_type")) or "other").lower()
    amount = _decimal_or_none(mapping.get("amount_per_share"))
    payload = {
        key: value
        for key, value in mapping.items()
        if value not in (None, "")
    }
    return CorporateActionEvent(
        instrument_id=instrument_id,
        ticker=ticker.upper() if ticker else None,
        action_type=action_type,
        declared_date=_date_or_none(mapping.get("declared_date")),
        ex_date=_date_or_none(mapping.get("ex_date")),
        registry_close_date=_date_or_none(mapping.get("registry_close_date")),
        payment_date=_date_or_none(mapping.get("payment_date")),
        amount_per_share=amount,
        currency=_optional_str(mapping.get("currency")),
        source=source,
        confidence=confidence,
        action_payload={**payload, "source": source, "confidence": confidence},
    )


def _upsert_special_day(
    session: Session,
    *,
    trading_date: date,
    calendar_date: date,
    instrument_id: str,
    ticker: str | None,
    special_day_type: str,
    session_type: str | None,
    reason_code: str,
    source: str,
    linked_corporate_action_id: UUID | None,
    open_gap_bps: Decimal | None,
    previous_close: Decimal | None,
    session_open_price: Decimal | None,
    expected_dividend_bps: Decimal | None,
    detected_gap_bps: Decimal | None,
    severity: str,
    exclude_from_primary_calibration: bool,
    trade_policy: str,
    payload: JsonPayload,
) -> tuple[MarketSpecialDay, bool]:
    existing = session.execute(
        select(MarketSpecialDay).where(
            MarketSpecialDay.trading_date == trading_date,
            MarketSpecialDay.instrument_id == instrument_id,
            MarketSpecialDay.special_day_type == special_day_type,
            MarketSpecialDay.reason_code == reason_code,
        )
    ).scalars().first()
    if existing is not None:
        existing.calendar_date = calendar_date
        existing.ticker = ticker
        existing.session_type = session_type
        existing.source = source
        existing.linked_corporate_action_id = linked_corporate_action_id
        existing.open_gap_bps = open_gap_bps
        existing.previous_close = previous_close
        existing.session_open_price = session_open_price
        existing.expected_dividend_bps = expected_dividend_bps
        existing.detected_gap_bps = detected_gap_bps
        existing.severity = severity
        existing.exclude_from_primary_calibration = exclude_from_primary_calibration
        existing.trade_policy = trade_policy
        existing.special_day_payload = payload
        return existing, False
    row = MarketSpecialDay(
        trading_date=trading_date,
        calendar_date=calendar_date,
        instrument_id=instrument_id,
        ticker=ticker,
        special_day_type=special_day_type,
        session_type=session_type,
        reason_code=reason_code,
        source=source,
        linked_corporate_action_id=linked_corporate_action_id,
        open_gap_bps=open_gap_bps,
        previous_close=previous_close,
        session_open_price=session_open_price,
        expected_dividend_bps=expected_dividend_bps,
        detected_gap_bps=detected_gap_bps,
        severity=severity,
        exclude_from_primary_calibration=exclude_from_primary_calibration,
        trade_policy=trade_policy,
        special_day_payload=payload,
    )
    session.add(row)
    return row, True


def _special_day_type_for_action(
    action: CorporateActionEventRow,
    *,
    is_future: bool,
) -> str:
    if is_future and action.action_type == "dividend":
        return "future_dividend_risk_window"
    return "dividend_gap_day" if action.action_type == "dividend" else "corporate_action_day"


def _special_day_reason_code_for_action(
    action: CorporateActionEventRow,
    *,
    is_future: bool,
) -> str:
    if is_future and action.action_type == "dividend":
        return "future_dividend_ex_date"
    return "dividend_ex_date" if action.action_type == "dividend" else "corporate_action_window"


def _payload_value(payload: JsonPayload, key: str) -> object:
    value = payload.get(key)
    if value is not None:
        return value
    raw_payload = payload.get("raw_corporate_action_payload")
    if isinstance(raw_payload, dict):
        return raw_payload.get(key)
    return None


def _open_gap_for(session: Session, instrument_id: str, trading_day: date) -> _OpenGap:
    current = session.execute(
        select(MarketCandle)
        .where(
            MarketCandle.instrument_id == instrument_id,
            MarketCandle.trading_date == trading_day,
            MarketCandle.is_closed.is_(True),
        )
        .order_by(MarketCandle.open_ts_utc)
    ).scalars().first()
    previous = session.execute(
        select(MarketCandle)
        .where(
            MarketCandle.instrument_id == instrument_id,
            MarketCandle.trading_date < trading_day,
            MarketCandle.is_closed.is_(True),
        )
        .order_by(MarketCandle.trading_date.desc(), MarketCandle.close_ts_utc.desc())
    ).scalars().first()
    if current is None:
        return _OpenGap(None, None, None, None)
    if previous is None or previous.close_price == Decimal("0"):
        return _OpenGap(None, current.open_price, None, current.session_type)
    gap = ((current.open_price - previous.close_price) / previous.close_price * Decimal("10000"))
    return _OpenGap(
        previous_close=previous.close_price,
        session_open_price=current.open_price,
        open_gap_bps=gap.quantize(Decimal("0.0001")),
        session_type=current.session_type,
    )


def _expected_dividend_bps(
    *,
    amount: Decimal | None,
    previous_close: Decimal | None,
) -> Decimal | None:
    if amount is None or previous_close is None or previous_close == Decimal("0"):
        return None
    return (amount / previous_close * Decimal("10000")).quantize(Decimal("0.0001"))


def _ticker_for(session: Session, instrument_id: str) -> str | None:
    row = session.get(InstrumentRegistry, instrument_id)
    return row.ticker if row is not None else instrument_id.rsplit(":", 1)[-1]


def _classification_audit_event(result: MarketSpecialDayResult) -> AuditEvent:
    now = datetime.now(tz=UTC)
    return AuditEvent(
        calendar_date=now.date(),
        trading_date=result.to_date,
        session_type="weekday_main",
        session_phase="closed",
        micro_session_id=f"special-day-classification:{now.isoformat()}",
        broker_trading_status="not_applicable",
        ts_utc=now,
        exchange_ts=now,
        received_ts=now,
        service=ServiceName.TRADE_CORE.value,
        actor="system",
        action="market_special_day_classification_completed",
        entity_type="market_special_day",
        entity_id=f"{result.from_date.isoformat()}:{result.to_date.isoformat()}",
        severity="info",
        correlation_id=None,
        audit_payload=result.as_payload(),
    )


def _dates(from_date: date, to_date: date) -> tuple[date, ...]:
    days: list[date] = []
    cursor = from_date
    while cursor <= to_date:
        days.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return tuple(days)


def _date_or_none(value: object) -> date | None:
    raw = _optional_str(value)
    return None if raw is None else date.fromisoformat(raw)


def _decimal_or_none(value: object) -> Decimal | None:
    raw = _optional_str(value)
    return None if raw is None else Decimal(raw)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
