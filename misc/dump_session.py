#!/usr/bin/env python3
"""Dump the stored dialog of one gateway session id.

The gateway owns its session state and exposes it over HTTP, so this script
reads ``GET /gateway/session/{session_id}`` instead of touching Valkey. The
endpoint returns the whole record - ``metadata`` (with the read-time
``age_sec``, ``idle_sec`` and ``expires_in_sec`` durations), the declared
``tools``, and the full ``messages`` transcript including the assistant turn.

    ./misc/dump_session.py <session-id>                  # JSON to stdout
    ./misc/dump_session.py <session-id> -o dialog.json   # JSON to a file
    ./misc/dump_session.py <session-id> --transcript     # plain-text dialog

JSON output is indented by two spaces unless ``--indent`` says otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:9090"
SESSION_ROUTE = "/gateway/session"


def fetch_session(
    *,
    base_url: str,
    session_id: str,
    api_key: str,
    timeout_sec: float,
) -> dict[str, Any]:
    """Return one stored session record from the gateway session endpoint."""

    url = f"{base_url.rstrip('/')}{SESSION_ROUTE}/{quote(session_id, safe='')}"
    headers = {"Accept": "application/json"}

    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = httpx.get(url, headers=headers, timeout=timeout_sec, trust_env=False)

    if response.status_code == 404:
        raise SessionNotFound(session_id, url)

    response.raise_for_status()
    record = response.json()

    if not isinstance(record, dict):
        raise ValueError(f"unexpected response shape from {url}: {type(record).__name__}")

    return record


def render_transcript(record: dict[str, Any]) -> str:
    """Render the stored session as a readable dialog with its turn audit.

    Tool calls and their results are shown where they happened, and each turn's
    accounting - model, finish reason, usage, how it was delivered - is listed
    after the dialog rather than inlined, so the conversation stays readable.
    """

    metadata = _as_dict(record.get("metadata"))
    messages = _as_list(record.get("messages"))
    tools = _as_list(record.get("tools"))
    turns = _as_list(record.get("turns"))
    revisions = _as_list(record.get("revisions"))
    totals = _as_dict(metadata.get("totals"))

    lines = [
        f"session_id:      {metadata.get('session_id')}",
        f"created_at:      {metadata.get('created_at')}",
        f"updated_at:      {metadata.get('updated_at')}",
        f"age_sec:         {metadata.get('age_sec')}",
        f"idle_sec:        {metadata.get('idle_sec')}",
        f"expires_in_sec:  {metadata.get('expires_in_sec')}",
        f"messages:        {len(messages)}",
        f"turns:           {len(turns)}",
        f"tools:           {len(tools)}",
    ]

    if totals:
        lines.append(
            "totals:          "
            f"requests={totals.get('requests')} "
            f"failed={totals.get('failed_requests') or 0} "
            f"prompt_tokens={totals.get('prompt_tokens') or 0} "
            f"completion_tokens={totals.get('completion_tokens') or 0}"
        )

    if revisions:
        lines.append(f"revisions:       {len(revisions)} (superseded history is archived)")

    if tools:
        lines.append("")
        lines.append("--- declared tools ---")
        for tool in tools:
            lines.append(f"  {_tool_signature(tool)}")

    lines.append("")
    lines.append("--- dialog ---")
    assistant_turns = {
        turn.get("assistant_index"): position
        for position, turn in enumerate(turns, start=1)
        if isinstance(turn, dict) and turn.get("assistant_index") is not None
    }

    for index, message in enumerate(messages):
        lines.extend(_render_message(index, message, assistant_turns.get(index)))

    if turns:
        lines.append("")
        lines.append("--- turns ---")
        for position, turn in enumerate(turns, start=1):
            lines.append(_render_turn(position, _as_dict(turn)))

    if revisions:
        lines.append("")
        lines.append("--- archived revisions ---")
        for revision in revisions:
            entry = _as_dict(revision)
            dropped = _as_list(entry.get("dropped"))
            lines.append(
                f"  {entry.get('at')} {entry.get('reason')}: "
                f"{len(dropped)} message(s) superseded"
            )
            for message in dropped:
                lines.append(f"    {_as_dict(message).get('role')}: {_one_line(message)}")

    return "\n".join(lines)


def _render_message(index: int, message: Any, turn_no: int | None) -> list[str]:
    """Render one dialog message, keeping tool calls in their own position."""

    if not isinstance(message, dict):
        return [f"\n[{index}] {message!r}"]

    role = message.get("role", "?")
    marker = f"  (turn {turn_no})" if turn_no is not None else ""
    tool_call_id = message.get("tool_call_id")
    header = f"\n[{index}] {role}"

    if tool_call_id:
        header += f" -> {tool_call_id}"

    if message.get("name"):
        header += f" ({message['name']})"

    lines = [header + ":" + marker]
    content = message.get("content")

    if content is not None:
        lines.append(
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False, indent=2)
        )

    reasoning = message.get("reasoning_content")
    if reasoning:
        lines.append(f"  [reasoning] {reasoning}")

    if message.get("refusal"):
        lines.append(f"  [refusal] {message['refusal']}")

    for call in _as_list(message.get("tool_calls")):
        entry = _as_dict(call)
        function = _as_dict(entry.get("function"))
        lines.append(
            f"  [tool_call {entry.get('id')}] "
            f"{function.get('name')}({function.get('arguments')})"
        )

    return lines


def _render_turn(position: int, turn: dict[str, Any]) -> str:
    """Render one line of turn accounting."""

    usage = _as_dict(turn.get("usage"))
    parts = [
        f"{position:>3}",
        str(turn.get("request_id")),
        "stream" if turn.get("stream") else "unary",
        f"status={turn.get('status_code')}",
        f"finish={turn.get('finish_reason')}",
    ]

    if usage:
        parts.append(
            f"usage={usage.get('prompt_tokens')}/{usage.get('completion_tokens')}"
        )

    for key, label in (("ttft_sec", "ttft"), ("e2e_sec", "e2e")):
        value = turn.get(key)
        if isinstance(value, (int, float)):
            parts.append(f"{label}={value:.3f}")

    parts.append(str(turn.get("delivery")))

    if turn.get("truncated"):
        parts.append("TRUNCATED")

    error = _as_dict(turn.get("error"))
    if error:
        parts.append(f"error={error.get('type')}: {_shorten(str(error.get('message')))}")

    if turn.get("extra_choices"):
        parts.append(f"extra_choices={len(_as_list(turn.get('extra_choices')))}")

    return "  ".join(parts)


def _tool_signature(tool: Any) -> str:
    """Return a compact signature for one declared tool."""

    entry = _as_dict(tool)
    function = _as_dict(entry.get("function"))
    name = function.get("name") or entry.get("name") or "?"
    description = function.get("description")

    return f"{name}" + (f" - {_shorten(str(description))}" if description else "")


def _one_line(value: Any) -> str:
    """Return a one-line rendering of a message body."""

    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, str):
            return _shorten(content)

        return _shorten(json.dumps(content, ensure_ascii=False))

    return _shorten(json.dumps(value, ensure_ascii=False))


def _shorten(text: str, limit: int = 120) -> str:
    """Return a single-line excerpt of at most ``limit`` characters."""

    collapsed = " ".join(text.split())

    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "\u2026"


def _as_dict(value: Any) -> dict[str, Any]:
    """Return the value when it is a dict, an empty dict otherwise."""

    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Return the value when it is a list, an empty list otherwise."""

    return value if isinstance(value, list) else []


def dump_json(record: dict[str, Any], indent: int) -> str:
    """Serialize a record as JSON: indented by default, single-line when indent is 0."""

    if indent > 0:
        return json.dumps(record, ensure_ascii=False, indent=indent)

    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


class SessionNotFound(Exception):
    """Raised when the gateway has no stored record for a session id."""

    def __init__(self, session_id: str, url: str) -> None:
        """Store the session id and the endpoint that reported the miss."""

        super().__init__(f"session {session_id!r} not found at {url}")
        self.session_id = session_id
        self.url = url


def main() -> None:
    """Parse CLI arguments, fetch the session, and write it to stdout or a file."""

    parser = argparse.ArgumentParser(
        description="Dump the stored dialog of one gateway session id.",
    )
    parser.add_argument("session_id", help="External session id (X-Session-ID value).")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Gateway base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Bearer token sent to the gateway, when it requires one.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "-o",
        "--out",
        default="",
        help="Write the dump to this file instead of stdout.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent width. 0 emits compact single-line JSON. Default: 2",
    )
    parser.add_argument(
        "--transcript",
        action="store_true",
        help="Render the dialog as plain text instead of JSON.",
    )
    args = parser.parse_args()

    try:
        record = fetch_session(
            base_url=args.base_url,
            session_id=args.session_id,
            api_key=args.api_key,
            timeout_sec=args.timeout,
        )

    except SessionNotFound as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc

    except httpx.HTTPStatusError as exc:
        print(
            f"Gateway returned {exc.response.status_code} for {exc.request.url}: "
            f"{exc.response.text.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    except httpx.HTTPError as exc:
        print(f"Gateway request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if args.transcript:
        payload = render_transcript(record)
    else:
        payload = dump_json(record, args.indent)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")

        print(f"Wrote session {args.session_id} to {args.out}", file=sys.stderr)

    else:
        print(payload)


if __name__ == "__main__":
    main()
