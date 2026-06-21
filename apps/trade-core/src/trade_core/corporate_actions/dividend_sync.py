"""Readonly T-Bank dividend synchronization for corporate-action awareness."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import (
    BrokerGateway,
    DividendsRequest,
    InstrumentRef,
)
from trade_core.corporate_actions.service import (
    CorporateActionEvent,
    CorporateActionService,
    MarketSpecialDayClassifier,
)
from trade_core.instruments import (
    InstrumentResolverService,
    assert_resolved_for_broker_call,
    is_broker_resolved_instrument,
)
from trading_common import LaunchModePolicy, RuntimeMode, ServiceName
from trading_common.db.models import (
    AuditEvent,
    DividendSyncRun,
    InstrumentRegistry,
)
from trading_common.db.models import (
    CorporateActionEvent as CorporateActionEventRow,
)

JsonPayload = dict[str, Any]
DEFAULT_INSTRUMENTS = ("SBER", "GAZP")


@dataclass(frozen=True, slots=True)
class DividendSyncConfig:
    instruments: tuple[str, ...] = DEFAULT_INSTRUMENTS
    from_date: date | None = None
    to_date: date | None = None
    lookback_days: int = 730
    lookahead_days: int = 365
    dry_run: bool = False
    force_rebuild: bool = False
    classify_special_days: bool = True
    gap_threshold_bps: Decimal = Decimal("150")
    dividend_gap_threshold_bps: Decimal = Decimal("50")
    exchange: str = "MOEX"
    class_code: str = "TQBR"
    runtime_mode: str = RuntimeMode.SHADOW.value
    resolve_instruments: bool = True
    require_resolved_instruments: bool = True


@dataclass(frozen=True, slots=True)
class DividendSyncInstrumentResult:
    instrument_id: str
    ticker: str | None
    dividends_fetched: int = 0
    dividends_inserted: int = 0
    dividends_updated: int = 0
    existing_unchanged: int = 0
    future_risk_windows_created: int = 0
    error_code: str | None = None
    error_message: str | None = None

    def as_payload(self) -> JsonPayload:
        return {
            "instrument_id": self.instrument_id,
            "ticker": self.ticker,
            "dividends_fetched": self.dividends_fetched,
            "dividends_inserted": self.dividends_inserted,
            "dividends_updated": self.dividends_updated,
            "existing_unchanged": self.existing_unchanged,
            "future_risk_windows_created": self.future_risk_windows_created,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class DividendSyncResult:
    from_date: date
    to_date: date
    instruments_processed: int
    successful_instruments: int
    failed_instruments: int
    dividends_fetched: int
    dividends_inserted: int
    dividends_updated: int
    existing_unchanged: int
    special_days_created: int
    future_risk_windows_created: int
    error_count: int
    errors: tuple[JsonPayload, ...]
    status: str
    clean: bool
    source: str = "api_import"
    real_orders_disabled: bool = True
    dry_run: bool = False
    instruments_unresolved: int = 0
    unresolved_instruments: tuple[JsonPayload, ...] = field(default_factory=tuple)
    resolution_attempted: bool = False
    instrument_resolution_status: str = "not_required"
    instruments: tuple[DividendSyncInstrumentResult, ...] = field(default_factory=tuple)

    def as_payload(self) -> JsonPayload:
        return {
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "status": self.status,
            "clean": self.clean,
            "dividend_sync_status": self.status,
            "dividend_sync_clean": self.clean,
            "instruments_processed": self.instruments_processed,
            "successful_instruments": self.successful_instruments,
            "failed_instruments": self.failed_instruments,
            "dividends_fetched": self.dividends_fetched,
            "dividends_inserted": self.dividends_inserted,
            "dividends_updated": self.dividends_updated,
            "existing_unchanged": self.existing_unchanged,
            "special_days_created": self.special_days_created,
            "future_risk_windows_created": self.future_risk_windows_created,
            "error_count": self.error_count,
            "errors": list(self.errors),
            "source": self.source,
            "real_orders_disabled": self.real_orders_disabled,
            "dry_run": self.dry_run,
            "instruments_unresolved": self.instruments_unresolved,
            "unresolved_instruments": list(self.unresolved_instruments),
            "resolution_attempted": self.resolution_attempted,
            "instrument_resolution_status": self.instrument_resolution_status,
            "instruments": [item.as_payload() for item in self.instruments],
        }


class DividendSyncService:
    """Synchronize dividend facts from BrokerGateway.get_dividends into Postgres."""

    def __init__(
        self,
        *,
        session: Session,
        broker_gateway: BrokerGateway,
    ) -> None:
        self._session = session
        self._gateway = broker_gateway
        self._corporate_actions = CorporateActionService(session)

    async def run(self, config: DividendSyncConfig) -> DividendSyncResult:
        started_at = datetime.now(tz=UTC)
        from_date, to_date = dividend_sync_window(config)
        instruments, unresolved, resolution_attempted = await self._resolve_instruments(config)
        if config.force_rebuild and not config.dry_run:
            self._delete_api_import_dividends(
                from_date=from_date,
                to_date=to_date,
                instruments=tuple(item.instrument_id for item in instruments),
            )
        self._write_audit(
            "dividend_sync_started",
            severity="info",
            payload={
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "instruments": [item.instrument_id for item in instruments],
                "dry_run": config.dry_run,
            },
        )

        instrument_results: list[DividendSyncInstrumentResult] = []
        for item in unresolved:
            instrument_results.append(
                DividendSyncInstrumentResult(
                    instrument_id=str(item.get("instrument_id") or ""),
                    ticker=str(item.get("ticker") or "") or None,
                    error_code=str(
                        item.get("error_code")
                        or "instrument_not_resolved_for_dividend_sync"
                    ),
                    error_message=str(
                        item.get("error_message")
                        or "Instrument is not resolved for T-Bank dividend sync."
                    ),
                )
            )
        for instrument in instruments:
            instrument_results.append(
                await self._sync_instrument(
                    instrument,
                    from_date=from_date,
                    to_date=to_date,
                    config=config,
                )
            )

        special_days_created = 0
        future_windows_created = sum(
            item.future_risk_windows_created for item in instrument_results
        )
        if config.classify_special_days and not config.dry_run:
            today = datetime.now(tz=UTC).date()
            classification_to_date = min(to_date, today)
            lookahead_days = max((to_date - classification_to_date).days, 0)
            classification = MarketSpecialDayClassifier(self._session).classify(
                from_date=from_date,
                to_date=classification_to_date,
                instruments=tuple(item.instrument_id for item in instruments),
                gap_threshold_bps=config.gap_threshold_bps,
                dividend_gap_threshold_bps=config.dividend_gap_threshold_bps,
                include_future=True,
                lookahead_days=lookahead_days,
            )
            special_days_created = classification.special_days_created
            future_windows_created = classification.future_risk_windows_created

        status, clean, errors = _sync_status(config=config, instrument_results=instrument_results)
        failed_instruments = sum(1 for item in instrument_results if item.error_code is not None)
        successful_instruments = len(instrument_results) - failed_instruments
        result = DividendSyncResult(
            from_date=from_date,
            to_date=to_date,
            instruments_processed=len(instrument_results),
            successful_instruments=successful_instruments,
            failed_instruments=failed_instruments,
            dividends_fetched=sum(item.dividends_fetched for item in instrument_results),
            dividends_inserted=sum(item.dividends_inserted for item in instrument_results),
            dividends_updated=sum(item.dividends_updated for item in instrument_results),
            existing_unchanged=sum(item.existing_unchanged for item in instrument_results),
            special_days_created=special_days_created,
            future_risk_windows_created=future_windows_created,
            error_count=len(errors),
            errors=errors,
            status=status,
            clean=clean,
            dry_run=config.dry_run,
            instruments_unresolved=len(unresolved),
            unresolved_instruments=tuple(unresolved),
            resolution_attempted=resolution_attempted,
            instrument_resolution_status=_instrument_resolution_status(
                unresolved=unresolved,
                attempted=resolution_attempted,
            ),
            instruments=tuple(instrument_results),
        )
        self._persist_sync_run(
            result,
            started_at=started_at,
            finished_at=datetime.now(tz=UTC),
        )
        self._write_audit(
            _audit_action_for_result(result),
            severity=_audit_severity_for_result(result),
            payload=result.as_payload(),
        )
        self._session.flush()
        return result

    def _persist_sync_run(
        self,
        result: DividendSyncResult,
        *,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        self._session.add(
            DividendSyncRun(
                started_at=started_at,
                finished_at=finished_at,
                status=result.status,
                clean=result.clean,
                from_date=result.from_date,
                to_date=result.to_date,
                instruments={
                    "values": [item.instrument_id for item in result.instruments],
                    "tickers": [
                        item.ticker
                        for item in result.instruments
                        if item.ticker is not None
                    ],
                },
                instruments_processed=result.instruments_processed,
                successful_instruments=result.successful_instruments,
                failed_instruments=result.failed_instruments,
                dividends_fetched=result.dividends_fetched,
                dividends_inserted=result.dividends_inserted,
                dividends_updated=result.dividends_updated,
                existing_unchanged=result.existing_unchanged,
                special_days_created=result.special_days_created,
                future_risk_windows_created=result.future_risk_windows_created,
                error_count=result.error_count,
                result_payload=result.as_payload(),
            )
        )

    async def _sync_instrument(
        self,
        instrument: InstrumentRef,
        *,
        from_date: date,
        to_date: date,
        config: DividendSyncConfig,
    ) -> DividendSyncInstrumentResult:
        try:
            response = await self._gateway.get_dividends(
                DividendsRequest(
                    instrument=instrument,
                    from_=datetime.combine(from_date, time.min, tzinfo=UTC),
                    to=datetime.combine(to_date, time.max, tzinfo=UTC),
                )
            )
            dividends = response.data.get("dividends", [])
            if not isinstance(dividends, list | tuple):
                dividends = []
            counters = {"inserted": 0, "updated": 0, "unchanged": 0, "future": 0}
            for item in dividends:
                if not isinstance(item, dict):
                    continue
                event = await self._event_from_dividend_payload(
                    instrument,
                    item,
                    config=config,
                )
                if config.dry_run:
                    if event.ex_date and event.ex_date > datetime.now(tz=UTC).date():
                        counters["future"] += 1
                    continue
                before = self._existing_event(event)
                before_snapshot = _row_snapshot(before)
                row = self._corporate_actions.upsert_event(event)
                if before is None:
                    counters["inserted"] += 1
                elif before_snapshot != _row_snapshot(row):
                    counters["updated"] += 1
                else:
                    counters["unchanged"] += 1
                if event.ex_date and event.ex_date > datetime.now(tz=UTC).date():
                    counters["future"] += 1
            return DividendSyncInstrumentResult(
                instrument_id=instrument.instrument_id,
                ticker=instrument.ticker,
                dividends_fetched=len(dividends),
                dividends_inserted=counters["inserted"],
                dividends_updated=counters["updated"],
                existing_unchanged=counters["unchanged"],
                future_risk_windows_created=counters["future"],
            )
        except Exception as exc:
            self._write_audit(
                "dividend_sync_failed",
                severity="error",
                payload={
                    "instrument_id": instrument.instrument_id,
                    "ticker": instrument.ticker,
                    "error_code": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return DividendSyncInstrumentResult(
                instrument_id=instrument.instrument_id,
                ticker=instrument.ticker,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )

    async def _event_from_dividend_payload(
        self,
        instrument: InstrumentRef,
        payload: JsonPayload,
        *,
        config: DividendSyncConfig,
    ) -> CorporateActionEvent:
        record_date = _date_from_payload(payload.get("record_date"))
        last_buy_date = _date_from_payload(payload.get("last_buy_date"))
        ex_date, inference = await self._infer_ex_date(
            last_buy_date=last_buy_date,
            explicit_ex_date=_date_from_payload(payload.get("ex_date")),
            config=config,
        )
        action_payload = {
            **payload,
            "source": "api_import",
            "confidence": "confirmed",
            "ex_date_inference_source": inference["source"],
            "ex_date_warning": inference.get("warning"),
            "last_buy_date": last_buy_date.isoformat() if last_buy_date else None,
            "record_date": record_date.isoformat() if record_date else None,
            "ex_date": ex_date.isoformat() if ex_date else None,
        }
        return CorporateActionEvent(
            instrument_id=instrument.instrument_id,
            ticker=instrument.ticker,
            action_type="dividend",
            declared_date=_date_from_payload(payload.get("declared_date")),
            ex_date=ex_date,
            registry_close_date=record_date,
            payment_date=_date_from_payload(payload.get("payment_date")),
            amount_per_share=_decimal_or_none(payload.get("amount_per_share")),
            currency=_optional_str(payload.get("currency")) or "RUB",
            source="api_import",
            confidence="confirmed",
            action_payload=action_payload,
        )

    async def _infer_ex_date(
        self,
        *,
        last_buy_date: date | None,
        explicit_ex_date: date | None,
        config: DividendSyncConfig,
    ) -> tuple[date | None, JsonPayload]:
        if explicit_ex_date is not None:
            return explicit_ex_date, {"source": "broker_explicit_ex_date"}
        if last_buy_date is None:
            return None, {"source": "missing", "warning": "missing_last_buy_date"}
        fallback = _next_weekday(last_buy_date)
        return fallback, {
            "source": "fallback_next_weekday",
            "warning": "trading_schedules_disabled_for_ex_date_inference",
        }

    async def _resolve_instruments(
        self,
        config: DividendSyncConfig,
    ) -> tuple[tuple[InstrumentRef, ...], tuple[JsonPayload, ...], bool]:
        raw_values = config.instruments or DEFAULT_INSTRUMENTS
        refs: list[InstrumentRef] = []
        unresolved: list[JsonPayload] = []
        resolution_attempted = False
        mode = RuntimeMode.HISTORICAL_REPLAY if config.dry_run else RuntimeMode(config.runtime_mode)
        for raw in raw_values:
            value = raw.strip()
            if not value:
                continue
            row = self._session.execute(
                select(InstrumentRegistry).where(InstrumentRegistry.ticker == value.upper())
            ).scalars().first()
            if row is None and ":" in value:
                row = self._session.get(InstrumentRegistry, value)
            if (
                not config.dry_run
                and config.resolve_instruments
                and (row is None or not is_broker_resolved_instrument(row))
            ):
                resolution_attempted = True
                try:
                    resolved = await InstrumentResolverService(
                        broker_gateway=self._gateway,
                        session=self._session,
                        launch_policy=LaunchModePolicy.from_mode(mode),
                        exchange=config.exchange,
                    ).resolve_startup_instruments(
                        (
                            InstrumentRef(
                                instrument_id=(
                                    row.instrument_id
                                    if row is not None
                                    else f"{config.exchange}:{value.upper()}"
                                ),
                                ticker=(row.ticker if row is not None else value.upper()),
                                class_code=(
                                    row.class_code if row is not None else config.class_code
                                ),
                            ),
                        )
                    )
                    row = self._session.execute(
                        select(InstrumentRegistry).where(
                            InstrumentRegistry.ticker == (resolved[0].ticker or value.upper())
                        )
                    ).scalars().first()
                except Exception as exc:
                    unresolved.append(
                        {
                            "instrument_id": row.instrument_id
                            if row is not None
                            else f"{config.exchange}:{value.upper()}",
                            "ticker": row.ticker if row is not None else value.upper(),
                            "error_code": "instrument_not_resolved_for_dividend_sync",
                            "error_message": str(exc),
                        }
                    )
                    continue
            if row is None:
                fallback = InstrumentRef(
                    instrument_id=f"{config.exchange}:{value.upper()}",
                    ticker=value.upper(),
                    class_code=config.class_code,
                )
                if config.dry_run or not config.require_resolved_instruments:
                    refs.append(fallback)
                else:
                    unresolved.append(
                        {
                            "instrument_id": fallback.instrument_id,
                            "ticker": fallback.ticker,
                            "error_code": "instrument_not_resolved_for_dividend_sync",
                            "error_message": "No instrument_registry row exists after resolve.",
                        }
                    )
                continue
            ref = InstrumentRef(
                instrument_id=row.instrument_id,
                instrument_uid=row.instrument_uid,
                figi=row.figi,
                ticker=row.ticker,
                class_code=row.class_code,
            )
            if not config.dry_run and config.require_resolved_instruments:
                try:
                    assert_resolved_for_broker_call(
                        ref,
                        mode=mode,
                        operation_name="GetDividends",
                    )
                except RuntimeError as exc:
                    unresolved.append(
                        {
                            "instrument_id": ref.instrument_id,
                            "ticker": ref.ticker,
                            "error_code": "instrument_not_resolved_for_dividend_sync",
                            "error_message": str(exc),
                        }
                    )
                    continue
            refs.append(
                ref
            )
        return tuple(dict.fromkeys(refs)), tuple(unresolved), resolution_attempted

    def _delete_api_import_dividends(
        self,
        *,
        from_date: date,
        to_date: date,
        instruments: tuple[str, ...],
    ) -> None:
        stmt = delete(CorporateActionEventRow).where(
            CorporateActionEventRow.source == "api_import",
            CorporateActionEventRow.action_type == "dividend",
            CorporateActionEventRow.ex_date >= from_date,
            CorporateActionEventRow.ex_date <= to_date,
        )
        if instruments:
            stmt = stmt.where(CorporateActionEventRow.instrument_id.in_(instruments))
        self._session.execute(stmt)

    def _existing_event(self, event: CorporateActionEvent) -> CorporateActionEventRow | None:
        return self._session.execute(
            select(CorporateActionEventRow).where(
                CorporateActionEventRow.instrument_id == event.instrument_id,
                CorporateActionEventRow.action_type == event.action_type,
                CorporateActionEventRow.ex_date == event.ex_date,
                CorporateActionEventRow.amount_per_share == event.amount_per_share,
                CorporateActionEventRow.source == event.source,
            )
        ).scalars().first()

    def _write_audit(self, action: str, *, severity: str, payload: JsonPayload) -> None:
        now = datetime.now(tz=UTC)
        from_date = payload.get("from_date", now.date().isoformat())
        to_date = payload.get("to_date", now.date().isoformat())
        self._session.add(
            AuditEvent(
                calendar_date=now.date(),
                trading_date=now.date(),
                session_type="weekday_main",
                session_phase="closed",
                micro_session_id=f"dividend-sync:{now.isoformat()}",
                broker_trading_status="not_applicable",
                ts_utc=now,
                exchange_ts=now,
                received_ts=now,
                service=ServiceName.TRADE_CORE.value,
                actor="system",
                action=action,
                entity_type="corporate_action_event",
                entity_id=f"{from_date}:{to_date}",
                severity=severity,
                correlation_id=None,
                audit_payload=payload,
            )
        )


def dividend_sync_window(config: DividendSyncConfig) -> tuple[date, date]:
    today = datetime.now(tz=UTC).date()
    start = config.from_date or (today - timedelta(days=config.lookback_days))
    end = config.to_date or (today + timedelta(days=config.lookahead_days))
    if start > end:
        msg = "from_date must be <= to_date"
        raise ValueError(msg)
    return start, end


def _row_snapshot(row: CorporateActionEventRow | None) -> tuple[object, ...] | None:
    if row is None:
        return None
    return (
        row.ticker,
        row.declared_date,
        row.ex_date,
        row.registry_close_date,
        row.payment_date,
        row.amount_per_share,
        row.currency,
        row.confidence,
        dict(row.action_payload),
    )


def _date_from_payload(value: object) -> date | None:
    raw = _optional_str(value)
    if raw is None:
        return None
    return date.fromisoformat(raw[:10])


def _decimal_or_none(value: object) -> Decimal | None:
    raw = _optional_str(value)
    return None if raw is None else Decimal(raw)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _next_weekday(value: date) -> date:
    cursor = value + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor


def _sync_status(
    *,
    config: DividendSyncConfig,
    instrument_results: list[DividendSyncInstrumentResult],
) -> tuple[str, bool, tuple[JsonPayload, ...]]:
    errors = tuple(
        {
            "instrument_id": item.instrument_id,
            "ticker": item.ticker,
            "error_code": item.error_code,
            "error_message": item.error_message,
        }
        for item in instrument_results
        if item.error_code is not None
    )
    processed = len(instrument_results)
    failed = len(errors)
    if config.dry_run:
        return "dry_run", processed > 0 and failed == 0, errors
    if processed == 0:
        return (
            "failed",
            False,
            (
                {
                    "instrument_id": None,
                    "ticker": None,
                    "error_code": "no_instruments_processed",
                    "error_message": "Dividend sync resolved zero instruments.",
                },
            ),
        )
    if failed == 0:
        return "completed", True, errors
    if failed == processed:
        return "failed", False, errors
    return "completed_with_errors", False, errors


def _instrument_resolution_status(
    *,
    unresolved: tuple[JsonPayload, ...],
    attempted: bool,
) -> str:
    if unresolved:
        return "unresolved"
    if attempted:
        return "resolved"
    return "not_required"


def _audit_action_for_result(result: DividendSyncResult) -> str:
    if result.status == "completed":
        return "dividend_sync_completed"
    if result.status == "completed_with_errors":
        return "dividend_sync_completed_with_errors"
    if result.status == "dry_run":
        return "dividend_sync_dry_run"
    return "dividend_sync_failed"


def _audit_severity_for_result(result: DividendSyncResult) -> str:
    if result.clean:
        return "info"
    if result.status == "failed":
        return "error"
    return "warning"
