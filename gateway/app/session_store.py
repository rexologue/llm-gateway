"""Persist chat session messages in Valkey for inspection endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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


    async def save_messages(
        self,
        session_id: str | None,
        messages: Any,
        tools: Any = None,
    ) -> bool:
        """Persist a session's current messages block when it is available.

        When ``tools`` is a list it is stored alongside the messages so the
        session viewer can show the declared tools next to the calls that used
        them. The assistant turn produced by the backend is not part of the
        request ``messages``; callers append it before saving so the stored
        record becomes a complete transcript.
        """

        if session_id is None or not isinstance(messages, list):
            return False

        try:
            with tracer.start_as_current_span(
                SPAN_VALKEY_OPERATION,
                attributes=valkey_operation_span_attrs(
                    operation="set",
                    prefix=self.store.prefix,
                    record_id=session_id,
                ),
            ) as span:
                # Preserve the original creation time across overwrites so the
                # viewer can report true session age. TTL cannot supply this: it
                # is reset on every write, so it only measures time since the
                # last request, not the lifetime since the first one.
                existing = await self.store.get(session_id)
                created_at = _existing_created_at(existing)

                now = utc_now_iso()
                record: dict[str, Any] = {
                    "metadata": {
                        "session_id": session_id,
                        "created_at": created_at or now,
                        "updated_at": now,
                        "message_cnt": len(messages),
                    },
                    "tools": tools if isinstance(tools, list) else [],
                    "messages": messages,
                }

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
        """Return one stored session as ``{metadata, tools, messages}``, or None.

        The stored ``metadata`` is enriched at read time with live durations the
        record itself cannot hold: ``expires_in_sec`` (remaining TTL), ``age_sec``
        (lifetime since the first request, from ``created_at``), and ``idle_sec``
        (time since the last request, from ``updated_at``).
        """

        with tracer.start_as_current_span(
            SPAN_VALKEY_OPERATION,
            attributes=valkey_operation_span_attrs(
                operation="get",
                prefix=self.store.prefix,
                record_id=session_id,
            ),
        ) as span:
            record = await self.store.get(session_id)
            found = isinstance(record, dict)
            set_span_attributes(span, valkey_result_span_attrs(found=found))

            if not found:
                return None

            ttl_sec = await self.store.ttl(session_id)

        return self._with_live_metadata(record, ttl_sec)


    async def list_sessions(self) -> list[dict[str, Any]]:
        """Return one metadata summary per stored session, newest activity first.

        Each entry mirrors the persisted ``metadata`` plus the read-time
        durations (``age_sec``, ``idle_sec``, ``expires_in_sec``) and a declared
        tool count, so a viewer can list sessions and their lifetimes without
        fetching every full record.
        """

        summaries: list[dict[str, Any]] = []
        now = datetime.now(UTC)

        with tracer.start_as_current_span(
            SPAN_VALKEY_OPERATION,
            attributes=valkey_operation_span_attrs(
                operation="scan_index",
                prefix=self.store.prefix,
                pattern=f"{self.store.prefix}*",
                count=100,
            ),
        ) as span:
            async for key, value in self.store.iter_states():
                if not isinstance(value, dict):
                    continue

                session_id = self.store.record_id_from_key(key)
                ttl_sec = await self.store.ttl(session_id)
                summaries.append(self._summarize_session(session_id, value, ttl_sec, now))

            set_span_attributes(span, valkey_result_span_attrs(count=len(summaries)))

        summaries.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return summaries


    @staticmethod
    def _summarize_session(
        session_id: str,
        record: dict[str, Any],
        ttl_sec: int | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Build a compact list entry from a stored ``{metadata, tools, ...}``."""

        metadata = record.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        tools = record.get("tools")

        created_at = metadata.get("created_at")
        updated_at = metadata.get("updated_at")

        return {
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "message_cnt": metadata.get("message_cnt"),
            "tools_cnt": len(tools) if isinstance(tools, list) else 0,
            "age_sec": _elapsed_seconds(created_at, now),
            "idle_sec": _elapsed_seconds(updated_at, now),
            "expires_in_sec": ttl_sec,
        }


    @staticmethod
    def _with_live_metadata(
        record: dict[str, Any],
        ttl_sec: int | None,
    ) -> dict[str, Any]:
        """Return the record with read-time durations folded into its metadata."""

        now = datetime.now(UTC)
        metadata = dict(record.get("metadata") or {})

        metadata["expires_in_sec"] = ttl_sec
        metadata["age_sec"] = _elapsed_seconds(metadata.get("created_at"), now)
        metadata["idle_sec"] = _elapsed_seconds(metadata.get("updated_at"), now)

        return {**record, "metadata": metadata}


def _existing_created_at(existing: Any) -> str | None:
    """Return the creation timestamp of an already stored session record."""

    if not isinstance(existing, dict):
        return None

    metadata = existing.get("metadata")
    created_at = metadata.get("created_at") if isinstance(metadata, dict) else None

    return created_at if isinstance(created_at, str) else None


def _elapsed_seconds(iso_timestamp: Any, now: datetime) -> int | None:
    """Return whole seconds between an ISO-8601 timestamp and ``now``."""

    if not isinstance(iso_timestamp, str):
        return None

    try:
        moment = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    return max(0, int((now - moment).total_seconds()))
