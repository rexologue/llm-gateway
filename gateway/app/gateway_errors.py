"""Gateway error event logging helpers."""

from __future__ import annotations

from app.log_payloads import build_error_event
from app.state import AppState


async def log_gateway_error(
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
    """Write a gateway error event with session-init timing when applicable."""

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

    await state.log_event(**event)
