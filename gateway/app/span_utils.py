"""OpenTelemetry span helpers for gateway request handling."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opentelemetry.trace import Span, Status, StatusCode

from app.utils import max_completion_tokens, message_count, model_label


def set_span_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    """Set non-null attributes on a span."""

    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def mark_error_if_needed(span: Span, status_code: int | None, cancelled: bool = False) -> None:
    """Mark a span as failed for cancellations, missing status, or HTTP errors."""

    if cancelled or status_code is None or status_code >= 400:
        span.set_status(Status(StatusCode.ERROR))


def request_span_attributes(
    *,
    request_id: str,
    session_id: str | None,
    session_first_request: bool,
    route: str,
    method: str,
    stream: bool,
    payload: Mapping[str, Any] | None,
    raw_body: bytes,
) -> dict[str, Any]:
    """Build standard attributes for the gateway request span."""

    attributes: dict[str, Any] = {
        "request.id": request_id,
        "session.present": session_id is not None,
        "session.first_request": session_first_request,
        "http.route": route,
        "http.method": method,
        "llm.model": model_label(payload),
        "llm.stream": stream,
        "llm.message_count": message_count(payload),
        "llm.max_completion_tokens": max_completion_tokens(payload),
        "llm.request.body_bytes": len(raw_body),
    }

    if session_id is not None:
        attributes["session.id"] = session_id

    return attributes
