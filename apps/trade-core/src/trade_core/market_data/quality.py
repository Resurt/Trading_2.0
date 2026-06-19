"""Historical market_candle quality checks for replay readiness."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_core.corporate_actions.service import (
    dividend_sync_status_payload,
    special_day_classification_exists,
)
from trade_core.market_data.events import Timeframe, ensure_utc, parse_timeframe
from trade_core.market_data.historical_backfill import classify_historical_exchange_ts
from trading_common import ServiceName
from trading_common.db.models import (
    AuditEvent,
    CorporateActionEvent,
    InstrumentRegistry,
    MarketCandle,
    MarketSpecialDay,
)
from trading_common.db.models import (
    HistoricalDataQualityReport as HistoricalDataQualityReportRow,
)

JsonPayload = dict[str, Any]
MSK = ZoneInfo("Europe/Moscow")


class InvalidCandleReason(StrEnum):
    """Machine-readable OHLC/timestamp validation reasons."""

    NON_MONOTONIC_TIMESTAMPS = "non_monotonic_timestamps"
    ZERO_OR_NEGATIVE_OHLC = "zero_or_negative_ohlc"
    HIGH_BELOW_LOW = "high_below_low"
    OPEN_OUTSIDE_HIGH_LOW = "open_outside_high_low"
    CLOSE_OUTSIDE_HIGH_LOW = "close_outside_high_low"
    OUTSIDE_EXPECTED_SESSION = "outside_expected_session"


@dataclass(frozen=True, slots=True)
class MissingInterval:
    instrument_id: str
    timeframe: str
    expected_open_ts_utc: datetime
    session_type: str

    def as_payload(self) -> JsonPayload:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "expected_open_ts_utc": self.expected_open_ts_utc.isoformat(),
            "session_type": self.session_type,
        }


@dataclass(frozen=True, slots=True)
class InstrumentTimeframeQuality:
    instrument_id: str
    timeframe: str
    coverage_pct: Decimal
    expected_candles: int
    actual_candles: int
    missing_intervals: tuple[MissingInterval, ...] = ()
    duplicate_count: int = 0
    invalid_ohlc_count: int = 0
    abnormal_gap_count: int = 0
    candles_outside_session_windows: int = 0
    first_candle: datetime | None = None
    last_candle: datetime | None = None
    source_distribution: dict[str, int] = field(default_factory=dict)
    session_type_distribution: dict[str, int] = field(default_factory=dict)

    def as_payload(self) -> JsonPayload:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "coverage_pct": str(self.coverage_pct),
            "expected_candles": self.expected_candles,
            "actual_candles": self.actual_candles,
            "missing_intervals": [item.as_payload() for item in self.missing_intervals[:100]],
            "missing_interval_count": len(self.missing_intervals),
            "duplicate_count": self.duplicate_count,
            "invalid_ohlc_count": self.invalid_ohlc_count,
            "abnormal_gap_count": self.abnormal_gap_count,
            "candles_outside_session_windows": self.candles_outside_session_windows,
            "first_candle": self.first_candle.isoformat() if self.first_candle else None,
            "last_candle": self.last_candle.isoformat() if self.last_candle else None,
            "source_distribution": dict(sorted(self.source_distribution.items())),
            "session_type_distribution": dict(sorted(self.session_type_distribution.items())),
        }


@dataclass(frozen=True, slots=True)
class HistoricalDataQualityReport:
    generated_at: datetime
    from_date: date
    to_date: date
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    coverage_pct: Decimal
    expected_candles: int
    actual_candles: int
    missing_intervals: int
    duplicate_count: int
    invalid_ohlc_count: int
    abnormal_gap_count: int
    non_monotonic_timestamp_count: int
    candles_outside_session_windows: int
    first_candle: datetime | None
    last_candle: datetime | None
    source_distribution: dict[str, int]
    session_type_distribution: dict[str, int]
    timeframe_distribution: dict[str, int]
    weekend_candles: int
    weekday_candles: int
    instrument_timeframes: tuple[InstrumentTimeframeQuality, ...]
    corporate_action_days_count: int = 0
    dividend_gap_days_count: int = 0
    abnormal_gap_days_count: int = 0
    excluded_days_count: int = 0
    included_days_count: int = 0
    special_day_distribution: dict[str, int] = field(default_factory=dict)
    corporate_action_classification_status: str = "missing"
    dividend_sync_status: str = "missing"
    dividend_sync_clean: bool = False
    dividend_sync_failed_instruments: int = 0
    dividend_sync_error_count: int = 0
    api_import_dividend_events_count: int = 0
    manual_dividend_events_count: int = 0
    quality_warnings: tuple[str, ...] = ()
    report_id: str | None = None

    @property
    def passed(self) -> bool:
        return self.invalid_ohlc_count == 0 and self.non_monotonic_timestamp_count == 0

    def as_payload(self) -> JsonPayload:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at.isoformat(),
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "instruments": list(self.instruments),
            "timeframes": list(self.timeframes),
            "coverage_pct": str(self.coverage_pct),
            "expected_candles": self.expected_candles,
            "actual_candles": self.actual_candles,
            "missing_intervals": self.missing_intervals,
            "duplicate_count": self.duplicate_count,
            "invalid_ohlc_count": self.invalid_ohlc_count,
            "abnormal_gap_count": self.abnormal_gap_count,
            "non_monotonic_timestamp_count": self.non_monotonic_timestamp_count,
            "candles_outside_session_windows": self.candles_outside_session_windows,
            "first_candle": self.first_candle.isoformat() if self.first_candle else None,
            "last_candle": self.last_candle.isoformat() if self.last_candle else None,
            "source_distribution": dict(sorted(self.source_distribution.items())),
            "session_type_distribution": dict(sorted(self.session_type_distribution.items())),
            "timeframe_distribution": dict(sorted(self.timeframe_distribution.items())),
            "weekend_candles": self.weekend_candles,
            "weekday_candles": self.weekday_candles,
            "corporate_action_days_count": self.corporate_action_days_count,
            "dividend_gap_days_count": self.dividend_gap_days_count,
            "abnormal_gap_days_count": self.abnormal_gap_days_count,
            "excluded_days_count": self.excluded_days_count,
            "included_days_count": self.included_days_count,
            "special_day_distribution": dict(sorted(self.special_day_distribution.items())),
            "corporate_action_classification_status": (
                self.corporate_action_classification_status
            ),
            "dividend_sync_status": self.dividend_sync_status,
            "dividend_sync_clean": self.dividend_sync_clean,
            "dividend_sync_failed_instruments": self.dividend_sync_failed_instruments,
            "dividend_sync_error_count": self.dividend_sync_error_count,
            "api_import_dividend_events_count": self.api_import_dividend_events_count,
            "manual_dividend_events_count": self.manual_dividend_events_count,
            "quality_warnings": list(self.quality_warnings),
            "quality_warning": self.quality_warnings[0] if self.quality_warnings else None,
            "instrument_timeframes": [item.as_payload() for item in self.instrument_timeframes],
            "passed": self.passed,
            "source": "historical_data_quality_report",
        }


@dataclass(frozen=True, slots=True)
class HistoricalDataQualityConfig:
    from_date: date
    to_date: date
    instruments: tuple[str, ...]
    timeframes: tuple[Timeframe, ...]
    fail_on_missing: bool = False
    fail_on_invalid_ohlc: bool = False
    max_missing_intervals: int | None = None
    write_report: bool = True
    require_special_day_classification: bool = False


class HistoricalDataQualityService:
    """Validate historical candles already stored in PostgreSQL/SQLite."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_report(self, config: HistoricalDataQualityConfig) -> HistoricalDataQualityReport:
        instrument_ids = self._resolve_instrument_ids(config.instruments)
        timeframe_values = tuple(timeframe.value for timeframe in config.timeframes)
        rows = self._load_candles(
            from_date=config.from_date,
            to_date=config.to_date,
            instrument_ids=instrument_ids,
            timeframes=timeframe_values,
        )
        duplicate_count = self._duplicate_count(
            from_date=config.from_date,
            to_date=config.to_date,
            instrument_ids=instrument_ids,
            timeframes=timeframe_values,
        )
        expected_by_key = _expected_open_times(
            from_date=config.from_date,
            to_date=config.to_date,
            instrument_ids=instrument_ids,
            timeframes=config.timeframes,
        )
        actual_by_key: dict[tuple[str, str], set[datetime]] = defaultdict(set)
        rows_by_key: dict[tuple[str, str], list[MarketCandle]] = defaultdict(list)
        source_distribution: Counter[str] = Counter()
        session_distribution: Counter[str] = Counter()
        timeframe_distribution: Counter[str] = Counter()
        invalid_ohlc_count = 0
        non_monotonic_timestamp_count = 0
        outside_session_count = 0

        for row in rows:
            key = (row.instrument_id, row.timeframe)
            rows_by_key[key].append(row)
            actual_by_key[key].add(ensure_utc(row.open_ts_utc))
            source_distribution[row.source] += 1
            session_distribution[row.session_type] += 1
            timeframe_distribution[row.timeframe] += 1
            reasons = invalid_reasons(row)
            invalid_ohlc_count += int(
                any(
                    reason is not InvalidCandleReason.OUTSIDE_EXPECTED_SESSION
                    for reason in reasons
                )
            )
            non_monotonic_timestamp_count += int(
                InvalidCandleReason.NON_MONOTONIC_TIMESTAMPS in reasons
            )
            outside_session_count += int(
                InvalidCandleReason.OUTSIDE_EXPECTED_SESSION in reasons
            )

        item_reports: list[InstrumentTimeframeQuality] = []
        all_missing: list[MissingInterval] = []
        abnormal_gap_count = 0
        for key, expected_times in expected_by_key.items():
            instrument_id, timeframe = key
            actual_times = actual_by_key.get(key, set())
            missing = tuple(
                MissingInterval(
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    expected_open_ts_utc=ts,
                    session_type=classify_historical_exchange_ts(ts.astimezone(MSK)).session_type.value,
                )
                for ts in sorted(expected_times - actual_times)
            )
            all_missing.extend(missing)
            key_rows = sorted(rows_by_key.get(key, []), key=lambda item: item.open_ts_utc)
            key_abnormal_gaps = _abnormal_gap_count(key_rows, parse_timeframe(timeframe))
            abnormal_gap_count += key_abnormal_gaps
            first_candle = ensure_utc(key_rows[0].open_ts_utc) if key_rows else None
            last_candle = ensure_utc(key_rows[-1].open_ts_utc) if key_rows else None
            item_reports.append(
                InstrumentTimeframeQuality(
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    coverage_pct=_coverage(len(expected_times), len(actual_times)),
                    expected_candles=len(expected_times),
                    actual_candles=len(actual_times),
                    missing_intervals=missing,
                    duplicate_count=0,
                    invalid_ohlc_count=sum(
                        1
                        for row in key_rows
                        if any(
                            reason is not InvalidCandleReason.OUTSIDE_EXPECTED_SESSION
                            for reason in invalid_reasons(row)
                        )
                    ),
                    abnormal_gap_count=key_abnormal_gaps,
                    candles_outside_session_windows=sum(
                        1
                        for row in key_rows
                        if InvalidCandleReason.OUTSIDE_EXPECTED_SESSION in invalid_reasons(row)
                    ),
                    first_candle=first_candle,
                    last_candle=last_candle,
                    source_distribution=dict(Counter(row.source for row in key_rows)),
                    session_type_distribution=dict(Counter(row.session_type for row in key_rows)),
                )
            )

        expected_count = sum(len(value) for value in expected_by_key.values())
        actual_count = len(rows)
        first = min((ensure_utc(row.open_ts_utc) for row in rows), default=None)
        last = max((ensure_utc(row.open_ts_utc) for row in rows), default=None)
        special_summary = self._special_day_summary(
            from_date=config.from_date,
            to_date=config.to_date,
            instrument_ids=instrument_ids,
        )
        quality_warnings: list[str] = []
        if special_summary["corporate_action_classification_status"] == "missing":
            quality_warnings.append(
                "Run market special day classification before using calibration as final."
            )
        if special_summary["dividend_sync_status"] == "manual_only":
            quality_warnings.append("manual_corporate_actions_only")
        if special_summary["dividend_sync_status"] == "missing":
            quality_warnings.append("dividend_sync_missing")
        if special_summary["dividend_sync_status"] == "completed_with_errors":
            quality_warnings.append("dividend_sync_completed_with_errors")
        if special_summary["dividend_sync_status"] == "failed":
            quality_warnings.append("dividend_sync_failed")
        report = HistoricalDataQualityReport(
            generated_at=datetime.now(tz=UTC),
            from_date=config.from_date,
            to_date=config.to_date,
            instruments=tuple(instrument_ids),
            timeframes=timeframe_values,
            coverage_pct=_coverage(expected_count, actual_count),
            expected_candles=expected_count,
            actual_candles=actual_count,
            missing_intervals=len(all_missing),
            duplicate_count=duplicate_count,
            invalid_ohlc_count=invalid_ohlc_count,
            abnormal_gap_count=abnormal_gap_count,
            non_monotonic_timestamp_count=non_monotonic_timestamp_count,
            candles_outside_session_windows=outside_session_count,
            first_candle=first,
            last_candle=last,
            source_distribution=dict(source_distribution),
            session_type_distribution=dict(session_distribution),
            timeframe_distribution=dict(timeframe_distribution),
            weekend_candles=session_distribution.get("weekend", 0),
            weekday_candles=sum(
                count
                for session_type, count in session_distribution.items()
                if session_type != "weekend"
            ),
            instrument_timeframes=tuple(item_reports),
            corporate_action_days_count=int(special_summary["corporate_action_days_count"]),
            dividend_gap_days_count=int(special_summary["dividend_gap_days_count"]),
            abnormal_gap_days_count=int(special_summary["abnormal_gap_days_count"]),
            excluded_days_count=int(special_summary["excluded_days_count"]),
            included_days_count=int(special_summary["included_days_count"]),
            special_day_distribution=dict(special_summary["special_day_distribution"]),
            corporate_action_classification_status=str(
                special_summary["corporate_action_classification_status"]
            ),
            dividend_sync_status=str(special_summary["dividend_sync_status"]),
            dividend_sync_clean=bool(special_summary["dividend_sync_clean"]),
            dividend_sync_failed_instruments=int(
                special_summary["dividend_sync_failed_instruments"]
            ),
            dividend_sync_error_count=int(special_summary["dividend_sync_error_count"]),
            api_import_dividend_events_count=int(
                special_summary["api_import_dividend_events_count"]
            ),
            manual_dividend_events_count=int(special_summary["manual_dividend_events_count"]),
            quality_warnings=tuple(quality_warnings),
        )
        if config.write_report:
            report = self._persist_report(report)
        return report

    def assert_passes(self, config: HistoricalDataQualityConfig) -> HistoricalDataQualityReport:
        report = self.build_report(config)
        if config.fail_on_invalid_ohlc and report.invalid_ohlc_count:
            raise SystemExit(2)
        max_missing = config.max_missing_intervals
        if config.fail_on_missing and report.missing_intervals:
            raise SystemExit(3)
        if max_missing is not None and report.missing_intervals > max_missing:
            raise SystemExit(4)
        if (
            config.require_special_day_classification
            and report.corporate_action_classification_status == "missing"
        ):
            raise SystemExit(5)
        return report

    def _persist_report(
        self,
        report: HistoricalDataQualityReport,
    ) -> HistoricalDataQualityReport:
        payload = report.as_payload()
        row = HistoricalDataQualityReportRow(
            generated_at=report.generated_at,
            from_date=report.from_date,
            to_date=report.to_date,
            instruments={"values": list(report.instruments)},
            timeframes={"values": list(report.timeframes)},
            coverage_pct=report.coverage_pct,
            expected_candles=report.expected_candles,
            actual_candles=report.actual_candles,
            missing_intervals=report.missing_intervals,
            duplicate_count=report.duplicate_count,
            invalid_ohlc_count=report.invalid_ohlc_count,
            abnormal_gap_count=report.abnormal_gap_count,
            report_payload=payload,
        )
        self._session.add(row)
        self._session.flush()
        self._session.add(_audit_event(report=report, report_id=str(row.report_id)))
        return replace(report, report_id=str(row.report_id))

    def _resolve_instrument_ids(self, instruments: tuple[str, ...]) -> tuple[str, ...]:
        if not instruments:
            stmt = (
                select(MarketCandle.instrument_id)
                .distinct()
                .order_by(MarketCandle.instrument_id)
            )
            return tuple(str(value) for value in self._session.execute(stmt).scalars())
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
            elif ":" in value or value.startswith("safe-noop-"):
                resolved.append(value)
            else:
                resolved.append(f"MOEX:{value.upper()}")
        return tuple(dict.fromkeys(resolved))

    def _load_candles(
        self,
        *,
        from_date: date,
        to_date: date,
        instrument_ids: tuple[str, ...],
        timeframes: tuple[str, ...],
    ) -> list[MarketCandle]:
        stmt = (
            select(MarketCandle)
            .where(
                MarketCandle.trading_date >= from_date,
                MarketCandle.trading_date <= to_date,
                MarketCandle.timeframe.in_(timeframes),
            )
            .order_by(MarketCandle.instrument_id, MarketCandle.timeframe, MarketCandle.open_ts_utc)
        )
        if instrument_ids:
            stmt = stmt.where(MarketCandle.instrument_id.in_(instrument_ids))
        return list(self._session.execute(stmt).scalars())

    def _duplicate_count(
        self,
        *,
        from_date: date,
        to_date: date,
        instrument_ids: tuple[str, ...],
        timeframes: tuple[str, ...],
    ) -> int:
        stmt = (
            select(
                MarketCandle.instrument_id,
                MarketCandle.timeframe,
                MarketCandle.open_ts_utc,
                func.count(),
            )
            .where(
                MarketCandle.trading_date >= from_date,
                MarketCandle.trading_date <= to_date,
                MarketCandle.timeframe.in_(timeframes),
            )
            .group_by(MarketCandle.instrument_id, MarketCandle.timeframe, MarketCandle.open_ts_utc)
            .having(func.count() > 1)
        )
        if instrument_ids:
            stmt = stmt.where(MarketCandle.instrument_id.in_(instrument_ids))
        return sum(int(count) - 1 for *_unused, count in self._session.execute(stmt).all())

    def _special_day_summary(
        self,
        *,
        from_date: date,
        to_date: date,
        instrument_ids: tuple[str, ...],
    ) -> JsonPayload:
        stmt = select(MarketSpecialDay).where(
            MarketSpecialDay.trading_date >= from_date,
            MarketSpecialDay.trading_date <= to_date,
        )
        if instrument_ids:
            stmt = stmt.where(MarketSpecialDay.instrument_id.in_(instrument_ids))
        rows = list(self._session.execute(stmt).scalars())
        distribution = Counter(row.special_day_type for row in rows)
        excluded_keys = {
            (row.trading_date, row.instrument_id)
            for row in rows
            if row.exclude_from_primary_calibration
        }
        included_keys = {
            (row.trading_date, row.instrument_id)
            for row in rows
            if not row.exclude_from_primary_calibration
        }
        status = (
            "completed"
            if special_day_classification_exists(
                self._session,
                from_date=from_date,
                to_date=to_date,
                instruments=instrument_ids,
            )
            else "missing"
        )
        return {
            "corporate_action_days_count": (
                distribution.get("corporate_action_day", 0)
                + distribution.get("dividend_gap_day", 0)
            ),
            "dividend_gap_days_count": distribution.get("dividend_gap_day", 0),
            "abnormal_gap_days_count": distribution.get("abnormal_gap_day", 0),
            "excluded_days_count": len(excluded_keys),
            "included_days_count": len(included_keys),
            "special_day_distribution": dict(distribution),
            "corporate_action_classification_status": status,
            **self._dividend_sync_summary(
                from_date=from_date,
                to_date=to_date,
                instrument_ids=instrument_ids,
            ),
        }

    def _dividend_sync_summary(
        self,
        *,
        from_date: date,
        to_date: date,
        instrument_ids: tuple[str, ...],
    ) -> JsonPayload:
        stmt = select(CorporateActionEvent).where(
            CorporateActionEvent.action_type == "dividend",
            CorporateActionEvent.ex_date >= from_date,
            CorporateActionEvent.ex_date <= to_date,
        )
        if instrument_ids:
            stmt = stmt.where(CorporateActionEvent.instrument_id.in_(instrument_ids))
        rows = list(self._session.execute(stmt).scalars())
        api_count = sum(1 for row in rows if row.source == "api_import")
        manual_count = sum(1 for row in rows if row.source != "api_import")
        latest = dividend_sync_status_payload(self._session)
        status = str(latest["status"])
        if status == "missing" and manual_count:
            status = "manual_only"
        return {
            "dividend_sync_status": status,
            "dividend_sync_clean": latest["clean"],
            "dividend_sync_failed_instruments": latest["failed_instruments"],
            "dividend_sync_error_count": latest["error_count"],
            "api_import_dividend_events_count": api_count,
            "manual_dividend_events_count": manual_count,
        }


def invalid_reasons(row: MarketCandle) -> tuple[InvalidCandleReason, ...]:
    reasons: list[InvalidCandleReason] = []
    if ensure_utc(row.open_ts_utc) >= ensure_utc(row.close_ts_utc):
        reasons.append(InvalidCandleReason.NON_MONOTONIC_TIMESTAMPS)
    if min(row.open_price, row.high_price, row.low_price, row.close_price) <= Decimal("0"):
        reasons.append(InvalidCandleReason.ZERO_OR_NEGATIVE_OHLC)
    if row.high_price < row.low_price:
        reasons.append(InvalidCandleReason.HIGH_BELOW_LOW)
    if not row.low_price <= row.open_price <= row.high_price:
        reasons.append(InvalidCandleReason.OPEN_OUTSIDE_HIGH_LOW)
    if not row.low_price <= row.close_price <= row.high_price:
        reasons.append(InvalidCandleReason.CLOSE_OUTSIDE_HIGH_LOW)
    if classify_historical_exchange_ts(row.exchange_open_ts).session_phase.value == "closed":
        reasons.append(InvalidCandleReason.OUTSIDE_EXPECTED_SESSION)
    return tuple(reasons)


def default_quality_window(
    *,
    from_date: date | None,
    to_date: date | None,
    lookback_days: int,
) -> tuple[date, date]:
    end = to_date or datetime.now(tz=MSK).date()
    start = from_date or (end - timedelta(days=lookback_days - 1))
    if start > end:
        msg = "from_date must be <= to_date"
        raise ValueError(msg)
    return start, end


def _expected_open_times(
    *,
    from_date: date,
    to_date: date,
    instrument_ids: tuple[str, ...],
    timeframes: tuple[Timeframe, ...],
) -> dict[tuple[str, str], set[datetime]]:
    expected: dict[tuple[str, str], set[datetime]] = defaultdict(set)
    cursor = from_date
    while cursor <= to_date:
        windows: tuple[tuple[time, time], ...]
        if cursor.weekday() < 5:
            windows = (
                (time(7, 0), time(10, 0)),
                (time(10, 0), time(18, 59)),
                (time(19, 0), time(23, 50)),
            )
        else:
            windows = ()
        for instrument_id in instrument_ids:
            for timeframe in timeframes:
                for start, end in windows:
                    current = datetime.combine(cursor, start, tzinfo=MSK)
                    window_end = datetime.combine(cursor, end, tzinfo=MSK)
                    while current + timedelta(minutes=timeframe.minutes) <= window_end:
                        expected[(instrument_id, timeframe.value)].add(current.astimezone(UTC))
                        current += timedelta(minutes=timeframe.minutes)
        cursor += timedelta(days=1)
    return expected


def _abnormal_gap_count(rows: list[MarketCandle], timeframe: Timeframe) -> int:
    count = 0
    previous: MarketCandle | None = None
    expected_delta = timedelta(minutes=timeframe.minutes)
    for row in rows:
        classification = classify_historical_exchange_ts(row.exchange_open_ts)
        if classification.session_phase.value != "continuous_trading":
            previous = None
            continue
        if previous is not None:
            previous_classification = classify_historical_exchange_ts(previous.exchange_open_ts)
            if previous_classification.session_type == classification.session_type:
                delta = ensure_utc(row.open_ts_utc) - ensure_utc(previous.open_ts_utc)
                if delta > expected_delta:
                    count += 1
        previous = row
    return count


def _coverage(expected: int, actual: int) -> Decimal:
    if expected <= 0:
        return Decimal("100.0000") if actual == 0 else Decimal("0.0000")
    return ((Decimal(actual) / Decimal(expected)) * Decimal("100")).quantize(Decimal("0.0001"))


def _audit_event(*, report: HistoricalDataQualityReport, report_id: str) -> AuditEvent:
    return AuditEvent(
        calendar_date=report.generated_at.date(),
        trading_date=report.to_date,
        session_type="weekday_main",
        session_phase="closed",
        micro_session_id=f"historical-quality:{report.generated_at.isoformat()}",
        broker_trading_status="not_applicable",
        ts_utc=report.generated_at,
        exchange_ts=report.generated_at,
        received_ts=report.generated_at,
        service=ServiceName.TRADE_CORE.value,
        actor="system",
        action="historical_data_quality_report_generated",
        entity_type="historical_data_quality_report",
        entity_id=report_id,
        severity="info" if report.passed else "warning",
        correlation_id=report_id,
        audit_payload=report.as_payload(),
    )
