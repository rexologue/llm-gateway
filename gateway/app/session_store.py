"""Persist the full chat transcript of each session in Valkey.

The store owns the shape of a stored dialog end to end: how an exchange is
appended, how a client's view of the history is reconciled with the recorded
one, and how older record versions are read. Those helpers live on
``SessionStore`` rather than in a module of their own because nothing else
consumes them, and they are pure - the merge can be tested without Valkey.

``record_exchange`` is the only write path. Both response shapes and both
outcomes - an answer or a failure - go through it, so a dialog can never be
half-recorded by one branch and fully by another.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import orjson
from opentelemetry import trace
from redis.exceptions import RedisError

from app.backend import ChatTurn
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

SCHEMA_VERSION = 2

DELIVERY_COMPLETE = "complete"
DELIVERY_DRAINED = "drained_after_disconnect"
DELIVERY_CLIENT_GONE = "client_disconnected"
DELIVERY_TRUNCATED = "truncated"

WARN_REVISIONS_EVICTED = "session_record_revisions_evicted"
WARN_TURNS_EVICTED = "session_record_turns_evicted"
WARN_OVER_LIMIT = "session_record_over_limit"
WARN_WRITE_CONTENDED = "session_record_write_contended"


@dataclass(slots=True)
class MergeResult:
    """The outcome of reconciling a client's history with the stored one."""

    messages: list[Any]
    appended_cnt: int
    dropped: list[Any]


@dataclass(slots=True)
class SessionExchange:
    """One completed proxy exchange, ready to be appended to a transcript.

    ``messages`` is the history exactly as the client sent it, and ``turn`` is
    what the backend answered. A failed exchange carries ``error`` and no turn:
    it still belongs in the record, because a dialog that broke is part of what
    happened.
    """

    session_id: str
    request_id: str
    messages: list[Any]
    started_at: str
    finished_at: str
    tools: list[Any] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    stream: bool = False
    status_code: int | None = None
    turn: ChatTurn | None = None
    delivery: str = DELIVERY_COMPLETE
    error: dict[str, Any] | None = None
    ttft_sec: float | None = None
    e2e_sec: float | None = None


@dataclass(slots=True)
class SessionWriteResult:
    """What one transcript write did, for logging and metrics."""

    saved: bool
    message_cnt: int = 0
    turn_cnt: int = 0
    appended_cnt: int = 0
    dropped_cnt: int = 0
    warn_reason: str | None = None


class SessionStore:
    """Store the full chat transcript observed for each external session id.

    The Valkey behind it lives in the central observability stack, which a
    gateway may run without. Persistence is therefore optional by construction:
    when it is off or unreachable, every write reports ``saved=False`` and the
    proxying itself is untouched.
    """

    def __init__(
        self,
        *,
        api_url: str,
        prefix: str,
        ttl_sec: int,
        max_connections: int,
        max_record_bytes: int = 0,
        write_attempts: int = 5,
        enabled: bool = True,
        breaker_failures: int = 3,
        breaker_cooldown_sec: float = 30.0,
    ) -> None:
        """Initialize the Valkey-backed persisted session store."""

        self.enabled = enabled
        self.max_record_bytes = max(0, max_record_bytes)
        self.write_attempts = max(1, write_attempts)
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
        """Return whether transcript persistence is currently reaching Valkey."""

        return self.enabled and self.store.available


    def _require_enabled(self) -> None:
        """Fail a read the same way an outage would, when persistence is off.

        The viewer routes already answer ``503`` on ``RedisError``, so a gateway
        deployed without the session store needs no separate branch there.
        """

        if not self.enabled:
            raise ValkeyUnavailable("session store disabled by GATEWAY_SESSIONS_ENABLED")


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


    async def record_exchange(self, exchange: SessionExchange) -> SessionWriteResult:
        """Append one exchange to its session transcript.

        The whole update runs inside one atomic Valkey transaction, so two
        concurrent requests on the same session cannot overwrite each other's
        turn - the loser of the race replays its append on the winner's record.
        """

        if not self.enabled or not isinstance(exchange.messages, list):
            return SessionWriteResult(saved=False)

        outcome = SessionWriteResult(saved=False)

        def mutate(current: Any) -> dict[str, Any]:
            """Merge this exchange into the record read under WATCH."""

            record = self._coerce_record(current)
            self._apply_exchange(record, exchange, outcome)

            return record

        try:
            with tracer.start_as_current_span(
                SPAN_VALKEY_OPERATION,
                attributes=valkey_operation_span_attrs(
                    operation="update",
                    prefix=self.store.prefix,
                    record_id=exchange.session_id,
                ),
            ) as span:
                saved = await self.store.update(
                    exchange.session_id,
                    mutate,
                    max_attempts=self.write_attempts,
                )
                outcome.saved = saved

                if not saved:
                    outcome.warn_reason = WARN_WRITE_CONTENDED

                set_span_attributes(
                    span,
                    valkey_result_span_attrs(updated=saved, count=outcome.message_cnt),
                )

            return outcome

        except RedisError as exc:
            # An already-open breaker is expected, not news: the outage it
            # stands for was logged when it was first detected.
            if isinstance(exc, ValkeyUnavailable):
                logger.debug("Session store record_exchange skipped: %s", exc)
            else:
                logger.warning("Session store record_exchange failed: %s", exc)

            add_current_span_error_event(
                "session_store.error",
                exc,
                {
                    "operation": "record_exchange",
                    "error.type": type(exc).__name__,
                },
            )
            return SessionWriteResult(saved=False)


    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return one stored session record, or None when it is absent.

        The stored ``metadata`` is enriched at read time with live durations the
        record itself cannot hold: ``expires_in_sec`` (remaining TTL), ``age_sec``
        (lifetime since the first request, from ``created_at``), and ``idle_sec``
        (time since the last request, from ``updated_at``).
        """

        self._require_enabled()

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
        durations (``age_sec``, ``idle_sec``, ``expires_in_sec``) and declared
        tool and turn counts, so a viewer can list sessions and their lifetimes
        without fetching every full record.
        """

        self._require_enabled()

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
    def merge_messages(stored: list[Any], incoming: list[Any]) -> MergeResult:
        """Reconcile a client's message history with the recorded transcript.

        A client is not a reliable narrator of its own dialog: it may resend the
        whole history, a trimmed window of it, or a corrected version of an
        earlier turn. Overwriting the record with whatever arrived loses
        everything the client no longer carries, so instead we align the two.

        We find the offset into the stored transcript whose continuation matches
        the incoming history for as long as possible, keep everything up to the
        end of that match, and append the rest of the incoming history. Ties are
        resolved towards the largest offset, which makes "nothing matched" a
        plain append rather than a wholesale replacement. Whatever the alignment
        pushes out is returned in ``dropped`` for the caller to archive.
        """

        if not stored:
            return MergeResult(messages=list(incoming), appended_cnt=len(incoming), dropped=[])

        if not incoming:
            return MergeResult(messages=list(stored), appended_cnt=0, dropped=[])

        stored_prints = [SessionStore.message_fingerprint(item) for item in stored]
        incoming_prints = [SessionStore.message_fingerprint(item) for item in incoming]

        best_offset = -1
        best_length = -1

        for offset in range(len(stored) + 1):
            length = 0
            while (
                offset + length < len(stored)
                and length < len(incoming)
                and stored_prints[offset + length] == incoming_prints[length]
            ):
                length += 1

            if length > best_length or (length == best_length and offset > best_offset):
                best_offset = offset
                best_length = length

        boundary = best_offset + best_length
        appended = list(incoming[best_length:])

        return MergeResult(
            messages=[*stored[:boundary], *appended],
            appended_cnt=len(appended),
            dropped=list(stored[boundary:]),
        )


    @staticmethod
    def message_fingerprint(message: Any) -> bytes:
        """Return a comparable identity for one chat message.

        Only the fields a client reliably echoes back take part: role, content,
        tool calls and the tool-result linkage. Fields the gateway records but
        clients drop - ``reasoning_content``, ``refusal`` - are excluded, since
        including them would make every stored assistant turn look different
        from the client's own copy of it and trigger a false divergence.

        Tool call arguments are compared as parsed JSON when they parse, so a
        client that re-serializes them with different spacing still matches.
        """

        if not isinstance(message, dict):
            return orjson.dumps(message, option=orjson.OPT_SORT_KEYS)

        content = message.get("content")
        identity: dict[str, Any] = {
            "role": message.get("role"),
            "content": content if content not in ("", None) else None,
        }

        for key in ("name", "tool_call_id"):
            if message.get(key) is not None:
                identity[key] = message[key]

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            identity["tool_calls"] = [
                SessionStore._tool_call_identity(call) for call in tool_calls
            ]

        return orjson.dumps(identity, option=orjson.OPT_SORT_KEYS)


    def _apply_exchange(
        self,
        record: dict[str, Any],
        exchange: SessionExchange,
        outcome: SessionWriteResult,
    ) -> None:
        """Merge one exchange into a coerced record, in place."""

        merge = self.merge_messages(record["messages"], exchange.messages)
        messages = merge.messages
        appended_indexes = list(
            range(len(messages) - merge.appended_cnt, len(messages))
        )

        assistant_index: int | None = None
        assistant_message = exchange.turn.assistant_message if exchange.turn else None

        if assistant_message is not None:
            messages.append(assistant_message)
            assistant_index = len(messages) - 1

        if merge.dropped:
            record["revisions"].append(
                {
                    "at": exchange.finished_at,
                    "reason": "divergence",
                    "request_id": exchange.request_id,
                    "dropped": merge.dropped,
                }
            )

        record["messages"] = messages

        # The dialog's declared tools survive a follow-up request that omits
        # them - a tool result carries no tools, and the viewer should still
        # show what the dialog was given. Which turn declared what stays exact
        # in the per-turn audit.
        record["tools"] = exchange.tools or record["tools"]
        record["turns"].append(self._turn_entry(exchange, appended_indexes, assistant_index))
        self._refresh_metadata(record, exchange)

        outcome.message_cnt = len(messages)
        outcome.turn_cnt = len(record["turns"])
        outcome.appended_cnt = merge.appended_cnt
        outcome.dropped_cnt = len(merge.dropped)
        outcome.warn_reason = self._enforce_size_limit(record, self.max_record_bytes)


    @staticmethod
    def _turn_entry(
        exchange: SessionExchange,
        appended_indexes: list[int],
        assistant_index: int | None,
    ) -> dict[str, Any]:
        """Build the audit entry describing one exchange."""

        turn = exchange.turn
        entry: dict[str, Any] = {
            "turn_id": uuid.uuid4().hex,
            "request_id": exchange.request_id,
            "started_at": exchange.started_at,
            "finished_at": exchange.finished_at,
            "model": turn.model if turn and turn.model else exchange.model,
            "stream": exchange.stream,
            "status_code": exchange.status_code,
            "params": exchange.params,
            "tools": exchange.tools,
            "appended_indexes": appended_indexes,
            "assistant_index": assistant_index,
            "finish_reason": turn.finish_reason if turn else None,
            "usage": turn.usage if turn else None,
            "ttft_sec": exchange.ttft_sec,
            "e2e_sec": exchange.e2e_sec,
            "delivery": exchange.delivery,
            "error": exchange.error,
        }

        # Branch: the backend answered with more than one candidate. Only the
        # first continues the dialog, so the rest are kept here instead of being
        # merged into the transcript.
        if turn is not None and len(turn.choices) > 1:
            entry["extra_choices"] = [
                {
                    "index": choice.index,
                    "message": choice.message,
                    "finish_reason": choice.finish_reason,
                }
                for choice in turn.choices[1:]
            ]

        if turn is not None and turn.truncated:
            entry["truncated"] = True

        return entry


    @staticmethod
    def _refresh_metadata(record: dict[str, Any], exchange: SessionExchange) -> None:
        """Update record metadata and running totals after an exchange."""

        metadata = record["metadata"]
        totals = metadata["totals"]
        turn = exchange.turn
        usage = turn.usage if turn else None

        metadata["session_id"] = exchange.session_id
        metadata["created_at"] = metadata.get("created_at") or exchange.started_at
        metadata["updated_at"] = exchange.finished_at
        metadata["message_cnt"] = len(record["messages"])
        metadata["turn_cnt"] = len(record["turns"])
        metadata["last_request_id"] = exchange.request_id
        metadata["last_model"] = (turn.model if turn and turn.model else exchange.model)
        metadata["last_finish_reason"] = turn.finish_reason if turn else None

        totals["requests"] = int(totals.get("requests") or 0) + 1

        failed = exchange.error is not None or (
            exchange.status_code is not None and exchange.status_code >= 400
        )
        if failed:
            totals["failed_requests"] = int(totals.get("failed_requests") or 0) + 1

        if isinstance(usage, dict):
            for source, target in (
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "completion_tokens"),
            ):
                value = usage.get(source)
                if isinstance(value, int):
                    totals[target] = int(totals.get(target) or 0) + value


    @staticmethod
    def _enforce_size_limit(record: dict[str, Any], max_bytes: int) -> str | None:
        """Trim audit data until the record fits, and report what was cut.

        The dialog itself is never trimmed: ``messages`` is the reason the record
        exists. Superseded revisions go first, then the oldest turn audit
        entries, and the newest turn always survives.
        """

        if max_bytes <= 0 or len(orjson.dumps(record)) <= max_bytes:
            return None

        warn_reason = None

        while record["revisions"] and len(orjson.dumps(record)) > max_bytes:
            record["revisions"].pop(0)
            warn_reason = WARN_REVISIONS_EVICTED

        while len(record["turns"]) > 1 and len(orjson.dumps(record)) > max_bytes:
            record["turns"].pop(0)
            warn_reason = WARN_TURNS_EVICTED

        if len(orjson.dumps(record)) > max_bytes:
            return WARN_OVER_LIMIT

        return warn_reason


    @staticmethod
    def _tool_call_identity(call: Any) -> dict[str, Any]:
        """Return the comparable identity of one tool call."""

        if not isinstance(call, dict):
            return {"raw": call}

        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        arguments = function.get("arguments")

        if isinstance(arguments, str):
            parsed = None
            try:
                parsed = orjson.loads(arguments)
            except orjson.JSONDecodeError:
                parsed = None

            arguments = parsed if parsed is not None else arguments

        return {
            "id": call.get("id"),
            "name": function.get("name"),
            "arguments": arguments,
        }


    @staticmethod
    def _coerce_record(record: Any) -> dict[str, Any]:
        """Return a stored record in the current schema.

        Three generations exist. The current one carries ``turns`` and
        ``revisions`` alongside the dialog. Version 1 had ``metadata``/``tools``/
        ``messages`` only. The oldest was flat (``session_id``, ``updated_at``,
        ``message_cnt``, ``messages``), and its top-level fields are lifted into
        a synthesized metadata block so every generation reads the same way.
        """

        if not isinstance(record, dict):
            record = {}

        metadata = record.get("metadata")
        if isinstance(metadata, dict):
            metadata = dict(metadata)
        else:
            metadata = {
                "session_id": record.get("session_id"),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
                "message_cnt": record.get("message_cnt"),
            }

        totals = metadata.get("totals")
        metadata["totals"] = dict(totals) if isinstance(totals, dict) else {}

        messages = record.get("messages")
        tools = record.get("tools")
        turns = record.get("turns")
        revisions = record.get("revisions")

        return {
            "schema_version": SCHEMA_VERSION,
            "metadata": metadata,
            "tools": tools if isinstance(tools, list) else [],
            "messages": messages if isinstance(messages, list) else [],
            "turns": turns if isinstance(turns, list) else [],
            "revisions": revisions if isinstance(revisions, list) else [],
        }


    @staticmethod
    def _summarize_session(
        session_id: str,
        record: dict[str, Any],
        ttl_sec: int | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Build a compact list entry from a stored record of any generation."""

        normalized = SessionStore._coerce_record(record)
        metadata = normalized["metadata"]

        created_at = metadata.get("created_at")
        updated_at = metadata.get("updated_at")

        return {
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "message_cnt": metadata.get("message_cnt"),
            "tools_cnt": len(normalized["tools"]),
            "turn_cnt": len(normalized["turns"]),
            "age_sec": SessionStore._elapsed_seconds(created_at, now),
            "idle_sec": SessionStore._elapsed_seconds(updated_at, now),
            "expires_in_sec": ttl_sec,
        }


    @staticmethod
    def _with_live_metadata(
        record: dict[str, Any],
        ttl_sec: int | None,
    ) -> dict[str, Any]:
        """Return a normalized record with read-time durations in its metadata."""

        now = datetime.now(UTC)
        normalized = SessionStore._coerce_record(record)
        metadata = normalized["metadata"]

        metadata["expires_in_sec"] = ttl_sec
        metadata["age_sec"] = SessionStore._elapsed_seconds(metadata.get("created_at"), now)
        metadata["idle_sec"] = SessionStore._elapsed_seconds(metadata.get("updated_at"), now)

        return normalized


    @staticmethod
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
