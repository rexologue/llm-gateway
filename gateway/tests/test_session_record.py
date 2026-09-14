"""Tests for how one exchange becomes a stored transcript record.

These exercise the record assembly without Valkey: the mutator that runs inside
the atomic update is pure, which is what makes it testable here.
"""

from __future__ import annotations

from typing import Any

import orjson

from app.backend import ChatResponseReader, ChatTurn
from app.session_store import (
    DELIVERY_DRAINED,
    SCHEMA_VERSION,
    SessionExchange,
    SessionStore,
    SessionWriteResult,
    WARN_OVER_LIMIT,
    WARN_REVISIONS_EVICTED,
    WARN_TURNS_EVICTED,
)
from tests.conftest import delta_event


def build_store(*, max_record_bytes: int = 0) -> SessionStore:
    """Return a store instance; the connection pool stays unused and lazy."""

    return SessionStore(
        api_url="redis://127.0.0.1:6379",
        prefix="test:session-store:",
        ttl_sec=600,
        max_connections=4,
        max_record_bytes=max_record_bytes,
    )


def turn_of(content: str, *, finish: str = "stop", usage: dict[str, Any] | None = None) -> ChatTurn:
    """Return a turn as a non-stream backend answer would produce it."""

    body: dict[str, Any] = {
        "model": "test-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
    }

    if usage is not None:
        body["usage"] = usage

    return ChatResponseReader.turn_from_body(orjson.dumps(body))


def exchange(
    messages: list[Any],
    *,
    turn: ChatTurn | None = None,
    request_id: str = "req-1",
    **overrides: Any,
) -> SessionExchange:
    """Return an exchange with sensible defaults for the fields under test."""

    fields: dict[str, Any] = {
        "session_id": "s-1",
        "request_id": request_id,
        "messages": messages,
        "started_at": "2026-09-10T09:00:00+00:00",
        "finished_at": "2026-09-10T09:00:02+00:00",
        "model": "test-model",
        "status_code": 200,
        "turn": turn,
    }
    fields.update(overrides)

    return SessionExchange(**fields)


def apply(store: SessionStore, current: Any, item: SessionExchange) -> tuple[dict[str, Any], SessionWriteResult]:
    """Run the record mutation the atomic update would run."""

    record = store._coerce_record(current)
    outcome = SessionWriteResult(saved=False)
    store._apply_exchange(record, item, outcome)

    return record, outcome


def test_first_exchange_builds_a_current_schema_record() -> None:
    """A fresh session gets the full record shape, not a bare message list."""

    store = build_store()
    messages = [{"role": "user", "content": "привет"}]

    record, outcome = apply(store, None, exchange(messages, turn=turn_of("здравствуйте")))

    assert record["schema_version"] == SCHEMA_VERSION
    assert [message["content"] for message in record["messages"]] == ["привет", "здравствуйте"]
    assert record["metadata"]["created_at"] == "2026-09-10T09:00:00+00:00"
    assert record["metadata"]["message_cnt"] == 2
    assert record["metadata"]["turn_cnt"] == 1
    assert record["metadata"]["totals"]["requests"] == 1
    assert outcome.message_cnt == 2
    assert outcome.warn_reason is None


def test_turn_audit_points_at_the_messages_it_added() -> None:
    """The audit entry must locate this turn inside the transcript."""

    store = build_store()
    stored, _ = apply(store, None, exchange([{"role": "user", "content": "u1"}], turn=turn_of("a1")))

    second = exchange(
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
        turn=turn_of("a2"),
        request_id="req-2",
    )
    record, _outcome = apply(store, stored, second)

    entry = record["turns"][-1]
    assert entry["appended_indexes"] == [2]
    assert entry["assistant_index"] == 3
    assert record["messages"][3]["content"] == "a2"
    assert entry["request_id"] == "req-2"
    assert entry["finish_reason"] == "stop"


def test_created_at_survives_later_exchanges() -> None:
    """Session age comes from the first request, so it must not be rewritten."""

    store = build_store()
    stored, _ = apply(store, None, exchange([{"role": "user", "content": "u1"}], turn=turn_of("a1")))

    later = exchange(
        [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"}],
        turn=turn_of("a2"),
        started_at="2026-09-10T10:00:00+00:00",
        finished_at="2026-09-10T10:00:03+00:00",
    )
    record, _outcome = apply(store, stored, later)

    assert record["metadata"]["created_at"] == "2026-09-10T09:00:00+00:00"
    assert record["metadata"]["updated_at"] == "2026-09-10T10:00:03+00:00"


def test_failed_exchange_is_recorded_without_an_assistant_message() -> None:
    """A dialog that broke is part of the record, but not part of the dialog."""

    store = build_store()
    failed = exchange(
        [{"role": "user", "content": "привет"}],
        turn=None,
        status_code=500,
        error={"type": "HTTPStatusError", "message": "backend exploded"},
    )

    record, outcome = apply(store, None, failed)

    assert [message["content"] for message in record["messages"]] == ["привет"]
    assert record["turns"][-1]["error"]["type"] == "HTTPStatusError"
    assert record["turns"][-1]["assistant_index"] is None
    assert record["metadata"]["totals"]["failed_requests"] == 1
    assert outcome.turn_cnt == 1


def test_usage_totals_accumulate_across_turns() -> None:
    """Token totals are a running sum over the session, not the last turn."""

    store = build_store()
    first = exchange(
        [{"role": "user", "content": "u1"}],
        turn=turn_of("a1", usage={"prompt_tokens": 10, "completion_tokens": 3}),
    )
    stored, _ = apply(store, None, first)

    second = exchange(
        [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"}],
        turn=turn_of("a2", usage={"prompt_tokens": 20, "completion_tokens": 5}),
    )
    record, _outcome = apply(store, stored, second)

    totals = record["metadata"]["totals"]
    assert totals["prompt_tokens"] == 30
    assert totals["completion_tokens"] == 8
    assert totals["requests"] == 2
    assert record["turns"][-1]["usage"] == {"prompt_tokens": 20, "completion_tokens": 5}


def test_divergence_archives_the_dropped_tail() -> None:
    """A superseded tail is kept in revisions instead of disappearing."""

    store = build_store()
    stored, _ = apply(store, None, exchange([{"role": "user", "content": "u1"}], turn=turn_of("a1")))

    rewritten = exchange(
        [{"role": "user", "content": "u1-fixed"}],
        turn=turn_of("a1-new"),
        request_id="req-2",
    )
    record, outcome = apply(store, stored, rewritten)

    assert [message["content"] for message in record["messages"]] == ["u1", "a1", "u1-fixed", "a1-new"]
    assert outcome.dropped_cnt == 0

    # A true divergence: the client keeps the head but rewrites the tail.
    stored2, _ = apply(store, None, exchange(
        [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"}],
        turn=turn_of("a2"),
    ))
    record2, outcome2 = apply(store, stored2, exchange(
        [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2-fixed"}],
        turn=turn_of("a2-new"),
    ))

    assert outcome2.dropped_cnt == 2
    assert record2["revisions"][-1]["reason"] == "divergence"
    assert [item["content"] for item in record2["revisions"][-1]["dropped"]] == ["u2", "a2"]
    assert [message["content"] for message in record2["messages"]] == ["u1", "a1", "u2-fixed", "a2-new"]


def test_extra_choices_are_audited_not_merged() -> None:
    """With n>1 only one choice continues the dialog; the rest are recorded."""

    store = build_store()
    turn = ChatResponseReader.turn_from_body(
        orjson.dumps(
            {
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "А"}, "finish_reason": "stop"},
                    {"index": 1, "message": {"role": "assistant", "content": "Б"}, "finish_reason": "stop"},
                ]
            }
        )
    )

    record, _outcome = apply(store, None, exchange([{"role": "user", "content": "u1"}], turn=turn))

    assert [message["content"] for message in record["messages"]] == ["u1", "А"]
    assert [item["message"]["content"] for item in record["turns"][-1]["extra_choices"]] == ["Б"]


def test_truncated_and_delivery_are_recorded() -> None:
    """How a turn ended is part of the audit, not something to infer later."""

    store = build_store()
    reader = ChatResponseReader()
    reader.feed(delta_event({"content": "полу"}))
    reader.flush()

    record, _outcome = apply(
        store,
        None,
        exchange([{"role": "user", "content": "u1"}], turn=reader.turn(), delivery=DELIVERY_DRAINED),
    )

    entry = record["turns"][-1]
    assert entry["truncated"] is True
    assert entry["delivery"] == DELIVERY_DRAINED


def test_legacy_flat_record_migrates() -> None:
    """The oldest flat record shape still reads and keeps its dialog."""

    store = build_store()
    legacy = {
        "session_id": "s-1",
        "updated_at": "2026-09-01T10:00:00+00:00",
        "created_at": "2026-09-01T09:00:00+00:00",
        "message_cnt": 2,
        "messages": [{"role": "user", "content": "старое"}, {"role": "assistant", "content": "тоже"}],
    }

    record, _outcome = apply(store, legacy, exchange(
        [{"role": "user", "content": "старое"}, {"role": "assistant", "content": "тоже"}, {"role": "user", "content": "новое"}],
        turn=turn_of("свежее"),
    ))

    assert record["schema_version"] == SCHEMA_VERSION
    assert [message["content"] for message in record["messages"]] == ["старое", "тоже", "новое", "свежее"]
    assert record["metadata"]["created_at"] == "2026-09-01T09:00:00+00:00"


def test_v1_record_migrates_and_gains_audit() -> None:
    """A version 1 record keeps its dialog and starts collecting turns."""

    store = build_store()
    v1 = {
        "metadata": {
            "session_id": "s-1",
            "created_at": "2026-09-02T09:00:00+00:00",
            "updated_at": "2026-09-02T09:05:00+00:00",
            "message_cnt": 1,
        },
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "messages": [{"role": "user", "content": "u1"}],
    }

    record, _outcome = apply(store, v1, exchange([{"role": "user", "content": "u1"}], turn=turn_of("a1")))

    assert record["turns"] and record["revisions"] == []
    assert [message["content"] for message in record["messages"]] == ["u1", "a1"]
    assert record["metadata"]["created_at"] == "2026-09-02T09:00:00+00:00"


def test_size_limit_evicts_audit_but_never_the_dialog() -> None:
    """Over the cap, revisions go first, then old turns; messages stay."""

    store = build_store(max_record_bytes=1200)
    current: Any = None
    history: list[Any] = []

    for index in range(12):
        history.append({"role": "user", "content": f"u{index}-{'x' * 20}"})
        record, outcome = apply(store, current, exchange(list(history), turn=turn_of(f"a{index}")))
        history.append({"role": "assistant", "content": f"a{index}"})
        current = record

    assert outcome.warn_reason in {WARN_REVISIONS_EVICTED, WARN_TURNS_EVICTED, WARN_OVER_LIMIT}
    assert len(record["messages"]) == 24, "the dialog itself must never be trimmed"
    assert len(record["turns"]) < 12, "old audit entries should have been evicted"
    assert record["turns"][-1]["request_id"] == "req-1"


def test_under_the_limit_nothing_is_evicted() -> None:
    """A record that fits keeps its whole audit and warns about nothing."""

    store = build_store(max_record_bytes=1_000_000)
    record, outcome = apply(store, None, exchange([{"role": "user", "content": "u1"}], turn=turn_of("a1")))

    assert outcome.warn_reason is None
    assert len(record["turns"]) == 1


def test_declared_tools_survive_a_request_that_omits_them() -> None:
    """A tool result carries no tools, and must not erase the declaration."""

    store = build_store()
    tools = [{"type": "function", "function": {"name": "get_current_weather"}}]
    stored, _ = apply(store, None, exchange([{"role": "user", "content": "погода?"}], turn=turn_of("a1"), tools=tools))

    assert stored["tools"] == tools

    follow_up = exchange(
        [
            {"role": "user", "content": "погода?"},
            {"role": "assistant", "content": "a1"},
            {"role": "tool", "tool_call_id": "c1", "content": "-3°C"},
        ],
        turn=turn_of("a2"),
        request_id="req-2",
    )
    record, _outcome = apply(store, stored, follow_up)

    assert record["tools"] == tools, "the dialog's tools must stay visible"
    assert record["turns"][0]["tools"] == tools
    assert record["turns"][1]["tools"] == [], "per-turn audit stays exact"
