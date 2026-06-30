from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
import pytest


BASE_URL = os.environ["BASE_URL"].rstrip("/")
MODEL = os.getenv("SMOKE_MODEL", "local-model")
PROMPT = os.getenv("SMOKE_PROMPT", "Say pong in one short sentence.")
TIMEOUT_SEC = float(os.getenv("SMOKE_TIMEOUT_SEC", "60"))
API_KEY = os.getenv("SMOKE_API_KEY", "")
CHECK_TOOLS = os.getenv("SMOKE_CHECK_TOOLS", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


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
