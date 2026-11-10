"""Read API for the stored session transcripts.

These endpoints only read what the proxy recorded, which is a different job
from proxying, so they live apart from the routes that talk to the backend. The
indented-JSON projection lives here too, because this is the only place that
needs it: the Grafana session viewer reads ``tools_pretty``/``messages_pretty``
out of ``?pretty=1`` and cannot render nesting from a structural JSON cell.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app.route_paths import (
    GATEWAY_SESSION_DETAIL_ROUTE,
    GATEWAY_SESSION_LIST_ROUTE,
)
from app.state import AppState

TRUTHY_QUERY_VALUES = {"1", "true", "yes", "on"}
PRETTY_FIELDS = ("tools", "messages", "turns")


def create_session_router() -> APIRouter:
    """Create the router exposing stored session transcripts."""

    router = APIRouter()

    @router.get(GATEWAY_SESSION_LIST_ROUTE)
    async def session_list(request: Request) -> JSONResponse:
        """Return metadata summaries of the sessions active in a period.

        ``from`` and ``to`` are optional epoch milliseconds, the format of
        Grafana's ``${__from}``/``${__to}``. Without them every stored session
        is listed.
        """

        state = cast(AppState, request.app.state.gateway_state)

        try:
            since = _query_epoch_ms(request, "from")
            until = _query_epoch_ms(request, "to")

        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        try:
            sessions = await state.session_store.list_sessions(since=since, until=until)

        except RedisError as exc:
            return JSONResponse(
                {"error": "session store unavailable", "detail": type(exc).__name__},
                status_code=503,
            )

        return JSONResponse(sessions)


    @router.get(GATEWAY_SESSION_DETAIL_ROUTE)
    async def session_get(session_id: str, request: Request) -> JSONResponse:
        """Return one persisted chat session by external session id."""

        state = cast(AppState, request.app.state.gateway_state)

        try:
            session = await state.session_store.get_session(session_id)

        except RedisError as exc:
            return JSONResponse(
                {"error": "session store unavailable", "detail": type(exc).__name__},
                status_code=503,
            )

        if session is None:
            return JSONResponse({"error": "session not found"}, status_code=404)

        if _query_flag(request, "pretty"):
            session = _with_pretty_fields(session)

        return JSONResponse(session)

    return router


def _query_flag(request: Request, name: str) -> bool:
    """Return whether a query flag was set to a truthy value."""

    return request.query_params.get(name, "").lower() in TRUTHY_QUERY_VALUES


def _query_epoch_ms(request: Request, name: str) -> datetime | None:
    """Return a query parameter given in epoch milliseconds as a UTC datetime.

    An absent or empty parameter is None; anything that is not an integer
    number of milliseconds raises ValueError with a message for the client.
    """

    raw = request.query_params.get(name, "").strip()

    if not raw:
        return None

    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)

    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"{name!r} must be epoch milliseconds, got {raw!r}") from exc


def _with_pretty_fields(session: dict[str, Any]) -> dict[str, Any]:
    """Add indented-JSON copies of the record's list fields for table display."""

    return {
        **session,
        **{
            f"{field}_pretty": _pretty_json(session.get(field, []))
            for field in PRETTY_FIELDS
        },
    }


def _pretty_json(value: Any) -> str:
    """Return a value as indented JSON for display in a wrapped table cell.

    Grafana table cells keep newlines under ``wrapText`` but collapse the
    leading spaces, which would flatten the nesting. Rendering each level's
    indentation with non-breaking spaces keeps the structure visible.
    """

    text = orjson.dumps(value, option=orjson.OPT_INDENT_2).decode("utf-8")

    return "\n".join(_nbsp_indent(line) for line in text.split("\n"))


def _nbsp_indent(line: str) -> str:
    """Replace a line's leading spaces with non-breaking spaces."""

    stripped = line.lstrip(" ")
    return " " * (len(line) - len(stripped)) + stripped
