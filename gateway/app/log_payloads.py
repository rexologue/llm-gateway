"""Helpers for building compact Loki payloads from gateway traffic."""

from __future__ import annotations

from typing import Any, Mapping

from app.http_utils import parse_json_maybe, sanitize_headers, sha256_hexdigest

GENERATION_ROUTE = "/v1/chat/completions"
SENSITIVE_PAYLOAD_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "id_token",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "set-cookie",
    "token",
    "access_key",
    "access_token",
}


def _compact_dict(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values from a mapping to keep Loki payloads compact."""

    return {key: value for key, value in values.items() if value is not None}


def _request_bucket(route: str) -> str:
    """Return the logical Loki bucket for a request."""

    return "request_generation" if route == GENERATION_ROUTE else "request_non_generation"


def _response_bucket(route: str) -> str:
    """Return the logical Loki bucket for a backend response."""

    return "response_generation" if route == GENERATION_ROUTE else "response_non_generation"


def _is_sensitive_payload_key(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered in SENSITIVE_PAYLOAD_KEYS
        or lowered.endswith("_api_key")
        or lowered.endswith("_password")
        or lowered.endswith("_secret")
        or lowered.endswith("_token")
    )


def _sanitize_payload(value: Any) -> Any:
    """Return a payload copy with common credential-bearing fields redacted."""

    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if _is_sensitive_payload_key(key) else _sanitize_payload(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]

    return value


def _payload_without_messages(value: Any) -> Any:
    """Return a payload copy with message arrays removed from mappings."""

    if isinstance(value, dict):
        return {
            key: _payload_without_messages(item)
            for key, item in value.items()
            if key != "messages"
        }

    if isinstance(value, list):
        return [_payload_without_messages(item) for item in value]

    return value


def _stream_response_json(response_text: str) -> dict[str, Any]:
    """Represent an SSE response body as a valid JSON-compatible object."""

    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    event_name: str | None = None
    done = False

    def flush_event() -> None:
        nonlocal done, event_name
        if not data_lines:
            event_name = None
            return

        payload_text = "\n".join(data_lines).strip()
        data_lines.clear()

        if payload_text == "[DONE]":
            done = True
            event_name = None
            return

        parsed_payload = parse_json_maybe(payload_text)
        event: dict[str, Any] = {
            "data": parsed_payload if parsed_payload is not None else payload_text,
        }
        if event_name is not None:
            event["event"] = event_name

        events.append(event)
        event_name = None

    for line in response_text.splitlines():
        if not line.strip():
            flush_event()
            continue

        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    flush_event()
    return {
        "format": "sse",
        "done": done,
        "event_count": len(events),
        "events": events,
    }


def _header_summary(headers: Mapping[str, str]) -> dict[str, Any]:
    """Return only the request headers that matter operationally."""

    lowered = {key.lower(): value for key, value in sanitize_headers(dict(headers)).items()}
    return _compact_dict(
        {
            "request_content_type": lowered.get("content-type"),
            "request_accept": lowered.get("accept"),
            "request_user_agent": lowered.get("user-agent"),
            "request_x_forwarded_for": lowered.get("x-forwarded-for"),
            "request_x_real_ip": lowered.get("x-real-ip"),
            "authorization_present": "authorization" in {key.lower() for key in headers},
        }
    )


def _chat_request_details(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Extract small chat-specific counters useful for system diagnostics."""

    if not isinstance(payload, dict):
        return {}

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return _compact_dict({"request_model": payload.get("model")})

    tool_message_count = 0
    assistant_tool_call_count = 0

    for message in messages:
        if not isinstance(message, dict):
            continue

        if message.get("role") == "tool":
            tool_message_count += 1

        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict):
                assistant_tool_call_count += 1

    return _compact_dict(
        {
            "request_model": payload.get("model"),
            "message_cnt": len(messages),
            "tool_message_count": tool_message_count,
            "assistant_tool_call_count": assistant_tool_call_count,
        }
    )


def _content_text(value: Any) -> str:
    """Flatten OpenAI-compatible text content blocks into plain text."""

    if isinstance(value, str):
        return value

    if isinstance(value, list):
        return "".join(_content_text(item) for item in value)

    if not isinstance(value, dict):
        return ""

    text = value.get("text")
    if isinstance(text, str):
        return text

    if isinstance(text, dict):
        return _content_text(text)

    return _content_text(value.get("content"))


def _message_text(value: Any) -> str:
    """Return assistant text from a chat-style message or delta payload."""

    if not isinstance(value, dict):
        return ""

    content_text = _content_text(value.get("content"))
    if content_text:
        return content_text

    refusal = value.get("refusal")
    return refusal if isinstance(refusal, str) else ""


def _response_output_text(value: Any) -> str:
    """Return assistant text from a Responses API output item."""

    if not isinstance(value, dict):
        return ""

    text = _content_text(value.get("content"))
    if text:
        return text

    return _content_text(value.get("text"))


def _json_assistant_text(payload: Any) -> str | None:
    """Extract assistant text from a non-stream OpenAI-compatible response."""

    if not isinstance(payload, dict):
        return None

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    choices = payload.get("choices")
    if isinstance(choices, list):
        text = "".join(
            fragment
            for choice in choices
            if isinstance(choice, dict)
            for fragment in (
                choice.get("text") if isinstance(choice.get("text"), str) else "",
                _message_text(choice.get("message")),
            )
            if fragment
        )
        if text:
            return text

    output = payload.get("output")
    if isinstance(output, list):
        text = "".join(_response_output_text(item) for item in output)
        if text:
            return text

    return None


def _stream_assistant_text(response_text: str) -> str | None:
    """Extract assistant text by concatenating streamed SSE payload chunks."""

    fragments: list[str] = []
    data_lines: list[str] = []

    def flush_event() -> None:
        if not data_lines:
            return

        payload_text = "\n".join(data_lines).strip()
        data_lines.clear()

        if not payload_text or payload_text == "[DONE]":
            return

        payload = parse_json_maybe(payload_text)
        if not isinstance(payload, dict):
            return

        delta = payload.get("delta")
        if isinstance(delta, str) and delta:
            fragments.append(delta)

        choices = payload.get("choices")
        if not isinstance(choices, list):
            return

        for choice in choices:
            if not isinstance(choice, dict):
                continue

            choice_text = choice.get("text")
            if isinstance(choice_text, str) and choice_text:
                fragments.append(choice_text)

            delta_text = _message_text(choice.get("delta"))
            if delta_text:
                fragments.append(delta_text)

    for line in response_text.splitlines():
        if not line.strip():
            flush_event()
            continue

        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    flush_event()
    text = "".join(fragments)
    return text or None


def build_request_event(
    *,
    route: str,
    method: str,
    request_id: str,
    session_id: str | None,
    session_first_request: bool,
    stream: bool,
    headers_in: Mapping[str, str],
    raw_body: bytes,
    payload: Any | None,
) -> dict[str, Any]:
    """Build a compact request event for Loki."""

    event: dict[str, Any] = {
        "bucket": _request_bucket(route),
        "route": route,
        "method": method,
        "request_id": request_id,
        "session_id": session_id,
        "session_present": session_id is not None,
        "session_first_request": session_first_request,
        "stream": stream,
        "body_bytes": len(raw_body),
        "body_sha256": sha256_hexdigest(raw_body),
        **_header_summary(headers_in),
    }

    if route == "/v1/chat/completions":
        event.update(_chat_request_details(payload if isinstance(payload, dict) else None))
    elif isinstance(payload, dict) and payload.get("model") is not None:
        event["request_model"] = payload.get("model")

    if route == GENERATION_ROUTE and isinstance(payload, dict):
        event["request_json"] = _payload_without_messages(payload)

    return event


def build_response_event(
    *,
    route: str,
    method: str,
    request_id: str,
    session_id: str | None,
    session_first_request: bool,
    stream: bool,
    status_code: int,
    response_headers: Mapping[str, str],
    response_bytes: bytes,
    response_text: str,
    duration_sec: float,
    ttft_sec: float | None = None,
    session_init_ttft_sec: float | None = None,
    session_init_e2e_sec: float | None = None,
) -> dict[str, Any]:
    """Build a compact response event for Loki."""

    parsed_response = parse_json_maybe(response_text)
    assistant_text = None
    if route == GENERATION_ROUTE:
        assistant_text = (
            _stream_assistant_text(response_text)
            if stream
            else _json_assistant_text(parsed_response)
        )

    event: dict[str, Any] = _compact_dict(
        {
            "bucket": _response_bucket(route),
            "route": route,
            "method": method,
            "request_id": request_id,
            "stream": stream,
            "status_code": status_code,
            "duration_sec": round(duration_sec, 6),
            "ttft_sec": round(ttft_sec, 6) if ttft_sec is not None else None,
            "session_init_ttft_sec": (
                round(session_init_ttft_sec, 6) if session_init_ttft_sec is not None else None
            ),
            "session_init_e2e_sec": (
                round(session_init_e2e_sec, 6) if session_init_e2e_sec is not None else None
            ),
            "body_bytes": len(response_bytes),
            "body_sha256": sha256_hexdigest(response_bytes),
            "response_content_type": dict(response_headers).get("content-type"),
            "assistant_text": assistant_text,
        }
    )
    event.update(
        {
            "session_id": session_id,
            "session_present": session_id is not None,
            "session_first_request": session_first_request,
        }
    )

    if event["bucket"] == "response_generation":
        if stream:
            event["response_json"] = _stream_response_json(response_text)
        elif parsed_response is not None:
            event["response_json"] = parsed_response
        elif response_text:
            event["response_text"] = response_text
    elif parsed_response is not None:
        event["response_json"] = _sanitize_payload(parsed_response)

    return event


def build_error_event(
    *,
    route: str,
    method: str,
    request_id: str,
    session_id: str | None,
    session_first_request: bool,
    stream: bool,
    error: BaseException,
    duration_sec: float,
) -> dict[str, Any]:
    """Build an error event for gateway failures before a backend response exists."""

    return {
        "bucket": "gateway_error",
        "route": route,
        "method": method,
        "request_id": request_id,
        "session_id": session_id,
        "session_present": session_id is not None,
        "session_first_request": session_first_request,
        "stream": stream,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "duration_sec": round(duration_sec, 6),
    }
