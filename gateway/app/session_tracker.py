"""Session first-request tracking backed by Valkey."""

from __future__ import annotations

import logging

from opentelemetry import trace
import redis.asyncio as redis
from redis.exceptions import RedisError

from app.observability import SESSION_TRACKER_ERRORS_COUNTER

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
        self.prefix = prefix
        self.ttl_sec = max(1, int(ttl_sec))
        self.pool = redis.ConnectionPool.from_url(
            api_url,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=30,
            max_connections=max_connections,
        )
        self.redis = redis.Redis(connection_pool=self.pool)

    def _key(self, session_id: str) -> str:
        return f"{self.prefix}{session_id}"

    async def close(self) -> None:
        """Close the underlying Valkey client."""

        await self.redis.aclose()

    async def mark_seen(self, session_id: str | None) -> bool:
        """
        Return True only for the first observed request of this session.

        Existing sessions get their TTL refreshed so expiration is based on the
        last request, not on the original creation time.
        """

        if session_id is None:
            return False

        key = self._key(session_id)

        try:
            created = await self.redis.set(key, "1", ex=self.ttl_sec, nx=True)
            if created:
                return True

            await self.redis.expire(key, self.ttl_sec)
            return False

        except RedisError as exc:
            error_type = type(exc).__name__
            SESSION_TRACKER_ERRORS_COUNTER.labels(
                operation="mark_seen",
                error_type=error_type,
            ).inc()
            logger.warning("Session tracker mark_seen failed: %s", exc)

            span = trace.get_current_span()
            if span.get_span_context().is_valid:
                span.record_exception(exc)
                span.add_event(
                    "session_tracker.error",
                    {
                        "operation": "mark_seen",
                        "error.type": error_type,
                    },
                )

            return False
