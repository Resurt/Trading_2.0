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
    "report_generation_duration_seconds",
    "gap_recovery_duration_seconds",
    "stream_reconnect_total",
    "recovered_candles_total",
    "reconciliation_mismatch_total",
    "rejected_orders_total",
    "risk_events_total",
    "emergency_stop_total",
    "emergency_cancel_failed_total",
    "counterfactual_jobs_total",
    "report_jobs_failed_total",
    "market_stream_alive",
    "last_stream_message_age_seconds",
    "open_orders",
    "active_positions",
    "working_orders_after_stop",
    "celery_queue_backlog",
)

BOUNDED_PROMETHEUS_LABELS: tuple[str, ...] = (
    "service",
    "instrument",
    "timeframe",
    "session_type",
    "stream_type",
    "status",
    "result",
)

LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
LAG_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0, 120.0)
QUEUE_BACKLOG_STATUS_READY = "ready"


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
        self._base_labels = {"service": identity.service.value}
        self.service_up = Gauge(
            "trading_service_up",
            "Service health status exposed by the local service.",
            ("service",),
            registry=self.registry,
        )
        self.service_info = Gauge(
            "trading_service_info",
            "Static service identity information.",
            ("service",),
            registry=self.registry,
        )
        self.broker_post_order_latency_seconds = Histogram(
            "broker_post_order_latency_seconds",
            "Latency of broker PostOrder calls in seconds.",
            ("service", "status"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.order_state_convergence_seconds = Histogram(
            "order_state_convergence_seconds",
            "Time until local and broker order state converge.",
            ("service", "status"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.candle_close_delivery_lag_seconds = Histogram(
            "candle_close_delivery_lag_seconds",
            "Lag between exchange candle close and local delivery.",
            ("service", "instrument", "timeframe"),
            buckets=LAG_BUCKETS,
            registry=self.registry,
        )
        self.session_rollover_duration_seconds = Histogram(
            "session_rollover_duration_seconds",
            "Duration of hourly micro-session rollover.",
            ("service", "session_type", "status"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.report_generation_duration_seconds = Histogram(
            "report_generation_duration_seconds",
            "Duration of hourly, daily and counterfactual report generation.",
            ("service", "status"),
            buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0),
            registry=self.registry,
        )
        self.gap_recovery_duration_seconds = Histogram(
            "gap_recovery_duration_seconds",
            "Duration of stream gap recovery attempts.",
            ("service", "stream_type", "status"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.stream_reconnect_total = Counter(
            "stream_reconnect_total",
            "Reconnect attempts by stream type.",
            ("service", "stream_type", "result"),
            registry=self.registry,
        )
        self.recovered_candles_total = Counter(
            "recovered_candles_total",
            "Closed candles recovered by gap backfill.",
            ("service", "instrument", "timeframe", "status"),
            registry=self.registry,
        )
        self.reconciliation_mismatch_total = Counter(
            "reconciliation_mismatch_total",
            "Detected reconciliation mismatches by bounded result code.",
            ("service", "result"),
            registry=self.registry,
        )
        self.rejected_orders_total = Counter(
            "rejected_orders_total",
            "Orders rejected, cancelled, or not accepted by machine-readable status.",
            ("service", "status"),
            registry=self.registry,
        )
        self.risk_events_total = Counter(
            "risk_events_total",
            "Risk and blocker events by bounded result code.",
            ("service", "result"),
            registry=self.registry,
        )
        self.emergency_stop_total = Counter(
            "emergency_stop_total",
            "Operator emergency-stop commands applied by result.",
            ("service", "result"),
            registry=self.registry,
        )
        self.emergency_cancel_failed_total = Counter(
            "emergency_cancel_failed_total",
            "Emergency-stop order cancellations that failed.",
            ("service", "result"),
            registry=self.registry,
        )
        self.counterfactual_jobs_total = Counter(
            "counterfactual_jobs_total",
            "Counterfactual analysis jobs by completion status.",
            ("service", "status"),
            registry=self.registry,
        )
        self.report_jobs_failed_total = Counter(
            "report_jobs_failed_total",
            "Report-worker jobs that failed before producing a completed result.",
            ("service", "status"),
            registry=self.registry,
        )
        self.market_stream_alive = Gauge(
            "market_stream_alive",
            "Whether a market stream is currently alive.",
            ("service", "stream_type", "instrument", "timeframe"),
            registry=self.registry,
        )
        self.last_stream_message_age_seconds = Gauge(
            "last_stream_message_age_seconds",
            "Age of the latest stream message by stream and market context.",
            ("service", "stream_type", "instrument", "timeframe"),
            registry=self.registry,
        )
        self.open_orders = Gauge(
            "open_orders",
            "Current open order count.",
            ("service",),
            registry=self.registry,
        )
        self.active_positions = Gauge(
            "active_positions",
            "Current active positions by instrument.",
            ("service", "instrument"),
            registry=self.registry,
        )
        self.working_orders_after_stop = Gauge(
            "working_orders_after_stop",
            "Working orders remaining after stop or emergency-stop policy.",
            ("service",),
            registry=self.registry,
        )
        self.celery_queue_backlog = Gauge(
            "celery_queue_backlog",
            "Current report-worker Celery queue backlog.",
            ("service", "status"),
            registry=self.registry,
        )
        self.set_service_health(HealthStatus.OK)
        self._initialize_default_series()

    def set_service_health(self, status: HealthStatus) -> None:
        self.service_up.labels(service=self.identity.service.value).set(
            1 if status is HealthStatus.OK else 0
        )
        self.service_info.labels(service=self.identity.service.value).set(1)

    def observe_broker_post_order_latency(
        self,
        seconds: float,
        *,
        status: str = "success",
        broker_method: str | None = None,
    ) -> None:
        """Record broker PostOrder latency.

        ``broker_method`` is accepted for compatibility with the earlier helper API but is
        intentionally not exported as a label; the metric is scoped to PostOrder by name.
        """

        del broker_method
        self.broker_post_order_latency_seconds.labels(
            **self._base_labels,
            status=status,
        ).observe(seconds)

    def observe_order_state_convergence(self, seconds: float, *, status: str = "success") -> None:
        self.order_state_convergence_seconds.labels(
            **self._base_labels,
            status=status,
        ).observe(seconds)

    def observe_candle_close_delivery_lag(
        self,
        seconds: float,
        *,
        instrument: str | None = None,
        instrument_id: str | None = None,
        timeframe: str,
    ) -> None:
        self.candle_close_delivery_lag_seconds.labels(
            **self._base_labels,
            instrument=self._instrument_label(instrument=instrument, instrument_id=instrument_id),
            timeframe=timeframe,
        ).observe(seconds)

    def observe_session_rollover_duration(
        self,
        seconds: float,
        *,
        session_type: str,
        status: str = "success",
    ) -> None:
        self.session_rollover_duration_seconds.labels(
            **self._base_labels,
            session_type=session_type,
            status=status,
        ).observe(seconds)

    def observe_report_generation_duration(
        self,
        seconds: float,
        *,
        status: str = "success",
    ) -> None:
        self.report_generation_duration_seconds.labels(
            **self._base_labels,
            status=status,
        ).observe(seconds)

    def observe_gap_recovery_duration(
        self,
        seconds: float,
        *,
        stream_type: str,
        status: str = "success",
    ) -> None:
        self.gap_recovery_duration_seconds.labels(
            **self._base_labels,
            stream_type=stream_type,
            status=status,
        ).observe(seconds)

    def inc_stream_reconnect(self, *, stream_type: str, result: str = "attempt") -> None:
        self.stream_reconnect_total.labels(
            **self._base_labels,
            stream_type=stream_type,
            result=result,
        ).inc()

    def inc_recovered_candle(
        self,
        *,
        instrument: str,
        timeframe: str,
        status: str = "success",
    ) -> None:
        self.recovered_candles_total.labels(
            **self._base_labels,
            instrument=instrument,
            timeframe=timeframe,
            status=status,
        ).inc()

    def inc_reconciliation_mismatch(self, *, result: str = "unknown") -> None:
        self.reconciliation_mismatch_total.labels(**self._base_labels, result=result).inc()

    def inc_reconnect(self, *, stream_name: str) -> None:
        """Backward-compatible alias for the previous ``reconnect_total`` helper."""

        self.inc_stream_reconnect(stream_type=stream_name)

    def inc_rejected_order(
        self,
        *,
        status: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        self.rejected_orders_total.labels(
            **self._base_labels,
            status=status or reason_code or "unknown",
        ).inc()

    def inc_risk_event(self, *, result: str | None = None, reason_code: str | None = None) -> None:
        self.risk_events_total.labels(
            **self._base_labels,
            result=result or reason_code or "unknown",
        ).inc()

    def inc_emergency_stop(self, *, result: str = "applied") -> None:
        self.emergency_stop_total.labels(**self._base_labels, result=result).inc()

    def inc_emergency_cancel_failed(self, *, result: str = "error") -> None:
        self.emergency_cancel_failed_total.labels(**self._base_labels, result=result).inc()

    def inc_counterfactual_job(self, *, status: str = "success") -> None:
        self.counterfactual_jobs_total.labels(**self._base_labels, status=status).inc()

    def inc_report_job_failed(self, *, status: str = "error") -> None:
        self.report_jobs_failed_total.labels(**self._base_labels, status=status).inc()

    def set_open_orders(self, count: int) -> None:
        self.open_orders.labels(**self._base_labels).set(count)

    def set_active_positions(
        self,
        count: int,
        *,
        instrument: str | None = None,
        instrument_id: str | None = None,
    ) -> None:
        self.active_positions.labels(
            **self._base_labels,
            instrument=self._instrument_label(instrument=instrument, instrument_id=instrument_id),
        ).set(count)

    def set_working_orders_after_stop(self, count: int) -> None:
        self.working_orders_after_stop.labels(**self._base_labels).set(count)

    def set_market_stream_alive(
        self,
        alive: bool,
        *,
        stream_type: str | None = None,
        stream_name: str | None = None,
        instrument: str = "all",
        timeframe: str = "all",
    ) -> None:
        self.market_stream_alive.labels(
            **self._base_labels,
            stream_type=stream_type or stream_name or "market_data",
            instrument=instrument,
            timeframe=timeframe,
        ).set(1 if alive else 0)

    def set_last_stream_message_age(
        self,
        seconds: float,
        *,
        stream_type: str,
        instrument: str = "all",
        timeframe: str = "all",
    ) -> None:
        self.last_stream_message_age_seconds.labels(
            **self._base_labels,
            stream_type=stream_type,
            instrument=instrument,
            timeframe=timeframe,
        ).set(seconds)

    def set_last_closed_candle_age(
        self,
        seconds: float,
        *,
        instrument_id: str,
        timeframe: str,
    ) -> None:
        """Backward-compatible alias for the newer stream freshness gauge."""

        self.set_last_stream_message_age(
            seconds,
            stream_type="candles",
            instrument=instrument_id,
            timeframe=timeframe,
        )

    def set_celery_queue_backlog(
        self,
        count: int,
        *,
        status: str = QUEUE_BACKLOG_STATUS_READY,
    ) -> None:
        self.celery_queue_backlog.labels(**self._base_labels, status=status).set(count)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def _initialize_default_series(self) -> None:
        self.broker_post_order_latency_seconds.labels(**self._base_labels, status="success")
        self.order_state_convergence_seconds.labels(**self._base_labels, status="success")
        self.candle_close_delivery_lag_seconds.labels(
            **self._base_labels,
            instrument="all",
            timeframe="all",
        )
        self.session_rollover_duration_seconds.labels(
            **self._base_labels,
            session_type="unknown",
            status="success",
        )
        self.report_generation_duration_seconds.labels(**self._base_labels, status="success")
        self.gap_recovery_duration_seconds.labels(
            **self._base_labels,
            stream_type="market_data",
            status="success",
        )
        self.stream_reconnect_total.labels(
            **self._base_labels,
            stream_type="market_data",
            result="attempt",
        ).inc(0)
        self.recovered_candles_total.labels(
            **self._base_labels,
            instrument="all",
            timeframe="all",
            status="success",
        ).inc(0)
        self.reconciliation_mismatch_total.labels(
            **self._base_labels,
            result="unknown",
        ).inc(0)
        self.rejected_orders_total.labels(**self._base_labels, status="unknown").inc(0)
        self.risk_events_total.labels(**self._base_labels, result="unknown").inc(0)
        self.emergency_stop_total.labels(**self._base_labels, result="applied").inc(0)
        self.emergency_cancel_failed_total.labels(**self._base_labels, result="error").inc(0)
        self.counterfactual_jobs_total.labels(**self._base_labels, status="success").inc(0)
        self.report_jobs_failed_total.labels(**self._base_labels, status="error").inc(0)
        self.set_market_stream_alive(False, stream_type="market_data")
        self.set_last_stream_message_age(0, stream_type="market_data")
        self.set_open_orders(0)
        self.set_active_positions(0, instrument="all")
        self.set_working_orders_after_stop(0)
        self.set_celery_queue_backlog(0)

    @staticmethod
    def _instrument_label(*, instrument: str | None, instrument_id: str | None) -> str:
        return instrument or instrument_id or "unknown"
