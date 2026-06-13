"""Prometheus metrics registration for the trading stack."""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from trading_common.models import AppIdentity, HealthStatus

PROMETHEUS_METRIC_NAMES: tuple[str, ...] = (
    "broker_post_order_latency_seconds",
    "order_state_convergence_seconds",
    "candle_close_delivery_lag_seconds",
    "session_rollover_duration_seconds",
    "reconnect_total",
    "rejected_orders_total",
    "risk_events_total",
    "open_orders",
    "active_positions",
    "market_stream_alive",
    "last_closed_candle_age_seconds",
)

BOUNDED_PROMETHEUS_LABELS: tuple[str, ...] = (
    "service",
    "runtime_mode",
    "broker_method",
    "session_type",
    "session_phase",
    "instrument_id",
    "timeframe",
    "reason_code",
    "stream_name",
)

LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
LAG_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0)


class TradingMetrics:
    """Owns the Prometheus collectors for one service process."""

    def __init__(
        self,
        identity: AppIdentity,
        *,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self.identity = identity
        self.registry = registry or CollectorRegistry()
        self._base_labels = {
            "service": identity.service.value,
            "runtime_mode": identity.runtime_mode.value,
        }
        self.service_up = Gauge(
            "trading_service_up",
            "Service health status exposed by the local service.",
            ("service", "runtime_mode"),
            registry=self.registry,
        )
        self.service_info = Gauge(
            "trading_service_info",
            "Static service identity information.",
            ("service", "version", "runtime_mode"),
            registry=self.registry,
        )
        self.broker_post_order_latency_seconds = Histogram(
            "broker_post_order_latency_seconds",
            "Latency of broker PostOrder calls in seconds.",
            ("service", "runtime_mode", "broker_method"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.order_state_convergence_seconds = Histogram(
            "order_state_convergence_seconds",
            "Time until local and broker order state converge.",
            ("service", "runtime_mode"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.candle_close_delivery_lag_seconds = Histogram(
            "candle_close_delivery_lag_seconds",
            "Lag between exchange candle close and local delivery.",
            ("service", "runtime_mode", "instrument_id", "timeframe"),
            buckets=LAG_BUCKETS,
            registry=self.registry,
        )
        self.session_rollover_duration_seconds = Histogram(
            "session_rollover_duration_seconds",
            "Duration of hourly micro-session rollover.",
            ("service", "runtime_mode", "session_type"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.reconnect_total = Counter(
            "reconnect_total",
            "Reconnect attempts by stream.",
            ("service", "runtime_mode", "stream_name"),
            registry=self.registry,
        )
        self.rejected_orders_total = Counter(
            "rejected_orders_total",
            "Orders rejected by reason code.",
            ("service", "runtime_mode", "reason_code"),
            registry=self.registry,
        )
        self.risk_events_total = Counter(
            "risk_events_total",
            "Risk events by machine-readable reason code.",
            ("service", "runtime_mode", "reason_code"),
            registry=self.registry,
        )
        self.open_orders = Gauge(
            "open_orders",
            "Current open order count.",
            ("service", "runtime_mode"),
            registry=self.registry,
        )
        self.active_positions = Gauge(
            "active_positions",
            "Current active positions by instrument.",
            ("service", "runtime_mode", "instrument_id"),
            registry=self.registry,
        )
        self.market_stream_alive = Gauge(
            "market_stream_alive",
            "Whether a market stream is currently alive.",
            ("service", "runtime_mode", "stream_name"),
            registry=self.registry,
        )
        self.last_closed_candle_age_seconds = Gauge(
            "last_closed_candle_age_seconds",
            "Age of last closed candle by instrument and timeframe.",
            ("service", "runtime_mode", "instrument_id", "timeframe"),
            registry=self.registry,
        )
        self.set_service_health(HealthStatus.OK)

    def set_service_health(self, status: HealthStatus) -> None:
        self.service_up.labels(**self._base_labels).set(1 if status is HealthStatus.OK else 0)
        self.service_info.labels(
            service=self.identity.service.value,
            version=self.identity.version,
            runtime_mode=self.identity.runtime_mode.value,
        ).set(1)

    def observe_broker_post_order_latency(
        self,
        seconds: float,
        *,
        broker_method: str = "PostOrder",
    ) -> None:
        self.broker_post_order_latency_seconds.labels(
            **self._base_labels,
            broker_method=broker_method,
        ).observe(seconds)

    def observe_order_state_convergence(self, seconds: float) -> None:
        self.order_state_convergence_seconds.labels(**self._base_labels).observe(seconds)

    def observe_candle_close_delivery_lag(
        self,
        seconds: float,
        *,
        instrument_id: str,
        timeframe: str,
    ) -> None:
        self.candle_close_delivery_lag_seconds.labels(
            **self._base_labels,
            instrument_id=instrument_id,
            timeframe=timeframe,
        ).observe(seconds)

    def observe_session_rollover_duration(self, seconds: float, *, session_type: str) -> None:
        self.session_rollover_duration_seconds.labels(
            **self._base_labels,
            session_type=session_type,
        ).observe(seconds)

    def inc_reconnect(self, *, stream_name: str) -> None:
        self.reconnect_total.labels(**self._base_labels, stream_name=stream_name).inc()

    def inc_rejected_order(self, *, reason_code: str) -> None:
        self.rejected_orders_total.labels(**self._base_labels, reason_code=reason_code).inc()

    def inc_risk_event(self, *, reason_code: str) -> None:
        self.risk_events_total.labels(**self._base_labels, reason_code=reason_code).inc()

    def set_open_orders(self, count: int) -> None:
        self.open_orders.labels(**self._base_labels).set(count)

    def set_active_positions(self, count: int, *, instrument_id: str) -> None:
        self.active_positions.labels(
            **self._base_labels,
            instrument_id=instrument_id,
        ).set(count)

    def set_market_stream_alive(self, alive: bool, *, stream_name: str) -> None:
        self.market_stream_alive.labels(**self._base_labels, stream_name=stream_name).set(
            1 if alive else 0
        )

    def set_last_closed_candle_age(
        self,
        seconds: float,
        *,
        instrument_id: str,
        timeframe: str,
    ) -> None:
        self.last_closed_candle_age_seconds.labels(
            **self._base_labels,
            instrument_id=instrument_id,
            timeframe=timeframe,
        ).set(seconds)

    def render(self) -> bytes:
        return generate_latest(self.registry)
