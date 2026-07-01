from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from trade_core.session import (
    BrokerTradingStatus,
    HourlyMicroSessionConfig,
    HourlyMicroSessionManager,
    InMemorySessionStateStore,
    OrderSessionPolicy,
    ScheduleWindow,
    SessionManager,
    SqlAlchemySessionStateStore,
    TradingSchedule,
)
from trade_core.session.reason_codes import (
    EXCHANGE_SESSION_BOUNDARY,
    HOURLY_ROLLOVER,
    PHASE_FORBIDDEN,
    WEEKEND_BROKER_MODE,
)
from trading_common.db.base import Base
from trading_common.db.models import MicroSession, ReportJobOutbox, SessionRun, StrategyStateEvent
from trading_common.enums import SessionPhase, SessionType

MSK = ZoneInfo("Europe/Moscow")


def msk(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=MSK)


def status(value: str = "normal_trading", *, available: bool = True) -> BrokerTradingStatus:
    return BrokerTradingStatus(status=value, api_trade_available=available)


def window(
    session_type: SessionType,
    phase: SessionPhase,
    start_at: datetime,
    end_at: datetime,
    *,
    trading_date: date | None = None,
) -> ScheduleWindow:
    return ScheduleWindow(
        session_type=session_type,
        session_phase=phase,
        start_at=start_at,
        end_at=end_at,
        trading_date=trading_date or start_at.date(),
        calendar_date=start_at.date(),
    )


def schedule(*windows: ScheduleWindow) -> TradingSchedule:
    return TradingSchedule(windows=windows)


def weekday_schedule() -> TradingSchedule:
    return schedule(
        window(
            SessionType.WEEKDAY_MORNING,
            SessionPhase.CONTINUOUS_TRADING,
            msk(2026, 6, 12, 7),
            msk(2026, 6, 12, 10),
        ),
        window(
            SessionType.WEEKDAY_MAIN,
            SessionPhase.CONTINUOUS_TRADING,
            msk(2026, 6, 12, 10),
            msk(2026, 6, 12, 19),
        ),
        window(
            SessionType.WEEKDAY_EVENING,
            SessionPhase.CONTINUOUS_TRADING,
            msk(2026, 6, 12, 19),
            msk(2026, 6, 12, 23, 50),
        ),
    )


def test_session_manager_transitions_weekday_morning_to_main() -> None:
    manager = SessionManager()

    morning = manager.evaluate(
        now=msk(2026, 6, 12, 9, 59, 30),
        schedule=weekday_schedule(),
        broker_status=status(),
    )
    main = manager.evaluate(
        now=msk(2026, 6, 12, 10),
        schedule=weekday_schedule(),
        broker_status=status(),
    )

    assert morning.session_type is SessionType.WEEKDAY_MORNING
    assert morning.trading_date == date(2026, 6, 12)
    assert morning.calendar_date == date(2026, 6, 12)
    assert morning.is_trading_allowed
    assert main.session_type is SessionType.WEEKDAY_MAIN
    assert main.session_phase is SessionPhase.CONTINUOUS_TRADING


def test_session_manager_transitions_main_to_evening_half_open() -> None:
    manager = SessionManager()

    main = manager.evaluate(
        now=msk(2026, 6, 12, 18, 59, 30),
        schedule=weekday_schedule(),
        broker_status=status(),
    )
    evening = manager.evaluate(
        now=msk(2026, 6, 12, 19),
        schedule=weekday_schedule(),
        broker_status=status(),
    )

    assert main.session_type is SessionType.WEEKDAY_MAIN
    assert main.session_phase is SessionPhase.CONTINUOUS_TRADING
    assert main.is_trading_allowed
    assert evening.session_type is SessionType.WEEKDAY_EVENING
    assert evening.is_trading_allowed


def test_weekend_session_keeps_trading_date_separate_and_blocks_dealer_mode() -> None:
    manager = SessionManager()
    weekend_schedule = schedule(
        window(
            SessionType.WEEKEND,
            SessionPhase.CONTINUOUS_TRADING,
            msk(2026, 6, 13, 10),
            msk(2026, 6, 13, 14),
            trading_date=date(2026, 6, 15),
        )
    )

    snapshot = manager.evaluate(
        now=msk(2026, 6, 13, 11),
        schedule=weekend_schedule,
        broker_status=status(),
    )
    dealer_snapshot = manager.evaluate(
        now=msk(2026, 6, 13, 11, 5),
        schedule=weekend_schedule,
        broker_status=status("dealer_normal_trading", available=False),
    )

    assert snapshot.session_type is SessionType.WEEKEND
    assert snapshot.calendar_date == date(2026, 6, 13)
    assert snapshot.trading_date == date(2026, 6, 15)
    assert snapshot.is_trading_allowed
    assert dealer_snapshot.session_phase is SessionPhase.DEALER_MODE
    assert dealer_snapshot.deny_reason_code == WEEKEND_BROKER_MODE


def test_hourly_micro_session_rollover_freezes_closes_reports_and_reopens() -> None:
    manager = SessionManager()
    store = InMemorySessionStateStore()
    micro_sessions = HourlyMicroSessionManager(
        store=store,
        config=HourlyMicroSessionConfig(freeze_seconds=90),
    )
    active_schedule = schedule(
        window(
            SessionType.WEEKDAY_MAIN,
            SessionPhase.CONTINUOUS_TRADING,
            msk(2026, 6, 12, 10),
            msk(2026, 6, 12, 12),
        )
    )

    opened = micro_sessions.on_snapshot(
        manager.evaluate(
            now=msk(2026, 6, 12, 10, 15),
            schedule=active_schedule,
            broker_status=status(),
        )
    )
    frozen = micro_sessions.on_snapshot(
        manager.evaluate(
            now=msk(2026, 6, 12, 10, 58, 31),
            schedule=active_schedule,
            broker_status=status(),
        )
    )
    rolled = micro_sessions.on_snapshot(
        manager.evaluate(
            now=msk(2026, 6, 12, 11),
            schedule=active_schedule,
            broker_status=status(),
        )
    )

    assert [event.event_type for event in opened.events] == ["session_run_opened"]
    assert [event.event_type for event in frozen.events] == ["freeze_new_entries"]
    assert [event.event_type for event in rolled.events] == [
        "snapshot_taken",
        "session_run_closed",
        "report_requested",
        "session_run_opened",
    ]
    assert rolled.events[1].payload["reason_code"] == HOURLY_ROLLOVER
    assert rolled.active_state is not None
    assert rolled.active_state.micro_session_id.endswith("20260612T1100")


def test_micro_session_state_is_persisted_to_database() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = SessionManager()
    active_schedule = schedule(
        window(
            SessionType.WEEKDAY_MAIN,
            SessionPhase.CONTINUOUS_TRADING,
            msk(2026, 6, 12, 10),
            msk(2026, 6, 12, 12),
        )
    )

    with Session(engine) as db_session:
        store = SqlAlchemySessionStateStore(
            db_session,
            strategy_id="baseline",
            strategy_version=1,
        )
        micro_sessions = HourlyMicroSessionManager(
            store=store,
            config=HourlyMicroSessionConfig(freeze_seconds=60),
        )
        for moment in (
            msk(2026, 6, 12, 10, 15),
            msk(2026, 6, 12, 10, 59),
            msk(2026, 6, 12, 11),
        ):
            micro_sessions.on_snapshot(
                manager.evaluate(
                    now=moment,
                    schedule=active_schedule,
                    broker_status=status(),
                )
            )

        runs = list(
            db_session.execute(select(SessionRun).order_by(SessionRun.started_at)).scalars()
        )
        micro_session_rows = list(
            db_session.execute(
                select(MicroSession).order_by(MicroSession.started_at)
            ).scalars()
        )
        event_types = list(
            db_session.execute(
                select(StrategyStateEvent.event_type).order_by(
                    StrategyStateEvent.ts_utc,
                    StrategyStateEvent.event_type,
                )
            ).scalars()
        )

        assert len(runs) == 2
        assert len(micro_session_rows) == 2
        assert runs[0].status == "closed"
        assert micro_session_rows[0].status == "closed"
        assert micro_session_rows[0].rollover_reason_code == HOURLY_ROLLOVER
        assert micro_session_rows[1].status == "open"
        assert runs[0].freeze_started_at == msk(2026, 6, 12, 10, 59).replace(tzinfo=None)
        assert runs[0].report_requested_at == msk(2026, 6, 12, 11).replace(tzinfo=None)
        assert runs[0].close_reason_code == HOURLY_ROLLOVER
        report_jobs = list(db_session.execute(select(ReportJobOutbox)).scalars())
        assert len(report_jobs) == 1
        assert report_jobs[0].task_name == "report_worker.build_hourly_report"
        assert report_jobs[0].micro_session_id == runs[0].micro_session_id
        assert report_jobs[0].strategy_id == "baseline"
        assert report_jobs[0].status == "pending"
        assert "snapshot_taken" in event_types
        assert "session_run_closed" in event_types
        assert "report_requested" in event_types

    engine.dispose()


def test_auction_and_break_boundaries_do_not_open_micro_sessions() -> None:
    manager = SessionManager()
    policy = OrderSessionPolicy()
    micro_sessions = HourlyMicroSessionManager(store=InMemorySessionStateStore())
    auction_schedule = schedule(
        window(
            SessionType.WEEKDAY_MORNING,
            SessionPhase.OPENING_AUCTION,
            msk(2026, 6, 12, 6, 50),
            msk(2026, 6, 12, 7),
        ),
        window(
            SessionType.WEEKDAY_MORNING,
            SessionPhase.CONTINUOUS_TRADING,
            msk(2026, 6, 12, 7),
            msk(2026, 6, 12, 10),
        ),
    )

    auction_snapshot = manager.evaluate(
        now=msk(2026, 6, 12, 6, 55),
        schedule=auction_schedule,
        broker_status=status(),
    )
    break_schedule = schedule(
        window(
            SessionType.WEEKDAY_MAIN,
            SessionPhase.CONTINUOUS_TRADING,
            msk(2026, 6, 12, 10),
            msk(2026, 6, 12, 11),
        ),
        window(
            SessionType.WEEKDAY_MAIN,
            SessionPhase.BREAK,
            msk(2026, 6, 12, 11),
            msk(2026, 6, 12, 12),
        ),
    )
    break_snapshot = manager.evaluate(
        now=msk(2026, 6, 12, 11, 30),
        schedule=break_schedule,
        broker_status=status(),
    )

    assert auction_snapshot.session_phase is SessionPhase.OPENING_AUCTION
    assert policy.evaluate(
        snapshot=auction_snapshot,
        action="entry",
        order_type="limit",
    ).reason_code == PHASE_FORBIDDEN
    assert micro_sessions.on_snapshot(auction_snapshot).active_state is None
    assert break_snapshot.session_phase is SessionPhase.BREAK
    assert micro_sessions.on_snapshot(break_snapshot).active_state is None


def test_schedule_and_broker_status_mismatch_blocks_new_entries() -> None:
    manager = SessionManager()
    policy = OrderSessionPolicy()
    active_schedule = schedule(
        window(
            SessionType.WEEKDAY_MAIN,
            SessionPhase.CONTINUOUS_TRADING,
            msk(2026, 6, 12, 10),
            msk(2026, 6, 12, 11),
        )
    )

    snapshot = manager.evaluate(
        now=msk(2026, 6, 12, 10, 30),
        schedule=active_schedule,
        broker_status=status("not_available_for_trading", available=False),
    )
    permission = policy.evaluate(snapshot=snapshot, action="entry", order_type="limit")

    assert snapshot.status_mismatch
    assert snapshot.session_phase is SessionPhase.CLOSED
    assert not snapshot.is_trading_allowed
    assert permission.reason_code == PHASE_FORBIDDEN
    assert permission.allowed is False


def test_session_boundary_closes_old_micro_session_as_exchange_boundary() -> None:
    manager = SessionManager()
    micro_sessions = HourlyMicroSessionManager(store=InMemorySessionStateStore())

    micro_sessions.on_snapshot(
        manager.evaluate(
            now=msk(2026, 6, 12, 9, 30),
            schedule=weekday_schedule(),
            broker_status=status(),
        )
    )
    boundary = micro_sessions.on_snapshot(
        manager.evaluate(
            now=msk(2026, 6, 12, 10),
            schedule=weekday_schedule(),
            broker_status=status(),
        )
    )

    assert [event.event_type for event in boundary.events] == [
        "snapshot_taken",
        "session_run_closed",
        "report_requested",
        "session_run_opened",
    ]
    assert boundary.events[1].payload["reason_code"] == EXCHANGE_SESSION_BOUNDARY
    assert boundary.events[0].micro_session_id.startswith("2026-06-12:weekday_morning")
    assert boundary.active_state is not None
    assert boundary.active_state.session_type is SessionType.WEEKDAY_MAIN
