#!/usr/bin/env python3
"""Dump the stored record of one gateway session id.

The gateway owns its session state and exposes it over HTTP, so this script
reads ``GET /gateway/session/{session_id}`` instead of touching Valkey. The
record has three blocks: ``messages`` and ``tools`` hold every unique message
and tools declaration under its hash, and ``turns`` lists every exchange as
the hashes the client sent (``input``) and the hash the model answered with
(``output``), plus its audit.

    ./misc/dump_session.py <session-id>                  # JSON to stdout
    ./misc/dump_session.py <session-id> -o dialog.json   # JSON to a file
    ./misc/dump_session.py <session-id> --transcript     # plain-text dialog

The transcript walks the turns in start order and prints each message once, at
the turn that first sent or produced it, so a client resending its whole
history every time still reads as one dialog. JSON output is indented by two
spaces unless ``--indent`` says otherwise.
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
SCHEMA_VERSION = 3
SYSTEM_PREVIEW_CHARS = 160


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


def render_transcript(record: dict[str, Any], *, full_system: bool = False) -> str:
    """Render the stored session as a readable dialog, turn by turn."""

    if record.get("schema_version") != SCHEMA_VERSION:
        return (
            f"schema_version={record.get('schema_version')!r}: this record was written "
            "by an older gateway; dump it as JSON instead of --transcript."
        )

    metadata = _as_dict(record.get("metadata"))
    messages = _as_dict(record.get("messages"))
    tools = _as_dict(record.get("tools"))
    turns = _as_list(record.get("turns"))
    totals = _as_dict(metadata.get("totals"))

    lines = [
        f"session_id:      {metadata.get('session_id')}",
        f"created_at:      {metadata.get('created_at')}",
        f"updated_at:      {metadata.get('updated_at')}",
        f"age_sec:         {metadata.get('age_sec')}",
        f"idle_sec:        {metadata.get('idle_sec')}",
        f"expires_in_sec:  {metadata.get('expires_in_sec')}",
        f"messages:        {len(messages)} unique",
        f"tools:           {len(tools)} declaration(s)",
        f"turns:           {len(turns)}",
        "totals:          "
        f"requests={totals.get('requests')} "
        f"failed={totals.get('failed_requests') or 0} "
        f"prompt_tokens={totals.get('prompt_tokens') or 0} "
        f"completion_tokens={totals.get('completion_tokens') or 0}",
    ]

    for digest, declaration in tools.items():
        names = ", ".join(_tool_name(tool) for tool in _as_list(declaration))
        lines.append(f"  tools {digest[:8]}: {names}")

    shown: set[str] = set()

    for position, turn in enumerate(turns, start=1):
        entry = _as_dict(turn)
        lines.append("")
        lines.append(_turn_header(position, entry))

        for digest in _as_list(entry.get("input")):
            if digest in shown:
                continue

            shown.add(digest)
            lines.extend(_render_message(digest, messages.get(digest), full_system=full_system))

        output = entry.get("output")
        if output is not None:
            shown.add(output)
            lines.extend(
                _render_message(output, messages.get(output), full_system=full_system, answer=True)
            )

        for extra in _as_list(entry.get("extra_outputs")):
            extra_entry = _as_dict(extra)
            digest = extra_entry.get("output")
            lines.append(f"  (extra choice {extra_entry.get('index')}, not continued)")
            lines.extend(_render_message(digest, messages.get(digest), full_system=full_system))

    return "\n".join(lines)


def _turn_header(position: int, turn: dict[str, Any]) -> str:
    """Return the one-line audit header of a turn."""

    usage = _as_dict(turn.get("usage"))
    parts = [
        f"=== turn {position}",
        str(turn.get("started_at") or "")[11:23],
        "stream" if turn.get("stream") else "unary",
        f"status={turn.get('status_code')}",
        f"finish={turn.get('finish_reason')}",
    ]

    if usage:
        parts.append(f"usage={usage.get('prompt_tokens')}/{usage.get('completion_tokens')}")

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

    parts.append(f"request_id={turn.get('request_id')}")

    return "  ".join(parts) + " ==="


def _render_message(
    digest: Any,
    message: Any,
    *,
    full_system: bool,
    answer: bool = False,
) -> list[str]:
    """Render one stored message under its role."""

    marker = "->" if answer else "  "
    tag = str(digest)[:8]

    if not isinstance(message, dict):
        return [f"{marker} [{tag}] {message!r}"]

    role = message.get("role", "?")
    header = f"{marker} [{tag}] {role}"

    if message.get("tool_call_id"):
        header += f" -> {message['tool_call_id']}"

    if message.get("name"):
        header += f" ({message['name']})"

    content = message.get("content")
    text = content if isinstance(content, str) else (
        json.dumps(content, ensure_ascii=False) if content is not None else ""
    )

    if role == "system" and not full_system and len(text) > SYSTEM_PREVIEW_CHARS:
        text = f"<{len(text)} chars> {_shorten(text, SYSTEM_PREVIEW_CHARS)}"

    lines = [f"{header}: {text}" if text else f"{header}:"]

    reasoning = message.get("reasoning_content")
    if reasoning:
        lines.append(f"     [reasoning] {reasoning}")

    if message.get("refusal"):
        lines.append(f"     [refusal] {message['refusal']}")

    for call in _as_list(message.get("tool_calls")):
        entry = _as_dict(call)
        function = _as_dict(entry.get("function"))
        lines.append(
            f"     [tool_call {entry.get('id')}] "
            f"{function.get('name')}({function.get('arguments')})"
        )

    return lines


def _tool_name(tool: Any) -> str:
    """Return the name of one declared tool."""

    entry = _as_dict(tool)
    function = _as_dict(entry.get("function"))

    return str(function.get("name") or entry.get("name") or "?")


def _shorten(text: str, limit: int = 120) -> str:
    """Return a single-line excerpt of at most ``limit`` characters."""

    collapsed = " ".join(text.split())

    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


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
        description="Dump the stored record of one gateway session id.",
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
    parser.add_argument(
        "--full-system",
        action="store_true",
        help="In --transcript, print system prompts whole instead of a preview.",
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
        payload = render_transcript(record, full_system=args.full_system)
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
