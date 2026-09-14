"""Smoke checks for gateway-only session persistence.

The gateway stores each dialog in Valkey and exposes it through
``/gateway/session/{session_id}``. A raw backend has neither the store nor the
route, so this suite runs only against the gateway stack. The endpoint contract
itself is covered by ``test_backend_contract.py``.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from smoke_common import (
    BASE_URL,
    CHECK_TOOLS,
    SESSIONS_ENABLED,
    TIMEOUT_SEC,
    TOOL_NAME,
    WEATHER_TOOL,
    assistant_choice_message,
    chat_payload,
    fetch_session,
    post_chat,
    request_headers,
    stream_chat,
)


pytestmark = pytest.mark.skipif(
    not SESSIONS_ENABLED,
    reason="GATEWAY_SESSIONS_ENABLED=false: this gateway runs without the session store",
)


def test_session_keeps_non_stream_assistant_turn() -> None:
    """The stored session must gain the assistant turn of a non-stream request."""

    headers = request_headers("smoke-session")
    response = post_chat(chat_payload(), headers)

    assert response.status_code == 200, response.text

    record = fetch_session(headers["X-Session-ID"])
    assert record["metadata"]["session_id"] == headers["X-Session-ID"]
    assert record["tools"] == []

    last_message = record["messages"][-1]
    assert last_message.get("role") == "assistant"
    assert isinstance(last_message.get("content"), str)
    assert last_message["content"].strip()


def test_session_keeps_stream_assistant_turn_when_drained() -> None:
    """A fully drained SSE stream must leave its assistant turn in the session."""

    headers = request_headers("smoke-session-stream-drained")
    content, _content_type, saw_done = stream_chat(
        chat_payload(stream=True),
        headers,
        drain=True,
    )

    assert saw_done, "stream did not terminate with a [DONE] sentinel"
    assert content.strip(), "stream produced no assistant content"

    last_message = fetch_session(headers["X-Session-ID"])["messages"][-1]
    assert last_message.get("role") == "assistant"
    assert isinstance(last_message.get("content"), str)
    assert last_message["content"].strip()


def test_session_keeps_stream_assistant_turn_after_early_disconnect() -> None:
    """The assistant turn must survive a client that stops reading at [DONE].

    OpenAI-compatible clients stop iterating as soon as the ``[DONE]`` sentinel
    arrives, which disconnects the caller while the gateway is still awaiting
    the backend's end of stream. The gateway must still finish its terminal
    bookkeeping and persist the turn it already received instead of dropping it
    with the cancelled request.
    """

    headers = request_headers("smoke-session-stream-early")
    content, _content_type, saw_done = stream_chat(chat_payload(stream=True), headers)

    assert saw_done, "stream did not terminate with a [DONE] sentinel"
    assert content.strip(), "stream produced no assistant content"

    last_message = fetch_session(headers["X-Session-ID"])["messages"][-1]
    assert last_message.get("role") == "assistant", (
        "assistant turn was dropped after the client disconnected at [DONE]"
    )
    assert isinstance(last_message.get("content"), str)
    assert last_message["content"].strip()


def test_session_keeps_declared_tools_and_tool_calls() -> None:
    """The stored session must keep declared tools and the assistant tool call."""

    if not CHECK_TOOLS:
        pytest.skip("tool calling smoke check is disabled")

    headers = request_headers("smoke-session-tools")
    payload = chat_payload(
        prompt="Use the weather tool to check the weather in Paris.",
        max_tokens=128,
        tools=[WEATHER_TOOL],
        tool_choice={
            "type": "function",
            "function": {
                "name": TOOL_NAME,
            },
        },
    )

    message = assistant_choice_message(post_chat(payload, headers))
    assert message.get("tool_calls"), "backend returned no tool_calls to persist"

    record = fetch_session(headers["X-Session-ID"])

    stored_tools = record.get("tools")
    assert isinstance(stored_tools, list) and stored_tools
    assert stored_tools[0]["function"]["name"] == TOOL_NAME

    last_message = record["messages"][-1]
    assert last_message.get("role") == "assistant"

    stored_tool_calls = last_message.get("tool_calls")
    assert isinstance(stored_tool_calls, list) and stored_tool_calls
    assert stored_tool_calls[0]["function"]["name"] == TOOL_NAME


def test_session_list_reports_the_session() -> None:
    """A stored session must be discoverable through the session list route."""

    headers = request_headers("smoke-session-list")
    response = post_chat(chat_payload(), headers)

    assert response.status_code == 200, response.text

    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_SEC) as client:
        listing = client.get("/gateway/session_list")

    assert listing.status_code == 200, listing.text

    sessions = listing.json()
    assert isinstance(sessions, list)

    session_ids = {
        entry.get("session_id")
        for entry in sessions
        if isinstance(entry, dict)
    }
    assert headers["X-Session-ID"] in session_ids, json.dumps(sessions)[:2000]




def test_session_keeps_the_whole_turn_after_a_mid_stream_hangup() -> None:
    """A caller leaving mid-answer must not truncate the stored turn.

    The gateway reads the backend in a task the disconnect cannot cancel, so
    the generation it already paid for is finished and recorded whole. The
    client here stops after a couple of content chunks, well before the answer
    is over.
    """

    headers = request_headers("smoke-session-hangup")
    payload = chat_payload(
        prompt="Count slowly from one to twenty, one number per line.",
        max_tokens=256,
        stream=True,
    )
    partial, _content_type, saw_done = stream_chat(payload, headers, stop_after_chunks=2)

    assert not saw_done, "the client was supposed to hang up before [DONE]"

    record = fetch_session(headers["X-Session-ID"])
    turn = record["turns"][-1]
    stored = record["messages"][-1]

    assert stored.get("role") == "assistant"
    assert isinstance(stored.get("content"), str) and stored["content"].strip()
    assert len(stored["content"]) > len(partial), (
        "the stored turn is no longer than what the client saw, "
        f"so the drain did not happen: stored={stored['content']!r} seen={partial!r}"
    )
    assert turn["delivery"] in {"drained_after_disconnect", "truncated"}, turn


def test_session_records_usage_and_finish_reason() -> None:
    """Each turn carries its own accounting, in both response shapes."""

    headers = request_headers("smoke-session-usage")
    response = post_chat(chat_payload(), headers)

    assert response.status_code == 200, response.text

    turn = fetch_session(headers["X-Session-ID"])["turns"][-1]

    assert turn["finish_reason"], turn
    assert isinstance(turn.get("usage"), dict) and turn["usage"].get("prompt_tokens"), turn
    assert turn["status_code"] == 200
    assert turn["stream"] is False

    stream_headers = request_headers("smoke-session-usage-stream")
    content, _content_type, saw_done = stream_chat(
        chat_payload(stream=True),
        stream_headers,
        drain=True,
    )

    assert saw_done and content.strip()

    stream_turn = fetch_session(stream_headers["X-Session-ID"])["turns"][-1]

    assert stream_turn["stream"] is True
    assert stream_turn["finish_reason"], stream_turn
    assert isinstance(stream_turn.get("usage"), dict), (
        "streamed usage must be recorded even though the caller never asked for it"
    )
    assert isinstance(stream_turn.get("ttft_sec"), float), stream_turn


def test_stream_hides_usage_the_caller_never_requested() -> None:
    """Asking the backend for usage must not change what the caller receives."""

    headers = request_headers("smoke-session-usage-hidden")
    events = _stream_raw_events(chat_payload(stream=True), headers)
    usage_events = [event for event in events if "usage" in event and '"choices": []' in event]

    assert not usage_events, f"caller received a usage event it never asked for: {usage_events[:1]}"

    headers = request_headers("smoke-session-usage-asked")
    events = _stream_raw_events(
        chat_payload(stream=True, stream_options={"include_usage": True}),
        headers,
    )

    assert any("usage" in event for event in events), (
        "a caller that asked for usage must still receive it"
    )


def test_session_keeps_history_the_client_stopped_sending() -> None:
    """A client trimming its context must not trim the recorded dialog."""

    headers = request_headers("smoke-session-window")
    first = post_chat(chat_payload(prompt="Say the word alpha and nothing else."), headers)

    assert first.status_code == 200, first.text

    record = fetch_session(headers["X-Session-ID"])
    head = record["messages"][0]
    trimmed = [
        record["messages"][-1],
        {"role": "user", "content": "Say the word beta and nothing else."},
    ]
    second = post_chat(chat_payload(messages=trimmed), headers)

    assert second.status_code == 200, second.text

    record = fetch_session(headers["X-Session-ID"], turns=2)

    assert record["messages"][0] == head, "the head of the dialog was dropped"
    assert len(record["messages"]) == 4, [m.get("role") for m in record["messages"]]


def test_session_records_concurrent_requests_without_losing_one() -> None:
    """Two requests racing on one session must both end up recorded."""

    headers = request_headers("smoke-session-race")
    payloads = [
        chat_payload(prompt="Say the word one and nothing else."),
        chat_payload(prompt="Say the word two and nothing else."),
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda payload: post_chat(payload, headers), payloads))

    for response in responses:
        assert response.status_code == 200, response.text

    record = fetch_session(headers["X-Session-ID"], turns=2)

    assert len(record["turns"]) == 2, record["turns"]
    assert record["metadata"]["totals"]["requests"] == 2, record["metadata"]["totals"]


def test_session_records_a_failed_exchange() -> None:
    """A dialog that broke belongs in the record too."""

    headers = request_headers("smoke-session-failure")
    payload = chat_payload()
    payload["model"] = "definitely-not-a-served-model"
    response = post_chat(payload, headers)

    assert response.status_code >= 400, response.text

    record = fetch_session(headers["X-Session-ID"])
    turn = record["turns"][-1]

    assert turn["assistant_index"] is None, turn
    assert isinstance(turn.get("error"), dict) and turn["error"].get("status_code"), turn
    assert record["metadata"]["totals"]["failed_requests"] >= 1, record["metadata"]["totals"]
    assert [message.get("role") for message in record["messages"]] == ["user"], record["messages"]


def _stream_raw_events(payload: dict[str, Any], headers: dict[str, str]) -> list[str]:
    """Return the raw SSE data payloads a streaming request delivered."""

    events: list[str] = []

    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_SEC) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as response:
            assert response.status_code == 200, response.read().decode(
                "utf-8", errors="replace"
            )

            for line in response.iter_lines():
                if line.startswith("data:"):
                    events.append(line[len("data:"):].strip())

    return events
