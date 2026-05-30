"""HTTP routes exposed by the OpenAI-compatible gateway application."""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Mapping, cast

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError

from app.gateway_responses import gateway_response_headers
from app.http_utils import (
    parse_json_maybe,
    request_id_from_headers,
    session_id_from_headers,
)
from app.llm_payloads import model_label
from app.loki_logging import LokiRequestContext
from app.metrics import MetricsRequestContext
from app.request_shaping import apply_chat_payload_overrides, apply_generic_payload_overrides
from app.route_paths import (
    CHAT_COMPLETIONS_ROUTE,
    GATEWAY_METRICS_ROUTE,
    GATEWAY_SESSION_DETAIL_ROUTE,
    GATEWAY_SESSION_LIST_ROUTE,
    GENERIC_V1_PROXY_ROUTE,
    HEALTH_ROUTE,
    ROOT_ROUTE,
    V1_ROUTE_PREFIX,
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


    @router.get(HEALTH_ROUTE)
    async def health(request: Request) -> Response:
        """Return the backend health endpoint response."""

        state = _get_state(request.app)

        try:
            backend_response = await state.backend.request(
                method="GET",
                route=HEALTH_ROUTE,
                headers={},
                content=b"",
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "backend": "unavailable",
                    "detail": type(exc).__name__,
                },
                status_code=503,
            )

        return Response(
            content=backend_response.content,
            status_code=backend_response.status_code,
            media_type=backend_response.headers.get("content-type"),
        )


    @router.get(GATEWAY_METRICS_ROUTE)
    async def gateway_metrics(request: Request) -> Response:
        """Expose Prometheus metrics collected by the gateway process."""

        state = _get_state(request.app)
        active_session_count = await state.session_tracker.active_session_count()

        if active_session_count is not None:
            state.metrics.set_active_sessions(active_session_count)

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


    @router.get(GATEWAY_SESSION_LIST_ROUTE)
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


    @router.get(GATEWAY_SESSION_DETAIL_ROUTE)
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


    @router.api_route(CHAT_COMPLETIONS_ROUTE, methods=["POST"])
    async def chat_completions(request: Request) -> Response:
        """Proxy chat completions with session tracking, metrics, logs, and tracing."""

        state = _get_state(request.app)
        settings = state.settings
        route = CHAT_COMPLETIONS_ROUTE
        method = "POST"
        started_at = time.perf_counter()

        raw_body = await request.body()
        decoded_body = raw_body.decode("utf-8", errors="replace")
        headers_in = {key: value for key, value in request.headers.items()}
        request_id = request_id_from_headers(headers_in)
        session_id = session_id_from_headers(headers_in)
        session_first_request = await state.session_tracker.mark_seen(session_id)

        payload = parse_json_maybe(decoded_body)

        log_context = state.loki.context(
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

        # Branch: malformed chat completion request. We get here when the
        # request body is not valid JSON or when it decodes to a JSON value
        # other than an object, so the gateway cannot safely apply OpenAI chat
        # request handling and returns its own 400 response without touching
        # the LLM backend.
        if not isinstance(payload, dict):
            metrics_context = state.metrics.context(
                route=route,
                method=method,
                stream=False,
                model="unknown",
                session_id=session_id,
                session_first_request=session_first_request,
            )

            return await _handle_invalid_chat_payload(
                route=route,
                method=method,
                started_at=started_at,
                raw_body=raw_body,
                request_id=request_id,
                session_id=session_id,
                session_first_request=session_first_request,
                log_context=log_context,
                metrics_context=metrics_context,
            )

        # Branch: valid chat JSON with configured gateway-side request shaping.
        # We get here only for a parsed JSON object, and only when deployment
        # settings require deterministic overrides before the payload is stored,
        # logged, and sent to the backend.
        if (
            settings.forced_max_completion_tokens is not None
            or settings.forced_thinking_disabled
        ):
            payload, raw_body, _decoded_body = apply_chat_payload_overrides(
                payload,
                forced_max_completion_tokens=settings.forced_max_completion_tokens,
                forced_thinking_disabled=settings.forced_thinking_disabled,
            )

        await state.session_store.save_messages(session_id, payload.get("messages"))

        stream = bool(payload.get("stream"))
        model = model_label(payload)

        log_context.stream = stream
        log_context.raw_body = raw_body
        log_context.payload = payload

        await log_context.request()
        metrics_context = state.metrics.context(
            route=route,
            method=method,
            stream=stream,
            model=model,
            session_id=session_id,
            session_first_request=session_first_request,
        )
        metrics_context.request()

        backend_headers = state.backend.forwarded_headers(
            headers_in,
            request_id=request_id,
            session_id=session_id,
        )
        backend_url = state.backend.url_for(route)

        # Branch: streaming chat completion. We reach this branch only after
        # the body has been parsed as a JSON object, request shaping has been
        # applied, session state has been persisted, and the caller explicitly
        # requested OpenAI-compatible SSE streaming with ``stream=true``.
        if stream:
            return await _handle_stream_chat_completion(
                state=state,
                route=route,
                method=method,
                started_at=started_at,
                raw_body=raw_body,
                payload=payload,
                model=model,
                request_id=request_id,
                session_id=session_id,
                session_first_request=session_first_request,
                backend_headers=backend_headers,
                backend_url=backend_url,
                log_context=log_context,
                metrics_context=metrics_context,
            )

        # Branch: non-streaming chat completion. We reach this branch after the
        # same validation and request shaping as the streaming branch, but the
        # payload either omits ``stream`` or sets it to a false value, so the
        # backend response is read fully before returning it to the caller.
        return await _handle_non_stream_chat_completion(
            state=state,
            route=route,
            method=method,
            started_at=started_at,
            raw_body=raw_body,
            payload=payload,
            model=model,
            request_id=request_id,
            session_id=session_id,
            session_first_request=session_first_request,
            backend_headers=backend_headers,
            backend_url=backend_url,
            log_context=log_context,
            metrics_context=metrics_context,
        )


    @router.api_route(
        GENERIC_V1_PROXY_ROUTE,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def generic_v1_proxy(full_path: str, request: Request) -> Response:
        """Proxy every other ``/v1/*`` route with compact final-state logging."""

        state = _get_state(request.app)
        settings = state.settings
        route = f"{V1_ROUTE_PREFIX}/{full_path}"
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
            payload, raw_body, _decoded_body = apply_generic_payload_overrides(
                payload,
                forced_thinking_disabled=settings.forced_thinking_disabled,
            )

        backend_headers = state.backend.forwarded_headers(
            headers_in,
            request_id=request_id,
            session_id=session_id,
        )
        log_context = state.loki.context(
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
        await log_context.request()
        metrics_context = state.metrics.context(
            route=route,
            method=method,
            stream=False,
            session_id=session_id,
            session_first_request=session_first_request,
        )
        metrics_context.request()

        try:
            backend_response = await state.backend.request(
                method=method,
                route=route,
                headers=backend_headers,
                content=raw_body,
                params=request.query_params,
            )
        except asyncio.CancelledError as exc:
            duration_sec = time.perf_counter() - started_at
            await log_context.error(exc, e2e_sec=duration_sec)
            metrics_context.response(
                status_code=None,
                cancelled=True,
                e2e_sec=duration_sec,
            )
            raise

        except Exception as exc:
            duration_sec = time.perf_counter() - started_at
            await log_context.error(exc, e2e_sec=duration_sec)
            metrics_context.response(
                status_code=None,
                cancelled=False,
                e2e_sec=duration_sec,
            )
            raise

        response_headers = gateway_response_headers(
            backend_response.headers,
            request_id=request_id,
            session_id=session_id,
        )
        duration_sec = time.perf_counter() - started_at

        await log_context.response(
            status_code=backend_response.status_code,
            response_headers=response_headers,
            response_bytes=backend_response.content,
            response_text=backend_response.text,
            e2e_sec=duration_sec,
        )

        metrics_context.response(
            status_code=backend_response.status_code,
            cancelled=False,
            e2e_sec=duration_sec,
        )

        return Response(
            content=backend_response.content,
            status_code=backend_response.status_code,
            headers=response_headers,
            media_type=backend_response.headers.get("content-type"),
        )


    @router.get(ROOT_ROUTE)
    async def root() -> PlainTextResponse:
        """Return a tiny human-readable status page for manual checks."""

        return PlainTextResponse("OpenAI-compatible gateway is up")

    return router



async def _handle_invalid_chat_payload(
    *,
    route: str,
    method: str,
    started_at: float,
    raw_body: bytes,
    request_id: str,
    session_id: str | None,
    session_first_request: bool,
    log_context: LokiRequestContext,
    metrics_context: MetricsRequestContext,
) -> Response:
    """Return and log the gateway-managed response for malformed chat payloads."""

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

    metrics_context.request()
    await log_context.request()

    # Branch: gateway-owned malformed-payload response. We enter this helper
    # only after chat payload parsing failed in the route, so this span and
    # response represent gateway validation rather than an LLM backend call.
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

        await log_context.response(
            status_code=status_code,
            response_headers=response_headers,
            response_bytes=response_body,
            response_text=response_text,
            e2e_sec=duration_sec,
        )
        metrics_context.response(
            status_code=status_code,
            cancelled=False,
            e2e_sec=duration_sec,
        )

    return Response(
        content=response_body,
        status_code=status_code,
        headers=response_headers,
        media_type="application/json",
    )


async def _handle_stream_chat_completion(
    *,
    state: AppState,
    route: str,
    method: str,
    started_at: float,
    raw_body: bytes,
    payload: dict[str, Any],
    model: str,
    request_id: str,
    session_id: str | None,
    session_first_request: bool,
    backend_headers: Mapping[str, str],
    backend_url: str,
    log_context: LokiRequestContext,
    metrics_context: MetricsRequestContext,
) -> Response:
    """Proxy one streaming chat completion request."""

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

    # Branch: caller disconnected or the ASGI server cancelled the request
    # before the gateway could obtain backend response headers. The stream body
    # never starts, so the terminal record is a gateway error with
    # ``cancelled=True`` in metrics.
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
        await log_context.error(exc, e2e_sec=duration_sec)
        metrics_context.response(
            status_code=None,
            cancelled=True,
            e2e_sec=duration_sec,
        )

        request_span.end()
        raise

    # Branch: backend request setup or header acquisition failed before a
    # streaming response could be returned to the caller. There is no backend
    # body to log, so the terminal Loki event is the exception itself.
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
        await log_context.error(exc, e2e_sec=duration_sec)
        metrics_context.response(
            status_code=None,
            cancelled=False,
            e2e_sec=duration_sec,
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
                        # Branch: non-empty SSE/body chunk from the backend.
                        # This is the only point where TTFT can be discovered;
                        # empty keepalive chunks are still forwarded but do not
                        # establish first-token timing.
                        if chunk:
                            chunk_count += 1
                            response_bytes_count += len(chunk)

                            if ttft_sec is None:
                                ttft_sec = time.perf_counter() - started_at
                                set_span_attributes(
                                    stream_span,
                                    {"llm.ttft_sec": ttft_sec},
                                )

                                metrics_context.ttft(
                                    status_code=status_code,
                                    cancelled=False,
                                    ttft_sec=ttft_sec,
                                )

                        raw_chunks.append(chunk)
                        yield chunk

                # Branch: downstream stream consumption was cancelled after
                # backend headers were already received. We preserve the bytes
                # collected so far for metrics, mark spans as cancelled, and
                # let the finalizer close the backend response.
                except asyncio.CancelledError as exc:
                    cancelled = True
                    stream_error = exc
                    stream_span.record_exception(exc)
                    stream_span.set_status(Status(StatusCode.ERROR))
                    request_span.record_exception(exc)
                    request_span.set_status(Status(StatusCode.ERROR))
                    raise

                # Branch: backend stream iteration failed mid-response. This
                # can happen after partial chunks were forwarded, so the final
                # accounting uses the partial byte buffer but emits an error
                # terminal Loki event instead of a successful response event.
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
                        # Branch: stream completed without iterator errors.
                        # We now have the full streamed backend response body,
                        # so this is the single response logging point for
                        # streaming chat completions.
                        if stream_error is None:
                            await log_context.response(
                                status_code=status_code,
                                response_headers=response_headers,
                                response_bytes=response_bytes,
                                response_text=body_text,
                                e2e_sec=duration_sec,
                                ttft_sec=ttft_sec,
                            )

                        # Branch: stream ended because the iterator raised or
                        # was cancelled. The terminal event is an error because
                        # the gateway could not observe a complete response.
                        else:
                            await log_context.error(stream_error, e2e_sec=duration_sec)

                        metrics_context.response(
                            status_code=metric_status_code,
                            cancelled=cancelled,
                            e2e_sec=duration_sec,
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


async def _handle_non_stream_chat_completion(
    *,
    state: AppState,
    route: str,
    method: str,
    started_at: float,
    raw_body: bytes,
    payload: dict[str, Any],
    model: str,
    request_id: str,
    session_id: str | None,
    session_first_request: bool,
    backend_headers: Mapping[str, str],
    backend_url: str,
    log_context: LokiRequestContext,
    metrics_context: MetricsRequestContext,
) -> Response:
    """Proxy one non-streaming chat completion request."""

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

            # Branch: successful non-stream backend response. The backend body
            # has already been fully read by httpx, so this is the only response
            # logging point for non-streaming chat completions.
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

            await log_context.response(
                status_code=status_code,
                response_headers=response_headers,
                response_bytes=backend_response.content,
                response_text=response_text,
                e2e_sec=duration_sec,
            )

            metrics_context.response(
                status_code=status_code,
                cancelled=False,
                e2e_sec=duration_sec,
            )

            return Response(
                content=backend_response.content,
                status_code=status_code,
                headers=response_headers,
                media_type=backend_response.headers.get("content-type"),
            )

        # Branch: caller disconnected or the ASGI server cancelled the request
        # while the non-stream backend call was still in progress. No complete
        # backend response exists, so the terminal Loki event is a gateway error.
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
            await log_context.error(exc, e2e_sec=duration_sec)
            metrics_context.response(
                status_code=None,
                cancelled=True,
                e2e_sec=duration_sec,
            )
            raise

        # Branch: non-stream backend call or response handling failed before a
        # complete response could be returned. This is logged as a terminal
        # gateway error rather than as a response event.
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
            await log_context.error(exc, e2e_sec=duration_sec)
            metrics_context.response(
                status_code=None,
                cancelled=False,
                e2e_sec=duration_sec,
            )
            raise
