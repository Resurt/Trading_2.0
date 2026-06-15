"""Report-worker metrics helpers shared by HTTP and Celery task entrypoints."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import cast

from redis import Redis
from redis.exceptions import RedisError

from report_worker.app import create_identity
from trading_common import parse_runtime_mode
from trading_common.observability import TradingMetrics

REPORT_WORKER_METRICS = TradingMetrics(
    create_identity(parse_runtime_mode(os.getenv("TRADING_RUNTIME_MODE")))
)
REPORT_METRICS_DURATION_STREAM = "report_worker:metrics:report_generation_duration"
REPORT_METRICS_COUNTER_PREFIX = "report_worker:metrics:counter"
_LAST_COUNTER_TOTALS: dict[str, int] = {}


@contextmanager
def observe_report_generation() -> Iterator[None]:
    """Observe report task duration with success/error status."""

    started_at = perf_counter()
    status = "success"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        seconds = perf_counter() - started_at
        REPORT_WORKER_METRICS.observe_report_generation_duration(
            seconds,
            status=status,
        )
        if status == "error":
            REPORT_WORKER_METRICS.inc_report_job_failed(status=status)
            _publish_counter("report_jobs_failed_total", status=status)
        _publish_report_duration(seconds=seconds, status=status)


@contextmanager
def observe_counterfactual_job() -> Iterator[None]:
    """Observe counterfactual job count and duration."""

    started_at = perf_counter()
    status = "success"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        seconds = perf_counter() - started_at
        REPORT_WORKER_METRICS.observe_report_generation_duration(
            seconds,
            status=status,
        )
        REPORT_WORKER_METRICS.inc_counterfactual_job(status=status)
        if status == "error":
            REPORT_WORKER_METRICS.inc_report_job_failed(status=status)
            _publish_counter("report_jobs_failed_total", status=status)
        _publish_report_duration(seconds=seconds, status=status)
        _publish_counter("counterfactual_jobs_total", status=status)


def sample_report_worker_metrics(metrics: TradingMetrics) -> None:
    """Update queue and task metrics from Redis-backed Celery worker signals."""

    client = _redis_client()
    if client is None:
        metrics.set_celery_queue_backlog(0, status="error")
        return
    _sample_celery_queue_backlog(metrics, client)
    _sample_report_duration_metrics(metrics, client)
    _sample_counter_metrics(metrics, client)


def sample_celery_queue_backlog(metrics: TradingMetrics) -> None:
    """Update Celery queue backlog gauge from Redis broker state."""

    sample_report_worker_metrics(metrics)


def _sample_celery_queue_backlog(metrics: TradingMetrics, client: Redis) -> None:
    queue_name = os.getenv("CELERY_DEFAULT_QUEUE", "reports")
    try:
        metrics.set_celery_queue_backlog(cast(int, client.llen(queue_name)), status="ready")
    except RedisError:
        metrics.set_celery_queue_backlog(0, status="error")


def _sample_report_duration_metrics(metrics: TradingMetrics, client: Redis) -> None:
    for _ in range(1000):
        try:
            raw = client.lpop(REPORT_METRICS_DURATION_STREAM)
        except RedisError:
            return
        if raw is None:
            return
        try:
            payload = json.loads(_decode_redis_value(raw))
            seconds = float(payload["seconds"])
            status = str(payload["status"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        metrics.observe_report_generation_duration(seconds, status=status)


def _sample_counter_metrics(metrics: TradingMetrics, client: Redis) -> None:
    for metric_name in ("counterfactual_jobs_total", "report_jobs_failed_total"):
        for status in ("success", "error"):
            key = _counter_key(metric_name, status)
            try:
                total = int(cast(int | bytes | str | None, client.get(key)) or 0)
            except (RedisError, TypeError, ValueError):
                continue
            last = _LAST_COUNTER_TOTALS.get(key, 0)
            delta = max(total - last, 0)
            if delta == 0:
                continue
            _LAST_COUNTER_TOTALS[key] = total
            if metric_name == "counterfactual_jobs_total":
                for _ in range(delta):
                    metrics.inc_counterfactual_job(status=status)
            if metric_name == "report_jobs_failed_total":
                for _ in range(delta):
                    metrics.inc_report_job_failed(status=status)


def _publish_report_duration(*, seconds: float, status: str) -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        client.rpush(
            REPORT_METRICS_DURATION_STREAM,
            json.dumps({"seconds": seconds, "status": status}),
        )
        client.ltrim(REPORT_METRICS_DURATION_STREAM, -10000, -1)
    except RedisError:
        return


def _publish_counter(metric_name: str, *, status: str) -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        client.incr(_counter_key(metric_name, status))
    except RedisError:
        return


def _counter_key(metric_name: str, status: str) -> str:
    return f"{REPORT_METRICS_COUNTER_PREFIX}:{metric_name}:{status}"


def _redis_client() -> Redis | None:
    broker_url = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    try:
        return Redis.from_url(
            broker_url,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            decode_responses=False,
        )
    except RedisError:
        return None


def _decode_redis_value(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
