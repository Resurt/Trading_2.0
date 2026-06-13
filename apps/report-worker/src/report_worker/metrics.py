"""Report-worker metrics helpers shared by HTTP and Celery task entrypoints."""

from __future__ import annotations

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
        REPORT_WORKER_METRICS.observe_report_generation_duration(
            perf_counter() - started_at,
            status=status,
        )


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
        REPORT_WORKER_METRICS.observe_report_generation_duration(
            perf_counter() - started_at,
            status=status,
        )
        REPORT_WORKER_METRICS.inc_counterfactual_job(status=status)


def sample_celery_queue_backlog(metrics: TradingMetrics) -> None:
    """Update Celery queue backlog gauge from Redis broker state."""

    broker_url = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    queue_name = os.getenv("CELERY_DEFAULT_QUEUE", "celery")
    try:
        client = Redis.from_url(
            broker_url,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            decode_responses=False,
        )
        metrics.set_celery_queue_backlog(cast(int, client.llen(queue_name)), status="ready")
    except RedisError:
        metrics.set_celery_queue_backlog(0, status="error")
