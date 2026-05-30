"""Persist chat session messages in Valkey for inspection endpoints."""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import trace
from redis.exceptions import RedisError

from app.http_utils import utc_now_iso
from app.tools.valkey_store import ValkeyJsonStore
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


class SessionStore:
    """Store the latest observed chat messages for each external session id."""

    def __init__(
        self,
        *,
        api_url: str,
        prefix: str,
        ttl_sec: int,
        max_connections: int,
    ) -> None:
        """Initialize the Valkey-backed persisted session store."""

        self.store = ValkeyJsonStore(
            api_url=api_url,
            prefix=prefix,
            default_ttl_sec=ttl_sec,
            max_connections=max_connections,
        )


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


    async def save_messages(self, session_id: str | None, messages: Any) -> bool:
        """Persist a session's current messages block when it is available."""

        if session_id is None or not isinstance(messages, list):
            return False

        record = {
            "session_id": session_id,
            "updated_at": utc_now_iso(),
            "message_cnt": len(messages),
            "messages": messages,
        }

        try:
            with tracer.start_as_current_span(
                SPAN_VALKEY_OPERATION,
                attributes=valkey_operation_span_attrs(
                    operation="set",
                    prefix=self.store.prefix,
                    record_id=session_id,
                ),
            ) as span:
                await self.store.set(session_id, record)
                set_span_attributes(span, valkey_result_span_attrs(updated=True))

            return True

        except RedisError as exc:
            logger.warning("Session store save_messages failed: %s", exc)
            add_current_span_error_event(
                "session_store.error",
                exc,
                {
                    "operation": "save_messages",
                    "error.type": type(exc).__name__,
                },
            )
            return False


    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return one stored session record, or None when absent."""

        with tracer.start_as_current_span(
            SPAN_VALKEY_OPERATION,
            attributes=valkey_operation_span_attrs(
                operation="get",
                prefix=self.store.prefix,
                record_id=session_id,
            ),
        ) as span:
            record = await self.store.get(session_id)
            set_span_attributes(span, valkey_result_span_attrs(found=record is not None))

        return record if isinstance(record, dict) else None


    async def list_session_ids(self) -> list[str]:
        """Return all stored session ids sorted lexicographically."""

        session_ids: list[str] = []

        with tracer.start_as_current_span(
            SPAN_VALKEY_OPERATION,
            attributes=valkey_operation_span_attrs(
                operation="scan_keys",
                prefix=self.store.prefix,
                pattern=f"{self.store.prefix}*",
                count=100,
            ),
        ) as span:
            async for key in self.store.iter_keys():
                session_ids.append(self.store.record_id_from_key(key))

            set_span_attributes(span, valkey_result_span_attrs(count=len(session_ids)))

        return sorted(session_ids)
