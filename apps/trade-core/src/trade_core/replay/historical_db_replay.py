"""DB-backed historical replay from persisted market_candle bars."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from trade_core.broker_gateway import BrokerGateway, InstrumentRef
from trade_core.market_data import Bar, FeedFreshness, MarketState, PriceLevel, Timeframe
from trade_core.market_data.events import parse_timeframe
from trade_core.session import SessionSnapshot
from trade_core.strategy import (
    ConfigDrivenStrategyConfig,
    ConfigDrivenStrategyEngine,
    DefaultExecutionEngine,
    DefaultRiskEngine,
    OrderIntentRequest,
    PortfolioSnapshot,
    RiskAssessmentInput,
    RiskLimits,
    SqlAlchemyStrategyEventStore,
    StrategyEvaluationContext,
    StrategyState,
)
from trading_common import LaunchModePolicy, RuntimeMode
from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    CounterfactualResult,
    InstrumentRegistry,
    MarketCandle,
    MarketContextSnapshot,
    OrderIntent,
    OrderStateEvent,
    RiskEvent,
    SignalCandidate,
    StrategyConfig,
    StrategyStateEvent,
)
from trading_common.db.repositories import (
    BlockerEventRepository,
    CandidateStageResultRepository,
    MarketContextSnapshotRepository,
    OrderRepository,
    RiskEventRepository,
    SignalCandidateRepository,
    StrategyStateEventRepository,
)
from trading_common.enums import SessionPhase, SessionType
from trading_common.observability import DomainEventType

JsonPayload = dict[str, Any]
SOURCE = "historical_db_replay"


@dataclass(frozen=True, slots=True)
class HistoricalDbReplayConfig:
    from_date: date
    to_date: date
    instruments: tuple[str, ...]
    timeframes: tuple[Timeframe, ...]
    strategy_id: str = "baseline"
    strategy_version: int | str = "latest"
    dry_run: bool = False
    reset_derived_events: bool = False
    max_days: int | None = None


@dataclass(frozen=True, slots=True)
class ReplayInstrumentResult:
    instrument_id: str
    timeframe: str
    bars_processed: int = 0
    candidates_created: int = 0
    blockers_created: int = 0
    order_intents_created: int = 0
    pseudo_orders_created: int = 0
    risk_events_created: int = 0
    market_context_snapshots_created: int = 0
    skipped_existing_events: int = 0

    def as_payload(self) -> JsonPayload:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "bars_processed": self.bars_processed,
            "candidates_created": self.candidates_created,
            "blockers_created": self.blockers_created,
            "order_intents_created": self.order_intents_created,
            "pseudo_orders_created": self.pseudo_orders_created,
            "risk_events_created": self.risk_events_created,
            "market_context_snapshots_created": self.market_context_snapshots_created,
            "skipped_existing_events": self.skipped_existing_events,
        }


@dataclass(frozen=True, slots=True)
class ReplayDayResult:
    trading_date: date
    bars_processed: int = 0
    candidates_created: int = 0
    blockers_created: int = 0
    order_intents_created: int = 0
    pseudo_orders_created: int = 0
    risk_events_created: int = 0
    market_context_snapshots_created: int = 0
    skipped_existing_events: int = 0

    def as_payload(self) -> JsonPayload:
        return {
            "trading_date": self.trading_date.isoformat(),
            "bars_processed": self.bars_processed,
            "candidates_created": self.candidates_created,
            "blockers_created": self.blockers_created,
            "order_intents_created": self.order_intents_created,
            "pseudo_orders_created": self.pseudo_orders_created,
            "risk_events_created": self.risk_events_created,
            "market_context_snapshots_created": self.market_context_snapshots_created,
            "skipped_existing_events": self.skipped_existing_events,
        }


@dataclass(frozen=True, slots=True)
class HistoricalDbReplayResult:
    trading_days_processed: int
    instruments_processed: int
    bars_processed: int
    candidates_created: int
    blockers_created: int
    order_intents_created: int
    pseudo_orders_created: int
    risk_events_created: int
    market_context_snapshots_created: int
    skipped_existing_events: int
    deterministic_fingerprint: str
    real_orders_disabled: bool = True
    dry_run: bool = False
    days: tuple[ReplayDayResult, ...] = ()
    instruments: tuple[ReplayInstrumentResult, ...] = ()

    def as_payload(self) -> JsonPayload:
        return {
            "trading_days_processed": self.trading_days_processed,
            "instruments_processed": self.instruments_processed,
            "bars_processed": self.bars_processed,
            "candidates_created": self.candidates_created,
            "blockers_created": self.blockers_created,
            "order_intents_created": self.order_intents_created,
            "pseudo_orders_created": self.pseudo_orders_created,
            "risk_events_created": self.risk_events_created,
            "market_context_snapshots_created": self.market_context_snapshots_created,
            "skipped_existing_events": self.skipped_existing_events,
            "deterministic_fingerprint": self.deterministic_fingerprint,
            "real_orders_disabled": self.real_orders_disabled,
            "dry_run": self.dry_run,
            "days": [item.as_payload() for item in self.days],
            "instruments": [item.as_payload() for item in self.instruments],
            "source": SOURCE,
        }


class HistoricalDbReplayService:
    """Run historical bars through the production strategy/risk/execution stack."""

    def __init__(self, session: Session, broker_gateway: BrokerGateway | None = None) -> None:
        self._session = session
        self._broker_gateway = cast(BrokerGateway, broker_gateway or _NoRealOrdersBrokerGateway())
        self._launch_policy = LaunchModePolicy.from_mode(RuntimeMode.HISTORICAL_REPLAY)

    async def run(self, config: HistoricalDbReplayConfig) -> HistoricalDbReplayResult:
        if config.reset_derived_events and not config.dry_run:
            self.reset_generated_events(config)
        strategy_version = self._strategy_version(config)
        strategy_config = _config_for_replay(config=config, strategy_version=strategy_version)
        strategy_engine = ConfigDrivenStrategyEngine(strategy_config)
        risk_limits = RiskLimits.from_strategy_config(strategy_config)
        risk_engine = DefaultRiskEngine()
        order_repository = OrderRepository(self._session)
        execution_engine = DefaultExecutionEngine(
            broker_gateway=self._broker_gateway,
            orders=order_repository,
            launch_policy=self._launch_policy,
        )
        event_store = SqlAlchemyStrategyEventStore(
            candidates=SignalCandidateRepository(self._session),
            blockers=BlockerEventRepository(self._session),
            risk_events=RiskEventRepository(self._session),
            state_events=StrategyStateEventRepository(self._session),
            candidate_stages=CandidateStageResultRepository(self._session),
            market_contexts=MarketContextSnapshotRepository(self._session),
        )
        bars = self._load_bars(config)
        counters: dict[str, int] = defaultdict_ints()
        by_day: dict[date, dict[str, int]] = {}
        by_instrument: dict[tuple[str, str], dict[str, int]] = {}
        current_state_by_instrument: dict[str, StrategyState] = {}
        latest_bars_by_instrument: dict[str, dict[Timeframe, Bar]] = {}
        fingerprint_parts: list[str] = []

        for row in bars:
            key = (row.instrument_id, row.timeframe)
            day_counts = by_day.setdefault(row.trading_date, defaultdict_ints())
            instrument_counts = by_instrument.setdefault(key, defaultdict_ints())
            _bump(counters, day_counts, instrument_counts, "bars_processed")
            bar = _bar_from_row(row)
            latest_bars = latest_bars_by_instrument.setdefault(row.instrument_id, {})
            latest_bars[bar.timeframe] = bar
            current_state = current_state_by_instrument.get(
                row.instrument_id,
                StrategyState.IDLE,
            )
            snapshot = _snapshot_from_row(row)
            instrument = self._instrument_ref(row.instrument_id)
            market_state = _synthetic_market_state(row)
            context = StrategyEvaluationContext(
                instrument=instrument,
                session_snapshot=snapshot,
                latest_closed_bars=dict(latest_bars),
                market_state=market_state,
                current_state=current_state,
                now=row.close_ts_utc,
            )
            decision = strategy_engine.evaluate(context)
            current_state_by_instrument[row.instrument_id] = decision.next_state
            if config.dry_run:
                fingerprint_parts.append(f"{row.instrument_id}:{row.timeframe}:{row.close_ts_utc}")
                continue
            event_store.record_state_transition(
                snapshot=snapshot,
                strategy_id=config.strategy_id,
                strategy_version=strategy_version,
                previous_state=decision.previous_state,
                new_state=decision.next_state,
                event_type=DomainEventType.STRATEGY_STATE_CHANGED.value,
                reason_code=decision.reason_code,
                instrument_id=row.instrument_id,
                payload={**decision.decision_payload, "source": SOURCE},
                ts_utc=row.close_ts_utc,
            )
            for candidate in decision.candidates:
                replay_fingerprint = deterministic_candidate_fingerprint(
                    strategy_id=config.strategy_id,
                    strategy_version=strategy_version,
                    instrument_id=row.instrument_id,
                    timeframe=row.timeframe,
                    bar_close_ts=row.close_ts_utc,
                    side=candidate.side.value,
                    action=candidate.action.value,
                )
                fingerprint_parts.append(replay_fingerprint)
                if _candidate_exists(self._session, replay_fingerprint):
                    _bump(counters, day_counts, instrument_counts, "skipped_existing_events")
                    continue
                replay_candidate = replace(
                    candidate,
                    signal_fingerprint=replay_fingerprint,
                    condition_payload={
                        **candidate.condition_payload,
                        "source": SOURCE,
                        "replay_run_fingerprint": replay_fingerprint,
                        "replay_period_from": config.from_date.isoformat(),
                        "replay_period_to": config.to_date.isoformat(),
                        "market_candle_id": str(row.market_candle_id),
                    },
                )
                persisted = event_store.record_candidate(
                    decision=replay_candidate,
                    snapshot=snapshot,
                    market_state=market_state,
                    ts_utc=row.close_ts_utc,
                )
                _bump(counters, day_counts, instrument_counts, "candidates_created")
                _bump(counters, day_counts, instrument_counts, "market_context_snapshots_created")
                replay_candidate = replace(replay_candidate, candidate_id=persisted.candidate_id)
                risk_decision = risk_engine.evaluate(
                    RiskAssessmentInput(
                        candidate=replay_candidate,
                        session_snapshot=snapshot,
                        market_state=market_state,
                        limits=risk_limits,
                        portfolio=PortfolioSnapshot(),
                    )
                )
                blockers = event_store.record_blockers(
                    candidate=persisted,
                    decision=risk_decision,
                    market_state=market_state,
                    ts_utc=row.close_ts_utc,
                )
                risk_events = event_store.record_risk_events(
                    candidate=persisted,
                    decision=risk_decision,
                    ts_utc=row.close_ts_utc,
                )
                _add(counters, day_counts, instrument_counts, "blockers_created", len(blockers))
                _add(
                    counters,
                    day_counts,
                    instrument_counts,
                    "risk_events_created",
                    len(risk_events),
                )
                if not risk_decision.allowed:
                    continue
                intent = execution_engine.create_order_intent(
                    OrderIntentRequest(
                        candidate=replay_candidate,
                        session_snapshot=snapshot,
                        account_id="historical-replay",
                        created_at=row.close_ts_utc,
                    )
                )
                intent.intent_payload = {
                    **intent.intent_payload,
                    "source": SOURCE,
                    "replay_run_fingerprint": replay_fingerprint,
                    "real_orders_disabled": True,
                }
                _bump(counters, day_counts, instrument_counts, "order_intents_created")
                lifecycle = await execution_engine.post_order(intent)
                if lifecycle.broker_status == "pseudo_posted":
                    _bump(counters, day_counts, instrument_counts, "pseudo_orders_created")

        deterministic = _hash_parts(
            config.strategy_id,
            str(strategy_version),
            config.from_date.isoformat(),
            config.to_date.isoformat(),
            *sorted(fingerprint_parts),
        )
        day_results = tuple(
            ReplayDayResult(trading_date=day, **counts)
            for day, counts in sorted(by_day.items())
        )
        instrument_results = tuple(
            ReplayInstrumentResult(instrument_id=key[0], timeframe=key[1], **counts)
            for key, counts in sorted(by_instrument.items())
        )
        return HistoricalDbReplayResult(
            trading_days_processed=len(by_day),
            instruments_processed=len({instrument for instrument, _timeframe in by_instrument}),
            bars_processed=counters["bars_processed"],
            candidates_created=counters["candidates_created"],
            blockers_created=counters["blockers_created"],
            order_intents_created=counters["order_intents_created"],
            pseudo_orders_created=counters["pseudo_orders_created"],
            risk_events_created=counters["risk_events_created"],
            market_context_snapshots_created=counters["market_context_snapshots_created"],
            skipped_existing_events=counters["skipped_existing_events"],
            deterministic_fingerprint=deterministic,
            dry_run=config.dry_run,
            days=day_results,
            instruments=instrument_results,
        )

    def reset_generated_events(self, config: HistoricalDbReplayConfig) -> int:
        candidates = self._replay_candidates(config)
        candidate_ids = [candidate.candidate_id for candidate in candidates]
        order_intents = (
            list(
                self._session.execute(
                    select(OrderIntent).where(OrderIntent.candidate_id.in_(candidate_ids))
                ).scalars()
            )
            if candidate_ids
            else []
        )
        order_intent_ids = [intent.order_intent_id for intent in order_intents]
        request_order_ids = [intent.request_order_id for intent in order_intents]
        deleted = 0
        for model, column, values in (
            (CounterfactualResult, CounterfactualResult.candidate_id, candidate_ids),
            (OrderStateEvent, OrderStateEvent.order_intent_id, order_intent_ids),
            (BrokerOrder, BrokerOrder.request_order_id, request_order_ids),
            (OrderIntent, OrderIntent.order_intent_id, order_intent_ids),
            (RiskEvent, RiskEvent.candidate_id, candidate_ids),
            (BlockerEvent, BlockerEvent.candidate_id, candidate_ids),
            (CandidateStageResult, CandidateStageResult.candidate_id, candidate_ids),
            (MarketContextSnapshot, MarketContextSnapshot.candidate_id, candidate_ids),
            (SignalCandidate, SignalCandidate.candidate_id, candidate_ids),
        ):
            if not values:
                continue
            result = self._session.execute(delete(model).where(column.in_(values)))
            deleted += int(getattr(result, "rowcount", 0) or 0)
        state_events = self._session.execute(
            select(StrategyStateEvent).where(
                StrategyStateEvent.trading_date >= config.from_date,
                StrategyStateEvent.trading_date <= config.to_date,
                StrategyStateEvent.strategy_id == config.strategy_id,
            )
        ).scalars()
        for event in state_events:
            if event.state_payload.get("source") != SOURCE:
                continue
            self._session.delete(event)
            deleted += 1
        return deleted

    def _replay_candidates(self, config: HistoricalDbReplayConfig) -> list[SignalCandidate]:
        stmt = select(SignalCandidate).where(
            SignalCandidate.trading_date >= config.from_date,
            SignalCandidate.trading_date <= config.to_date,
            SignalCandidate.strategy_id == config.strategy_id,
        )
        rows = list(self._session.execute(stmt).scalars())
        return [
            row
            for row in rows
            if _payload_source(row.signal_payload) == SOURCE
            or row.signal_fingerprint and row.signal_fingerprint.endswith(":historical_db_replay")
        ]

    def _load_bars(self, config: HistoricalDbReplayConfig) -> list[MarketCandle]:
        instrument_ids = self._resolve_instrument_ids(config.instruments)
        stmt = (
            select(MarketCandle)
            .where(
                MarketCandle.trading_date >= config.from_date,
                MarketCandle.trading_date <= config.to_date,
                MarketCandle.timeframe.in_([timeframe.value for timeframe in config.timeframes]),
                MarketCandle.is_closed.is_(True),
            )
            .order_by(MarketCandle.open_ts_utc, MarketCandle.instrument_id, MarketCandle.timeframe)
        )
        if instrument_ids:
            stmt = stmt.where(MarketCandle.instrument_id.in_(instrument_ids))
        if config.max_days is not None:
            allowed_dates = _date_values(config)[: config.max_days]
            stmt = stmt.where(MarketCandle.trading_date.in_(allowed_dates))
        return list(self._session.execute(stmt).scalars())

    def _resolve_instrument_ids(self, instruments: tuple[str, ...]) -> tuple[str, ...]:
        if not instruments:
            return tuple(
                self._session.execute(
                    select(MarketCandle.instrument_id).distinct().order_by(MarketCandle.instrument_id)
                ).scalars()
            )
        values: list[str] = []
        for raw in instruments:
            item = raw.strip()
            registry = self._session.execute(
                select(InstrumentRegistry).where(InstrumentRegistry.ticker == item.upper())
            ).scalars().first()
            if registry is not None:
                values.append(registry.instrument_id)
            elif ":" in item or item.startswith("safe-noop-"):
                values.append(item)
            else:
                values.append(f"MOEX:{item.upper()}")
        return tuple(dict.fromkeys(values))

    def _instrument_ref(self, instrument_id: str) -> InstrumentRef:
        row = self._session.get(InstrumentRegistry, instrument_id)
        if row is None:
            return InstrumentRef(instrument_id=instrument_id)
        return InstrumentRef(
            instrument_id=row.instrument_id,
            instrument_uid=row.instrument_uid,
            ticker=row.ticker,
            class_code=row.class_code,
        )

    def _strategy_version(self, config: HistoricalDbReplayConfig) -> int:
        if isinstance(config.strategy_version, int):
            return config.strategy_version
        if str(config.strategy_version) != "latest":
            return int(str(config.strategy_version))
        row = self._session.execute(
            select(StrategyConfig)
            .where(StrategyConfig.strategy_id == config.strategy_id)
            .order_by(StrategyConfig.version.desc())
        ).scalars().first()
        return int(row.version) if row is not None else 1


class _NoRealOrdersBrokerGateway:
    async def post_order(self, *_args: object, **_kwargs: object) -> object:
        msg = "historical replay must use pseudo-orders and never call BrokerGateway.post_order"
        raise AssertionError(msg)

    async def cancel_order(self, *_args: object, **_kwargs: object) -> object:
        msg = "historical replay must never call BrokerGateway.cancel_order"
        raise AssertionError(msg)


def deterministic_candidate_fingerprint(
    *,
    strategy_id: str,
    strategy_version: int,
    instrument_id: str,
    timeframe: str,
    bar_close_ts: datetime,
    side: str,
    action: str,
) -> str:
    return "|".join(
        (
            strategy_id,
            str(strategy_version),
            instrument_id,
            timeframe,
            bar_close_ts.isoformat(),
            side,
            action,
            SOURCE,
        )
    )


def _config_for_replay(
    *,
    config: HistoricalDbReplayConfig,
    strategy_version: int,
) -> ConfigDrivenStrategyConfig:
    base = ConfigDrivenStrategyConfig.conservative_default()
    selected = set(config.timeframes)
    templates = {
        session_type: replace(
            template,
            rules_by_timeframe={
                timeframe: rule
                for timeframe, rule in template.rules_by_timeframe.items()
                if timeframe in selected
            },
        )
        for session_type, template in base.session_templates.items()
    }
    return replace(
        base,
        strategy_id=config.strategy_id,
        strategy_version=strategy_version,
        session_templates=templates,
    )


def default_replay_window(
    *,
    from_date: date | None,
    to_date: date | None,
    lookback_days: int,
) -> tuple[date, date]:
    end = to_date or datetime.now(tz=UTC).date()
    start = from_date or (end - timedelta(days=lookback_days - 1))
    if start > end:
        msg = "from_date must be <= to_date"
        raise ValueError(msg)
    return start, end


def _bar_from_row(row: MarketCandle) -> Bar:
    raw_source_count = row.candle_payload.get("source_candle_count", 1)
    return Bar(
        instrument_id=row.instrument_id,
        timeframe=parse_timeframe(row.timeframe),
        open_ts_utc=row.open_ts_utc,
        close_ts_utc=row.close_ts_utc,
        exchange_open_ts=row.exchange_open_ts,
        exchange_close_ts=row.exchange_close_ts,
        open_price=row.open_price,
        high_price=row.high_price,
        low_price=row.low_price,
        close_price=row.close_price,
        volume_lots=row.volume_lots,
        source_candle_count=int(str(raw_source_count)),
        is_closed=row.is_closed,
    )


def _snapshot_from_row(row: MarketCandle) -> SessionSnapshot:
    session_phase = SessionPhase(row.session_phase)
    return SessionSnapshot(
        observed_at=row.close_ts_utc,
        calendar_date=row.calendar_date,
        trading_date=row.trading_date,
        session_type=SessionType(row.session_type),
        session_phase=session_phase,
        broker_phase=session_phase,
        broker_trading_status=row.broker_trading_status,
        broker_api_trade_available=session_phase is SessionPhase.CONTINUOUS_TRADING,
        schedule_phase=session_phase,
        schedule_window_start_at=None,
        schedule_window_end_at=None,
        micro_session_id=row.micro_session_id,
        is_trading_allowed=session_phase is SessionPhase.CONTINUOUS_TRADING,
        deny_reason_code=(
            None
            if session_phase is SessionPhase.CONTINUOUS_TRADING
            else "session_forbidden"
        ),
        status_mismatch=False,
    )


def _synthetic_market_state(row: MarketCandle) -> MarketState:
    price = row.close_price
    level = PriceLevel(price=price, quantity_lots=Decimal("1"))
    return MarketState(
        instrument_id=row.instrument_id,
        best_bid=level,
        best_ask=level,
        mid_price=price,
        spread_abs=Decimal("0"),
        spread_bps=Decimal("0"),
        bid_depth_lots=Decimal("1"),
        ask_depth_lots=Decimal("1"),
        book_imbalance=Decimal("0"),
        market_quality_score=Decimal("1"),
        feed_freshness=FeedFreshness(age_ms=0, is_stale=False),
    )


def _candidate_exists(session: Session, fingerprint: str) -> bool:
    return (
        session.execute(
            select(SignalCandidate).where(SignalCandidate.signal_fingerprint == fingerprint)
        ).scalars().first()
        is not None
    )


def _payload_source(payload: JsonPayload) -> object:
    if payload.get("source") == SOURCE:
        return SOURCE
    condition_payload = payload.get("condition_payload")
    if isinstance(condition_payload, dict):
        return condition_payload.get("source")
    return None


def _hash_parts(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def defaultdict_ints() -> dict[str, int]:
    return {
        "bars_processed": 0,
        "candidates_created": 0,
        "blockers_created": 0,
        "order_intents_created": 0,
        "pseudo_orders_created": 0,
        "risk_events_created": 0,
        "market_context_snapshots_created": 0,
        "skipped_existing_events": 0,
    }


def _bump(*args: object) -> None:
    *targets, key = args
    _add(*targets, key, 1)


def _add(*args: object) -> None:
    *targets, key, value = args
    if not isinstance(value, int):
        value = int(str(value))
    for target in targets:
        typed_target = target
        if not isinstance(typed_target, dict):
            msg = "counter target must be a dict"
            raise TypeError(msg)
        typed_target[str(key)] = typed_target.get(str(key), 0) + value


def _date_values(config: HistoricalDbReplayConfig) -> list[date]:
    values: list[date] = []
    cursor = config.from_date
    while cursor <= config.to_date:
        values.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return values
