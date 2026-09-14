"""Runtime state container for shared gateway services."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Coroutine

import httpx

from app.backend import OpenAICompatibleBackend
from app.loki_logging import GatewayLokiLogger
from app.metrics import GatewayMetrics
from app.session_store import SessionStore
from app.session_tracker import SessionTracker
from app.settings import Settings
from app.tools.loki import LokiEventPublisher
from app.tools.monitor import MONITOR_CONV_PATH, MonitorPublisher


logger = logging.getLogger(__name__)


class BackgroundTasks:
    """Track detached tasks so shutdown can wait for them.

    The gateway finishes reading a backend response even when the caller has
    gone, which means work outliving its request. A bare ``create_task`` would
    be garbage-collected mid-flight and would vanish silently on shutdown, so
    every such task is held here until it completes.
    """

    def __init__(self) -> None:
        """Initialize an empty task registry."""

        self._tasks: set[asyncio.Task[Any]] = set()


    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Start a tracked task that outlives the request that created it."""

        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return task


    async def close(self, *, timeout: float) -> int:
        """Wait for tracked tasks, cancel whatever is still running, return the count."""

        pending = set(self._tasks)
        if not pending:
            return 0

        done, still_running = await asyncio.wait(pending, timeout=max(0.0, timeout))

        for task in still_running:
            task.cancel()

        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)
            logger.warning(
                "Background tasks cancelled at shutdown: %d finished, %d cancelled",
                len(done),
                len(still_running),
            )

        return len(pending)


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
    monitor: MonitorPublisher
    tasks: BackgroundTasks = field(default_factory=BackgroundTasks)


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
        engine_id=settings.engine_id,
        metrics=metrics,
    )
    loki = GatewayLokiLogger(loki_publisher)
    monitor = MonitorPublisher(
        enabled=settings.monitor_enabled,
        url=f"{settings.monitor_base_url}{MONITOR_CONV_PATH}",
        timeout_sec=settings.monitor_timeout_sec,
        queue_max_size=settings.monitor_queue_max_size,
        concurrency=settings.monitor_concurrency,
        metrics=metrics,
    )
    session_tracker = SessionTracker(
        api_url=settings.session_runtime_valkey_url,
        prefix=settings.session_key_prefix,
        ttl_sec=settings.session_ttl_sec,
        max_connections=settings.session_tracker_max_connections,
        metrics=metrics,
        enabled=settings.sessions_enabled,
        breaker_failures=settings.valkey_breaker_failures,
        breaker_cooldown_sec=settings.valkey_breaker_cooldown_sec,
    )
    session_store = SessionStore(
        api_url=settings.session_store_valkey_url,
        prefix=settings.session_store_key_prefix,
        ttl_sec=settings.session_store_ttl_sec,
        max_connections=settings.session_store_max_connections,
        max_record_bytes=settings.session_store_max_record_bytes,
        write_attempts=settings.session_store_write_attempts,
        enabled=settings.sessions_enabled,
        breaker_failures=settings.valkey_breaker_failures,
        breaker_cooldown_sec=settings.valkey_breaker_cooldown_sec,
    )
    return AppState(
        settings=settings,
        http=http_client,
        backend=backend,
        metrics=metrics,
        loki=loki,
        session_tracker=session_tracker,
        session_store=session_store,
        monitor=monitor,
    )
