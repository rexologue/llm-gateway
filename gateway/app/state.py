"""Runtime state container for shared gateway services."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.event_sinks import LokiSink
from app.http_utils import utc_now_iso
from app.session_tracker import SessionTracker
from app.settings import Settings
from app.tracing import current_trace_context


@dataclass(slots=True)
class AppState:
    """Long-lived dependencies shared by all request handlers."""

    settings: Settings
    http: httpx.AsyncClient
    loki_sink: LokiSink
    session_tracker: SessionTracker

    async def log_event(self, **event: Any) -> None:
        """Attach timestamps and enqueue an event for Loki delivery."""

        record = {
            "ts": utc_now_iso(),
            "ts_unix_ns": time.time_ns(),
            **current_trace_context(),
            **event,
        }
        await self.loki_sink.submit(record)


def create_app_state(settings: Settings) -> AppState:
    """Construct the shared clients and sinks used during request handling."""

    timeout = httpx.Timeout(
        connect=settings.connect_timeout,
        read=settings.read_timeout,
        write=settings.write_timeout,
        pool=settings.pool_timeout,
    )
    limits = httpx.Limits(
        max_connections=settings.http_max_connections,
        max_keepalive_connections=settings.http_max_keepalive_connections,
    )
    http_client = httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False)
    loki_sink = LokiSink(
        enabled=settings.loki_enabled,
        push_url=settings.loki_push_url,
        batch_size=settings.loki_batch_size,
        flush_interval_sec=settings.loki_flush_interval_sec,
        queue_max_size=settings.loki_queue_max_size,
        request_log_label=settings.request_log_label,
        environment=settings.environment,
    )
    session_tracker = SessionTracker(
        api_url=settings.session_valkey_url,
        prefix=settings.session_key_prefix,
        ttl_sec=settings.session_ttl_sec,
        max_connections=settings.session_tracker_max_connections,
    )
    return AppState(
        settings=settings,
        http=http_client,
        loki_sink=loki_sink,
        session_tracker=session_tracker,
    )
