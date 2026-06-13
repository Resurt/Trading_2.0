"""Minimal HTTP health and metrics server for local compose smoke checks."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

from trading_common.models import ServiceHealth
from trading_common.observability.metrics import TradingMetrics
from trading_common.telemetry import log_event

CONTENT_TYPE_JSON: Final = "application/json; charset=utf-8"
CONTENT_TYPE_TEXT: Final = "text/plain; version=0.0.4; charset=utf-8"


def render_health(health: ServiceHealth) -> bytes:
    """Render a health payload as JSON bytes."""

    payload = {
        "service": health.identity.service,
        "version": health.identity.version,
        "runtime_mode": health.identity.runtime_mode,
        "status": health.status,
        "detail": health.detail,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def render_metrics(target: ServiceHealth | TradingMetrics) -> bytes:
    """Render Prometheus metrics payload."""

    if isinstance(target, ServiceHealth):
        metrics = TradingMetrics(target.identity)
        metrics.set_service_health(target.status)
        return metrics.render()
    metrics = target
    return metrics.render()


def run_health_server(
    health: ServiceHealth,
    host: str | None = None,
    port: int | None = None,
    metrics: TradingMetrics | None = None,
    metrics_sampler: Callable[[TradingMetrics], None] | None = None,
) -> None:
    """Run a small blocking HTTP server exposing /health and /metrics."""

    bind_host = host if host is not None else os.environ.get("HOST") or "0.0.0.0"
    bind_port = port if port is not None else int(os.getenv("PORT", "8000"))
    service_metrics = metrics or TradingMetrics(health.identity)
    service_metrics.set_service_health(health.status)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path == "/health":
                self._write_response(HTTPStatus.OK, CONTENT_TYPE_JSON, render_health(health))
                return
            if self.path == "/metrics":
                service_metrics.set_service_health(health.status)
                if metrics_sampler is not None:
                    metrics_sampler(service_metrics)
                self._write_response(
                    HTTPStatus.OK,
                    CONTENT_TYPE_TEXT,
                    render_metrics(service_metrics),
                )
                return
            self._write_response(HTTPStatus.NOT_FOUND, CONTENT_TYPE_TEXT, b"not found\n")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_response(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((bind_host, bind_port), Handler)
    log_event(
        logger="trading_common.http_health",
        event_type="service_started",
        component="http_health",
        service=health.identity.service,
        runtime_mode=health.identity.runtime_mode,
        host=bind_host,
        port=bind_port,
    )
    server.serve_forever()
