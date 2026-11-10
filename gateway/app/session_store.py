"""Persist every chat session as content-addressed blocks in Valkey.

A session is four keys, all sharing one hash tag so they land in one slot:

    {prefix}{<session_id>}:meta      HASH  created_at, updated_at, running totals
    {prefix}{<session_id>}:messages  HASH  hash -> message JSON, each one stored once
    {prefix}{<session_id>}:tools     HASH  hash -> tools declaration JSON
    {prefix}{<session_id>}:turns     LIST  one JSON entry per exchange

The store keeps what was observed and nothing it would have to infer. Every
message that crossed the gateway - the client's history, a tool result, the
model's answer - is stored once under the hash of its exact JSON, and a turn is
the ordered list of hashes the client sent plus the hash of what the model
answered. The client may resend the whole history, trim it, rewrite the system
prompt, or drop tool calls it already handled; none of that needs reconciling,
because nothing is merged. Rebuilding a readable dialog is the reader's job.

Writing never reads. One exchange is a single MULTI/EXEC of HSET, RPUSH and
HINCRBY commands, so concurrent requests on one session cannot lose each
other's turn, and the cost of a write does not grow with the session.
"""

from __future__ import annotations

import hashlib
import logging
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

SCHEMA_VERSION = 3
HASH_HEX_CHARS = 32

DELIVERY_COMPLETE = "complete"
DELIVERY_DRAINED = "drained_after_disconnect"
DELIVERY_CLIENT_GONE = "client_disconnected"
DELIVERY_TRUNCATED = "truncated"

TOTAL_FIELDS = ("requests", "failed_requests", "prompt_tokens", "completion_tokens")


@dataclass(slots=True)
class SessionExchange:
    """One completed proxy exchange, ready to be appended to its session.

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
    turn_cnt: int = 0


@dataclass(slots=True)
class TurnBlocks:
    """One exchange split into the blocks it adds to its session."""

    messages: dict[str, bytes]
    tools: dict[str, bytes]
    entry: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SessionKeys:
    """The Valkey keys of one session."""

    meta: str
    messages: str
    tools: str
    turns: str

    def all(self) -> tuple[str, str, str, str]:
        """Return every key, for commands that apply to the whole session."""

        return (self.meta, self.messages, self.tools, self.turns)


def content_hash(value: Any) -> tuple[str, bytes]:
    """Return the content address of a JSON value and its canonical encoding.

    Keys are sorted so the same message always hashes the same, whichever order
    a client serialized it in. Beyond ``compact_message`` nothing is
    normalized: a message that differs in any set field is a different message.
    """

    encoded = orjson.dumps(value, option=orjson.OPT_SORT_KEYS)

    return hashlib.sha256(encoded).hexdigest()[:HASH_HEX_CHARS], encoded


def compact_message(message: Any) -> Any:
    """Drop the fields of a message that carry no value.

    Backends fill a non-streamed answer with ``"refusal": null``,
    ``"tool_calls": []`` and the like, while a client echoing that answer back
    usually sends only what is set. Both are the same message, and dropping the
    empty fields lets them share one hash. Anything with a value is kept as is.
    """

    if not isinstance(message, dict):
        return message

    return {
        key: value
        for key, value in message.items()
        if value is not None and value != [] and value != {}
    }


def build_turn(exchange: SessionExchange) -> TurnBlocks:
    """Split one exchange into new messages, a tools declaration and a turn entry."""

    messages: dict[str, bytes] = {}

    def address(message: Any) -> str:
        digest, encoded = content_hash(compact_message(message))
        messages[digest] = encoded
        return digest

    tools: dict[str, bytes] = {}
    tools_hash: str | None = None

    if exchange.tools:
        tools_hash, encoded_tools = content_hash(exchange.tools)
        tools[tools_hash] = encoded_tools

    turn = exchange.turn
    choices = turn.choices if turn is not None else []

    entry: dict[str, Any] = {
        "request_id": exchange.request_id,
        "started_at": exchange.started_at,
        "finished_at": exchange.finished_at,
        "model": turn.model if turn is not None and turn.model else exchange.model,
        "stream": exchange.stream,
        "status_code": exchange.status_code,
        "params": exchange.params,
        "tools": tools_hash,
        "input": [address(message) for message in exchange.messages],
        "output": address(choices[0].message) if choices else None,
        "finish_reason": turn.finish_reason if turn is not None else None,
        "usage": turn.usage if turn is not None else None,
        "ttft_sec": exchange.ttft_sec,
        "e2e_sec": exchange.e2e_sec,
        "delivery": exchange.delivery,
        "error": exchange.error,
    }

    # Branch: the backend answered with more than one candidate. The first one
    # is the turn's output; the rest are kept addressable but apart from it.
    if len(choices) > 1:
        entry["extra_outputs"] = [
            {
                "index": choice.index,
                "output": address(choice.message),
                "finish_reason": choice.finish_reason,
            }
            for choice in choices[1:]
        ]

    if turn is not None and turn.truncated:
        entry["truncated"] = True

    return TurnBlocks(messages=messages, tools=tools, entry=entry)


def turn_totals(exchange: SessionExchange) -> dict[str, int]:
    """Return what one exchange adds to the session's running totals."""

    totals = {"requests": 1}

    failed = exchange.error is not None or (
        exchange.status_code is not None and exchange.status_code >= 400
    )
    if failed:
        totals["failed_requests"] = 1

    usage = exchange.turn.usage if exchange.turn is not None else None
    if isinstance(usage, dict):
        for name in ("prompt_tokens", "completion_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and value:
                totals[name] = value

    return totals


def assemble_session(
    session_id: str,
    *,
    meta: dict[str, str],
    messages: dict[str, Any],
    tools: dict[str, Any],
    turns: list[dict[str, Any]],
    ttl_sec: int | None,
    now: datetime,
) -> dict[str, Any]:
    """Return the session record a reader gets, from its four stored blocks.

    Turns are ordered by when they started, not by when they were written: an
    answer the gateway drained after its caller left is written after the
    request that replaced it. Messages and tools are listed in the order the
    turns first reference them, so the record reads top to bottom.
    """

    ordered_turns = sorted(turns, key=lambda turn: str(turn.get("started_at") or ""))

    message_order: list[str] = []
    tools_order: list[str] = []

    for turn in ordered_turns:
        message_order.extend(turn.get("input") or [])
        message_order.append(turn.get("output"))
        message_order.extend(extra.get("output") for extra in turn.get("extra_outputs") or [])
        tools_order.append(turn.get("tools"))

    created_at = meta.get("created_at")
    updated_at = meta.get("updated_at")

    return {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "age_sec": _elapsed_seconds(created_at, now),
            "idle_sec": _elapsed_seconds(updated_at, now),
            "expires_in_sec": ttl_sec,
            "message_cnt": len(messages),
            "tools_cnt": len(tools),
            "turn_cnt": len(ordered_turns),
            "totals": {name: int(meta.get(name) or 0) for name in TOTAL_FIELDS},
        },
        "messages": _ordered(messages, message_order),
        "tools": _ordered(tools, tools_order),
        "turns": ordered_turns,
    }


class SessionStore:
    """Store every exchange observed for each external session id.

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
        enabled: bool = True,
        breaker_failures: int = 3,
        breaker_cooldown_sec: float = 30.0,
    ) -> None:
        """Initialize the Valkey-backed session store."""

        self.enabled = enabled
        self.prefix = prefix
        self.ttl_sec = max(1, int(ttl_sec))
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
        """Return whether session persistence is currently reaching Valkey."""

        return self.enabled and self.store.available


    def keys(self, session_id: str) -> SessionKeys:
        """Return the keys of one session."""

        base = f"{self.prefix}{{{session_id}}}"

        return SessionKeys(
            meta=f"{base}:meta",
            messages=f"{base}:messages",
            tools=f"{base}:tools",
            turns=f"{base}:turns",
        )


    def session_id_from_meta_key(self, key: bytes | str) -> str:
        """Return the session id encoded in a ``…{<session_id>}:meta`` key."""

        text = key.decode("utf-8") if isinstance(key, bytes) else key

        return text[len(self.prefix) + 1 : -len("}:meta")]


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
            attributes=valkey_operation_span_attrs(operation="close", prefix=self.prefix),
        ):
            await self.store.close()


    async def record_exchange(self, exchange: SessionExchange) -> SessionWriteResult:
        """Append one exchange to its session in a single transaction."""

        if not self.enabled or not isinstance(exchange.messages, list):
            return SessionWriteResult(saved=False)

        blocks = build_turn(exchange)
        keys = self.keys(exchange.session_id)

        def write(pipe: Any) -> None:
            if blocks.messages:
                pipe.hset(keys.messages, mapping=blocks.messages)

            if blocks.tools:
                pipe.hset(keys.tools, mapping=blocks.tools)

            pipe.rpush(keys.turns, orjson.dumps(blocks.entry))
            pipe.hsetnx(keys.meta, "created_at", exchange.started_at)
            pipe.hset(keys.meta, "updated_at", exchange.finished_at)

            for name, value in turn_totals(exchange).items():
                pipe.hincrby(keys.meta, name, value)

            for key in keys.all():
                pipe.expire(key, self.ttl_sec)

        try:
            with tracer.start_as_current_span(
                SPAN_VALKEY_OPERATION,
                attributes=valkey_operation_span_attrs(
                    operation="append_turn",
                    prefix=self.prefix,
                    record_id=exchange.session_id,
                ),
            ) as span:
                replies = await self.store.execute(write)
                # RPUSH answers with the new list length; it follows the HSETs
                # that were actually queued.
                turn_cnt = int(replies[bool(blocks.messages) + bool(blocks.tools)])
                set_span_attributes(span, valkey_result_span_attrs(updated=True, count=turn_cnt))

            return SessionWriteResult(saved=True, turn_cnt=turn_cnt)

        except RedisError as exc:
            self._log_error("record_exchange", exc)
            return SessionWriteResult(saved=False)


    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return one stored session, or None when it is absent."""

        self._require_enabled()
        keys = self.keys(session_id)

        def read(pipe: Any) -> None:
            pipe.hgetall(keys.meta)
            pipe.hgetall(keys.messages)
            pipe.hgetall(keys.tools)
            pipe.lrange(keys.turns, 0, -1)
            pipe.ttl(keys.turns)
            pipe.get(self._legacy_key(session_id))

        with tracer.start_as_current_span(
            SPAN_VALKEY_OPERATION,
            attributes=valkey_operation_span_attrs(
                operation="get_session",
                prefix=self.prefix,
                record_id=session_id,
            ),
        ) as span:
            meta, messages, tools, turns, ttl, legacy = await self.store.execute(
                read,
                transaction=False,
            )
            found = bool(meta or turns or legacy)
            set_span_attributes(span, valkey_result_span_attrs(found=found))

        if not meta and not turns:
            # Records written before schema 3 are one JSON value under the bare
            # session key. They are served untouched until their TTL runs out.
            return orjson.loads(legacy) if legacy is not None else None

        return assemble_session(
            session_id,
            meta=_decode_fields(meta),
            messages=_decode_values(messages),
            tools=_decode_values(tools),
            turns=[orjson.loads(raw) for raw in turns],
            ttl_sec=ttl if isinstance(ttl, int) and ttl >= 0 else None,
            now=datetime.now(UTC),
        )


    async def list_sessions(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return one summary per session active in a period, newest activity first.

        A session is active in ``[since, until]`` when its lifetime overlaps it:
        it was created no later than ``until`` and last updated no earlier than
        ``since``. Either bound may be omitted. Only metadata is read to decide,
        and the counters are fetched for the sessions that made it through.
        """

        self._require_enabled()
        pattern = f"{self.prefix}{{*}}:meta"

        with tracer.start_as_current_span(
            SPAN_VALKEY_OPERATION,
            attributes=valkey_operation_span_attrs(
                operation="scan_sessions",
                prefix=self.prefix,
                pattern=pattern,
                count=100,
            ),
        ) as span:
            # SCAN may return a key more than once; dict keeps the first.
            session_ids = list(
                dict.fromkeys(
                    [
                        self.session_id_from_meta_key(key)
                        async for key in self.store.iter_keys(pattern, count=100)
                    ]
                )
            )

            def read_meta(pipe: Any) -> None:
                for session_id in session_ids:
                    pipe.hgetall(self.keys(session_id).meta)

            metas = await self.store.execute(read_meta, transaction=False) if session_ids else []
            selected = [
                (session_id, fields)
                for session_id, fields in zip(session_ids, map(_decode_fields, metas))
                if session_in_period(
                    fields.get("created_at"),
                    fields.get("updated_at"),
                    since=since,
                    until=until,
                )
            ]

            def read_counts(pipe: Any) -> None:
                for session_id, _fields in selected:
                    keys = self.keys(session_id)
                    pipe.hlen(keys.messages)
                    pipe.hlen(keys.tools)
                    pipe.llen(keys.turns)
                    pipe.ttl(keys.meta)

            counts = await self.store.execute(read_counts, transaction=False) if selected else []
            set_span_attributes(span, valkey_result_span_attrs(count=len(selected)))

        now = datetime.now(UTC)
        summaries = []

        for position, (session_id, fields) in enumerate(selected):
            message_cnt, tools_cnt, turn_cnt, ttl = counts[position * 4 : position * 4 + 4]
            created_at = fields.get("created_at")
            updated_at = fields.get("updated_at")
            summaries.append(
                {
                    "session_id": session_id,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "message_cnt": message_cnt,
                    "tools_cnt": tools_cnt,
                    "turn_cnt": turn_cnt,
                    "age_sec": _elapsed_seconds(created_at, now),
                    "idle_sec": _elapsed_seconds(updated_at, now),
                    "expires_in_sec": ttl if isinstance(ttl, int) and ttl >= 0 else None,
                }
            )

        summaries.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return summaries


    def _legacy_key(self, session_id: str) -> str:
        """Return the key a pre-schema-3 gateway stored this session under."""

        return f"{self.prefix}{session_id}"


    @staticmethod
    def _log_error(operation: str, exc: RedisError) -> None:
        """Record a failed store call; an already-open breaker is not news."""

        if isinstance(exc, ValkeyUnavailable):
            logger.debug("Session store %s skipped: %s", operation, exc)
        else:
            logger.warning("Session store %s failed: %s", operation, exc)

        add_current_span_error_event(
            "session_store.error",
            exc,
            {"operation": operation, "error.type": type(exc).__name__},
        )


def session_in_period(
    created_at: str | None,
    updated_at: str | None,
    *,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    """Return whether a session's lifetime overlaps ``[since, until]``.

    A missing ``created_at`` falls back to ``updated_at`` and vice versa. A
    session with neither timestamp only passes when no bound is set.
    """

    if since is None and until is None:
        return True

    started = _parse_iso(created_at) or _parse_iso(updated_at)
    last = _parse_iso(updated_at) or started

    if started is None or last is None:
        return False

    if since is not None and last < since:
        return False

    return until is None or started <= until


def _ordered(blocks: dict[str, Any], order: list[Any]) -> dict[str, Any]:
    """Return blocks keyed by hash, in first-reference order, unreferenced last."""

    result: dict[str, Any] = {}

    for digest in order:
        if isinstance(digest, str) and digest in blocks and digest not in result:
            result[digest] = blocks[digest]

    for digest, value in blocks.items():
        result.setdefault(digest, value)

    return result


def _decode_fields(raw: dict[Any, Any]) -> dict[str, str]:
    """Decode a Valkey hash of plain string fields."""

    return {
        _text(name): _text(value)
        for name, value in (raw or {}).items()
    }


def _decode_values(raw: dict[Any, Any]) -> dict[str, Any]:
    """Decode a Valkey hash whose values are JSON documents."""

    return {_text(name): orjson.loads(value) for name, value in (raw or {}).items()}


def _text(value: bytes | str) -> str:
    """Return a Valkey reply as text."""

    return value.decode("utf-8") if isinstance(value, bytes) else value


def _elapsed_seconds(iso_timestamp: Any, now: datetime) -> int | None:
    """Return whole seconds between an ISO-8601 timestamp and ``now``."""

    moment = _parse_iso(iso_timestamp)

    if moment is None:
        return None

    return max(0, int((now - moment).total_seconds()))


def _parse_iso(iso_timestamp: Any) -> datetime | None:
    """Return an aware datetime from an ISO-8601 string, or None."""

    if not isinstance(iso_timestamp, str):
        return None

    try:
        moment = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None

    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
