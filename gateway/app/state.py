"""Runtime state container for shared gateway services."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.backend import OpenAICompatibleBackend
from app.loki_logging import GatewayLokiLogger
from app.metrics import GatewayMetrics
from app.session_store import SessionStore
from app.session_tracker import SessionTracker
from app.settings import Settings
from app.tools.loki import LokiEventPublisher


@dataclass(slots=True)
class AppState:
    """Long-lived dependencies shared by all request handlers."""

    settings: Settings
    http: httpx.AsyncClient
    backend: OpenAICompatibleBackend
    metrics: GatewayMetrics
    loki: GatewayLokiLogger
    session_tracker: SessionTracker
    session_store: SessionStore


def create_app_state(settings: Settings) -> AppState:
    """Construct the shared clients and loggers used during request handling."""

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
    backend = OpenAICompatibleBackend(
        base_url=settings.backend_base_url,
        http=http_client,
    )
    metrics = GatewayMetrics()
    loki_publisher = LokiEventPublisher(
        enabled=settings.loki_enabled,
        push_url=settings.loki_push_url,
        batch_size=settings.loki_batch_size,
        flush_interval_sec=settings.loki_flush_interval_sec,
        queue_max_size=settings.loki_queue_max_size,
        loki_app_name=settings.loki_app_name,
        metrics=metrics,
    )
    loki = GatewayLokiLogger(loki_publisher)
    session_tracker = SessionTracker(
        api_url=settings.session_runtime_valkey_url,
        prefix=settings.session_key_prefix,
        ttl_sec=settings.session_ttl_sec,
        max_connections=settings.session_tracker_max_connections,
        metrics=metrics,
    )
    session_store = SessionStore(
        api_url=settings.session_store_valkey_url,
        prefix=settings.session_store_key_prefix,
        ttl_sec=settings.session_store_ttl_sec,
        max_connections=settings.session_store_max_connections,
    )
    return AppState(
        settings=settings,
        http=http_client,
        backend=backend,
        metrics=metrics,
        loki=loki,
        session_tracker=session_tracker,
        session_store=session_store,
    )
