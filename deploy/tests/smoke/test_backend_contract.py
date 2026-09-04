"""Smoke checks for the OpenAI-compatible contract of the endpoint under test.

These tests only use ``/v1`` routes, so they are valid against a raw vLLM or
SGLang backend as well as against the gateway. Gateway-only behaviour lives in
``test_gateway_sessions.py``.
"""

from __future__ import annotations

import json

import pytest

from smoke_common import (
    CHECK_THINKING,
    CHECK_TOOLS,
    THINKING_PROMPT,
    TOOL_NAME,
    WEATHER_TOOL,
    assistant_choice_message,
    chat_payload,
    choice_text,
    post_chat,
    request_headers,
    stream_chat,
)


def test_chat_completion_smoke() -> None:
    """Send one non-streaming chat completion request and verify a model answer."""

    response = post_chat(chat_payload(), request_headers("smoke"))

    assert response.status_code == 200, response.text

    data = response.json()
    choices = data.get("choices")

    assert isinstance(choices, list)
    assert choices
    assert choice_text(choices[0])


def test_chat_completion_stream_smoke() -> None:
    """Send one streaming chat completion request and verify SSE deltas."""

    content, content_type, saw_done = stream_chat(
        chat_payload(stream=True),
        request_headers("smoke-stream"),
    )

    assert "text/event-stream" in content_type, content_type
    assert saw_done, "stream did not terminate with a [DONE] sentinel"
    assert content.strip(), "stream produced no assistant content"


def test_chat_completion_tools_smoke() -> None:
    """Optionally verify OpenAI-compatible tool calling support."""

    if not CHECK_TOOLS:
        pytest.skip("tool calling smoke check is disabled")

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

    message = assistant_choice_message(post_chat(payload, request_headers("smoke-tools")))

    tool_calls = message.get("tool_calls")
    assert isinstance(tool_calls, list)
    assert tool_calls

    first_call = tool_calls[0]
    assert isinstance(first_call, dict)
    assert first_call.get("type") == "function"

    function_call = first_call.get("function")
    assert isinstance(function_call, dict)
    assert function_call.get("name") == TOOL_NAME

    arguments = function_call.get("arguments")
    assert isinstance(arguments, str)

    parsed_arguments = json.loads(arguments)
    assert isinstance(parsed_arguments, dict)
    assert parsed_arguments.get("city")


def test_chat_completion_no_reasoning_smoke() -> None:
    """Verify the endpoint under test answers without any reasoning trace.

    The client sends a plain request without backend-specific knobs, so a
    reasoning-free response proves the endpoint itself disabled thinking: the
    backend launcher for the LLM stack, the gateway's
    ``GATEWAY_FORCED_THINKING_DISABLED`` for the gateway stack. The check is
    opt-in because a deployment that intentionally allows thinking would fail it.
    """

    if not CHECK_THINKING:
        pytest.skip("thinking smoke check is disabled")

    payload = chat_payload(prompt=THINKING_PROMPT, max_tokens=64)
    message = assistant_choice_message(post_chat(payload, request_headers("smoke-think")))

    # The model must still answer, just without any reasoning trace.
    content = message.get("content")
    assert isinstance(content, str) and content.strip(), "empty assistant content"

    reasoning = message.get("reasoning_content")
    assert not (isinstance(reasoning, str) and reasoning.strip()), (
        "thinking was not disabled: non-empty reasoning_content returned"
    )
    assert "<think>" not in content.lower(), (
        "thinking was not disabled: <think> block leaked into content"
    )
