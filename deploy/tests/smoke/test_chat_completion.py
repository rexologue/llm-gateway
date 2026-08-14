from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
import pytest


def _env_flag(name: str) -> bool:
    """Return True when an environment flag is set to a truthy value."""

    return os.getenv(name, "false").lower() in {"1", "true", "yes", "on"}


BASE_URL = os.environ["BASE_URL"].rstrip("/")
MODEL = os.getenv("SMOKE_MODEL", "local-model")
PROMPT = os.getenv("SMOKE_PROMPT", "Say pong in one short sentence.")
THINKING_PROMPT = os.getenv(
    "SMOKE_THINKING_PROMPT",
    "What is 17 + 25? Reply with just the number.",
)
TIMEOUT_SEC = float(os.getenv("SMOKE_TIMEOUT_SEC", "60"))
API_KEY = os.getenv("SMOKE_API_KEY", "")
CHECK_TOOLS = _env_flag("SMOKE_CHECK_TOOLS")
CHECK_THINKING = _env_flag("SMOKE_CHECK_THINKING")


def test_chat_completion_smoke() -> None:
    """Send one non-streaming chat completion request and verify a model answer."""

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": PROMPT,
            }
        ],
        "max_tokens": 16,
        "stream": False,
    }
    headers = {
        "X-Request-ID": f"smoke-{uuid.uuid4()}",
        "X-Session-ID": f"smoke-{uuid.uuid4()}",
    }
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_SEC) as client:
        response = client.post(
            "/v1/chat/completions",
            json=payload,
            headers=headers,
        )

    assert response.status_code == 200, response.text

    data = response.json()
    choices = data.get("choices")

    assert isinstance(choices, list)
    assert choices
    assert _choice_text(choices[0])

    # The persisted session must include the assistant turn the backend just
    # produced, not only the request messages.
    record = _fetch_session(headers["X-Session-ID"])
    assert record["metadata"]["session_id"] == headers["X-Session-ID"]
    assert record["tools"] == []

    last_message = record["messages"][-1]
    assert last_message.get("role") == "assistant"
    assert isinstance(last_message.get("content"), str)
    assert last_message["content"].strip()


def test_chat_completion_stream_smoke() -> None:
    """Send one streaming chat completion request and verify SSE deltas."""

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": PROMPT,
            }
        ],
        "max_tokens": 16,
        "stream": True,
    }
    headers = {
        "X-Request-ID": f"smoke-stream-{uuid.uuid4()}",
        "X-Session-ID": f"smoke-stream-{uuid.uuid4()}",
    }
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    content, content_type, saw_done = _stream_chat(payload, headers)

    assert "text/event-stream" in content_type, content_type
    assert saw_done, "stream did not terminate with a [DONE] sentinel"
    assert content.strip(), "stream produced no assistant content"

    # The assistant turn reconstructed from the SSE stream must be persisted.
    record = _fetch_session(headers["X-Session-ID"])
    last_message = record["messages"][-1]
    assert last_message.get("role") == "assistant"
    assert isinstance(last_message.get("content"), str)
    assert last_message["content"].strip()


def test_chat_completion_tools_smoke() -> None:
    """Optionally verify OpenAI-compatible tool calling support."""

    if not CHECK_TOOLS:
        pytest.skip("tool calling smoke check is disabled")

    tool_name = "get_current_weather"
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": "Use the weather tool to check the weather in Paris.",
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": "Get current weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {
                                "type": "string",
                                "description": "City name.",
                            }
                        },
                        "required": ["city"],
                    },
                },
            }
        ],
        "tool_choice": {
            "type": "function",
            "function": {
                "name": tool_name,
            },
        },
        "max_tokens": 128,
        "stream": False,
    }
    headers = {
        "X-Request-ID": f"smoke-tools-{uuid.uuid4()}",
        "X-Session-ID": f"smoke-tools-{uuid.uuid4()}",
    }
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_SEC) as client:
        response = client.post(
            "/v1/chat/completions",
            json=payload,
            headers=headers,
        )

    assert response.status_code == 200, response.text

    data = response.json()
    choices = data.get("choices")

    assert isinstance(choices, list)
    assert choices

    message = choices[0].get("message")
    assert isinstance(message, dict)

    tool_calls = message.get("tool_calls")
    assert isinstance(tool_calls, list)
    assert tool_calls

    first_call = tool_calls[0]
    assert isinstance(first_call, dict)
    assert first_call.get("type") == "function"

    function_call = first_call.get("function")
    assert isinstance(function_call, dict)
    assert function_call.get("name") == tool_name

    arguments = function_call.get("arguments")
    assert isinstance(arguments, str)

    parsed_arguments = json.loads(arguments)
    assert isinstance(parsed_arguments, dict)
    assert parsed_arguments.get("city")

    # The persisted session must keep the declared tools and the assistant turn
    # that called one of them.
    record = _fetch_session(headers["X-Session-ID"])

    stored_tools = record.get("tools")
    assert isinstance(stored_tools, list) and stored_tools
    assert stored_tools[0]["function"]["name"] == tool_name

    last_message = record["messages"][-1]
    assert last_message.get("role") == "assistant"

    stored_tool_calls = last_message.get("tool_calls")
    assert isinstance(stored_tool_calls, list) and stored_tool_calls
    assert stored_tool_calls[0]["function"]["name"] == tool_name


def test_chat_completion_no_reasoning_smoke() -> None:
    """Verify the gateway forces thinking off regardless of backend defaults.

    The client sends a plain request without any backend-specific knobs, so a
    reasoning-free response proves the gateway (not the backend) disabled
    thinking. This check assumes the deployment runs with
    ``GATEWAY_FORCED_THINKING_DISABLED=true`` and is opt-in because a gateway
    that intentionally allows thinking would fail it.
    """

    if not CHECK_THINKING:
        pytest.skip("thinking smoke check is disabled")

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": THINKING_PROMPT,
            }
        ],
        "max_tokens": 64,
        "stream": False,
    }
    headers = {
        "X-Request-ID": f"smoke-think-{uuid.uuid4()}",
        "X-Session-ID": f"smoke-think-{uuid.uuid4()}",
    }
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_SEC) as client:
        response = client.post(
            "/v1/chat/completions",
            json=payload,
            headers=headers,
        )

    assert response.status_code == 200, response.text

    data = response.json()
    choices = data.get("choices")

    assert isinstance(choices, list)
    assert choices

    message = choices[0].get("message")
    assert isinstance(message, dict)

    # The model must still answer, just without any reasoning trace.
    content = message.get("content")
    assert isinstance(content, str) and content.strip(), "empty assistant content"

    reasoning = message.get("reasoning_content")
    assert not (isinstance(reasoning, str) and reasoning.strip()), (
        "gateway did not disable thinking: non-empty reasoning_content returned"
    )
    assert "<think>" not in content.lower(), (
        "gateway did not disable thinking: <think> block leaked into content"
    )


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


def _stream_chat(
    payload: dict[str, Any],
    headers: dict[str, str],
) -> tuple[str, str, bool]:
    """Consume an SSE chat completion stream and return (content, type, done)."""

    contents: list[str] = []
    saw_done = False

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
            content_type = response.headers.get("content-type", "")

            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue

                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    saw_done = True
                    break

                chunk = json.loads(data)
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str):
                        contents.append(piece)

    return "".join(contents), content_type, saw_done


def _choice_text(choice: dict[str, Any]) -> str:
    """Return text from OpenAI-compatible chat or completion choices."""

    message = choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()

    text = choice.get("text")
    if isinstance(text, str):
        return text.strip()

    return ""
