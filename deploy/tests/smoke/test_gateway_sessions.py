"""Smoke checks for gateway-only session persistence.

The gateway stores each dialog in Valkey and exposes it through
``/gateway/session/{session_id}``. A raw backend has neither the store nor the
route, so this suite runs only against the gateway stack. The endpoint contract
itself is covered by ``test_backend_contract.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from smoke_common import (
    BASE_URL,
    CHECK_TOOLS,
    TIMEOUT_SEC,
    TOOL_NAME,
    WEATHER_TOOL,
    assistant_choice_message,
    chat_payload,
    post_chat,
    request_headers,
    stream_chat,
)


def test_session_keeps_non_stream_assistant_turn() -> None:
    """The stored session must gain the assistant turn of a non-stream request."""

    headers = request_headers("smoke-session")
    response = post_chat(chat_payload(), headers)

    assert response.status_code == 200, response.text

    record = _fetch_session(headers["X-Session-ID"])
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

    last_message = _fetch_session(headers["X-Session-ID"])["messages"][-1]
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

    last_message = _fetch_session(headers["X-Session-ID"])["messages"][-1]
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

    record = _fetch_session(headers["X-Session-ID"])

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


def _fetch_session(session_id: str) -> dict[str, Any]:
    """Return the persisted session record for a session id via the gateway API."""

    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_SEC) as client:
        response = client.get(f"/gateway/session/{session_id}")

    assert response.status_code == 200, response.text

    record = response.json()

    assert isinstance(record, dict)
    assert isinstance(record.get("metadata"), dict)
    assert isinstance(record.get("messages"), list) and record["messages"]

    return record
