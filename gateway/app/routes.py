"""HTTP routes exposed by the OpenAI-compatible gateway application."""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, cast

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError

from app.gateway_errors import log_gateway_error
from app.gateway_responses import gateway_response_headers
from app.http_utils import (
    parse_json_maybe,
    request_id_from_headers,
    session_id_from_headers,
)
from app.llm_payloads import model_label
from app.log_payloads import build_request_event, build_response_event
from app.observability import (
    ACTIVE_SESSION_GAUGE,
    REQUEST_COUNTER,
    REQUEST_LATENCY,
    SESSION_INIT_TTFT,
    uptime_seconds,
)
from app.proxy_metrics import record_proxy_response
from app.request_shaping import apply_chat_payload_overrides, apply_generic_payload_overrides
from app.session_metrics import (
    bool_label,
    observe_session_e2e,
    record_session_request,
    session_metric_labels,
)
from app.span_utils import mark_error_if_needed, request_span_attributes, set_span_attributes
from app.state import AppState

tracer = trace.get_tracer("llm-gateway")


def _get_state(app: FastAPI) -> AppState:
    """Return the initialized gateway state from the FastAPI application."""

    return cast(AppState, app.state.gateway_state)


def create_router() -> APIRouter:
    """Create the application router."""

    router = APIRouter()


    @router.get("/healthz")
    async def healthz() -> JSONResponse:
        """Expose a minimal liveness endpoint for Docker and external probes."""

        return JSONResponse({"ok": True, "uptime_sec": round(uptime_seconds(), 3)})


    @router.get("/gateway/metrics")
    async def gateway_metrics(request: Request) -> Response:
        """Expose Prometheus metrics collected by the gateway process."""

        state = _get_state(request.app)
        active_session_count = await state.session_tracker.active_session_count()
        
        if active_session_count is not None:
            ACTIVE_SESSION_GAUGE.set(active_session_count)

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


    @router.get("/gateway/session_list")
    async def session_list(request: Request) -> JSONResponse:
        """Return ids for all persisted chat sessions."""

        state = _get_state(request.app)
        try:
            session_ids = await state.session_store.list_session_ids()
        except RedisError as exc:
            return JSONResponse(
                {"error": "session store unavailable", "detail": type(exc).__name__},
                status_code=503,
            )

        return JSONResponse(session_ids)

    @router.get("/gateway/session/{session_id}")
    async def session_get(session_id: str, request: Request) -> JSONResponse:
        """Return one persisted chat session by external session id."""

        state = _get_state(request.app)
        try:
            session = await state.session_store.get_session(session_id)
        except RedisError as exc:
            return JSONResponse(
                {"error": "session store unavailable", "detail": type(exc).__name__},
                status_code=503,
            )

        if session is None:
            return JSONResponse({"error": "session not found"}, status_code=404)

        return JSONResponse(session)

    @router.api_route("/v1/chat/completions", methods=["POST"])
    async def chat_completions(request: Request) -> Response:
        """Proxy chat completions with session tracking, metrics, logs, and tracing."""

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
            response_headers = gateway_response_headers(
                {},
                request_id=request_id,
                session_id=session_id,
            )
            record_session_request(
                route=route,
                method=method,
                stream=stream,
                session_id=session_id,
                session_first_request=session_first_request,
            )
            REQUEST_COUNTER.labels(route=route, method=method, stream="false").inc()

            with tracer.start_as_current_span(
                "llm.gateway.request",
                attributes=request_span_attributes(
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
                set_span_attributes(
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
                observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=stream,
                    model=model,
                    status_code=status_code,
                    cancelled=False,
                    duration_sec=duration_sec,
                )
                record_proxy_response(
                    route=route,
                    method=method,
                    stream=stream,
                    status_code=status_code,
                    cancelled=False,
                )

            return Response(
                content=response_body,
                status_code=status_code,
                headers=response_headers,
                media_type="application/json",
            )

        if (
            settings.forced_max_completion_tokens is not None
            or settings.forced_thinking_disabled
        ):
            payload, raw_body, decoded_body = apply_chat_payload_overrides(
                payload,
                forced_max_completion_tokens=settings.forced_max_completion_tokens,
                forced_thinking_disabled=settings.forced_thinking_disabled,
            )

        await state.session_store.save_messages(session_id, payload.get("messages"))

        stream = bool(payload.get("stream"))
        model = model_label(payload)
        record_session_request(
            route=route,
            method=method,
            stream=stream,
            session_id=session_id,
            session_first_request=session_first_request,
        )
        REQUEST_COUNTER.labels(route=route, method=method, stream=bool_label(stream)).inc()

        backend_headers = state.backend.forwarded_headers(
            headers_in,
            request_id=request_id,
            session_id=session_id,
        )
        backend_url = state.backend.url_for(route)

        if stream:
            request_span = tracer.start_span(
                "llm.gateway.request",
                attributes=request_span_attributes(
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
                        set_span_attributes(
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
                        set_span_attributes(
                            backend_span,
                            {"http.status_code": backend_response.status_code},
                        )
                        mark_error_if_needed(backend_span, backend_response.status_code)

            except asyncio.CancelledError as exc:
                duration_sec = time.perf_counter() - started_at
                request_span.record_exception(exc)
                request_span.set_status(Status(StatusCode.ERROR))
                set_span_attributes(
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
                await log_gateway_error(
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
                observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=True,
                    model=model,
                    status_code=None,
                    cancelled=True,
                    duration_sec=duration_sec,
                )
                record_proxy_response(
                    route=route,
                    method=method,
                    stream=True,
                    status_code=None,
                    cancelled=True,
                )
                request_span.end()
                raise

            except Exception as exc:
                duration_sec = time.perf_counter() - started_at
                request_span.record_exception(exc)
                request_span.set_status(Status(StatusCode.ERROR))
                set_span_attributes(
                    request_span,
                    {
                        "llm.duration_sec": duration_sec,
                        "llm.response.body_bytes": 0,
                    },
                )
                REQUEST_LATENCY.labels(route=route, method=method, stream="true").observe(
                    duration_sec
                )
                await log_gateway_error(
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
                observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=True,
                    model=model,
                    status_code=None,
                    cancelled=False,
                    duration_sec=duration_sec,
                )
                record_proxy_response(
                    route=route,
                    method=method,
                    stream=True,
                    status_code=None,
                    cancelled=False,
                )
                request_span.end()
                raise

            status_code = backend_response.status_code
            response_headers = gateway_response_headers(
                backend_response.headers,
                request_id=request_id,
                session_id=session_id,
            )
            raw_chunks: list[bytes] = []

            set_span_attributes(request_span, {"http.status_code": status_code})
            mark_error_if_needed(request_span, status_code)

            async def iterator() -> AsyncIterator[bytes]:
                """Yield backend stream chunks while recording final stream telemetry."""

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
                                        set_span_attributes(
                                            stream_span,
                                            {"llm.ttft_sec": ttft_sec},
                                        )
                                        if session_first_request:
                                            SESSION_INIT_TTFT.labels(
                                                **session_metric_labels(
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
                            set_span_attributes(
                                stream_span,
                                {
                                    "llm.chunk_count": chunk_count,
                                    "llm.response.body_bytes": response_bytes_count,
                                    "llm.stream_duration_sec": duration_sec,
                                    "llm.cancelled": cancelled,
                                },
                            )
                            set_span_attributes(
                                request_span,
                                {
                                    "llm.response.body_bytes": response_bytes_count,
                                    "llm.duration_sec": duration_sec,
                                    "llm.cancelled": cancelled,
                                },
                            )
                            mark_error_if_needed(stream_span, status_code, cancelled=cancelled)
                            mark_error_if_needed(request_span, status_code, cancelled=cancelled)
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
                                    await log_gateway_error(
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
                                REQUEST_LATENCY.labels(
                                    route=route,
                                    method=method,
                                    stream="true",
                                ).observe(duration_sec)
                                observe_session_e2e(
                                    session_first_request=session_first_request,
                                    route=route,
                                    method=method,
                                    stream=True,
                                    model=model,
                                    status_code=metric_status_code,
                                    cancelled=cancelled,
                                    duration_sec=duration_sec,
                                )
                                record_proxy_response(
                                    route=route,
                                    method=method,
                                    stream=True,
                                    status_code=metric_status_code,
                                    cancelled=cancelled,
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
            attributes=request_span_attributes(
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
                    set_span_attributes(
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
                    set_span_attributes(
                        backend_span,
                        {"http.status_code": backend_response.status_code},
                    )
                    mark_error_if_needed(backend_span, backend_response.status_code)

                response_headers = gateway_response_headers(
                    backend_response.headers,
                    request_id=request_id,
                    session_id=session_id,
                )
                response_text = backend_response.text
                duration_sec = time.perf_counter() - started_at
                status_code = backend_response.status_code

                set_span_attributes(
                    span,
                    {
                        "http.status_code": status_code,
                        "llm.response.body_bytes": len(backend_response.content),
                        "llm.duration_sec": duration_sec,
                    },
                )
                mark_error_if_needed(span, status_code)

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
                observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=False,
                    model=model,
                    status_code=status_code,
                    cancelled=False,
                    duration_sec=duration_sec,
                )
                record_proxy_response(
                    route=route,
                    method=method,
                    stream=False,
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
                set_span_attributes(
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
                await log_gateway_error(
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
                observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=False,
                    model=model,
                    status_code=None,
                    cancelled=True,
                    duration_sec=duration_sec,
                )
                record_proxy_response(
                    route=route,
                    method=method,
                    stream=False,
                    status_code=None,
                    cancelled=True,
                )
                raise

            except Exception as exc:
                duration_sec = time.perf_counter() - started_at
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                set_span_attributes(
                    span,
                    {
                        "llm.response.body_bytes": 0,
                        "llm.duration_sec": duration_sec,
                    },
                )
                REQUEST_LATENCY.labels(route=route, method=method, stream="false").observe(
                    duration_sec
                )
                await log_gateway_error(
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
                observe_session_e2e(
                    session_first_request=session_first_request,
                    route=route,
                    method=method,
                    stream=False,
                    model=model,
                    status_code=None,
                    cancelled=False,
                    duration_sec=duration_sec,
                )
                record_proxy_response(
                    route=route,
                    method=method,
                    stream=False,
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
        if isinstance(payload, dict) and settings.forced_thinking_disabled:
            payload, raw_body, decoded_body = apply_generic_payload_overrides(
                payload,
                forced_thinking_disabled=settings.forced_thinking_disabled,
            )

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
        response_headers = gateway_response_headers(
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
        record_proxy_response(
            route=route,
            method=method,
            stream=False,
            status_code=backend_response.status_code,
            cancelled=False,
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
