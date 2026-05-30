from __future__ import annotations

import os
import uuid
from typing import Any

import httpx


BASE_URL = os.environ["BASE_URL"].rstrip("/")
MODEL = os.getenv("SMOKE_MODEL", "local-model")
PROMPT = os.getenv("SMOKE_PROMPT", "Say pong in one short sentence.")
TIMEOUT_SEC = float(os.getenv("SMOKE_TIMEOUT_SEC", "60"))
API_KEY = os.getenv("SMOKE_API_KEY", "")


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
