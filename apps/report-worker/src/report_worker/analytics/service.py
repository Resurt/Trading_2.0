"""SQLAlchemy-backed report analytics service."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html import escape
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from report_worker.analytics.calculations import (
    analyze_counterfactual,
    build_funnel_metrics,
    classify_day_regimes,
    counts_by,
    fill_ratio,
    realised_pnl_from_fills,
    state_time_distribution,
)
from report_worker.analytics.models import (
    AnalyticsAssumptions,
    AnalyticsFilters,
    CounterfactualAnalysis,
    CounterfactualSource,
    PricePathPoint,
)
from report_worker.analytics.read_models import (
    counterfactual_read_model,
    daily_report_read_model,
    hourly_report_read_model,
)
from trading_common.db.models import (
    AuditEvent,
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    CounterfactualResult,
    DailyReport,
    FillEvent,
    HourlyReport,
    MarketCandle,
    OrderIntent,
    OrderStateEvent,
    PositionSnapshot,
    RiskEvent,
    SessionRun,
    SignalCandidate,
    StrategyStateEvent,
)

JsonPayload = dict[str, object]
ZERO = Decimal("0")


class ReportAnalyticsService:
    """Builds hourly/daily reports and counterfactual results outside FastAPI."""

    def __init__(
        self,
        session: Session,
        *,
        assumptions: AnalyticsAssumptions | None = None,
    ) -> None:
        self._session = session
        self._assumptions = assumptions or AnalyticsAssumptions()

    def build_hourly_report(
        self,
        *,
        micro_session_id: str,
        strategy_id: str,
        force_rebuild: bool = True,
    ) -> HourlyReport:
        existing = self._session.execute(
            select(HourlyReport).where(
                HourlyReport.micro_session_id == micro_session_id,
                HourlyReport.strategy_id == strategy_id,
            )
        ).scalar_one_or_none()
        if existing is not None and not force_rebuild:
            return existing

        run = self._session.execute(
            select(SessionRun).where(
                SessionRun.micro_session_id == micro_session_id,
                SessionRun.strategy_id == strategy_id,
            )
        ).scalar_one_or_none()
        if run is None:
            msg = f"SessionRun not found: {micro_session_id} / {strategy_id}"
            raise LookupError(msg)

        candidates = self._list(
            select(SignalCandidate).where(
                SignalCandidate.micro_session_id == micro_session_id,
                SignalCandidate.strategy_id == strategy_id,
            )
        )
        blockers = self._list(
            select(BlockerEvent).where(
                BlockerEvent.micro_session_id == micro_session_id,
                BlockerEvent.strategy_id == strategy_id,
            )
        )
        intents = self._list(
            select(OrderIntent).where(
                OrderIntent.micro_session_id == micro_session_id,
                OrderIntent.strategy_id == strategy_id,
            )
        )
        broker_orders = self._list(
            select(BrokerOrder).where(BrokerOrder.micro_session_id == micro_session_id)
        )
        fills = self._list(select(FillEvent).where(FillEvent.micro_session_id == micro_session_id))
        risk_events = self._list(
            select(RiskEvent).where(RiskEvent.micro_session_id == micro_session_id)
        )
        positions = self._list(
            select(PositionSnapshot).where(PositionSnapshot.micro_session_id == micro_session_id)
        )
        audit_events = self._list(
            select(AuditEvent).where(AuditEvent.micro_session_id == micro_session_id)
        )

        posted_count = _posted_count(broker_orders)
        filled_count = len(fills)
        reject_count = _reject_count(broker_orders, intents)
        cancel_count = _cancel_count(broker_orders, intents)
        replace_count = sum(1 for intent in intents if intent.order_action == "replace")
        blocked_count = _blocked_count(candidates, blockers)
        commission = sum((fill.commission for fill in fills), ZERO)
        realised_pnl = realised_pnl_from_fills(
            (fill.side, fill.lot_qty, fill.price, fill.commission) for fill in fills
        )
        unrealised_pnl = _latest_unrealised_pnl(positions)
        latency_ms = _latency_values_ms(broker_orders)
        stale_incidents = sum(
            1
            for event in (*blockers, *risk_events)
            if getattr(event, "reason_code", None) == "stale_market_data"
        )
        reconnect_count = sum(1 for event in audit_events if "reconnect" in event.action.lower())
        broker_error_count = sum(
            1
            for event in audit_events
            if event.severity == "error" or "error" in event.action.lower()
        )

        report_payload: JsonPayload = {
            "format": "hourly_report_v1",
            "outputs": {"json": True, "html": True},
            "estimated_slippage": str(_estimated_slippage(fills)),
            "replace_count": replace_count,
            "posted_count": posted_count,
            "filled_count": filled_count,
            "broker_error_count": broker_error_count,
            "risk_blockers": counts_by(
                blocker.reason_code for blocker in blockers if not blocker.passed
            ),
            "stale_market_data_incidents": stale_incidents,
            "latency_ms": _distribution(latency_ms),
            "funnel": build_funnel_metrics(
                created=len(candidates),
                passed_gates=_passed_gates_count(candidates, []),
                blockers=blocked_count,
                order_intent=len(intents),
                posted=posted_count,
                filled=filled_count,
                exited=_exited_count(fills, []),
                profitable=sum(1 for fill in fills if fill.side == "sell"),
            ).as_payload(),
        }
        report_payload["html_output"] = _render_report_html(
            title=f"Hourly report {micro_session_id}",
            summary={
                "signals": len(candidates),
                "blocked": blocked_count,
                "posted": posted_count,
                "filled": filled_count,
                "realised_pnl": str(realised_pnl),
            },
        )

        self._session.execute(
            delete(HourlyReport).where(
                HourlyReport.micro_session_id == micro_session_id,
                HourlyReport.strategy_id == strategy_id,
            )
        )
        report = HourlyReport(
            calendar_date=run.calendar_date,
            trading_date=run.trading_date,
            session_type=run.session_type,
            session_phase=run.session_phase,
            micro_session_id=run.micro_session_id,
            broker_trading_status=run.broker_trading_status,
            run_id=run.run_id,
            strategy_id=strategy_id,
            instrument_id=None,
            started_at=run.started_at,
            ended_at=run.ended_at or datetime.now(tz=UTC),
            realised_pnl=realised_pnl,
            unrealised_pnl=unrealised_pnl,
            commission=commission,
            signal_count=len(candidates),
            entry_count=sum(1 for candidate in candidates if candidate.signal_type == "entry"),
            exit_count=sum(1 for candidate in candidates if candidate.signal_type == "exit"),
            blocked_count=blocked_count,
            reject_count=reject_count,
            cancel_count=cancel_count,
            reconnect_count=reconnect_count,
            risk_event_count=len(risk_events),
            fill_ratio=fill_ratio(filled=filled_count, posted=posted_count),
            report_payload=report_payload,
            generated_at=datetime.now(tz=UTC),
        )
        self._session.add(report)
        self._session.flush()
        return report

    def build_daily_report(
        self,
        *,
        trading_date: date,
        strategy_id: str,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        strategy_version: int | None = None,
        force_rebuild: bool = True,
    ) -> DailyReport:
        filters = AnalyticsFilters(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            strategy_version=strategy_version,
        )
        existing = self._existing_daily_report(filters)
        if existing is not None and not force_rebuild:
            return existing

        candidates = self._list(
            _apply_signal_candidate_filters(select(SignalCandidate), filters)
        )
        blockers = self._list(
            _apply_blocker_filters(select(BlockerEvent), filters)
        )
        intents = self._list(
            _apply_order_intent_filters(select(OrderIntent), filters)
        )
        broker_orders = self._list(
            _apply_broker_order_filters(select(BrokerOrder), filters)
        )
        fills = self._list(select(FillEvent).where(FillEvent.trading_date == trading_date))
        fills = _filter_rows(fills, filters)
        stage_results = self._list(
            _apply_candidate_stage_filters(select(CandidateStageResult), filters)
        )
        order_state_events = self._list(
            _apply_order_state_filters(select(OrderStateEvent), filters)
        )
        state_events = self._list(
            _apply_strategy_state_filters(select(StrategyStateEvent), filters)
        )
        counterfactuals = self._list(
            _apply_counterfactual_filters(select(CounterfactualResult), filters)
        )
        candles = self._list(
            _apply_market_candle_filters(select(MarketCandle), filters)
        )

        trend = classify_day_regimes(_candles_by_scope(candles))
        posted_count = _posted_count(broker_orders)
        filled_count = len(fills)
        blocked_count = _blocked_count(candidates, blockers)
        commission = sum((fill.commission for fill in fills), ZERO)
        pnl_gross = _sum_optional_decimal(fill.pnl_gross for fill in fills)
        pnl_net = _sum_optional_decimal(fill.pnl_net for fill in fills)
        slippage_bp = _average_optional_decimal(fill.slippage_bp for fill in fills)
        realised_pnl = realised_pnl_from_fills(
            (fill.side, fill.lot_qty, fill.price, fill.commission) for fill in fills
        )
        funnel = build_funnel_metrics(
            created=len(candidates),
            passed_gates=_passed_gates_count(candidates, stage_results),
            blockers=blocked_count,
            order_intent=len(intents),
            posted=posted_count,
            filled=filled_count,
            exited=_exited_count(fills, order_state_events),
            profitable=sum(1 for fill in fills if (fill.pnl_net or ZERO) > ZERO),
        ).as_payload()
        blocker_ranking = _blocker_ranking(blockers=blockers, counterfactuals=counterfactuals)
        canceled_order_analytics = _canceled_order_analytics(
            intents=intents,
            broker_orders=broker_orders,
            counterfactuals=counterfactuals,
        )
        report_payload: JsonPayload = {
            "format": "daily_report_v1",
            "outputs": {"json": True, "html": True},
            "filters": filters.as_payload(),
            "assumptions": self._assumptions.as_payload(),
            "trend": trend.as_payload(),
            "summary_by_session_type": _summary_by(candidates, key="session_type"),
            "summary_by_instrument": _summary_by(candidates, key="instrument_id"),
            "summary_by_timeframe": _summary_by(candidates, key="timeframe"),
            "blocker_ranking": blocker_ranking,
            "canceled_order_analytics": canceled_order_analytics,
            "execution_quality": {
                "posted_count": posted_count,
                "filled_count": filled_count,
                "reject_count": _reject_count(broker_orders, intents),
                "cancel_count": _cancel_count(broker_orders, intents),
                "replace_count": sum(1 for intent in intents if intent.order_action == "replace"),
                "fill_ratio": str(fill_ratio(filled=filled_count, posted=posted_count)),
            },
            "missed_opportunity_summary": _missed_opportunity_summary(counterfactuals),
            "strategy_state_time_distribution_seconds": state_time_distribution(
                (event.ts_utc, event.new_state) for event in state_events
            ),
            "funnel": funnel,
            "explainability": {
                "market_regime": (
                    "per instrument/timeframe first-open to last-close return plus "
                    "intraday range"
                ),
                "candidate_funnel": (
                    "created -> passed gates -> blocked/order_intent -> posted -> "
                    "filled -> exited"
                ),
                "blocker_false_positive_rate": (
                    "counterfactual 15m profitable count / counterfactual count"
                ),
                "gross_net": (
                    "gross is before costs; net subtracts configured commission "
                    "and slippage assumptions"
                ),
            },
        }
        report_payload["html_output"] = _render_report_html(
            title=f"Daily report {trading_date.isoformat()}",
            summary={
                "market_regime": trend.market_regime,
                "signals": len(candidates),
                "blocked": blocked_count,
                "posted": posted_count,
                "filled": filled_count,
                "realised_pnl": str(realised_pnl),
                "pnl_net": str(pnl_net),
            },
        )

        self._session.execute(
            _apply_daily_report_filters(delete(DailyReport), filters)
        )
        report = DailyReport(
            calendar_date=trading_date,
            trading_date=trading_date,
            session_type=session_type,
            session_phase=None,
            micro_session_id=None,
            broker_trading_status=None,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            market_regime=trend.market_regime,
            realised_pnl=realised_pnl,
            commission=commission,
            commission_gross=commission,
            commission_net=commission,
            slippage_bp=slippage_bp,
            pnl_gross=pnl_gross,
            pnl_net=pnl_net if pnl_net != ZERO else realised_pnl,
            signal_count=len(candidates),
            blocked_count=blocked_count,
            fill_ratio=fill_ratio(filled=filled_count, posted=posted_count),
            report_payload=report_payload,
            generated_at=datetime.now(tz=UTC),
        )
        self._session.add(report)
        self._session.flush()
        return report

    def rebuild_reports_for_date(
        self,
        *,
        trading_date: date,
        strategy_id: str,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        strategy_version: int | None = None,
        force_rebuild: bool = True,
        include_counterfactual: bool = True,
    ) -> DailyReport:
        runs = self._list(
            _apply_session_run_filters(
                select(SessionRun),
                AnalyticsFilters(
                    trading_date=trading_date,
                    strategy_id=strategy_id,
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    session_type=session_type,
                    strategy_version=strategy_version,
                ),
            )
        )
        for run in runs:
            self.build_hourly_report(
                micro_session_id=run.micro_session_id,
                strategy_id=strategy_id,
                force_rebuild=force_rebuild,
            )
        if include_counterfactual:
            self.run_counterfactual_analysis_for_date(
                trading_date=trading_date,
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                session_type=session_type,
                strategy_version=strategy_version,
                force_rebuild=force_rebuild,
            )
        return self.build_daily_report(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            strategy_version=strategy_version,
            force_rebuild=force_rebuild,
        )

    def build_hourly_reports_for_date(
        self,
        *,
        trading_date: date,
        strategy_id: str,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        strategy_version: int | None = None,
        force_rebuild: bool = True,
    ) -> list[HourlyReport]:
        filters = AnalyticsFilters(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            strategy_version=strategy_version,
        )
        runs = self._list(_apply_session_run_filters(select(SessionRun), filters))
        return [
            self.build_hourly_report(
                micro_session_id=run.micro_session_id,
                strategy_id=strategy_id,
                force_rebuild=force_rebuild,
            )
            for run in runs
        ]

    def run_counterfactual_analysis_for_date(
        self,
        *,
        trading_date: date,
        strategy_id: str,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        strategy_version: int | None = None,
        force_rebuild: bool = True,
    ) -> list[CounterfactualResult]:
        filters = AnalyticsFilters(
            trading_date=trading_date,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            session_type=session_type,
            strategy_version=strategy_version,
        )
        existing = self._list(_apply_counterfactual_filters(select(CounterfactualResult), filters))
        if existing and not force_rebuild:
            return existing

        self._session.execute(_apply_counterfactual_filters(delete(CounterfactualResult), filters))
        sources = self._counterfactual_sources(filters=filters)
        results: list[CounterfactualResult] = []
        for source, context in sources:
            analysis = analyze_counterfactual(
                source=source,
                price_path=self._price_path(source=source),
                assumptions=self._assumptions,
            )
            result = self._counterfactual_result_from_analysis(
                analysis=analysis,
                context=context,
            )
            self._session.add(result)
            results.append(result)
        self._session.flush()
        return results

    def hourly_read_model(self, report: HourlyReport) -> dict[str, object]:
        return hourly_report_read_model(report)

    def daily_read_model(self, report: DailyReport) -> dict[str, object]:
        return daily_report_read_model(report)

    def counterfactual_read_models(
        self,
        results: list[CounterfactualResult],
    ) -> list[dict[str, object]]:
        return [counterfactual_read_model(result) for result in results]

    def _existing_daily_report(self, filters: AnalyticsFilters) -> DailyReport | None:
        return self._session.execute(
            _apply_daily_report_filters(select(DailyReport), filters)
        ).scalar_one_or_none()

    def _counterfactual_sources(
        self,
        *,
        filters: AnalyticsFilters,
    ) -> list[tuple[CounterfactualSource, JsonPayload]]:
        candidates = self._list(
            _apply_signal_candidate_filters(select(SignalCandidate), filters)
        )
        final_blockers = {
            blocker.candidate_id: blocker.reason_code
            for blocker in self._list(
                _apply_blocker_filters(
                    select(BlockerEvent).where(BlockerEvent.is_final_blocker.is_(True)),
                    filters,
                )
            )
            if blocker.candidate_id is not None
        }
        sources: list[tuple[CounterfactualSource, JsonPayload]] = []
        for candidate in candidates:
            blocker_code = final_blockers.get(candidate.candidate_id)
            if candidate.candidate_status != "blocked" and blocker_code is None:
                continue
            entry_price = candidate.mid_price or candidate.last_price
            if entry_price is None:
                continue
            sources.append(
                (
                    CounterfactualSource(
                        candidate_id=candidate.candidate_id,
                        order_intent_id=None,
                        source_event_type="blocked_candidate",
                        instrument_id=candidate.instrument_id,
                        strategy_id=candidate.strategy_id,
                        side=candidate.side,
                        event_ts=candidate.ts_utc,
                        entry_price=entry_price,
                        lot_qty=_payload_int(candidate.signal_payload, "lot_qty", default=1),
                        blocker_code=blocker_code,
                        cancel_reason_code=None,
                        timeframe=candidate.timeframe,
                        strategy_version=candidate.strategy_version,
                    ),
                    _session_context_from(candidate),
                )
            )

        cancelled_intents = self._list(
            _apply_order_intent_filters(
                select(OrderIntent).where(OrderIntent.cancel_reason_code.is_not(None)),
                filters,
            )
        )
        for intent in cancelled_intents:
            if intent.intended_price is None:
                continue
            sources.append(
                (
                    CounterfactualSource(
                        candidate_id=intent.candidate_id,
                        order_intent_id=intent.order_intent_id,
                        source_event_type="cancelled_order",
                        instrument_id=intent.instrument_id,
                        strategy_id=intent.strategy_id,
                        side=intent.side,
                        event_ts=intent.terminal_ts or intent.submitted_ts or intent.created_ts,
                        entry_price=intent.intended_price,
                        lot_qty=intent.lot_qty,
                        blocker_code=None,
                        cancel_reason_code=intent.cancel_reason_code,
                        timeframe=intent.timeframe,
                        strategy_version=intent.strategy_version,
                    ),
                    _session_context_from(intent),
                )
            )
        return sources

    def _price_path(self, *, source: CounterfactualSource) -> list[PricePathPoint]:
        candles = self._list(
            select(MarketCandle)
            .where(
                MarketCandle.instrument_id == source.instrument_id,
                MarketCandle.trading_date == source.event_ts.date(),
                *((MarketCandle.timeframe == source.timeframe,) if source.timeframe else ()),
                MarketCandle.close_ts_utc > source.event_ts,
                MarketCandle.close_ts_utc <= source.event_ts + timedelta(minutes=15),
                MarketCandle.is_closed.is_(True),
            )
            .order_by(MarketCandle.close_ts_utc)
        )
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

    def _counterfactual_result_from_analysis(
        self,
        *,
        analysis: CounterfactualAnalysis,
        context: JsonPayload,
    ) -> CounterfactualResult:
        window_5 = analysis.windows[5]
        window_10 = analysis.windows[10]
        window_15 = analysis.windows[15]
        result_payload = analysis.as_payload()
        result_payload["html_output"] = _render_report_html(
            title=f"Counterfactual {analysis.source.source_event_type}",
            summary={
                "instrument": analysis.source.instrument_id,
                "timeframe": analysis.source.timeframe,
                "source": analysis.source.source_event_type,
                "blocker": analysis.source.blocker_code,
                "cancel_reason": analysis.source.cancel_reason_code,
                "net_pnl_15m": window_15.net_pnl_rub,
            },
        )
        return CounterfactualResult(
            calendar_date=context["calendar_date"],
            trading_date=context["trading_date"],
            session_type=str(context["session_type"]),
            session_phase=str(context["session_phase"]),
            micro_session_id=str(context["micro_session_id"]),
            broker_trading_status=str(context["broker_trading_status"]),
            candidate_id=analysis.source.candidate_id,
            order_intent_id=analysis.source.order_intent_id,
            source_event_type=analysis.source.source_event_type,
            instrument_id=analysis.source.instrument_id,
            timeframe=analysis.source.timeframe,
            strategy_id=analysis.source.strategy_id,
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
            result_payload=result_payload,
            generated_at=datetime.now(tz=UTC),
        )

    def _list(self, statement: Any) -> list[Any]:
        return list(self._session.execute(statement).scalars())


def _apply_session_run_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(
        SessionRun.trading_date == filters.trading_date,
        SessionRun.strategy_id == filters.strategy_id,
    )
    if filters.session_type is not None:
        statement = statement.where(SessionRun.session_type == filters.session_type)
    if filters.strategy_version is not None:
        statement = statement.where(SessionRun.strategy_version == filters.strategy_version)
    return statement


def _apply_signal_candidate_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(
        SignalCandidate.trading_date == filters.trading_date,
        SignalCandidate.strategy_id == filters.strategy_id,
    )
    if filters.instrument_id is not None:
        statement = statement.where(SignalCandidate.instrument_id == filters.instrument_id)
    if filters.timeframe is not None:
        statement = statement.where(SignalCandidate.timeframe == filters.timeframe)
    if filters.session_type is not None:
        statement = statement.where(SignalCandidate.session_type == filters.session_type)
    if filters.strategy_version is not None:
        statement = statement.where(SignalCandidate.strategy_version == filters.strategy_version)
    return statement


def _apply_candidate_stage_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(
        CandidateStageResult.trading_date == filters.trading_date,
        CandidateStageResult.strategy_id == filters.strategy_id,
    )
    if filters.instrument_id is not None:
        statement = statement.where(CandidateStageResult.instrument_id == filters.instrument_id)
    if filters.timeframe is not None:
        statement = statement.where(CandidateStageResult.timeframe == filters.timeframe)
    if filters.session_type is not None:
        statement = statement.where(CandidateStageResult.session_type == filters.session_type)
    if filters.strategy_version is not None:
        statement = statement.where(
            CandidateStageResult.strategy_version == filters.strategy_version
        )
    return statement


def _apply_blocker_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(
        BlockerEvent.trading_date == filters.trading_date,
        BlockerEvent.strategy_id == filters.strategy_id,
    )
    if filters.instrument_id is not None:
        statement = statement.where(BlockerEvent.instrument_id == filters.instrument_id)
    if filters.timeframe is not None:
        statement = statement.where(BlockerEvent.timeframe == filters.timeframe)
    if filters.session_type is not None:
        statement = statement.where(BlockerEvent.session_type == filters.session_type)
    return statement


def _apply_order_intent_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(
        OrderIntent.trading_date == filters.trading_date,
        OrderIntent.strategy_id == filters.strategy_id,
    )
    if filters.instrument_id is not None:
        statement = statement.where(OrderIntent.instrument_id == filters.instrument_id)
    if filters.timeframe is not None:
        statement = statement.where(OrderIntent.timeframe == filters.timeframe)
    if filters.session_type is not None:
        statement = statement.where(OrderIntent.session_type == filters.session_type)
    if filters.strategy_version is not None:
        statement = statement.where(OrderIntent.strategy_version == filters.strategy_version)
    return statement


def _apply_broker_order_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(BrokerOrder.trading_date == filters.trading_date)
    if filters.instrument_id is not None:
        statement = statement.where(BrokerOrder.instrument_id == filters.instrument_id)
    if filters.timeframe is not None:
        statement = statement.where(BrokerOrder.timeframe == filters.timeframe)
    if filters.session_type is not None:
        statement = statement.where(BrokerOrder.session_type == filters.session_type)
    return statement


def _apply_order_state_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(OrderStateEvent.trading_date == filters.trading_date)
    if filters.instrument_id is not None:
        statement = statement.where(OrderStateEvent.instrument_id == filters.instrument_id)
    if filters.timeframe is not None:
        statement = statement.where(OrderStateEvent.timeframe == filters.timeframe)
    if filters.session_type is not None:
        statement = statement.where(OrderStateEvent.session_type == filters.session_type)
    return statement


def _apply_strategy_state_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(
        StrategyStateEvent.trading_date == filters.trading_date,
        StrategyStateEvent.strategy_id == filters.strategy_id,
    )
    if filters.instrument_id is not None:
        statement = statement.where(StrategyStateEvent.instrument_id == filters.instrument_id)
    if filters.session_type is not None:
        statement = statement.where(StrategyStateEvent.session_type == filters.session_type)
    if filters.strategy_version is not None:
        statement = statement.where(StrategyStateEvent.strategy_version == filters.strategy_version)
    return statement


def _apply_counterfactual_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(
        CounterfactualResult.trading_date == filters.trading_date,
        CounterfactualResult.strategy_id == filters.strategy_id,
    )
    if filters.instrument_id is not None:
        statement = statement.where(CounterfactualResult.instrument_id == filters.instrument_id)
    if filters.timeframe is not None:
        statement = statement.where(CounterfactualResult.timeframe == filters.timeframe)
    if filters.session_type is not None:
        statement = statement.where(CounterfactualResult.session_type == filters.session_type)
    return statement


def _apply_market_candle_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(
        MarketCandle.trading_date == filters.trading_date,
        MarketCandle.is_closed.is_(True),
    )
    if filters.instrument_id is not None:
        statement = statement.where(MarketCandle.instrument_id == filters.instrument_id)
    if filters.timeframe is not None:
        statement = statement.where(MarketCandle.timeframe == filters.timeframe)
    if filters.session_type is not None:
        statement = statement.where(MarketCandle.session_type == filters.session_type)
    return statement


def _apply_daily_report_filters(statement: Any, filters: AnalyticsFilters) -> Any:
    statement = statement.where(
        DailyReport.trading_date == filters.trading_date,
        DailyReport.strategy_id == filters.strategy_id,
    )
    statement = _where_nullable(statement, DailyReport.instrument_id, filters.instrument_id)
    statement = _where_nullable(statement, DailyReport.timeframe, filters.timeframe)
    statement = _where_nullable(statement, DailyReport.session_type, filters.session_type)
    return statement


def _where_nullable(statement: Any, column: Any, value: object | None) -> Any:
    if value is None:
        return statement.where(column.is_(None))
    return statement.where(column == value)


def _posted_count(broker_orders: list[Any]) -> int:
    return sum(
        1
        for order in broker_orders
        if order.posted_at is not None
        or order.broker_status in {"posted", "working", "partially_filled", "filled"}
    )


def _reject_count(broker_orders: list[Any], intents: list[Any]) -> int:
    broker_rejects = sum(1 for order in broker_orders if order.broker_status == "rejected")
    intent_rejects = sum(1 for intent in intents if intent.reject_reason_code is not None)
    return max(broker_rejects, intent_rejects)


def _cancel_count(broker_orders: list[Any], intents: list[Any]) -> int:
    broker_cancels = sum(1 for order in broker_orders if order.broker_status == "cancelled")
    intent_cancels = sum(1 for intent in intents if intent.cancel_reason_code is not None)
    return max(broker_cancels, intent_cancels)


def _blocked_count(candidates: list[Any], blockers: list[Any]) -> int:
    candidate_count = sum(1 for candidate in candidates if candidate.candidate_status == "blocked")
    final_blockers = {
        blocker.candidate_id
        for blocker in blockers
        if not blocker.passed and blocker.is_final_blocker and blocker.candidate_id is not None
    }
    return max(candidate_count, len(final_blockers))


def _passed_gates_count(candidates: list[Any], stage_results: list[Any]) -> int:
    blocked_candidate_ids = {
        candidate.candidate_id
        for candidate in candidates
        if candidate.candidate_status == "blocked"
    }
    stages_by_candidate: dict[Any, list[Any]] = defaultdict(list)
    for stage in stage_results:
        stages_by_candidate[stage.candidate_id].append(stage)
    passed = 0
    for candidate in candidates:
        stages = stages_by_candidate.get(candidate.candidate_id, [])
        if (stages and all(stage.passed for stage in stages)) or (
            not stages and candidate.candidate_id not in blocked_candidate_ids
        ):
            passed += 1
    return passed


def _exited_count(fills: list[Any], order_state_events: list[Any]) -> int:
    exited_order_ids = {
        event.order_intent_id
        for event in order_state_events
        if event.new_state in {"exited", "closed", "filled_exit"}
    }
    sell_fill_orders = {fill.order_intent_id for fill in fills if fill.side.lower() == "sell"}
    return len({order_id for order_id in (*exited_order_ids, *sell_fill_orders) if order_id})


def _latest_unrealised_pnl(positions: list[Any]) -> Decimal:
    if not positions:
        return ZERO
    latest = max(positions, key=lambda position: position.snapshot_ts)
    return latest.unrealized_pnl or ZERO


def _estimated_slippage(fills: list[Any]) -> Decimal:
    values: list[Decimal] = []
    for fill in fills:
        raw = fill.fill_payload.get("estimated_slippage")
        if raw is not None:
            values.append(Decimal(str(raw)))
    return sum(values, ZERO).quantize(Decimal("0.0001"))


def _latency_values_ms(broker_orders: list[Any]) -> list[Decimal]:
    values: list[Decimal] = []
    for order in broker_orders:
        raw = _nested_payload_value(order.broker_payload, "latency_ms")
        if raw is not None:
            values.append(Decimal(str(raw)))
    return values


def _distribution(values: list[Decimal]) -> JsonPayload:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None}
    return {
        "count": len(ordered),
        "p50": str(_percentile(ordered, Decimal("0.50"))),
        "p95": str(_percentile(ordered, Decimal("0.95"))),
    }


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal:
    index = int((Decimal(len(values) - 1) * percentile).to_integral_value())
    return values[index].quantize(Decimal("0.0001"))


def _nested_payload_value(payload: dict[str, object], key: str) -> object | None:
    if key in payload:
        return payload[key]
    for value in payload.values():
        if isinstance(value, dict) and key in value:
            return cast(object, value[key])
    return None


def _summary_by(candidates: list[Any], *, key: str) -> dict[str, JsonPayload]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for candidate in candidates:
        grouped[str(getattr(candidate, key))].append(candidate)
    return {
        group_key: {
            "signal_count": len(items),
            "entry_count": sum(1 for item in items if item.signal_type == "entry"),
            "exit_count": sum(1 for item in items if item.signal_type == "exit"),
            "blocked_count": sum(1 for item in items if item.candidate_status == "blocked"),
        }
        for group_key, items in sorted(grouped.items())
    }


def _ranking(values: Iterable[str | None]) -> list[JsonPayload]:
    return [
        {"reason_code": reason_code, "count": count}
        for reason_code, count in counts_by(values).items()
    ]


def _blocker_ranking(
    *,
    blockers: list[Any],
    counterfactuals: list[Any],
) -> list[JsonPayload]:
    blocked = [blocker for blocker in blockers if not blocker.passed]
    counterfactuals_by_code: dict[str, list[Any]] = defaultdict(list)
    for result in counterfactuals:
        if result.blocker_code:
            counterfactuals_by_code[result.blocker_code].append(result)

    rows: list[JsonPayload] = []
    for reason_code, count in counts_by(blocker.reason_code for blocker in blocked).items():
        results = counterfactuals_by_code.get(reason_code, [])
        missed_gross = _positive_sum(result.pnl_gross for result in results)
        missed_net = _positive_sum(result.pnl_net for result in results)
        avoided_loss = abs(_negative_sum(result.pnl_net for result in results))
        false_positive_count = sum(1 for result in results if result.would_profit_15m)
        false_positive_rate = (
            Decimal(false_positive_count) / Decimal(len(results)) if results else ZERO
        )
        rows.append(
            {
                "blocker_code": reason_code,
                "reason_code": reason_code,
                "count": count,
                "missed_gross_pnl": str(missed_gross),
                "missed_net_pnl": str(missed_net),
                "avoided_loss": str(avoided_loss),
                "false_positive_rate": str(false_positive_rate.quantize(Decimal("0.0001"))),
                "counterfactual_count": len(results),
            }
        )
    return sorted(rows, key=lambda row: (-_row_count(row), str(row["blocker_code"])))


def _canceled_order_analytics(
    *,
    intents: list[Any],
    broker_orders: list[Any],
    counterfactuals: list[Any],
) -> JsonPayload:
    cancelled_intents = [intent for intent in intents if intent.cancel_reason_code]
    cancelled_broker_orders = [
        order for order in broker_orders if order.broker_status == "cancelled"
    ]
    counterfactuals_by_reason: dict[str, list[Any]] = defaultdict(list)
    for result in counterfactuals:
        if result.cancel_reason_code:
            counterfactuals_by_reason[result.cancel_reason_code].append(result)

    by_reason: list[JsonPayload] = []
    for reason_code, count in counts_by(
        intent.cancel_reason_code for intent in cancelled_intents
    ).items():
        results = counterfactuals_by_reason.get(reason_code, [])
        by_reason.append(
            {
                "cancel_reason_code": reason_code,
                "count": count,
                "missed_gross_pnl": str(_positive_sum(result.pnl_gross for result in results)),
                "missed_net_pnl": str(_positive_sum(result.pnl_net for result in results)),
                "avoided_loss": str(abs(_negative_sum(result.pnl_net for result in results))),
                "counterfactual_count": len(results),
            }
        )
    return {
        "cancelled_intent_count": len(cancelled_intents),
        "cancelled_broker_order_count": len(cancelled_broker_orders),
        "by_cancel_reason": sorted(
            by_reason,
            key=lambda row: (-_row_count(row), str(row["cancel_reason_code"])),
        ),
    }


def _missed_opportunity_summary(results: list[Any]) -> JsonPayload:
    return {
        "would_profit_5m": sum(1 for result in results if result.would_profit_5m),
        "would_profit_10m": sum(1 for result in results if result.would_profit_10m),
        "would_profit_15m": sum(1 for result in results if result.would_profit_15m),
        "missed_gross_pnl": str(_positive_sum(result.pnl_gross for result in results)),
        "missed_net_pnl": str(_positive_sum(result.pnl_net for result in results)),
        "avoided_loss": str(abs(_negative_sum(result.pnl_net for result in results))),
        "total_counterfactuals": len(results),
    }


def _row_count(row: JsonPayload) -> int:
    value = row.get("count", 0)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _candles_by_instrument(candles: list[Any]) -> dict[str, list[PricePathPoint]]:
    grouped: dict[str, list[PricePathPoint]] = defaultdict(list)
    for candle in candles:
        grouped[candle.instrument_id].append(
            PricePathPoint(
                ts_utc=candle.close_ts_utc,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
            )
        )
    return dict(grouped)


def _candles_by_scope(candles: list[Any]) -> dict[str, list[PricePathPoint]]:
    grouped: dict[str, list[PricePathPoint]] = defaultdict(list)
    for candle in candles:
        grouped[f"{candle.instrument_id}|{candle.timeframe}"].append(
            PricePathPoint(
                ts_utc=candle.close_ts_utc,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
            )
        )
    return dict(grouped)


def _payload_int(payload: dict[str, object], key: str, *, default: int) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def _session_context_from(row: Any) -> JsonPayload:
    return {
        "calendar_date": row.calendar_date,
        "trading_date": row.trading_date,
        "session_type": row.session_type,
        "session_phase": row.session_phase,
        "micro_session_id": row.micro_session_id,
        "broker_trading_status": row.broker_trading_status,
    }


def _sum_optional_decimal(values: Iterable[Decimal | None]) -> Decimal:
    return sum((value for value in values if value is not None), ZERO).quantize(Decimal("0.0001"))


def _average_optional_decimal(values: Iterable[Decimal | None]) -> Decimal | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return (sum(present, ZERO) / Decimal(len(present))).quantize(Decimal("0.0001"))


def _positive_sum(values: Iterable[Decimal | None]) -> Decimal:
    return sum((value for value in values if value is not None and value > ZERO), ZERO).quantize(
        Decimal("0.0001")
    )


def _negative_sum(values: Iterable[Decimal | None]) -> Decimal:
    return sum((value for value in values if value is not None and value < ZERO), ZERO).quantize(
        Decimal("0.0001")
    )


def _filter_rows(rows: list[Any], filters: AnalyticsFilters) -> list[Any]:
    filtered = rows
    if filters.instrument_id is not None:
        filtered = [
            row
            for row in filtered
            if getattr(row, "instrument_id", None) == filters.instrument_id
        ]
    if filters.timeframe is not None:
        filtered = [
            row for row in filtered if getattr(row, "timeframe", None) == filters.timeframe
        ]
    if filters.session_type is not None:
        filtered = [
            row
            for row in filtered
            if getattr(row, "session_type", None) == filters.session_type
        ]
    return filtered


def _render_report_html(*, title: str, summary: dict[str, object]) -> str:
    rows = "".join(
        f"<tr><th>{escape(str(key))}</th><td>{escape(str(value))}</td></tr>"
        for key, value in sorted(summary.items())
    )
    return (
        '<section class="report-summary">'
        f"<h2>{escape(title)}</h2>"
        f"<table>{rows}</table>"
        "</section>"
    )
