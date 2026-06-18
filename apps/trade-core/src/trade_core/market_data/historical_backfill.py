"""Historical candle backfill through BrokerGateway and the existing BarEngine."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import BrokerGateway, CandleRequest, InstrumentRef
from trade_core.instruments import InstrumentResolverService
from trade_core.market_data.bars import BarEngine
from trade_core.market_data.events import Candle, Timeframe, ensure_utc, parse_timeframe
from trade_core.market_data.persistence import SqlAlchemyMarketDataStore
from trade_core.market_data.subscriptions import candle_from_mapping
from trade_core.session.models import SessionEventContext
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.models import InstrumentRegistry, MarketCandle
from trading_common.db.repositories import InstrumentRepository, MarketDataRepository
from trading_common.enums import SessionPhase, SessionType
from trading_common.telemetry import get_logger, log_event

JsonPayload = dict[str, Any]
MSK = ZoneInfo("Europe/Moscow")
DEFAULT_INSTRUMENTS = ("SBER", "GAZP")
SUPPORTED_INSTRUMENTS = {"SBER", "GAZP", "LKOH"}
DEFAULT_DERIVED_TIMEFRAMES = (Timeframe.M5, Timeframe.M10, Timeframe.M15)

LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HistoricalBackfillConfig:
    """Configuration for one historical candle backfill run."""

    instruments: tuple[str, ...] = DEFAULT_INSTRUMENTS
    raw_interval: Timeframe = Timeframe.M1
    derived_intervals: tuple[Timeframe, ...] = DEFAULT_DERIVED_TIMEFRAMES
    lookback_days: int = 90
    chunk_days: int = 7
    strategy_id: str = "baseline"
    exchange: str = "MOEX"
    class_code: str = "TQBR"
    dry_run: bool = False
    runtime_mode: str = RuntimeMode.HISTORICAL_REPLAY.value

    def normalized_instruments(self) -> tuple[str, ...]:
        values = tuple(ticker.strip().upper() for ticker in self.instruments if ticker.strip())
        unsupported = sorted(set(values) - SUPPORTED_INSTRUMENTS)
        if unsupported:
            msg = "Unsupported historical backfill instruments: " + ", ".join(unsupported)
            raise ValueError(msg)
        return values or DEFAULT_INSTRUMENTS


@dataclass(frozen=True, slots=True)
class HistoricalBackfillChunk:
    """One GetCandles request window for one instrument."""

    instrument: InstrumentRef
    from_ts_utc: datetime
    to_ts_utc: datetime
    raw_interval: Timeframe


@dataclass(frozen=True, slots=True)
class HistoricalBackfillPlan:
    """Resolved instruments and chunked request windows."""

    instruments: tuple[InstrumentRef, ...]
    chunks: tuple[HistoricalBackfillChunk, ...]
    raw_interval: Timeframe
    derived_intervals: tuple[Timeframe, ...]
    from_ts_utc: datetime
    to_ts_utc: datetime
    dry_run: bool


@dataclass(frozen=True, slots=True)
class HistoricalBackfillQualitySummary:
    """Data quality counters collected while ingesting candles."""

    raw_candles_seen: int = 0
    raw_candles_closed: int = 0
    raw_candles_incomplete: int = 0
    duplicate_raw_candles: int = 0
    invalid_price_candles: int = 0
    gap_count: int = 0
    first_open_ts_utc: datetime | None = None
    last_close_ts_utc: datetime | None = None

    @property
    def passed(self) -> bool:
        return self.invalid_price_candles == 0

    def as_payload(self) -> JsonPayload:
        return {
            "passed": self.passed,
            "raw_candles_seen": self.raw_candles_seen,
            "raw_candles_closed": self.raw_candles_closed,
            "raw_candles_incomplete": self.raw_candles_incomplete,
            "duplicate_raw_candles": self.duplicate_raw_candles,
            "invalid_price_candles": self.invalid_price_candles,
            "gap_count": self.gap_count,
            "first_open_ts_utc": (
                self.first_open_ts_utc.isoformat() if self.first_open_ts_utc else None
            ),
            "last_close_ts_utc": (
                self.last_close_ts_utc.isoformat() if self.last_close_ts_utc else None
            ),
        }


@dataclass(frozen=True, slots=True)
class HistoricalBackfillInstrumentResult:
    """Backfill result for one instrument."""

    instrument: InstrumentRef
    requested_chunks: int = 0
    raw_candles_fetched: int = 0
    raw_candles_written: int = 0
    raw_candles_existing: int = 0
    derived_bars_written: dict[str, int] = field(default_factory=dict)
    derived_bars_existing: dict[str, int] = field(default_factory=dict)
    quality: HistoricalBackfillQualitySummary = field(
        default_factory=HistoricalBackfillQualitySummary
    )

    def as_payload(self) -> JsonPayload:
        return {
            "instrument_id": self.instrument.instrument_id,
            "instrument_uid": self.instrument.instrument_uid,
            "ticker": self.instrument.ticker,
            "requested_chunks": self.requested_chunks,
            "raw_candles_fetched": self.raw_candles_fetched,
            "raw_candles_written": self.raw_candles_written,
            "raw_candles_existing": self.raw_candles_existing,
            "derived_bars_written": dict(sorted(self.derived_bars_written.items())),
            "derived_bars_existing": dict(sorted(self.derived_bars_existing.items())),
            "quality": self.quality.as_payload(),
        }


@dataclass(frozen=True, slots=True)
class HistoricalBackfillResult:
    """Backfill run result."""

    plan: HistoricalBackfillPlan
    instruments: tuple[HistoricalBackfillInstrumentResult, ...]
    dry_run: bool

    @property
    def raw_candles_written(self) -> int:
        return sum(item.raw_candles_written for item in self.instruments)

    @property
    def passed_quality(self) -> bool:
        return all(item.quality.passed for item in self.instruments)

    def as_payload(self) -> JsonPayload:
        return {
            "dry_run": self.dry_run,
            "from_ts_utc": self.plan.from_ts_utc.isoformat(),
            "to_ts_utc": self.plan.to_ts_utc.isoformat(),
            "raw_interval": self.plan.raw_interval.value,
            "derived_intervals": [item.value for item in self.plan.derived_intervals],
            "chunk_count": len(self.plan.chunks),
            "instrument_count": len(self.instruments),
            "raw_candles_written": self.raw_candles_written,
            "passed_quality": self.passed_quality,
            "instruments": [item.as_payload() for item in self.instruments],
            "real_orders_disabled": True,
        }


class HistoricalCandleBackfillService:
    """Download historical candles and persist raw/derived market data idempotently."""

    def __init__(
        self,
        *,
        broker_gateway: BrokerGateway,
        session: Session,
        launch_policy: LaunchModePolicy | None = None,
        exchange: str = "MOEX",
    ) -> None:
        self._broker_gateway = broker_gateway
        self._session = session
        self._launch_policy = launch_policy or LaunchModePolicy.from_mode(
            RuntimeMode.HISTORICAL_REPLAY
        )
        self._exchange = exchange
        self._store = SqlAlchemyMarketDataStore(session)
        self._market_repository = MarketDataRepository(session)

    async def build_plan(
        self,
        config: HistoricalBackfillConfig,
        *,
        from_ts_utc: datetime | None = None,
        to_ts_utc: datetime | None = None,
    ) -> HistoricalBackfillPlan:
        """Resolve instruments and split the date range into broker request chunks."""

        to_ts = ensure_utc(to_ts_utc or datetime.now(tz=UTC))
        from_ts = ensure_utc(from_ts_utc or (to_ts - timedelta(days=config.lookback_days)))
        if from_ts >= to_ts:
            msg = "from_ts_utc must be before to_ts_utc"
            raise ValueError(msg)
        instruments = await self._resolve_instruments(config)
        chunks = tuple(
            chunk
            for instrument in instruments
            for chunk in _chunk_range(
                instrument=instrument,
                from_ts_utc=from_ts,
                to_ts_utc=to_ts,
                raw_interval=config.raw_interval,
                chunk_days=config.chunk_days,
            )
        )
        return HistoricalBackfillPlan(
            instruments=instruments,
            chunks=chunks,
            raw_interval=config.raw_interval,
            derived_intervals=config.derived_intervals,
            from_ts_utc=from_ts,
            to_ts_utc=to_ts,
            dry_run=config.dry_run,
        )

    async def run(
        self,
        config: HistoricalBackfillConfig,
        *,
        from_ts_utc: datetime | None = None,
        to_ts_utc: datetime | None = None,
    ) -> HistoricalBackfillResult:
        """Execute a full historical candle backfill run."""

        plan = await self.build_plan(config, from_ts_utc=from_ts_utc, to_ts_utc=to_ts_utc)
        log_event(
            logger=LOGGER,
            event_type="historical_candle_backfill_started",
            component="market_data.historical_backfill",
            instruments=[instrument.instrument_id for instrument in plan.instruments],
            chunk_count=len(plan.chunks),
            raw_interval=plan.raw_interval.value,
            derived_intervals=[item.value for item in plan.derived_intervals],
            dry_run=plan.dry_run,
        )
        if plan.dry_run:
            return HistoricalBackfillResult(
                plan=plan,
                instruments=tuple(
                    HistoricalBackfillInstrumentResult(
                        instrument=instrument,
                        requested_chunks=sum(
                            1 for chunk in plan.chunks if chunk.instrument == instrument
                        ),
                    )
                    for instrument in plan.instruments
                ),
                dry_run=True,
            )

        results: list[HistoricalBackfillInstrumentResult] = []
        for instrument in plan.instruments:
            result = await self._run_instrument(plan=plan, instrument=instrument)
            results.append(result)
            self._session.flush()
        final = HistoricalBackfillResult(plan=plan, instruments=tuple(results), dry_run=False)
        log_event(
            logger=LOGGER,
            event_type="historical_candle_backfill_completed",
            component="market_data.historical_backfill",
            raw_candles_written=final.raw_candles_written,
            passed_quality=final.passed_quality,
            instrument_count=len(final.instruments),
        )
        return final

    async def _run_instrument(
        self,
        *,
        plan: HistoricalBackfillPlan,
        instrument: InstrumentRef,
    ) -> HistoricalBackfillInstrumentResult:
        chunks = tuple(chunk for chunk in plan.chunks if chunk.instrument == instrument)
        raw_written = 0
        raw_existing = 0
        raw_fetched = 0
        derived_written: dict[str, int] = {
            timeframe.value: 0 for timeframe in plan.derived_intervals
        }
        derived_existing: dict[str, int] = {
            timeframe.value: 0 for timeframe in plan.derived_intervals
        }
        candles_for_quality: list[Candle] = []
        bar_engine = BarEngine(target_timeframes=plan.derived_intervals)

        for chunk in chunks:
            response = await self._broker_gateway.get_candles(
                CandleRequest(
                    instrument=chunk.instrument,
                    interval=chunk.raw_interval.value,
                    from_=chunk.from_ts_utc,
                    to=chunk.to_ts_utc,
                )
            )
            for payload in _iter_candle_payloads(response.data):
                raw_fetched += 1
                candle = _candle_from_payload(
                    payload,
                    received_at=chunk.to_ts_utc,
                    instrument=instrument,
                    raw_interval=plan.raw_interval,
                )
                candles_for_quality.append(candle)
                if not candle.is_closed:
                    continue
                context = _session_context_for_candle(candle)
                was_existing = _candle_exists(
                    self._market_repository,
                    instrument_id=candle.instrument_id,
                    timeframe=candle.timeframe.value,
                    open_ts_utc=candle.open_ts_utc,
                )
                self._store.save_candle(candle=candle, context=context)
                if was_existing:
                    raw_existing += 1
                else:
                    raw_written += 1

                for bar in bar_engine.on_candle(candle):
                    bar_context = _session_context_for_candle(bar.as_candle())
                    bar_exists = _candle_exists(
                        self._market_repository,
                        instrument_id=bar.instrument_id,
                        timeframe=bar.timeframe.value,
                        open_ts_utc=bar.open_ts_utc,
                    )
                    self._store.save_bar(bar=bar, context=bar_context)
                    if bar_exists:
                        derived_existing[bar.timeframe.value] += 1
                    else:
                        derived_written[bar.timeframe.value] += 1

        quality = _quality_summary(candles_for_quality, raw_existing)
        return HistoricalBackfillInstrumentResult(
            instrument=instrument,
            requested_chunks=len(chunks),
            raw_candles_fetched=raw_fetched,
            raw_candles_written=raw_written,
            raw_candles_existing=raw_existing,
            derived_bars_written=derived_written,
            derived_bars_existing=derived_existing,
            quality=quality,
        )

    async def _resolve_instruments(
        self,
        config: HistoricalBackfillConfig,
    ) -> tuple[InstrumentRef, ...]:
        tickers = config.normalized_instruments()
        from_registry = self._load_registry_instruments(tickers)
        missing_tickers = tuple(ticker for ticker in tickers if ticker not in from_registry)
        if missing_tickers:
            requested = tuple(
                InstrumentRef(
                    instrument_id=f"{config.exchange}:{ticker}",
                    ticker=ticker,
                    class_code=config.class_code,
                )
                for ticker in missing_tickers
            )
            resolver = InstrumentResolverService(
                broker_gateway=self._broker_gateway,
                session=self._session,
                launch_policy=self._launch_policy,
                exchange=config.exchange,
            )
            resolved = await resolver.resolve_startup_instruments(requested)
            for instrument in resolved:
                from_registry[_ticker_for(instrument)] = instrument
        instruments = tuple(from_registry[ticker] for ticker in tickers)
        if self._launch_policy.mode is not RuntimeMode.HISTORICAL_REPLAY:
            placeholders = [
                instrument.instrument_id
                for instrument in instruments
                if _looks_like_placeholder(instrument)
            ]
            if placeholders:
                msg = "historical backfill refuses placeholder instrument_uid: " + ", ".join(
                    placeholders
                )
                raise RuntimeError(msg)
        return instruments

    def _load_registry_instruments(self, tickers: tuple[str, ...]) -> dict[str, InstrumentRef]:
        repository = InstrumentRepository(self._session)
        result: dict[str, InstrumentRef] = {}
        for ticker in tickers:
            row = repository.get_by_ticker(ticker)
            if row is not None and row.is_enabled:
                result[ticker] = _instrument_from_registry(row)
        return result


def default_backfill_window(
    *,
    to_date: date | None,
    from_date: date | None,
    lookback_days: int,
) -> tuple[datetime, datetime]:
    """Build an inclusive date window as UTC timestamps."""

    local_to = to_date or datetime.now(tz=MSK).date()
    to_ts = datetime.combine(local_to + timedelta(days=1), time.min, tzinfo=MSK).astimezone(UTC)
    if from_date is None:
        from_ts = to_ts - timedelta(days=lookback_days)
    else:
        from_ts = datetime.combine(from_date, time.min, tzinfo=MSK).astimezone(UTC)
    return ensure_utc(from_ts), ensure_utc(to_ts)


def config_from_strings(
    *,
    instruments: str,
    raw_interval: str,
    derive: str,
    lookback_days: int,
    chunk_days: int,
    strategy_id: str,
    dry_run: bool,
    runtime_mode: str = RuntimeMode.HISTORICAL_REPLAY.value,
) -> HistoricalBackfillConfig:
    """Parse CLI strings into a typed config."""

    return HistoricalBackfillConfig(
        instruments=tuple(item.strip().upper() for item in instruments.split(",") if item.strip()),
        raw_interval=parse_timeframe(raw_interval),
        derived_intervals=tuple(
            parse_timeframe(item.strip()) for item in derive.split(",") if item.strip()
        ),
        lookback_days=lookback_days,
        chunk_days=chunk_days,
        strategy_id=strategy_id,
        dry_run=dry_run,
        runtime_mode=runtime_mode,
    )


def _iter_candle_payloads(data: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    candles = data.get("candles", ())
    if not isinstance(candles, Iterable):
        return ()
    return (item for item in candles if isinstance(item, Mapping))


def _candle_from_payload(
    payload: Mapping[str, object],
    *,
    received_at: datetime,
    instrument: InstrumentRef,
    raw_interval: Timeframe,
) -> Candle:
    normalized = dict(payload)
    normalized["instrument_id"] = instrument.instrument_id
    normalized.setdefault("instrument_uid", instrument.instrument_uid)
    normalized.setdefault("ticker", instrument.ticker)
    normalized.setdefault("class_code", instrument.class_code)
    normalized.setdefault("timeframe", raw_interval.value)
    normalized.setdefault("source", "tbank_historical_backfill")
    return candle_from_mapping(normalized, received_at=received_at)


def _session_context_for_candle(candle: Candle) -> SessionEventContext:
    exchange_ts = candle.exchange_open_ts.astimezone(MSK)
    session_type = _session_type_for_exchange_ts(exchange_ts)
    micro_session_id = (
        f"{exchange_ts.date().isoformat()}:{session_type.value}:"
        f"{exchange_ts.hour:02d}00"
    )
    return SessionEventContext(
        calendar_date=exchange_ts.date(),
        trading_date=exchange_ts.date(),
        session_type=session_type,
        session_phase=(
            SessionPhase.CLOSED
            if session_type is SessionType.WEEKEND
            else SessionPhase.CONTINUOUS_TRADING
        ),
        micro_session_id=micro_session_id,
        broker_trading_status="historical_backfill",
    )


def _session_type_for_exchange_ts(exchange_ts: datetime) -> SessionType:
    if exchange_ts.weekday() >= 5:
        return SessionType.WEEKEND
    local_time = exchange_ts.time()
    if local_time < time(10, 0):
        return SessionType.WEEKDAY_MORNING
    if local_time < time(19, 0):
        return SessionType.WEEKDAY_MAIN
    return SessionType.WEEKDAY_EVENING


def _chunk_range(
    *,
    instrument: InstrumentRef,
    from_ts_utc: datetime,
    to_ts_utc: datetime,
    raw_interval: Timeframe,
    chunk_days: int,
) -> Iterable[HistoricalBackfillChunk]:
    if chunk_days < 1:
        msg = "chunk_days must be >= 1"
        raise ValueError(msg)
    cursor = ensure_utc(from_ts_utc)
    end = ensure_utc(to_ts_utc)
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        yield HistoricalBackfillChunk(
            instrument=instrument,
            from_ts_utc=cursor,
            to_ts_utc=chunk_end,
            raw_interval=raw_interval,
        )
        cursor = chunk_end


def _quality_summary(
    candles: list[Candle],
    duplicate_count: int,
) -> HistoricalBackfillQualitySummary:
    if not candles:
        return HistoricalBackfillQualitySummary(duplicate_raw_candles=duplicate_count)
    ordered = sorted(candles, key=lambda item: item.open_ts_utc)
    invalid_price_count = sum(1 for candle in ordered if not _prices_are_valid(candle))
    closed_count = sum(1 for candle in ordered if candle.is_closed)
    incomplete_count = len(ordered) - closed_count
    gap_count = 0
    previous: Candle | None = None
    for candle in ordered:
        if previous is not None and ensure_utc(candle.open_ts_utc) > ensure_utc(
            previous.close_ts_utc
        ):
            gap_count += 1
        previous = candle
    return HistoricalBackfillQualitySummary(
        raw_candles_seen=len(ordered),
        raw_candles_closed=closed_count,
        raw_candles_incomplete=incomplete_count,
        duplicate_raw_candles=duplicate_count,
        invalid_price_candles=invalid_price_count,
        gap_count=gap_count,
        first_open_ts_utc=ensure_utc(ordered[0].open_ts_utc),
        last_close_ts_utc=ensure_utc(ordered[-1].close_ts_utc),
    )


def _prices_are_valid(candle: Candle) -> bool:
    if min(candle.open_price, candle.high_price, candle.low_price, candle.close_price) <= Decimal(
        "0"
    ):
        return False
    return candle.low_price <= candle.open_price <= candle.high_price and (
        candle.low_price <= candle.close_price <= candle.high_price
    )


def _candle_exists(
    repository: MarketDataRepository,
    *,
    instrument_id: str,
    timeframe: str,
    open_ts_utc: datetime,
) -> bool:
    return (
        repository.get_candle(
            instrument_id=instrument_id,
            timeframe=timeframe,
            open_ts_utc=ensure_utc(open_ts_utc),
        )
        is not None
    )


def _instrument_from_registry(row: InstrumentRegistry) -> InstrumentRef:
    return InstrumentRef(
        instrument_id=row.instrument_id,
        instrument_uid=row.instrument_uid,
        class_code=row.class_code,
        ticker=row.ticker,
    )


def _ticker_for(instrument: InstrumentRef) -> str:
    if instrument.ticker:
        return instrument.ticker.upper()
    return instrument.instrument_id.rsplit(":", 1)[-1].upper()


def _looks_like_placeholder(instrument: InstrumentRef) -> bool:
    value = (instrument.instrument_uid or instrument.instrument_id).lower()
    return "placeholder" in value


def count_market_candles(
    session: Session,
    *,
    from_ts_utc: datetime,
    to_ts_utc: datetime,
    instruments: tuple[InstrumentRef, ...],
    timeframes: tuple[Timeframe, ...],
) -> int:
    """Return rows in market_candle for a backfill range, useful for CLI checks."""

    instrument_ids = [instrument.instrument_id for instrument in instruments]
    timeframe_values = [timeframe.value for timeframe in timeframes]
    stmt = select(MarketCandle).where(
        MarketCandle.instrument_id.in_(instrument_ids),
        MarketCandle.timeframe.in_(timeframe_values),
        MarketCandle.open_ts_utc >= ensure_utc(from_ts_utc),
        MarketCandle.open_ts_utc < ensure_utc(to_ts_utc),
    )
    return len(session.execute(stmt).scalars().all())
