"""Repository layer for database-backed domain aggregates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trading_common.db.models import (
    BlockerEvent,
    BrokerOrder,
    CandidateStageResult,
    CounterfactualResult,
    DailyReport,
    FillEvent,
    HourlyReport,
    InstrumentRegistry,
    MarketCandle,
    MarketContextSnapshot,
    MarketMicrostructureSnapshot,
    MarketStatusSnapshot,
    MarketTradeSample,
    MicroSession,
    OrderBookSummary,
    OrderIntent,
    OrderStateEvent,
    ReportJobOutbox,
    RiskEvent,
    RobotCommand,
    SessionRun,
    SignalCandidate,
    StrategyConfig,
    StrategyStateEvent,
)

REPORT_JOB_PENDING = "pending"
REPORT_JOB_QUEUED = "queued"
REPORT_JOB_RUNNING = "running"
REPORT_JOB_SUCCEEDED = "succeeded"
REPORT_JOB_RETRY = "retry"
REPORT_JOB_DEAD_LETTER = "dead_letter"
HOURLY_REPORT_TASK = "report_worker.build_hourly_report"
DAILY_REBUILD_TASK = "report_worker.rebuild_reports_for_date"

ROBOT_COMMAND_REQUESTED = "requested"
ROBOT_COMMAND_ACCEPTED = "accepted"
ROBOT_COMMAND_APPLIED = "applied"
ROBOT_COMMAND_REJECTED = "rejected"
ROBOT_COMMAND_FAILED = "failed"
ROBOT_COMMAND_TERMINAL_STATUSES = (
    ROBOT_COMMAND_APPLIED,
    ROBOT_COMMAND_REJECTED,
    ROBOT_COMMAND_FAILED,
)


@dataclass(frozen=True, slots=True)
class CandidateJourney:
    """Read model for candidate -> blocker/order/fill/counterfactual analysis."""

    candidate: SignalCandidate | None
    market_context: tuple[MarketContextSnapshot, ...]
    stage_results: tuple[CandidateStageResult, ...]
    blockers: tuple[BlockerEvent, ...]
    order_intents: tuple[OrderIntent, ...]
    broker_orders: tuple[BrokerOrder, ...]
    order_state_events: tuple[OrderStateEvent, ...]
    fills: tuple[FillEvent, ...]
    counterfactuals: tuple[CounterfactualResult, ...]


class InstrumentRepository:
    """CRUD helpers for `instrument_registry`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, instrument_id: str) -> InstrumentRegistry | None:
        return self._session.get(InstrumentRegistry, instrument_id)

    def get_by_ticker(self, ticker: str) -> InstrumentRegistry | None:
        stmt = select(InstrumentRegistry).where(InstrumentRegistry.ticker == ticker)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_enabled(self) -> list[InstrumentRegistry]:
        stmt = (
            select(InstrumentRegistry)
            .where(InstrumentRegistry.is_enabled.is_(True))
            .order_by(InstrumentRegistry.ticker)
        )
        return list(self._session.execute(stmt).scalars())

    def upsert(self, instrument: InstrumentRegistry) -> InstrumentRegistry:
        existing = self.get(instrument.instrument_id)
        if existing is None:
            self._session.add(instrument)
            self._session.flush()
            return instrument

        existing.ticker = instrument.ticker
        existing.class_code = instrument.class_code
        existing.figi = instrument.figi
        existing.instrument_uid = instrument.instrument_uid
        existing.name = instrument.name
        existing.lot_size = instrument.lot_size
        existing.min_price_increment = instrument.min_price_increment
        existing.currency = instrument.currency
        existing.is_enabled = instrument.is_enabled
        existing.supports_morning = instrument.supports_morning
        existing.supports_evening = instrument.supports_evening
        existing.supports_weekend = instrument.supports_weekend
        existing.source = instrument.source
        existing.resolved_at = instrument.resolved_at
        existing.resolution_status = instrument.resolution_status
        existing.resolution_error_code = instrument.resolution_error_code
        existing.resolution_error_message = instrument.resolution_error_message
        existing.broker_payload = instrument.broker_payload
        existing.instrument_payload = instrument.instrument_payload
        self._session.flush()
        return existing


class StrategyConfigRepository:
    """CRUD helpers for versioned `strategy_config` rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_version(self, config: StrategyConfig) -> StrategyConfig:
        self._session.add(config)
        self._session.flush()
        return config

    def get_active(self, strategy_id: str, session_template: str) -> StrategyConfig | None:
        stmt = (
            select(StrategyConfig)
            .where(
                StrategyConfig.strategy_id == strategy_id,
                StrategyConfig.session_template == session_template,
                StrategyConfig.is_active.is_(True),
            )
            .order_by(StrategyConfig.version.desc())
        )
        return self._session.execute(stmt).scalars().first()

    def deactivate_previous(self, strategy_id: str, session_template: str) -> int:
        active_configs = self._session.execute(
            select(StrategyConfig).where(
                StrategyConfig.strategy_id == strategy_id,
                StrategyConfig.session_template == session_template,
                StrategyConfig.is_active.is_(True),
            )
        ).scalars()
        updated = 0
        for config in active_configs:
            config.is_active = False
            updated += 1
        self._session.flush()
        return updated


class SessionRunRepository:
    """CRUD helpers for logical micro-session runs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, run: SessionRun) -> SessionRun:
        self._session.add(run)
        self._session.flush()
        return run

    def create_idempotent(self, run: SessionRun) -> SessionRun:
        existing = self.get_by_micro_session_id(run.micro_session_id)
        if existing is not None:
            return existing
        return self.create(run)

    def get(self, run_id: UUID) -> SessionRun | None:
        return self._session.get(SessionRun, run_id)

    def get_by_micro_session_id(self, micro_session_id: str) -> SessionRun | None:
        stmt = select(SessionRun).where(SessionRun.micro_session_id == micro_session_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def close(
        self,
        run_id: UUID,
        *,
        ended_at: datetime,
        close_reason_code: str,
    ) -> SessionRun:
        run = self.get(run_id)
        if run is None:
            msg = f"SessionRun not found: {run_id}"
            raise LookupError(msg)
        run.status = "closed"
        run.ended_at = ended_at
        run.close_reason_code = close_reason_code
        self._session.flush()
        return run

    def mark_freeze(self, run_id: UUID, *, freeze_started_at: datetime) -> SessionRun:
        run = self.get(run_id)
        if run is None:
            msg = f"SessionRun not found: {run_id}"
            raise LookupError(msg)
        if run.freeze_started_at is None:
            run.freeze_started_at = freeze_started_at
        if run.status == "open":
            run.status = "freezing"
        self._session.flush()
        return run

    def request_report(self, run_id: UUID, *, requested_at: datetime) -> SessionRun:
        run = self.get(run_id)
        if run is None:
            msg = f"SessionRun not found: {run_id}"
            raise LookupError(msg)
        run.report_requested_at = requested_at
        self._session.flush()
        return run


class MicroSessionRepository:
    """CRUD helpers for hourly logical `micro_session` rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, micro_session: MicroSession) -> MicroSession:
        self._session.add(micro_session)
        self._session.flush()
        return micro_session

    def create_idempotent(self, micro_session: MicroSession) -> MicroSession:
        existing = self.get(micro_session.micro_session_id)
        if existing is not None:
            return existing
        return self.create(micro_session)

    def get(self, micro_session_id: str) -> MicroSession | None:
        return self._session.get(MicroSession, micro_session_id)

    def list_open(self, trading_date: date | None = None) -> list[MicroSession]:
        stmt = select(MicroSession).where(MicroSession.status.in_(("open", "freezing")))
        if trading_date is not None:
            stmt = stmt.where(MicroSession.trading_date == trading_date)
        stmt = stmt.order_by(MicroSession.started_at)
        return list(self._session.execute(stmt).scalars())

    def close(
        self,
        micro_session_id: str,
        *,
        ended_at: datetime,
        rollover_reason_code: str,
        snapshot_payload: Mapping[str, object] | None = None,
    ) -> MicroSession:
        micro_session = self.get(micro_session_id)
        if micro_session is None:
            msg = f"MicroSession not found: {micro_session_id}"
            raise LookupError(msg)
        micro_session.status = "closed"
        micro_session.ended_at = ended_at
        micro_session.rollover_reason_code = rollover_reason_code
        if snapshot_payload:
            micro_session.snapshot_payload = {
                **micro_session.snapshot_payload,
                **dict(snapshot_payload),
            }
        self._session.flush()
        return micro_session

    def mark_freeze(self, micro_session_id: str, *, freeze_started_at: datetime) -> MicroSession:
        micro_session = self.get(micro_session_id)
        if micro_session is None:
            msg = f"MicroSession not found: {micro_session_id}"
            raise LookupError(msg)
        if micro_session.freeze_started_at is None:
            micro_session.freeze_started_at = freeze_started_at
        if micro_session.status == "open":
            micro_session.status = "freezing"
        self._session.flush()
        return micro_session


class ReportJobRepository:
    """Transactional outbox helpers for report-worker Celery jobs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_job_idempotent(
        self,
        *,
        report_type: str,
        task_name: str,
        strategy_id: str,
        requested_at: datetime,
        micro_session_id: str | None = None,
        trading_date: date | None = None,
        force_rebuild: bool = True,
        job_payload: Mapping[str, object] | None = None,
        max_retries: int = 3,
        idempotency_key: str | None = None,
    ) -> ReportJobOutbox:
        key = idempotency_key or self.idempotency_key(
            report_type=report_type,
            strategy_id=strategy_id,
            micro_session_id=micro_session_id,
            trading_date=trading_date,
            job_payload=job_payload,
        )
        existing = self.get_by_idempotency_key(key)
        if existing is not None:
            return existing

        payload = {
            **dict(job_payload or {}),
            "force_rebuild": force_rebuild,
        }
        job = ReportJobOutbox(
            celery_task_id=None,
            task_name=task_name,
            report_type=report_type,
            micro_session_id=micro_session_id,
            strategy_id=strategy_id,
            trading_date=trading_date,
            status=REPORT_JOB_PENDING,
            retry_count=0,
            max_retries=max_retries,
            last_error=None,
            requested_at=requested_at,
            started_at=None,
            finished_at=None,
            next_retry_at=None,
            idempotency_key=key,
            job_payload=payload,
            result_payload={},
        )
        self._session.add(job)
        self._session.flush()
        return job

    def create_hourly_job_idempotent(
        self,
        *,
        micro_session_id: str,
        strategy_id: str,
        trading_date: date,
        requested_at: datetime,
        force_rebuild: bool = True,
        job_payload: Mapping[str, object] | None = None,
    ) -> ReportJobOutbox:
        return self.create_job_idempotent(
            report_type="hourly",
            task_name=HOURLY_REPORT_TASK,
            strategy_id=strategy_id,
            micro_session_id=micro_session_id,
            trading_date=trading_date,
            requested_at=requested_at,
            force_rebuild=force_rebuild,
            job_payload=job_payload,
            idempotency_key=self.hourly_idempotency_key(
                micro_session_id=micro_session_id,
                strategy_id=strategy_id,
            ),
        )

    def get(self, report_job_id: UUID | str) -> ReportJobOutbox | None:
        job_id = report_job_id if isinstance(report_job_id, UUID) else UUID(str(report_job_id))
        return self._session.get(ReportJobOutbox, job_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> ReportJobOutbox | None:
        stmt = select(ReportJobOutbox).where(ReportJobOutbox.idempotency_key == idempotency_key)
        return self._session.execute(stmt).scalar_one_or_none()

    def get_by_celery_task_id(self, celery_task_id: str | None) -> ReportJobOutbox | None:
        if not celery_task_id:
            return None
        stmt = select(ReportJobOutbox).where(ReportJobOutbox.celery_task_id == celery_task_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_dispatchable(
        self,
        *,
        now: datetime,
        limit: int = 50,
    ) -> list[ReportJobOutbox]:
        stmt = (
            select(ReportJobOutbox)
            .where(
                ReportJobOutbox.status.in_((REPORT_JOB_PENDING, REPORT_JOB_RETRY)),
                or_(
                    ReportJobOutbox.next_retry_at.is_(None),
                    ReportJobOutbox.next_retry_at <= now,
                ),
            )
            .order_by(ReportJobOutbox.requested_at)
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars())

    def mark_enqueued(
        self,
        job: ReportJobOutbox,
        *,
        celery_task_id: str,
    ) -> ReportJobOutbox:
        job.celery_task_id = celery_task_id
        job.status = REPORT_JOB_QUEUED
        job.last_error = None
        job.next_retry_at = None
        self._session.flush()
        return job

    def mark_started_by_celery_task_id(
        self,
        celery_task_id: str | None,
        *,
        started_at: datetime,
    ) -> ReportJobOutbox | None:
        job = self.get_by_celery_task_id(celery_task_id)
        if job is None:
            return None
        job.status = REPORT_JOB_RUNNING
        job.started_at = started_at
        job.last_error = None
        self._session.flush()
        return job

    def mark_succeeded_by_celery_task_id(
        self,
        celery_task_id: str | None,
        *,
        finished_at: datetime,
        result_payload: Mapping[str, object],
    ) -> ReportJobOutbox | None:
        job = self.get_by_celery_task_id(celery_task_id)
        if job is None:
            return None
        job.status = REPORT_JOB_SUCCEEDED
        job.finished_at = finished_at
        job.last_error = None
        job.next_retry_at = None
        job.result_payload = dict(result_payload)
        self._session.flush()
        return job

    def mark_failed_by_celery_task_id(
        self,
        celery_task_id: str | None,
        *,
        failed_at: datetime,
        error: str,
        retry_delay_seconds: int = 60,
    ) -> ReportJobOutbox | None:
        job = self.get_by_celery_task_id(celery_task_id)
        if job is None:
            return None
        return self.mark_failed(
            job,
            failed_at=failed_at,
            error=error,
            retry_delay_seconds=retry_delay_seconds,
        )

    def mark_failed(
        self,
        job: ReportJobOutbox,
        *,
        failed_at: datetime,
        error: str,
        retry_delay_seconds: int = 60,
    ) -> ReportJobOutbox:
        job.retry_count += 1
        job.last_error = error[:2048]
        job.finished_at = failed_at
        if job.retry_count < job.max_retries:
            job.status = REPORT_JOB_RETRY
            job.next_retry_at = failed_at + timedelta(seconds=retry_delay_seconds)
            job.celery_task_id = None
        else:
            job.status = REPORT_JOB_DEAD_LETTER
            job.next_retry_at = None
        self._session.flush()
        return job

    @staticmethod
    def hourly_idempotency_key(*, micro_session_id: str, strategy_id: str) -> str:
        return f"hourly:{strategy_id}:{micro_session_id}"

    @staticmethod
    def idempotency_key(
        *,
        report_type: str,
        strategy_id: str,
        micro_session_id: str | None,
        trading_date: date | None,
        job_payload: Mapping[str, object] | None,
    ) -> str:
        payload_items = sorted((job_payload or {}).items())
        payload_fingerprint = "|".join(f"{key}={value}" for key, value in payload_items)
        return ":".join(
            (
                report_type,
                strategy_id,
                micro_session_id or "none",
                trading_date.isoformat() if trading_date else "none",
                payload_fingerprint or "default",
            )
        )


class RobotCommandRepository:
    """Persistent robot command queue for API -> trade-core control plane."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        command_type: str,
        requested_by: str,
        requested_role: str,
        requested_at: datetime,
        payload: Mapping[str, object] | None = None,
        reason_code: str | None = None,
    ) -> RobotCommand:
        command = RobotCommand(
            command_type=command_type,
            requested_by=requested_by,
            requested_role=requested_role,
            requested_at=requested_at,
            status=ROBOT_COMMAND_REQUESTED,
            reason_code=reason_code,
            accepted_at=None,
            applied_at=None,
            finished_at=None,
            payload=dict(payload or {}),
            result_payload={},
        )
        self._session.add(command)
        self._session.flush()
        return command

    def get(self, command_id: UUID | str) -> RobotCommand | None:
        parsed_id = command_id if isinstance(command_id, UUID) else UUID(str(command_id))
        return self._session.get(RobotCommand, parsed_id)

    def latest(self) -> RobotCommand | None:
        stmt = select(RobotCommand).order_by(RobotCommand.requested_at.desc()).limit(1)
        return self._session.execute(stmt).scalars().first()

    def list_requested(self, *, limit: int = 20) -> list[RobotCommand]:
        stmt = (
            select(RobotCommand)
            .where(RobotCommand.status == ROBOT_COMMAND_REQUESTED)
            .order_by(RobotCommand.requested_at, RobotCommand.command_id)
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars())

    def mark_accepted(
        self,
        command: RobotCommand,
        *,
        accepted_at: datetime,
        reason_code: str = "runtime_command_accepted",
    ) -> RobotCommand:
        command.status = ROBOT_COMMAND_ACCEPTED
        command.reason_code = reason_code
        command.accepted_at = accepted_at
        self._session.flush()
        return command

    def mark_applied(
        self,
        command: RobotCommand,
        *,
        applied_at: datetime,
        reason_code: str,
        result_payload: Mapping[str, object] | None = None,
    ) -> RobotCommand:
        command.status = ROBOT_COMMAND_APPLIED
        command.reason_code = reason_code
        command.applied_at = applied_at
        command.finished_at = applied_at
        command.result_payload = dict(result_payload or {})
        self._session.flush()
        return command

    def mark_rejected(
        self,
        command: RobotCommand,
        *,
        rejected_at: datetime,
        reason_code: str,
        result_payload: Mapping[str, object] | None = None,
    ) -> RobotCommand:
        command.status = ROBOT_COMMAND_REJECTED
        command.reason_code = reason_code
        command.finished_at = rejected_at
        command.result_payload = dict(result_payload or {})
        self._session.flush()
        return command

    def mark_failed(
        self,
        command: RobotCommand,
        *,
        failed_at: datetime,
        reason_code: str,
        error: str,
        result_payload: Mapping[str, object] | None = None,
    ) -> RobotCommand:
        command.status = ROBOT_COMMAND_FAILED
        command.reason_code = reason_code
        command.finished_at = failed_at
        command.result_payload = {
            **dict(result_payload or {}),
            "error": error[:2048],
        }
        self._session.flush()
        return command

    @staticmethod
    def robot_state_from_command(command: RobotCommand | None) -> str:
        if command is None:
            return "stopped"
        if command.status not in ROBOT_COMMAND_TERMINAL_STATUSES:
            return f"{command.command_type}_{command.status}"
        if command.status != ROBOT_COMMAND_APPLIED:
            return f"{command.command_type}_{command.status}"
        if command.command_type in {"start", "resume"}:
            return "running"
        if command.command_type == "pause":
            return "paused"
        if command.command_type == "stop":
            return "stopped"
        if command.command_type == "emergency_stop":
            return "emergency_stopped"
        return command.status


class StrategyStateEventRepository:
    """Append-only helpers for `strategy_state_event` rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, event: StrategyStateEvent) -> StrategyStateEvent:
        self._session.add(event)
        self._session.flush()
        return event


class SignalCandidateRepository:
    """Append and update helpers for `signal_candidate` rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, candidate: SignalCandidate) -> SignalCandidate:
        self._session.add(candidate)
        self._session.flush()
        return candidate

    def create_idempotent(self, candidate: SignalCandidate) -> SignalCandidate:
        existing = self.get_by_fingerprint(candidate.signal_fingerprint)
        if existing is not None:
            return existing
        return self.create(candidate)

    def get(self, candidate_id: UUID) -> SignalCandidate | None:
        return self._session.get(SignalCandidate, candidate_id)

    def get_by_fingerprint(self, signal_fingerprint: str | None) -> SignalCandidate | None:
        if not signal_fingerprint:
            return None
        stmt = select(SignalCandidate).where(
            SignalCandidate.signal_fingerprint == signal_fingerprint
        )
        return self._session.execute(stmt).scalars().first()

    def update_status(self, candidate_id: UUID, status: str) -> SignalCandidate:
        candidate = self.get(candidate_id)
        if candidate is None:
            msg = f"SignalCandidate not found: {candidate_id}"
            raise LookupError(msg)
        candidate.candidate_status = status
        self._session.flush()
        return candidate


class CandidateStageResultRepository:
    """Append-only helpers for candidate decision stage results."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, result: CandidateStageResult) -> CandidateStageResult:
        self._session.add(result)
        self._session.flush()
        return result

    def create_idempotent(self, result: CandidateStageResult) -> CandidateStageResult:
        existing = self.get_by_candidate_stage(result.candidate_id, result.stage_seq)
        if existing is not None:
            return existing
        return self.create(result)

    def get_by_candidate_stage(
        self,
        candidate_id: UUID,
        stage_seq: int,
    ) -> CandidateStageResult | None:
        stmt = select(CandidateStageResult).where(
            CandidateStageResult.candidate_id == candidate_id,
            CandidateStageResult.stage_seq == stage_seq,
        )
        return self._session.execute(stmt).scalars().first()

    def list_for_candidate(self, candidate_id: UUID) -> list[CandidateStageResult]:
        stmt = (
            select(CandidateStageResult)
            .where(CandidateStageResult.candidate_id == candidate_id)
            .order_by(CandidateStageResult.stage_seq)
        )
        return list(self._session.execute(stmt).scalars())


class BlockerEventRepository:
    """Append-only helpers for causal risk blocker events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, event: BlockerEvent) -> BlockerEvent:
        self._session.add(event)
        self._session.flush()
        return event

    def create_idempotent(self, event: BlockerEvent) -> BlockerEvent:
        if event.candidate_id is not None:
            stmt = select(BlockerEvent).where(
                BlockerEvent.candidate_id == event.candidate_id,
                BlockerEvent.gate_rank == event.gate_rank,
                BlockerEvent.reason_code == event.reason_code,
            )
            existing = self._session.execute(stmt).scalars().first()
            if existing is not None:
                return existing
        return self.create(event)


class RiskEventRepository:
    """Append-only helpers for risk decisions and limit events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, event: RiskEvent) -> RiskEvent:
        self._session.add(event)
        self._session.flush()
        return event


class MarketDataRepository:
    """Persistence helpers for market candles and lightweight market snapshots."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get_candle(
        self,
        *,
        instrument_id: str,
        timeframe: str,
        open_ts_utc: datetime,
    ) -> MarketCandle | None:
        stmt = select(MarketCandle).where(
            MarketCandle.instrument_id == instrument_id,
            MarketCandle.timeframe == timeframe,
            MarketCandle.open_ts_utc == open_ts_utc,
        )
        return self._session.execute(stmt).scalars().first()

    def upsert_candle(self, candle: MarketCandle) -> MarketCandle:
        existing = self.get_candle(
            instrument_id=candle.instrument_id,
            timeframe=candle.timeframe,
            open_ts_utc=candle.open_ts_utc,
        )
        if existing is None:
            self._session.add(candle)
            self._session.flush()
            return candle

        existing.close_ts_utc = candle.close_ts_utc
        existing.exchange_open_ts = candle.exchange_open_ts
        existing.exchange_close_ts = candle.exchange_close_ts
        existing.open_price = candle.open_price
        existing.high_price = candle.high_price
        existing.low_price = candle.low_price
        existing.close_price = candle.close_price
        existing.volume_lots = candle.volume_lots
        existing.is_closed = candle.is_closed
        existing.source = candle.source
        existing.candle_payload = candle.candle_payload
        existing.calendar_date = candle.calendar_date
        existing.trading_date = candle.trading_date
        existing.session_type = candle.session_type
        existing.session_phase = candle.session_phase
        existing.micro_session_id = candle.micro_session_id
        existing.broker_trading_status = candle.broker_trading_status
        self._session.flush()
        return existing

    def save_status_snapshot(self, snapshot: MarketStatusSnapshot) -> MarketStatusSnapshot:
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def save_order_book_summary(self, summary: OrderBookSummary) -> OrderBookSummary:
        self._session.add(summary)
        self._session.flush()
        return summary

    def save_microstructure_snapshot(
        self,
        snapshot: MarketMicrostructureSnapshot,
    ) -> MarketMicrostructureSnapshot:
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def save_market_trade_sample(self, sample: MarketTradeSample) -> MarketTradeSample:
        query = select(MarketTradeSample).where(
            MarketTradeSample.trading_date == sample.trading_date,
            MarketTradeSample.instrument_id == sample.instrument_id,
            MarketTradeSample.source == sample.source,
        )
        if sample.trade_id:
            query = query.where(MarketTradeSample.trade_id == sample.trade_id)
        else:
            query = query.where(
                MarketTradeSample.exchange_ts == sample.exchange_ts,
                MarketTradeSample.price == sample.price,
                MarketTradeSample.quantity_lots == sample.quantity_lots,
                MarketTradeSample.side == sample.side,
            )
        existing = self._session.execute(query.limit(1)).scalars().first()
        if existing is not None:
            return existing
        self._session.add(sample)
        self._session.flush()
        return sample


class MarketContextSnapshotRepository:
    """Persistence helpers for explainable market context snapshots."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, snapshot: MarketContextSnapshot) -> MarketContextSnapshot:
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def create_idempotent(self, snapshot: MarketContextSnapshot) -> MarketContextSnapshot:
        if snapshot.candidate_id is not None:
            stmt = select(MarketContextSnapshot).where(
                MarketContextSnapshot.candidate_id == snapshot.candidate_id,
                MarketContextSnapshot.snapshot_kind == snapshot.snapshot_kind,
            )
            existing = self._session.execute(stmt).scalars().first()
            if existing is not None:
                return existing
        return self.create(snapshot)

    def latest_for_candidate(self, candidate_id: UUID) -> MarketContextSnapshot | None:
        stmt = (
            select(MarketContextSnapshot)
            .where(MarketContextSnapshot.candidate_id == candidate_id)
            .order_by(MarketContextSnapshot.ts_utc.desc())
        )
        return self._session.execute(stmt).scalars().first()

    def latest_for_instrument(
        self,
        *,
        instrument_id: str,
        timeframe: str,
    ) -> MarketContextSnapshot | None:
        stmt = (
            select(MarketContextSnapshot)
            .where(
                MarketContextSnapshot.instrument_id == instrument_id,
                MarketContextSnapshot.timeframe == timeframe,
            )
            .order_by(MarketContextSnapshot.ts_utc.desc())
        )
        return self._session.execute(stmt).scalars().first()


class OrderRepository:
    """Order repositories with request id based idempotency."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_intent_by_request_order_id(self, request_order_id: UUID) -> OrderIntent | None:
        stmt = select(OrderIntent).where(OrderIntent.request_order_id == request_order_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def get_intent_by_idempotency_key(self, idempotency_key: str) -> OrderIntent | None:
        stmt = select(OrderIntent).where(OrderIntent.idempotency_key == idempotency_key)
        return self._session.execute(stmt).scalar_one_or_none()

    def create_intent_idempotent(self, intent: OrderIntent) -> OrderIntent:
        existing = self.get_intent_by_request_order_id(intent.request_order_id)
        if existing is not None:
            return existing
        existing_by_key = self.get_intent_by_idempotency_key(intent.idempotency_key)
        if existing_by_key is not None:
            return existing_by_key
        self._session.add(intent)
        self._session.flush()
        return intent

    def update_intent_status(
        self,
        intent: OrderIntent,
        *,
        status: str,
        submitted_ts: datetime | None = None,
        terminal_ts: datetime | None = None,
        cancel_reason_code: str | None = None,
        reject_reason_code: str | None = None,
        payload_patch: Mapping[str, object] | None = None,
    ) -> OrderIntent:
        intent.status = status
        if submitted_ts is not None:
            intent.submitted_ts = submitted_ts
        if terminal_ts is not None:
            intent.terminal_ts = terminal_ts
        if cancel_reason_code is not None:
            intent.cancel_reason_code = cancel_reason_code
        if reject_reason_code is not None:
            intent.reject_reason_code = reject_reason_code
        if payload_patch:
            intent.intent_payload = {**intent.intent_payload, **dict(payload_patch)}
        self._session.flush()
        return intent

    def get_broker_order_by_request_order_id(self, request_order_id: UUID) -> BrokerOrder | None:
        stmt = select(BrokerOrder).where(BrokerOrder.request_order_id == request_order_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def upsert_broker_order_state(self, order: BrokerOrder) -> BrokerOrder:
        existing = self.get_broker_order_by_request_order_id(order.request_order_id)
        if existing is None:
            self._session.add(order)
            self._session.flush()
            return order

        if order.lifecycle_seq < existing.lifecycle_seq:
            return existing

        existing.candidate_id = order.candidate_id
        existing.instrument_id = order.instrument_id
        existing.timeframe = order.timeframe
        existing.exchange_order_id = order.exchange_order_id
        existing.tracking_id = order.tracking_id
        existing.broker_status = order.broker_status
        existing.lifecycle_seq = order.lifecycle_seq
        existing.latency_ms = order.latency_ms
        existing.posted_at = order.posted_at
        existing.cancelled_at = order.cancelled_at
        existing.rejected_at = order.rejected_at
        existing.reject_reason_code = order.reject_reason_code
        existing.broker_tracking_id = order.broker_tracking_id
        existing.last_observed_at = order.last_observed_at
        existing.broker_payload = order.broker_payload
        self._session.flush()
        return existing

    def create_order_state_event(self, event: OrderStateEvent) -> OrderStateEvent:
        self._session.add(event)
        self._session.flush()
        return event

    def create_order_state_event_idempotent(
        self,
        event: OrderStateEvent,
    ) -> OrderStateEvent:
        if event.order_intent_id is not None:
            stmt = select(OrderStateEvent).where(
                OrderStateEvent.order_intent_id == event.order_intent_id,
                OrderStateEvent.state_seq == event.state_seq,
                OrderStateEvent.event_type == event.event_type,
            )
            existing = self._session.execute(stmt).scalars().first()
            if existing is not None:
                return existing
        return self.create_order_state_event(event)

    def list_order_state_events(self, order_intent_id: UUID) -> list[OrderStateEvent]:
        stmt = (
            select(OrderStateEvent)
            .where(OrderStateEvent.order_intent_id == order_intent_id)
            .order_by(OrderStateEvent.state_seq, OrderStateEvent.ts_utc)
        )
        return list(self._session.execute(stmt).scalars())

    def create_fill_event_idempotent(self, event: FillEvent) -> FillEvent:
        stmt = select(FillEvent).where(
            FillEvent.exchange_order_id == event.exchange_order_id,
            FillEvent.broker_fill_id == event.broker_fill_id,
            FillEvent.trading_date == event.trading_date,
        )
        existing = self._session.execute(stmt).scalars().first()
        if existing is not None:
            return existing
        self._session.add(event)
        self._session.flush()
        return event


class AnalyticsReadRepository:
    """Use-case helpers for frontend report screens and analytics jobs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_candidate_journey(self, candidate_id: UUID) -> CandidateJourney:
        candidate = self._session.get(SignalCandidate, candidate_id)
        market_context = tuple(
            self._session.execute(
                select(MarketContextSnapshot)
                .where(MarketContextSnapshot.candidate_id == candidate_id)
                .order_by(MarketContextSnapshot.ts_utc)
            ).scalars()
        )
        stage_results = tuple(
            self._session.execute(
                select(CandidateStageResult)
                .where(CandidateStageResult.candidate_id == candidate_id)
                .order_by(CandidateStageResult.stage_seq, CandidateStageResult.ts_utc)
            ).scalars()
        )
        blockers = tuple(
            self._session.execute(
                select(BlockerEvent)
                .where(BlockerEvent.candidate_id == candidate_id)
                .order_by(BlockerEvent.gate_rank, BlockerEvent.ts_utc)
            ).scalars()
        )
        order_intents = tuple(
            self._session.execute(
                select(OrderIntent)
                .where(OrderIntent.candidate_id == candidate_id)
                .order_by(OrderIntent.created_ts)
            ).scalars()
        )
        request_order_ids = [intent.request_order_id for intent in order_intents]

        broker_order_conditions = [BrokerOrder.candidate_id == candidate_id]
        order_state_conditions = [OrderStateEvent.candidate_id == candidate_id]
        fill_conditions = [FillEvent.candidate_id == candidate_id]
        if request_order_ids:
            broker_order_conditions.append(BrokerOrder.request_order_id.in_(request_order_ids))
            order_state_conditions.append(OrderStateEvent.request_order_id.in_(request_order_ids))
            fill_conditions.append(FillEvent.request_order_id.in_(request_order_ids))

        broker_orders = tuple(
            self._session.execute(
                select(BrokerOrder)
                .where(or_(*broker_order_conditions))
                .order_by(BrokerOrder.last_observed_at)
            ).scalars()
        )
        order_state_events = tuple(
            self._session.execute(
                select(OrderStateEvent)
                .where(or_(*order_state_conditions))
                .order_by(OrderStateEvent.state_seq, OrderStateEvent.ts_utc)
            ).scalars()
        )
        fills = tuple(
            self._session.execute(
                select(FillEvent)
                .where(or_(*fill_conditions))
                .order_by(FillEvent.ts_utc)
            ).scalars()
        )
        counterfactuals = tuple(
            self._session.execute(
                select(CounterfactualResult)
                .where(CounterfactualResult.candidate_id == candidate_id)
                .order_by(CounterfactualResult.generated_at)
            ).scalars()
        )

        return CandidateJourney(
            candidate=candidate,
            market_context=market_context,
            stage_results=stage_results,
            blockers=blockers,
            order_intents=order_intents,
            broker_orders=broker_orders,
            order_state_events=order_state_events,
            fills=fills,
            counterfactuals=counterfactuals,
        )

    def recent_candidates(
        self,
        *,
        trading_date: date,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        session_type: str | None = None,
        limit: int = 100,
    ) -> list[SignalCandidate]:
        stmt = select(SignalCandidate).where(SignalCandidate.trading_date == trading_date)
        if instrument_id is not None:
            stmt = stmt.where(SignalCandidate.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(SignalCandidate.timeframe == timeframe)
        if session_type is not None:
            stmt = stmt.where(SignalCandidate.session_type == session_type)
        stmt = stmt.order_by(SignalCandidate.ts_utc.desc()).limit(limit)
        return list(self._session.execute(stmt).scalars())

    def blocker_ranking(
        self,
        *,
        trading_date: date,
        session_type: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        limit: int = 20,
    ) -> list[tuple[str, int]]:
        stmt = (
            select(BlockerEvent.blocker_code, func.count())
            .where(
                BlockerEvent.trading_date == trading_date,
                BlockerEvent.blocker_code.is_not(None),
            )
            .group_by(BlockerEvent.blocker_code)
            .order_by(func.count().desc())
            .limit(limit)
        )
        if session_type is not None:
            stmt = stmt.where(BlockerEvent.session_type == session_type)
        if instrument_id is not None:
            stmt = stmt.where(BlockerEvent.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(BlockerEvent.timeframe == timeframe)
        return [(str(code), int(count)) for code, count in self._session.execute(stmt)]

    def list_hourly_reports(
        self,
        *,
        trading_date: date,
        session_type: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
    ) -> list[HourlyReport]:
        stmt = select(HourlyReport).where(HourlyReport.trading_date == trading_date)
        if session_type is not None:
            stmt = stmt.where(HourlyReport.session_type == session_type)
        if instrument_id is not None:
            stmt = stmt.where(HourlyReport.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(HourlyReport.timeframe == timeframe)
        stmt = stmt.order_by(HourlyReport.started_at)
        return list(self._session.execute(stmt).scalars())

    def list_daily_reports(
        self,
        *,
        trading_date: date,
        session_type: str | None = None,
        instrument_id: str | None = None,
        timeframe: str | None = None,
    ) -> list[DailyReport]:
        stmt = select(DailyReport).where(DailyReport.trading_date == trading_date)
        if session_type is not None:
            stmt = stmt.where(DailyReport.session_type == session_type)
        if instrument_id is not None:
            stmt = stmt.where(DailyReport.instrument_id == instrument_id)
        if timeframe is not None:
            stmt = stmt.where(DailyReport.timeframe == timeframe)
        stmt = stmt.order_by(DailyReport.generated_at.desc())
        return list(self._session.execute(stmt).scalars())
