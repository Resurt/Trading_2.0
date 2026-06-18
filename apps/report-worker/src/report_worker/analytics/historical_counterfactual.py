"""Historical replay counterfactual rebuild from domain tables and market_candle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from report_worker.analytics.calculations import analyze_counterfactual
from report_worker.analytics.models import (
    AnalyticsAssumptions,
    CounterfactualAnalysis,
    CounterfactualSource,
    PricePathPoint,
)
from trading_common.db.models import (
    BlockerEvent,
    CounterfactualResult,
    InstrumentRegistry,
    MarketCandle,
    OrderIntent,
    SignalCandidate,
)

JsonPayload = dict[str, Any]
SOURCE = "historical_counterfactual_rebuild"
REPLAY_SOURCE = "historical_db_replay"


@dataclass(frozen=True, slots=True)
class HistoricalCounterfactualConfig:
    from_date: date
    to_date: date
    strategy_id: str
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    dry_run: bool = False
    force_rebuild: bool = False
    commission_bps_per_side: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("2")


@dataclass(frozen=True, slots=True)
class HistoricalCounterfactualResult:
    candidates_scanned: int
    results_created: int
    results_existing: int
    dry_run: bool
    force_rebuild: bool

    def as_payload(self) -> JsonPayload:
        return {
            "candidates_scanned": self.candidates_scanned,
            "counterfactual_results_built": self.results_created,
            "results_existing": self.results_existing,
            "dry_run": self.dry_run,
            "force_rebuild": self.force_rebuild,
            "source": SOURCE,
        }


class HistoricalCounterfactualService:
    """Build +5/+10/+15 counterfactuals for historical blocked/cancelled rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def rebuild(self, config: HistoricalCounterfactualConfig) -> HistoricalCounterfactualResult:
        candidates = self._candidate_sources(config)
        if config.force_rebuild and not config.dry_run:
            self._delete_existing(config, tuple(candidate.candidate_id for candidate in candidates))
        existing_ids = self._existing_candidate_ids()
        created = 0
        existing = 0
        assumptions = AnalyticsAssumptions(
            commission_bps_per_side=config.commission_bps_per_side,
            fee_bps=max(config.commission_bps_per_side * Decimal("2"), Decimal("10")),
            slippage_bps=config.slippage_bps,
        )
        for candidate in candidates:
            if candidate.candidate_id in existing_ids:
                existing += 1
                continue
            analysis = analyze_counterfactual(
                source=self._source_for(candidate),
                price_path=self._price_path(candidate),
                assumptions=assumptions,
            )
            if config.dry_run:
                created += 1
                continue
            self._session.add(self._row_from_analysis(candidate=candidate, analysis=analysis))
            created += 1
        return HistoricalCounterfactualResult(
            candidates_scanned=len(candidates),
            results_created=created,
            results_existing=existing,
            dry_run=config.dry_run,
            force_rebuild=config.force_rebuild,
        )

    def _candidate_sources(
        self,
        config: HistoricalCounterfactualConfig,
    ) -> list[SignalCandidate]:
        instrument_ids = self._resolve_instrument_ids(config.instruments)
        stmt = select(SignalCandidate).where(
            SignalCandidate.trading_date >= config.from_date,
            SignalCandidate.trading_date <= config.to_date,
            SignalCandidate.strategy_id == config.strategy_id,
        )
        if instrument_ids:
            stmt = stmt.where(SignalCandidate.instrument_id.in_(instrument_ids))
        if config.timeframes:
            stmt = stmt.where(SignalCandidate.timeframe.in_(config.timeframes))
        candidates = list(self._session.execute(stmt).scalars())
        return [
            candidate
            for candidate in candidates
            if _is_historical_replay_candidate(candidate)
            and (
                candidate.candidate_status in {"blocked", "cancelled", "rejected"}
                or self._has_cancelled_or_rejected_intent(candidate)
            )
        ]

    def _resolve_instrument_ids(self, instruments: tuple[str, ...]) -> tuple[str, ...]:
        resolved: list[str] = []
        for item in instruments:
            raw = item.strip()
            if not raw:
                continue
            registry = self._session.execute(
                select(InstrumentRegistry).where(InstrumentRegistry.ticker == raw.upper())
            ).scalars().first()
            if registry is not None:
                resolved.append(registry.instrument_id)
            elif ":" in raw:
                resolved.append(raw)
            else:
                resolved.append(f"MOEX:{raw.upper()}")
        return tuple(dict.fromkeys(resolved))

    def _has_cancelled_or_rejected_intent(self, candidate: SignalCandidate) -> bool:
        intent = self._session.execute(
            select(OrderIntent).where(OrderIntent.candidate_id == candidate.candidate_id)
        ).scalars().first()
        return bool(
            intent is not None
            and (
                intent.cancel_reason_code is not None
                or intent.reject_reason_code is not None
                or intent.status in {"cancelled", "rejected"}
            )
        )

    def _delete_existing(
        self,
        config: HistoricalCounterfactualConfig,
        candidate_ids: tuple[object, ...],
    ) -> None:
        if not candidate_ids:
            return
        rows = list(
            self._session.execute(
                select(CounterfactualResult).where(
                    CounterfactualResult.trading_date >= config.from_date,
                    CounterfactualResult.trading_date <= config.to_date,
                    CounterfactualResult.candidate_id.in_(candidate_ids),
                )
            ).scalars()
        )
        for row in rows:
            if row.result_payload.get("source") == SOURCE:
                self._session.delete(row)
        self._session.flush()

    def _existing_candidate_ids(self) -> set[object]:
        rows = self._session.execute(select(CounterfactualResult)).scalars()
        return {
            row.candidate_id
            for row in rows
            if row.result_payload.get("source") == SOURCE and row.candidate_id is not None
        }

    def _source_for(self, candidate: SignalCandidate) -> CounterfactualSource:
        intent = self._session.execute(
            select(OrderIntent).where(OrderIntent.candidate_id == candidate.candidate_id)
        ).scalars().first()
        blocker = _final_blocker(self._session, candidate)
        source_event_type = "blocked"
        if intent is not None and intent.cancel_reason_code:
            source_event_type = "cancelled"
        elif intent is not None and intent.reject_reason_code:
            source_event_type = "rejected"
        return CounterfactualSource(
            candidate_id=candidate.candidate_id,
            order_intent_id=getattr(intent, "order_intent_id", None),
            source_event_type=source_event_type,
            instrument_id=candidate.instrument_id,
            strategy_id=candidate.strategy_id,
            side=candidate.side,
            event_ts=candidate.ts_utc,
            entry_price=candidate.last_price or candidate.mid_price or Decimal("0"),
            lot_qty=int(getattr(intent, "lot_qty", 1) if intent is not None else 1),
            blocker_code=getattr(blocker, "reason_code", None),
            cancel_reason_code=getattr(intent, "cancel_reason_code", None),
            timeframe=candidate.timeframe,
            strategy_version=candidate.strategy_version,
        )

    def _price_path(self, candidate: SignalCandidate) -> list[PricePathPoint]:
        candles = self._session.execute(
            select(MarketCandle)
            .where(
                MarketCandle.instrument_id == candidate.instrument_id,
                MarketCandle.timeframe == candidate.timeframe,
                MarketCandle.close_ts_utc > candidate.ts_utc,
                MarketCandle.close_ts_utc <= candidate.ts_utc + timedelta(minutes=15),
                MarketCandle.is_closed.is_(True),
            )
            .order_by(MarketCandle.close_ts_utc)
        ).scalars()
        return [
            PricePathPoint(
                ts_utc=candle.close_ts_utc,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
            )
            for candle in candles
        ]

    def _row_from_analysis(
        self,
        *,
        candidate: SignalCandidate,
        analysis: CounterfactualAnalysis,
    ) -> CounterfactualResult:
        window_5 = analysis.windows[5]
        window_10 = analysis.windows[10]
        window_15 = analysis.windows[15]
        payload = analysis.as_payload()
        payload.update(
            {
                "source": SOURCE,
                "replay_source": REPLAY_SOURCE,
                "candidate_fingerprint": candidate.signal_fingerprint,
                "would_profit": window_15.would_profit,
                "avoided_loss": bool(
                    window_15.net_pnl_bps is not None and window_15.net_pnl_bps < 0
                ),
                "missed_opportunity": bool(
                    window_15.net_pnl_bps is not None and window_15.net_pnl_bps > 0
                ),
            }
        )
        return CounterfactualResult(
            calendar_date=candidate.calendar_date,
            trading_date=candidate.trading_date,
            session_type=candidate.session_type,
            session_phase=candidate.session_phase,
            micro_session_id=candidate.micro_session_id,
            broker_trading_status=candidate.broker_trading_status,
            candidate_id=candidate.candidate_id,
            order_intent_id=analysis.source.order_intent_id,
            source_event_type=analysis.source.source_event_type,
            instrument_id=candidate.instrument_id,
            timeframe=candidate.timeframe,
            strategy_id=candidate.strategy_id,
            blocker_code=analysis.source.blocker_code,
            cancel_reason_code=analysis.source.cancel_reason_code,
            fee_bps_assumed=analysis.assumptions.fee_bps,
            slippage_bps_assumed=analysis.assumptions.slippage_bps,
            slippage_bp=analysis.assumptions.slippage_bps,
            pnl_gross=window_15.gross_pnl_rub,
            pnl_net=window_15.net_pnl_rub,
            mfe_5m_bps=window_5.mfe_bps,
            mae_5m_bps=window_5.mae_bps,
            mfe_10m_bps=window_10.mfe_bps,
            mae_10m_bps=window_10.mae_bps,
            mfe_15m_bps=window_15.mfe_bps,
            mae_15m_bps=window_15.mae_bps,
            would_profit_5m=window_5.would_profit,
            would_profit_10m=window_10.would_profit,
            would_profit_15m=window_15.would_profit,
            result_payload=payload,
            generated_at=datetime.now(tz=UTC),
        )


def default_counterfactual_window(
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


def _is_historical_replay_candidate(candidate: SignalCandidate) -> bool:
    payload = candidate.signal_payload or {}
    condition_payload = payload.get("condition_payload")
    return (
        payload.get("source") == REPLAY_SOURCE
        or (
            isinstance(condition_payload, dict)
            and condition_payload.get("source") == REPLAY_SOURCE
        )
    )


def _final_blocker(session: Session, candidate: SignalCandidate) -> BlockerEvent | None:
    return (
        session.execute(
            select(BlockerEvent)
            .where(
                BlockerEvent.candidate_id == candidate.candidate_id,
                BlockerEvent.passed.is_(False),
            )
            .order_by(BlockerEvent.is_final_blocker.desc(), BlockerEvent.gate_rank.desc())
        )
        .scalars()
        .first()
    )
