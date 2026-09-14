"""Session first-request tracking backed by Valkey."""

from __future__ import annotations

import logging

from opentelemetry import trace
from redis.exceptions import RedisError

from app.metrics import GatewayMetrics
from app.tools.valkey_store import ValkeyJsonStore, ValkeyUnavailable
from app.tracing import (
    SPAN_VALKEY_OPERATION,
    TRACER_NAME,
    add_current_span_error_event,
    set_span_attributes,
    valkey_operation_span_attrs,
    valkey_result_span_attrs,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(TRACER_NAME)


class SessionTracker:
    """Track whether a session was already observed using Valkey sliding TTL.

    Valkey lives in the central observability stack, which a gateway is allowed
    to run without. When it is absent the tracker reports every request as a
    continuation rather than refusing to serve: first-request classification is
    telemetry, and telemetry must not decide whether a chat completion happens.
    """

    def __init__(
        self,
        *,
        api_url: str,
        prefix: str,
        ttl_sec: int,
        max_connections: int,
        metrics: GatewayMetrics,
        enabled: bool = True,
        breaker_failures: int = 3,
        breaker_cooldown_sec: float = 30.0,
    ) -> None:
        """Initialize the Valkey-backed runtime session store."""

        self.metrics = metrics
        self.enabled = enabled
        self.store = ValkeyJsonStore(
            api_url=api_url,
            prefix=prefix,
            default_ttl_sec=ttl_sec,
            max_connections=max_connections,
            breaker_failures=breaker_failures,
            breaker_cooldown_sec=breaker_cooldown_sec,
        )


    @property
    def available(self) -> bool:
        """Return whether session tracking is currently reaching Valkey."""

        return self.enabled and self.store.available


    async def close(self) -> None:
        """Close the underlying Valkey client."""

        with tracer.start_as_current_span(
            SPAN_VALKEY_OPERATION,
            attributes=valkey_operation_span_attrs(
                operation="close",
                prefix=self.store.prefix,
            ),
        ):
            await self.store.close()


    async def mark_seen(self, session_id: str | None) -> bool:
        """
        Return True only for the first observed request of this session.

        Existing sessions get their TTL refreshed so expiration is based on the
        last request, not on the original creation time.
        """

        if session_id is None or not self.enabled:
            return False

        try:
            with tracer.start_as_current_span(
                SPAN_VALKEY_OPERATION,
                attributes=valkey_operation_span_attrs(
                    operation="set_if_absent",
                    prefix=self.store.prefix,
                    record_id=session_id,
                ),
            ) as span:
                created = await self.store.set_if_absent(session_id, True)
                set_span_attributes(span, valkey_result_span_attrs(created=created))

            if created:
                return True

            with tracer.start_as_current_span(
                SPAN_VALKEY_OPERATION,
                attributes=valkey_operation_span_attrs(
                    operation="touch",
                    prefix=self.store.prefix,
                    record_id=session_id,
                ),
            ) as span:
                updated = await self.store.touch(session_id)
                set_span_attributes(span, valkey_result_span_attrs(updated=updated))

            return False

        except RedisError as exc:
            self._record_error("mark_seen", exc)
            return False


    async def active_session_count(self) -> int | None:
        """Return the current number of runtime session keys, if Valkey is available."""

        if not self.enabled:
            return None

        try:
            pattern = f"{self.store.prefix}*"

            with tracer.start_as_current_span(
                SPAN_VALKEY_OPERATION,
                attributes=valkey_operation_span_attrs(
                    operation="scan_count",
                    prefix=self.store.prefix,
                    pattern=pattern,
                    count=1000,
                ),
            ) as span:
                count = await self.store.count_matching()
                set_span_attributes(span, valkey_result_span_attrs(count=count))

                return count

        except RedisError as exc:
            self._record_error("active_session_count", exc)
            return None


    def _record_error(self, operation: str, exc: RedisError) -> None:
        """Record a Valkey failure in logs, metrics, and the current trace.

        A breaker that is already open is expected, not news: the outage it
        stands for was logged when it was first detected, and warning again on
        every request would bury the line that actually mattered.
        """

        error_type = type(exc).__name__
        self.metrics.session_tracker_error(operation, exc)

        if isinstance(exc, ValkeyUnavailable):
            logger.debug("Session tracker %s skipped: %s", operation, exc)
        else:
            logger.warning("Session tracker %s failed: %s", operation, exc)

        add_current_span_error_event(
            "session_tracker.error",
            exc,
            {
                "operation": operation,
                "error.type": error_type,
            },
        )
