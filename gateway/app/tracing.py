"""OpenTelemetry tracing setup, span names, and span attribute contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, Status, StatusCode

from app.settings import Settings
from app.utils import max_completion_tokens, message_count, model_label


SPAN_GATEWAY_REQUEST = "llm.gateway.request"
SPAN_BACKEND_REQUEST = "llm.backend.request"
SPAN_STREAM_RESPONSE = "llm.stream_response"
SPAN_SESSION_FLOW = "llm.session.flow"
SPAN_VALKEY_OPERATION = "valkey.operation"

TRACER_NAME = "llm-gateway"


#################
# OTEL SETUP    #
#################


def configure_tracing(app: FastAPI, settings: Settings) -> None:
    """Configure OTLP tracing for the FastAPI application when enabled."""

    if not settings.otel_enabled:
        return

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.namespace": "llm-serving",
        }
    )

    sampler = ParentBased(TraceIdRatioBased(settings.otel_sample_ratio))
    provider = TracerProvider(resource=resource, sampler=sampler)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        )
    )

    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=settings.otel_fastapi_excluded_urls,
    )


#################
# TRACE CONTEXT #
#################


def current_trace_context() -> dict[str, str | None]:
    """Return the current span identifiers formatted for Loki JSON events."""

    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {"trace_id": None, "span_id": None}

    return {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }


##################
# SPAN MUTATIONS #
##################


def set_span_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    """Set every non-null attribute on a span."""

    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def mark_error_if_needed(
    span: Span,
    status_code: int | None,
    *,
    cancelled: bool = False,
) -> None:
    """Mark a span as failed for cancellations, missing status, or HTTP errors."""

    if cancelled or status_code is None or status_code >= 400:
        span.set_status(Status(StatusCode.ERROR))


def record_span_exception(span: Span, exc: BaseException) -> None:
    """Record an exception and mark a span as failed."""

    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR))


def add_current_span_error_event(
    event_name: str,
    exc: BaseException,
    attributes: Mapping[str, Any],
) -> None:
    """Attach an error event to the current span when tracing is active."""

    span = trace.get_current_span()

    if not span.get_span_context().is_valid:
        return

    span.record_exception(exc)
    span.add_event(event_name, attributes)


##################
# SPAN CONTRACTS #
##################


def gateway_request_span_attrs(
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
    """Build initial attributes for ``llm.gateway.request`` spans."""

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


def gateway_response_span_attrs(
    *,
    status_code: int | None,
    response_body_bytes: int,
    duration_sec: float,
    cancelled: bool = False,
) -> dict[str, Any]:
    """Build terminal attributes for ``llm.gateway.request`` spans."""

    return {
        "http.status_code": status_code,
        "llm.response.body_bytes": response_body_bytes,
        "llm.duration_sec": duration_sec,
        "llm.cancelled": cancelled,
    }


def backend_request_span_attrs(
    *,
    method: str,
    url: str,
    route: str,
    model: str,
    request_body_bytes: int,
) -> dict[str, Any]:
    """Build initial attributes for ``llm.backend.request`` spans."""

    return {
        "http.method": method,
        "http.url": url,
        "http.route": route,
        "llm.model": model,
        "llm.request.body_bytes": request_body_bytes,
    }


def backend_response_span_attrs(status_code: int | None) -> dict[str, Any]:
    """Build terminal attributes for ``llm.backend.request`` spans."""

    return {"http.status_code": status_code}


def http_status_span_attrs(status_code: int | None) -> dict[str, Any]:
    """Build the shared HTTP status attribute for any HTTP-related span."""

    return {"http.status_code": status_code}


def stream_ttft_span_attrs(ttft_sec: float) -> dict[str, Any]:
    """Build first-token attributes for ``llm.stream_response`` spans."""

    return {"llm.ttft_sec": ttft_sec}


def stream_response_span_attrs(
    *,
    chunk_count: int,
    response_body_bytes: int,
    duration_sec: float,
    cancelled: bool,
) -> dict[str, Any]:
    """Build terminal attributes for ``llm.stream_response`` spans."""

    return {
        "llm.chunk_count": chunk_count,
        "llm.response.body_bytes": response_body_bytes,
        "llm.stream_duration_sec": duration_sec,
        "llm.cancelled": cancelled,
    }


def session_flow_span_attrs(
    *,
    route: str,
    method: str,
    session_id: str | None,
) -> dict[str, Any]:
    """Build initial attributes for ``llm.session.flow`` spans."""

    attributes: dict[str, Any] = {
        "http.route": route,
        "http.method": method,
        "session.present": session_id is not None,
    }

    if session_id is not None:
        attributes["session.id"] = session_id

    return attributes


def session_flow_result_span_attrs(
    *,
    session_first_request: bool,
    messages_saved: bool,
) -> dict[str, Any]:
    """Build terminal attributes for ``llm.session.flow`` spans."""

    return {
        "session.first_request": session_first_request,
        "session.messages_saved": messages_saved,
    }


def valkey_operation_span_attrs(
    *,
    operation: str,
    prefix: str,
    record_id: str | None = None,
    pattern: str | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    """Build attributes for one Valkey command or scan operation."""

    return {
        "db.system": "valkey",
        "valkey.operation": operation,
        "valkey.key_prefix": prefix,
        "valkey.record_present": record_id is not None,
        "valkey.pattern": pattern,
        "valkey.scan_count": count,
    }


def valkey_result_span_attrs(
    *,
    found: bool | None = None,
    created: bool | None = None,
    updated: bool | None = None,
    deleted: bool | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    """Build result attributes for a Valkey operation span."""

    return {
        "valkey.found": found,
        "valkey.created": created,
        "valkey.updated": updated,
        "valkey.deleted": deleted,
        "valkey.result_count": count,
    }
