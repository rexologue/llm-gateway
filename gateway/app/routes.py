"""HTTP routes exposed by the OpenAI-compatible gateway application.

The proxy routes are thin on purpose: they read the request, shape it, and hand
one ``ProxyExchange`` the job of talking to the backend and recording what
happened. Telemetry and persistence are not written here - a route that has to
remember to log, measure and persist is a route that will eventually forget in
one branch and not the other.
"""

from __future__ import annotations

from typing import cast

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.chat_payload import (
    apply_chat_payload_overrides,
    apply_generic_payload_overrides,
    model_label,
)
from app.exchange import ProxyExchange
from app.http_utils import (
    gateway_response_headers,
    parse_json_maybe,
    request_id_from_headers,
    session_id_from_headers,
)
from app.route_paths import (
    CHAT_COMPLETIONS_ROUTE,
    GATEWAY_METRICS_ROUTE,
    GENERIC_V1_PROXY_ROUTE,
    HEALTH_ROUTE,
    METRICS_ROUTE,
    ROOT_ROUTE,
    V1_ROUTE_PREFIX,
)
from app.state import AppState

INVALID_PAYLOAD_BODY = b'{"error":"request body must be a JSON object"}'


def create_router() -> APIRouter:
    """Create the application router."""

    router = APIRouter()


    @router.get(HEALTH_ROUTE)
    async def health(request: Request) -> Response:
        """Return the backend health endpoint response."""

        return await _proxy_probe(_get_state(request.app), HEALTH_ROUTE)


    @router.get(METRICS_ROUTE)
    async def metrics(request: Request) -> Response:
        """Return the backend metrics endpoint response."""

        return await _proxy_probe(_get_state(request.app), METRICS_ROUTE)


    @router.get(GATEWAY_METRICS_ROUTE)
    async def gateway_metrics(request: Request) -> Response:
        """Expose Prometheus metrics collected by the gateway process."""

        state = _get_state(request.app)
        active_session_count = await state.session_tracker.active_session_count()

        if active_session_count is not None:
            state.metrics.set_active_sessions(active_session_count)

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


    @router.api_route(CHAT_COMPLETIONS_ROUTE, methods=["POST"])
    async def chat_completions(request: Request) -> Response:
        """Proxy one chat completion, tracking and persisting its dialog."""

        state = _get_state(request.app)
        settings = state.settings
        raw_body = await request.body()
        headers_in = dict(request.headers.items())
        request_id = request_id_from_headers(headers_in)
        session_id = session_id_from_headers(headers_in)
        payload = parse_json_maybe(raw_body.decode("utf-8", errors="replace"))

        session_first_request = await state.session_tracker.mark_seen(session_id)
        shaped = None

        if isinstance(payload, dict):
            shaped = apply_chat_payload_overrides(
                payload,
                forced_max_completion_tokens=settings.forced_max_completion_tokens,
                forced_thinking_disabled=settings.forced_thinking_disabled,
                enable_sampling_fallback_override=settings.enable_sampling_fallback_override,
                force_stream_usage=settings.force_stream_usage,
            )
            payload = shaped.payload
            raw_body = shaped.raw_body

        exchange = ProxyExchange(
            state=state,
            route=CHAT_COMPLETIONS_ROUTE,
            method="POST",
            request_id=request_id,
            session_id=session_id,
            session_first_request=session_first_request,
            headers_in=headers_in,
            raw_body=raw_body,
            payload=payload if isinstance(payload, dict) else None,
            model=model_label(payload if isinstance(payload, dict) else None),
            stream=bool(payload.get("stream")) if isinstance(payload, dict) else False,
            fallback_params=shaped.fallback_params if shaped else None,
            usage_forced=shaped.usage_forced if shaped else False,
            record_transcript=True,
            track_inflight=True,
        )
        await exchange.open()

        # Branch: the body is not a JSON object, so the gateway cannot apply
        # OpenAI chat request handling and answers 400 itself without touching
        # the backend.
        if not isinstance(payload, dict):
            return await exchange.fail_locally(
                Response(
                    content=INVALID_PAYLOAD_BODY,
                    status_code=400,
                    headers=gateway_response_headers(
                        {},
                        request_id=request_id,
                        session_id=session_id,
                    ),
                    media_type="application/json",
                ),
                response_text=INVALID_PAYLOAD_BODY.decode("utf-8"),
            )

        return await exchange.run_chat(
            backend_headers=state.backend.forwarded_headers(
                headers_in,
                request_id=request_id,
                session_id=session_id,
            ),
            backend_url=state.backend.url_for(CHAT_COMPLETIONS_ROUTE),
        )


    @router.api_route(
        GENERIC_V1_PROXY_ROUTE,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def generic_v1_proxy(full_path: str, request: Request) -> Response:
        """Proxy every other ``/v1/*`` route with the same terminal records."""

        state = _get_state(request.app)
        settings = state.settings
        route = f"{V1_ROUTE_PREFIX}/{full_path}"
        method = request.method.upper()
        raw_body = await request.body()
        headers_in = dict(request.headers.items())
        request_id = request_id_from_headers(headers_in)
        session_id = session_id_from_headers(headers_in)
        payload = parse_json_maybe(raw_body.decode("utf-8", errors="replace"))

        if isinstance(payload, dict) and settings.forced_thinking_disabled:
            payload, raw_body, _decoded_body = apply_generic_payload_overrides(
                payload,
                forced_thinking_disabled=settings.forced_thinking_disabled,
            )

        exchange = ProxyExchange(
            state=state,
            route=route,
            method=method,
            request_id=request_id,
            session_id=session_id,
            session_first_request=False,
            headers_in=headers_in,
            raw_body=raw_body,
            payload=payload,
            model=model_label(payload if isinstance(payload, dict) else None),
        )
        await exchange.open()

        return await exchange.run_passthrough(
            backend_headers=state.backend.forwarded_headers(
                headers_in,
                request_id=request_id,
                session_id=session_id,
            ),
            backend_url=state.backend.url_for(route),
            params=request.query_params,
        )


    @router.get(ROOT_ROUTE)
    async def root() -> PlainTextResponse:
        """Return a tiny human-readable status page for manual checks."""

        return PlainTextResponse("OpenAI-compatible gateway is up")

    return router


def _get_state(app: FastAPI) -> AppState:
    """Return the initialized gateway state from the FastAPI application."""

    return cast(AppState, app.state.gateway_state)


async def _proxy_probe(state: AppState, route: str) -> Response:
    """Relay one unauthenticated backend probe endpoint verbatim.

    Health and metrics probes are polled constantly by Prometheus and by the
    container healthcheck, so they stay outside the exchange lifecycle: logging
    and measuring them would drown the records that matter.
    """

    try:
        backend_response = await state.backend.request(
            method="GET",
            route=route,
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
