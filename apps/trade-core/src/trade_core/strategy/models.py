"""Typed models for strategy, risk, execution, and reconciliation engines."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from trade_core.broker_gateway import InstrumentRef
from trade_core.market_data import Bar, MarketState, Timeframe
from trade_core.session import SessionSnapshot
from trading_common.enums import SessionType

JsonPayload = dict[str, object]


class StrategyState(StrEnum):
    """Canonical strategy states stored in `strategy_state_event`."""

    IDLE = "idle"
    WARMING_UP = "warming_up"
    WAIT = "wait"
    CANDIDATE = "candidate"
    BLOCKED = "blocked"
    PLACING_ORDER = "placing_order"
    WORKING_ORDER = "working_order"
    PARTIALLY_FILLED = "partially_filled"
    IN_POSITION = "in_position"
    EXITING = "exiting"
    DEGRADED = "degraded"
    STOPPED = "stopped"


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class SignalAction(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    HOLD = "hold"


class OrderAction(StrEnum):
    PLACE = "place"
    CANCEL = "cancel"
    REPLACE = "replace"
    SKIP = "skip"


class BlockerCode(StrEnum):
    """Risk blocker taxonomy used by UI, reports, and counterfactual analytics."""

    SPREAD_TOO_WIDE = "spread_too_wide"
    MARKET_QUALITY_LOW = "market_quality_low"
    STALE_MARKET_DATA = "stale_market_data"
    NO_EDGE_AFTER_COSTS = "no_edge_after_costs"
    RISK_BUDGET_EXCEEDED = "risk_budget_exceeded"
    SESSION_FORBIDDEN = "session_forbidden"
    ORDER_TYPE_FORBIDDEN = "order_type_forbidden"
    MAX_DRAWDOWN_REACHED = "max_drawdown_reached"
    OPEN_ORDER_CONFLICT = "open_order_conflict"
    POSITION_LIMIT_REACHED = "position_limit_reached"


class CancelReasonCode(StrEnum):
    """Machine-readable cancel reasons accepted by the execution layer."""

    HOURLY_ROLLOVER = "hourly_rollover"
    EXCHANGE_SESSION_BOUNDARY = "exchange_session_boundary"
    STRATEGY_EXIT = "strategy_exit"
    RISK_REDUCTION = "risk_reduction"
    STALE_ORDER = "stale_order"
    PRICE_MOVED = "price_moved"
    MANUAL_OPERATOR_ACTION = "manual_operator_action"
    BROKER_REJECT_FOLLOWUP = "broker_reject_followup"


class RejectReasonCode(StrEnum):
    BROKER_REJECTED = "broker_rejected"
    TRANSPORT_ERROR = "transport_error"
    UNKNOWN_BROKER_ERROR = "unknown_broker_error"


@dataclass(frozen=True, slots=True)
class TimeframeStrategyRule:
    """One deterministic placeholder rule for a timeframe."""

    timeframe: Timeframe
    enabled: bool
    min_move_bps: Decimal
    lot_qty: int
    order_type: str = "limit"
    time_in_force: str = "day"
    expected_holding_minutes: int = 15
    min_expected_edge_bps: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class SessionStrategyTemplate:
    """Strategy settings scoped by `session_type`."""

    session_type: SessionType
    enabled: bool
    rules_by_timeframe: Mapping[Timeframe, TimeframeStrategyRule]


@dataclass(frozen=True, slots=True)
class ConfigDrivenStrategyConfig:
    """Versioned config consumed by the placeholder strategy engine."""

    strategy_id: str
    strategy_version: int
    session_templates: Mapping[SessionType, SessionStrategyTemplate]

    @classmethod
    def conservative_default(cls) -> ConfigDrivenStrategyConfig:
        """Return a deterministic config that emits candidates but claims no edge."""

        rules = {
            Timeframe.M5: TimeframeStrategyRule(
                timeframe=Timeframe.M5,
                enabled=True,
                min_move_bps=Decimal("12"),
                lot_qty=1,
                expected_holding_minutes=5,
            ),
            Timeframe.M10: TimeframeStrategyRule(
                timeframe=Timeframe.M10,
                enabled=True,
                min_move_bps=Decimal("18"),
                lot_qty=1,
                expected_holding_minutes=10,
            ),
            Timeframe.M15: TimeframeStrategyRule(
                timeframe=Timeframe.M15,
                enabled=True,
                min_move_bps=Decimal("24"),
                lot_qty=1,
                expected_holding_minutes=15,
            ),
        }
        return cls(
            strategy_id="baseline_config_stub",
            strategy_version=1,
            session_templates={
                SessionType.WEEKDAY_MORNING: SessionStrategyTemplate(
                    session_type=SessionType.WEEKDAY_MORNING,
                    enabled=True,
                    rules_by_timeframe=rules,
                ),
                SessionType.WEEKDAY_MAIN: SessionStrategyTemplate(
                    session_type=SessionType.WEEKDAY_MAIN,
                    enabled=True,
                    rules_by_timeframe=rules,
                ),
                SessionType.WEEKDAY_EVENING: SessionStrategyTemplate(
                    session_type=SessionType.WEEKDAY_EVENING,
                    enabled=True,
                    rules_by_timeframe=rules,
                ),
                SessionType.WEEKEND: SessionStrategyTemplate(
                    session_type=SessionType.WEEKEND,
                    enabled=False,
                    rules_by_timeframe=rules,
                ),
            },
        )


@dataclass(frozen=True, slots=True)
class StrategyEvaluationContext:
    instrument: InstrumentRef
    session_snapshot: SessionSnapshot
    latest_closed_bars: Mapping[Timeframe, Bar]
    market_state: MarketState | None
    current_state: StrategyState
    open_position_lots: int = 0
    now: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(frozen=True, slots=True)
class SignalCandidateDecision:
    """A strategy candidate before risk gates and execution."""

    strategy_id: str
    strategy_version: int
    instrument: InstrumentRef
    timeframe: Timeframe
    action: SignalAction
    side: TradeSide
    order_type: str
    lot_qty: int
    intended_price: Decimal | None
    time_in_force: str
    expected_edge_bps: Decimal
    expected_holding_minutes: int
    signal_fingerprint: str
    condition_payload: JsonPayload
    candidate_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    previous_state: StrategyState
    next_state: StrategyState
    candidates: tuple[SignalCandidateDecision, ...]
    reason_code: str | None = None
    decision_payload: JsonPayload = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_spread_bps: Decimal = Decimal("20")
    min_market_quality_score: Decimal = Decimal("0.70")
    max_data_age_ms: int = 5_000
    min_edge_after_costs_bps: Decimal = Decimal("0")
    assumed_cost_bps: Decimal = Decimal("4")
    risk_budget_remaining_rub: Decimal = Decimal("100000")
    max_daily_loss_rub: Decimal = Decimal("10000")
    current_daily_pnl_rub: Decimal = Decimal("0")
    max_position_lots: int = 10


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    open_position_lots: int = 0
    open_order_count: int = 0


@dataclass(frozen=True, slots=True)
class RiskAssessmentInput:
    candidate: SignalCandidateDecision
    session_snapshot: SessionSnapshot
    market_state: MarketState | None
    limits: RiskLimits
    portfolio: PortfolioSnapshot = PortfolioSnapshot()


@dataclass(frozen=True, slots=True)
class RiskBlocker:
    code: BlockerCode
    gate_name: str
    gate_rank: int
    passed: bool
    is_final_blocker: bool
    reason_payload: JsonPayload
    limit_value: Decimal | None = None
    observed_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    blockers: tuple[RiskBlocker, ...]

    @property
    def final_blocker(self) -> RiskBlocker | None:
        return next((blocker for blocker in self.blockers if blocker.is_final_blocker), None)


@dataclass(frozen=True, slots=True)
class OrderIntentRequest:
    candidate: SignalCandidateDecision
    session_snapshot: SessionSnapshot
    account_id: str
    execution_policy_version: int = 1
    run_id: UUID | None = None
    idempotency_key: str | None = None
    request_order_id: UUID | None = None
    order_action: OrderAction = OrderAction.PLACE
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(frozen=True, slots=True)
class OrderLifecycleResult:
    order_intent_id: UUID
    request_order_id: UUID
    status: str
    exchange_order_id: str | None
    broker_status: str | None
    payload: JsonPayload


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    observed_order_count: int
    updated_order_count: int
    payload: JsonPayload = field(default_factory=dict)
