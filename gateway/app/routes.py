"""HTTP routes exposed by the OpenAI-compatible gateway application."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any, AsyncIterator, cast

import orjson
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.http_utils import (
    parse_json_maybe,
    request_id_from_headers,
    session_id_from_headers,
    strip_hop_by_hop_headers,
)
from app.log_payloads import build_error_event, build_request_event, build_response_event
from app.observability import (
    REQUEST_COUNTER,
    REQUEST_LATENCY,
    SESSION_ID_MISSING_COUNTER,
    SESSION_INIT_E2E_LATENCY,
    SESSION_INIT_TTFT,
    SESSION_INIT_TTFT_MISSING_COUNTER,
    SESSION_REQUEST_COUNTER,
    uptime_seconds,
)
from app.state import AppState

tracer = trace.get_tracer("llm-gateway")
MAX_MODEL_LABEL_LENGTH = 128


def _get_state(app: FastAPI) -> AppState:
    """Return the initialized gateway state from the FastAPI application."""

    return cast(AppState, app.state.gateway_state)


def apply_max_completion_tokens_override(
    payload: dict[str, Any],
    max_completion_tokens: int,
) -> tuple[dict[str, Any], bytes, str]:
    patched = dict(payload)
    patched["max_completion_tokens"] = max_completion_tokens
    patched.pop("max_tokens", None)

    raw_body = orjson.dumps(patched)
    decoded_body = raw_body.decode("utf-8")
    return patched, raw_body, decoded_body


def status_family(status_code: int | None) -> str:
    if status_code is None:
        return "unknown"
    return f"{status_code // 100}xx"


def result_from_status(status_code: int | None, cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    if status_code is None or status_code >= 400:
        return "error"
    return "success"


def _bool_label(value: bool) -> str:
    return str(value).lower()


def _model_label(payload: Mapping[str, Any] | None) -> str:
    model = payload.get("model") if payload is not None else None
    if not isinstance(model, str):
        return "unknown"

    model = model.strip()
    if not model or len(model) > MAX_MODEL_LABEL_LENGTH:
        return "unknown"

    return model


def _message_count(payload: Mapping[str, Any] | None) -> int | None:
    messages = payload.get("messages") if payload is not None else None
    return len(messages) if isinstance(messages, list) else None


def _max_completion_tokens(payload: Mapping[str, Any] | None) -> int | None:
    if payload is None:
        return None

    value = payload.get("max_completion_tokens", payload.get("max_tokens"))
    if isinstance(value, int):
        return value
    return None


def _set_span_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def _mark_error_if_needed(span: Span, status_code: int | None, cancelled: bool = False) -> None:
    if cancelled or status_code is None or status_code >= 400:
        span.set_status(Status(StatusCode.ERROR))


def _request_span_attributes(
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
    attributes: dict[str, Any] = {
        "request.id": request_id,
        "session.present": session_id is not None,
        "session.first_request": session_first_request,
        "http.route": route,
        "http.method": method,
        "llm.model": _model_label(payload),
        "llm.stream": stream,
        "llm.message_count": _message_count(payload),
        "llm.max_completion_tokens": _max_completion_tokens(payload),
        "llm.request.body_bytes": len(raw_body),
    }

    if session_id is not None:
        attributes["session.id"] = session_id

    return attributes


def _gateway_response_headers(
    headers: Mapping[str, str],
    *,
    request_id: str,
    session_id: str | None,
) -> dict[str, str]:
    response_headers = strip_hop_by_hop_headers(headers)
    response_headers["x-request-id"] = request_id
    if session_id is not None:
        response_headers["x-session-id"] = session_id
    return response_headers


def _session_metric_labels(
    *,
    route: str,
    method: str,
    stream: bool,
    model: str,
    status_code: int | None,
    cancelled: bool,
) -> dict[str, str]:
    return {
        "route": route,
        "method": method,
        "stream": _bool_label(stream),
        "model": model,
        "status_family": status_family(status_code),
        "result": result_from_status(status_code, cancelled),
    }


def _record_session_counters(
    *,
    route: str,
    method: str,
    stream: bool,
    session_id: str | None,
    session_first_request: bool,
) -> None:
    if session_id is None:
        SESSION_ID_MISSING_COUNTER.labels(
            route=route,
            method=method,
            stream=_bool_label(stream),
        ).inc()

    SESSION_REQUEST_COUNTER.labels(
        route=route,
        method=method,
        stream=_bool_label(stream),
        session_present=_bool_label(session_id is not None),
        session_first_request=_bool_label(session_first_request),
    ).inc()


def _observe_session_e2e(
    *,
    session_first_request: bool,
    route: str,
    method: str,
    stream: bool,
    model: str,
    status_code: int | None,
    cancelled: bool,
    duration_sec: float,
) -> None:
    if not session_first_request:
        return

    SESSION_INIT_E2E_LATENCY.labels(
        **_session_metric_labels(
            route=route,
            method=method,
            stream=stream,
            model=model,
            status_code=status_code,
            cancelled=cancelled,
        )
    ).observe(duration_sec)


def _observe_session_ttft_missing(
    *,
    session_first_request: bool,
    route: str,
    method: str,
    stream: bool,
    model: str,
    reason: str,
    status_code: int | None,
    cancelled: bool,
) -> None:
    if not session_first_request:
        return

    SESSION_INIT_TTFT_MISSING_COUNTER.labels(
        route=route,
        method=method,
        stream=_bool_label(stream),
        model=model,
        reason=reason,
        result=result_from_status(status_code, cancelled),
    ).inc()


async def _log_gateway_error(
    *,
    state: AppState,
    route: str,
    method: str,
    request_id: str,
    session_id: str | None,
    session_first_request: bool,
    stream: bool,
    error: BaseException,
    duration_sec: float,
) -> None:
    event = build_error_event(
        route=route,
        method=method,
        request_id=request_id,
        session_id=session_id,
        session_first_request=session_first_request,
        stream=stream,
        error=error,
        duration_sec=duration_sec,
    )
    if session_first_request:
        event["session_init_e2e_sec"] = round(duration_sec, 6)

    await state.log_event(
        **event
    )


def create_router() -> APIRouter:
    """Create the application router."""

    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> JSONResponse:
        """Expose a minimal liveness endpoint for Docker and external probes."""

        return JSONResponse({"ok": True, "uptime_sec": round(uptime_seconds(), 3)})

    @router.get("/gateway/metrics")
    async def gateway_metrics() -> Response:
        """Expose Prometheus metrics collected by the gateway process."""

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @router.api_route("/v1/chat/completions", methods=["POST"])
    async def chat_completions(request: Request) -> Response:
        state = _get_state(request.app)
        settings = state.settings
        route = "/v1/chat/completions"
        method = "POST"
        started_at = time.perf_counter()

        raw_body = await request.body()
        decoded_body = raw_body.decode("utf-8", errors="replace")
        headers_in = {key: value for key, value in request.headers.items()}
        request_id = request_id_from_headers(headers_in)
        session_id = session_id_from_headers(headers_in)
        session_first_request = await state.session_tracker.mark_seen(session_id)

        payload = parse_json_maybe(decoded_body)
        if not isinstance(payload, dict):
            stream = False
            model = "unknown"
            status_code = 400
            response_body = b'{"error":"request body must be a JSON object"}'
            response_text = response_body.decode("utf-8")
            response_headers = _gateway_response_headers(
                {},
                request_id=request_id,
                session_id=session_id,
            )
            _record_session_counters(
                route=route,
                method=method,
                stream=stream,
                session_id=session_id,
                session_first_request=session_first_request,
            )
            REQUEST_COUNTER.labels(route=route, method=method, stream="false").inc()

            with tracer.start_as_current_span(
                "llm.gateway.request",
                attributes=_request_span_attributes(
                    request_id=request_id,
                    session_id=session_id,
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=stream,
                    payload=None,
                    raw_body=raw_body,
                ),
            ) as span:
                duration_sec = time.perf_counter() - started_at
                _set_span_attributes(
                    span,
                    {
                        "http.status_code": status_code,
                        "llm.response.body_bytes": len(response_body),
                        "llm.duration_sec": duration_sec,
                    },
                )
                span.set_status(Status(StatusCode.ERROR))
                await state.log_event(
                    **build_request_event(
                        route=route,
                        method=method,
                        request_id=request_id,
                        session_id=session_id,
                        session_first_request=session_first_request,
                        stream=stream,
                        headers_in=headers_in,
                        raw_body=raw_body,
                        decoded_body=decoded_body,
                        payload=None,
                    )
                )
                await state.log_event(
                    **build_response_event(
                        route=route,
                        method=method,
                        request_id=request_id,
                        session_id=session_id,
                        session_first_request=session_first_request,
                        stream=stream,
                        status_code=status_code,
                        response_headers=response_headers,
                        response_bytes=response_body,
                        response_text=response_text,
                        duration_sec=duration_sec,
                        session_init_e2e_sec=duration_sec if session_first_request else None,
                    )
                )
                REQUEST_LATENCY.labels(route=route, method=method, stream="false").observe(
                    duration_sec
                )
                _observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=stream,
                    model=model,
                    status_code=status_code,
                    cancelled=False,
                    duration_sec=duration_sec,
                )
                _observe_session_ttft_missing(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=stream,
                    model=model,
                    reason="non_stream",
                    status_code=status_code,
                    cancelled=False,
                )

            return Response(
                content=response_body,
                status_code=status_code,
                headers=response_headers,
                media_type="application/json",
            )

        if settings.enable_max_completion_tokens_override:
            payload, raw_body, decoded_body = apply_max_completion_tokens_override(
                payload,
                settings.forced_max_completion_tokens,
            )

        stream = bool(payload.get("stream"))
        model = _model_label(payload)
        _record_session_counters(
            route=route,
            method=method,
            stream=stream,
            session_id=session_id,
            session_first_request=session_first_request,
        )
        REQUEST_COUNTER.labels(route=route, method=method, stream=_bool_label(stream)).inc()

        backend_headers = state.backend.forwarded_headers(
            headers_in,
            request_id=request_id,
            session_id=session_id,
        )
        backend_url = state.backend.url_for(route)

        if stream:
            request_span = tracer.start_span(
                "llm.gateway.request",
                attributes=_request_span_attributes(
                    request_id=request_id,
                    session_id=session_id,
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=True,
                    payload=payload,
                    raw_body=raw_body,
                ),
            )

            try:
                with trace.use_span(request_span, end_on_exit=False):
                    backend_request = state.backend.build_request(
                        method=method,
                        route=route,
                        headers=backend_headers,
                        content=raw_body,
                    )
                    with tracer.start_as_current_span("llm.backend.request") as backend_span:
                        _set_span_attributes(
                            backend_span,
                            {
                                "http.method": method,
                                "http.url": backend_url,
                                "http.route": route,
                                "llm.model": model,
                                "llm.request.body_bytes": len(raw_body),
                            },
                        )
                        backend_response = await state.backend.send(backend_request, stream=True)
                        _set_span_attributes(
                            backend_span,
                            {"http.status_code": backend_response.status_code},
                        )
                        _mark_error_if_needed(backend_span, backend_response.status_code)

            except asyncio.CancelledError as exc:
                duration_sec = time.perf_counter() - started_at
                request_span.record_exception(exc)
                request_span.set_status(Status(StatusCode.ERROR))
                _set_span_attributes(
                    request_span,
                    {
                        "llm.duration_sec": duration_sec,
                        "llm.response.body_bytes": 0,
                        "llm.cancelled": True,
                    },
                )
                REQUEST_LATENCY.labels(route=route, method=method, stream="true").observe(
                    duration_sec
                )
                await _log_gateway_error(
                    state=state,
                    route=route,
                    method=method,
                    request_id=request_id,
                    session_id=session_id,
                    session_first_request=session_first_request,
                    stream=True,
                    error=exc,
                    duration_sec=duration_sec,
                )
                _observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=True,
                    model=model,
                    status_code=None,
                    cancelled=True,
                    duration_sec=duration_sec,
                )
                _observe_session_ttft_missing(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=True,
                    model=model,
                    reason="cancelled_before_first_chunk",
                    status_code=None,
                    cancelled=True,
                )
                request_span.end()
                raise

            except Exception as exc:
                duration_sec = time.perf_counter() - started_at
                request_span.record_exception(exc)
                request_span.set_status(Status(StatusCode.ERROR))
                _set_span_attributes(
                    request_span,
                    {
                        "llm.duration_sec": duration_sec,
                        "llm.response.body_bytes": 0,
                    },
                )
                REQUEST_LATENCY.labels(route=route, method=method, stream="true").observe(
                    duration_sec
                )
                await _log_gateway_error(
                    state=state,
                    route=route,
                    method=method,
                    request_id=request_id,
                    session_id=session_id,
                    session_first_request=session_first_request,
                    stream=True,
                    error=exc,
                    duration_sec=duration_sec,
                )
                _observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=True,
                    model=model,
                    status_code=None,
                    cancelled=False,
                    duration_sec=duration_sec,
                )
                _observe_session_ttft_missing(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=True,
                    model=model,
                    reason="error_before_first_chunk",
                    status_code=None,
                    cancelled=False,
                )
                request_span.end()
                raise

            status_code = backend_response.status_code
            response_headers = _gateway_response_headers(
                backend_response.headers,
                request_id=request_id,
                session_id=session_id,
            )
            raw_chunks: list[bytes] = []

            _set_span_attributes(request_span, {"http.status_code": status_code})
            _mark_error_if_needed(request_span, status_code)

            async def iterator() -> AsyncIterator[bytes]:
                chunk_count = 0
                response_bytes_count = 0
                ttft_sec: float | None = None
                cancelled = False
                stream_error: BaseException | None = None

                with trace.use_span(request_span, end_on_exit=False):
                    stream_span = tracer.start_span("llm.stream_response")
                    with trace.use_span(stream_span, end_on_exit=False):
                        try:
                            async for chunk in backend_response.aiter_bytes():
                                if chunk:
                                    chunk_count += 1
                                    response_bytes_count += len(chunk)
                                    if ttft_sec is None:
                                        ttft_sec = time.perf_counter() - started_at
                                        _set_span_attributes(
                                            stream_span,
                                            {"llm.ttft_sec": ttft_sec},
                                        )
                                        if session_first_request:
                                            SESSION_INIT_TTFT.labels(
                                                **_session_metric_labels(
                                                    route=route,
                                                    method=method,
                                                    stream=True,
                                                    model=model,
                                                    status_code=status_code,
                                                    cancelled=False,
                                                )
                                            ).observe(ttft_sec)

                                raw_chunks.append(chunk)
                                yield chunk

                        except asyncio.CancelledError as exc:
                            cancelled = True
                            stream_error = exc
                            stream_span.record_exception(exc)
                            stream_span.set_status(Status(StatusCode.ERROR))
                            request_span.record_exception(exc)
                            request_span.set_status(Status(StatusCode.ERROR))
                            raise

                        except Exception as exc:
                            stream_error = exc
                            stream_span.record_exception(exc)
                            stream_span.set_status(Status(StatusCode.ERROR))
                            request_span.record_exception(exc)
                            request_span.set_status(Status(StatusCode.ERROR))
                            raise

                        finally:
                            duration_sec = time.perf_counter() - started_at
                            response_bytes = b"".join(raw_chunks)
                            body_text = response_bytes.decode("utf-8", errors="replace")
                            _set_span_attributes(
                                stream_span,
                                {
                                    "llm.chunk_count": chunk_count,
                                    "llm.response.body_bytes": response_bytes_count,
                                    "llm.stream_duration_sec": duration_sec,
                                    "llm.cancelled": cancelled,
                                },
                            )
                            _set_span_attributes(
                                request_span,
                                {
                                    "llm.response.body_bytes": response_bytes_count,
                                    "llm.duration_sec": duration_sec,
                                    "llm.cancelled": cancelled,
                                },
                            )
                            _mark_error_if_needed(stream_span, status_code, cancelled=cancelled)
                            _mark_error_if_needed(request_span, status_code, cancelled=cancelled)
                            metric_status_code = (
                                None if stream_error is not None and not cancelled else status_code
                            )

                            try:
                                await state.log_event(
                                    **build_request_event(
                                        route=route,
                                        method=method,
                                        request_id=request_id,
                                        session_id=session_id,
                                        session_first_request=session_first_request,
                                        stream=True,
                                        headers_in=headers_in,
                                        raw_body=raw_body,
                                        decoded_body=decoded_body,
                                        payload=payload,
                                    )
                                )
                                await state.log_event(
                                    **build_response_event(
                                        route=route,
                                        method=method,
                                        request_id=request_id,
                                        session_id=session_id,
                                        session_first_request=session_first_request,
                                        stream=True,
                                        status_code=status_code,
                                        response_headers=response_headers,
                                        response_bytes=response_bytes,
                                        response_text=body_text,
                                        duration_sec=duration_sec,
                                        ttft_sec=ttft_sec,
                                        session_init_ttft_sec=(
                                            ttft_sec
                                            if session_first_request and ttft_sec is not None
                                            else None
                                        ),
                                        session_init_e2e_sec=(
                                            duration_sec if session_first_request else None
                                        ),
                                    )
                                )
                                if stream_error is not None:
                                    await _log_gateway_error(
                                        state=state,
                                        route=route,
                                        method=method,
                                        request_id=request_id,
                                        session_id=session_id,
                                        session_first_request=session_first_request,
                                        stream=True,
                                        error=stream_error,
                                        duration_sec=duration_sec,
                                    )
                                if ttft_sec is None:
                                    if cancelled:
                                        ttft_missing_reason = "cancelled_before_first_chunk"
                                    elif stream_error is not None:
                                        ttft_missing_reason = "error_before_first_chunk"
                                    else:
                                        ttft_missing_reason = "no_chunk"

                                    _observe_session_ttft_missing(
                                        session_first_request=session_first_request,
                                        route=route,
                                        method=method,
                                        stream=True,
                                        model=model,
                                        reason=ttft_missing_reason,
                                        status_code=metric_status_code,
                                        cancelled=cancelled,
                                    )
                                REQUEST_LATENCY.labels(
                                    route=route,
                                    method=method,
                                    stream="true",
                                ).observe(duration_sec)
                                _observe_session_e2e(
                                    session_first_request=session_first_request,
                                    route=route,
                                    method=method,
                                    stream=True,
                                    model=model,
                                    status_code=metric_status_code,
                                    cancelled=cancelled,
                                    duration_sec=duration_sec,
                                )

                            finally:
                                try:
                                    await backend_response.aclose()
                                finally:
                                    stream_span.end()
                                    request_span.end()

            return StreamingResponse(
                iterator(),
                status_code=status_code,
                headers=response_headers,
                media_type=backend_response.headers.get("content-type"),
            )

        with tracer.start_as_current_span(
            "llm.gateway.request",
            attributes=_request_span_attributes(
                request_id=request_id,
                session_id=session_id,
                session_first_request=session_first_request,
                route=route,
                method=method,
                stream=False,
                payload=payload,
                raw_body=raw_body,
            ),
        ) as span:
            try:
                with tracer.start_as_current_span("llm.backend.request") as backend_span:
                    _set_span_attributes(
                        backend_span,
                        {
                            "http.method": method,
                            "http.url": backend_url,
                            "http.route": route,
                            "llm.model": model,
                            "llm.request.body_bytes": len(raw_body),
                        },
                    )
                    backend_response = await state.backend.post(
                        route=route,
                        headers=backend_headers,
                        content=raw_body,
                    )
                    _set_span_attributes(
                        backend_span,
                        {"http.status_code": backend_response.status_code},
                    )
                    _mark_error_if_needed(backend_span, backend_response.status_code)

                response_headers = _gateway_response_headers(
                    backend_response.headers,
                    request_id=request_id,
                    session_id=session_id,
                )
                response_text = backend_response.text
                duration_sec = time.perf_counter() - started_at
                status_code = backend_response.status_code

                _set_span_attributes(
                    span,
                    {
                        "http.status_code": status_code,
                        "llm.response.body_bytes": len(backend_response.content),
                        "llm.duration_sec": duration_sec,
                    },
                )
                _mark_error_if_needed(span, status_code)

                await state.log_event(
                    **build_request_event(
                        route=route,
                        method=method,
                        request_id=request_id,
                        session_id=session_id,
                        session_first_request=session_first_request,
                        stream=False,
                        headers_in=headers_in,
                        raw_body=raw_body,
                        decoded_body=decoded_body,
                        payload=payload,
                    )
                )
                await state.log_event(
                    **build_response_event(
                        route=route,
                        method=method,
                        request_id=request_id,
                        session_id=session_id,
                        session_first_request=session_first_request,
                        stream=False,
                        status_code=status_code,
                        response_headers=response_headers,
                        response_bytes=backend_response.content,
                        response_text=response_text,
                        duration_sec=duration_sec,
                        session_init_e2e_sec=duration_sec if session_first_request else None,
                    )
                )

                REQUEST_LATENCY.labels(route=route, method=method, stream="false").observe(
                    duration_sec
                )
                _observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=False,
                    model=model,
                    status_code=status_code,
                    cancelled=False,
                    duration_sec=duration_sec,
                )
                _observe_session_ttft_missing(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=False,
                    model=model,
                    reason="non_stream",
                    status_code=status_code,
                    cancelled=False,
                )

                return Response(
                    content=backend_response.content,
                    status_code=status_code,
                    headers=response_headers,
                    media_type=backend_response.headers.get("content-type"),
                )

            except asyncio.CancelledError as exc:
                duration_sec = time.perf_counter() - started_at
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                _set_span_attributes(
                    span,
                    {
                        "llm.response.body_bytes": 0,
                        "llm.duration_sec": duration_sec,
                        "llm.cancelled": True,
                    },
                )
                REQUEST_LATENCY.labels(route=route, method=method, stream="false").observe(
                    duration_sec
                )
                await _log_gateway_error(
                    state=state,
                    route=route,
                    method=method,
                    request_id=request_id,
                    session_id=session_id,
                    session_first_request=session_first_request,
                    stream=False,
                    error=exc,
                    duration_sec=duration_sec,
                )
                _observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=False,
                    model=model,
                    status_code=None,
                    cancelled=True,
                    duration_sec=duration_sec,
                )
                _observe_session_ttft_missing(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=False,
                    model=model,
                    reason="non_stream",
                    status_code=None,
                    cancelled=True,
                )
                raise

            except Exception as exc:
                duration_sec = time.perf_counter() - started_at
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                _set_span_attributes(
                    span,
                    {
                        "llm.response.body_bytes": 0,
                        "llm.duration_sec": duration_sec,
                    },
                )
                REQUEST_LATENCY.labels(route=route, method=method, stream="false").observe(
                    duration_sec
                )
                await _log_gateway_error(
                    state=state,
                    route=route,
                    method=method,
                    request_id=request_id,
                    session_id=session_id,
                    session_first_request=session_first_request,
                    stream=False,
                    error=exc,
                    duration_sec=duration_sec,
                )
                _observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=False,
                    model=model,
                    status_code=None,
                    cancelled=False,
                    duration_sec=duration_sec,
                )
                _observe_session_ttft_missing(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=False,
                    model=model,
                    reason="non_stream",
                    status_code=None,
                    cancelled=False,
                )
                raise

    @router.api_route(
        "/v1/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def generic_v1_proxy(full_path: str, request: Request) -> Response:
        """Proxy every other ``/v1/*`` route with compact final-state logging."""

        state = _get_state(request.app)
        settings = state.settings
        route = f"/v1/{full_path}"
        method = request.method.upper()
        started_at = time.perf_counter()
        raw_body = await request.body()
        decoded_body = raw_body.decode("utf-8", errors="replace")
        headers_in = {key: value for key, value in request.headers.items()}
        request_id = request_id_from_headers(headers_in)
        session_id = session_id_from_headers(headers_in)
        session_first_request = False
        payload = parse_json_maybe(decoded_body)

        backend_headers = state.backend.forwarded_headers(
            headers_in,
            request_id=request_id,
            session_id=session_id,
        )

        backend_response = await state.backend.request(
            method=method,
            route=route,
            headers=backend_headers,
            content=raw_body,
            params=request.query_params,
        )
        response_headers = _gateway_response_headers(
            backend_response.headers,
            request_id=request_id,
            session_id=session_id,
        )

        # Non-generation routes share the same compact shape so dashboards and
        # retention policies can treat them as one operational bucket.
        await state.log_event(
            **build_request_event(
                route=route,
                method=method,
                request_id=request_id,
                session_id=session_id,
                session_first_request=session_first_request,
                stream=False,
                headers_in=headers_in,
                raw_body=raw_body,
                decoded_body=decoded_body,
                payload=payload,
            )
        )
        await state.log_event(
            **build_response_event(
                route=route,
                method=method,
                request_id=request_id,
                session_id=session_id,
                session_first_request=session_first_request,
                stream=False,
                status_code=backend_response.status_code,
                response_headers=response_headers,
                response_bytes=backend_response.content,
                response_text=backend_response.text,
                duration_sec=time.perf_counter() - started_at,
            )
        )

        REQUEST_COUNTER.labels(route=route, method=method, stream="false").inc()
        REQUEST_LATENCY.labels(route=route, method=method, stream="false").observe(
            time.perf_counter() - started_at
        )
        return Response(
            content=backend_response.content,
            status_code=backend_response.status_code,
            headers=response_headers,
            media_type=backend_response.headers.get("content-type"),
        )

    @router.get("/")
    async def root() -> PlainTextResponse:
        """Return a tiny human-readable status page for manual checks."""

        return PlainTextResponse("OpenAI-compatible gateway is up")

    return router
