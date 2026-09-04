"""Shared configuration and HTTP helpers for the smoke test suites.

Two suites use these helpers. ``test_backend_contract.py`` asserts the
OpenAI-compatible contract and runs against any endpoint that claims to speak
it: a raw vLLM/SGLang backend or the gateway in front of one.
``test_gateway_sessions.py`` asserts gateway-only behaviour and therefore runs
only against the gateway stack.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx


def env_flag(name: str) -> bool:
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
CHECK_TOOLS = env_flag("SMOKE_CHECK_TOOLS")
CHECK_THINKING = env_flag("SMOKE_CHECK_THINKING")

TOOL_NAME = "get_current_weather"
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
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


def request_headers(prefix: str) -> dict[str, str]:
    """Return correlation headers for one smoke request with a fresh session."""

    headers = {
        "X-Request-ID": f"{prefix}-{uuid.uuid4()}",
        "X-Session-ID": f"{prefix}-{uuid.uuid4()}",
    }
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    return headers


def chat_payload(
    *,
    prompt: str = PROMPT,
    stream: bool = False,
    max_tokens: int = 16,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> dict[str, Any]:
    """Return a minimal OpenAI-compatible chat completion payload."""

    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "max_tokens": max_tokens,
        "stream": stream,
    }

    if tools is not None:
        payload["tools"] = tools

    if tool_choice is not None:
        payload["tool_choice"] = tool_choice

    return payload


def post_chat(
    payload: dict[str, Any],
    headers: dict[str, str],
) -> httpx.Response:
    """Send one non-streaming chat completion request."""

    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_SEC) as client:
        return client.post(
            "/v1/chat/completions",
            json=payload,
            headers=headers,
        )


def stream_chat(
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    drain: bool = False,
) -> tuple[str, str, bool]:
    """Consume an SSE chat completion stream and return (content, type, done).

    ``drain`` selects how the client leaves the stream. The default stops
    reading as soon as the ``[DONE]`` sentinel arrives, which is what the
    OpenAI SDKs do and what makes the server observe a downstream disconnect
    while it is still awaiting the backend's end of stream. With ``drain`` the
    client instead reads to the real end of the body.
    """

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
                    if drain:
                        continue

                    break

                if saw_done:
                    continue

                chunk = json.loads(data)
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str):
                        contents.append(piece)

    return "".join(contents), content_type, saw_done


def choice_text(choice: dict[str, Any]) -> str:
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


def assistant_choice_message(response: httpx.Response) -> dict[str, Any]:
    """Return the first assistant message of a non-streaming chat response."""

    assert response.status_code == 200, response.text

    data = response.json()
    choices = data.get("choices")

    assert isinstance(choices, list)
    assert choices

    message = choices[0].get("message")
    assert isinstance(message, dict)

    return message
