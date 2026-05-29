"""Session first-request tracking backed by Valkey."""

from __future__ import annotations

import logging

from opentelemetry import trace
from redis.exceptions import RedisError

from app.observability import SESSION_TRACKER_ERRORS_COUNTER
from app.tools.valkey_store import ValkeyJsonStore

logger = logging.getLogger(__name__)


class SessionTracker:
    """Track whether a session was already observed using Valkey sliding TTL."""

    def __init__(
        self,
        *,
        api_url: str,
        prefix: str,
        ttl_sec: int,
        max_connections: int,
    ) -> None:
        """Initialize the Valkey-backed runtime session store."""

        self.store = ValkeyJsonStore(
            api_url=api_url,
            prefix=prefix,
            default_ttl_sec=ttl_sec,
            max_connections=max_connections,
        )

    async def close(self) -> None:
        """Close the underlying Valkey client."""

        await self.store.close()

    async def mark_seen(self, session_id: str | None) -> bool:
        """
        Return True only for the first observed request of this session.

        Existing sessions get their TTL refreshed so expiration is based on the
        last request, not on the original creation time.
        """

        if session_id is None:
            return False

        try:
            created = await self.store.set_if_absent(session_id, True)
            if created:
                return True

            await self.store.touch(session_id)
            return False

        except RedisError as exc:
            self._record_error("mark_seen", exc)
            return False

    async def active_session_count(self) -> int | None:
        """Return the current number of runtime session keys, if Valkey is available."""

        try:
            return await self.store.count_matching()

        except RedisError as exc:
            self._record_error("active_session_count", exc)
            return None

    def _record_error(self, operation: str, exc: RedisError) -> None:
        """Record a Valkey failure in logs, metrics, and the current trace."""

        error_type = type(exc).__name__
        SESSION_TRACKER_ERRORS_COUNTER.labels(
            operation=operation,
            error_type=error_type,
        ).inc()
        logger.warning("Session tracker %s failed: %s", operation, exc)

        span = trace.get_current_span()
        if span.get_span_context().is_valid:
            span.record_exception(exc)
            span.add_event(
                "session_tracker.error",
                {
                    "operation": operation,
                    "error.type": error_type,
                },
            )
