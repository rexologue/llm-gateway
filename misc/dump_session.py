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
    """Render the stored messages as a plain-text dialog for quick reading."""

    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    messages = record.get("messages")
    messages = messages if isinstance(messages, list) else []
    tools = record.get("tools")
    tools = tools if isinstance(tools, list) else []

    lines = [
        f"session_id:      {metadata.get('session_id')}",
        f"created_at:      {metadata.get('created_at')}",
        f"updated_at:      {metadata.get('updated_at')}",
        f"age_sec:         {metadata.get('age_sec')}",
        f"idle_sec:        {metadata.get('idle_sec')}",
        f"expires_in_sec:  {metadata.get('expires_in_sec')}",
        f"messages:        {len(messages)}",
        f"tools:           {len(tools)}",
    ]

    if tools:
        lines.append("")
        lines.append("--- tools ---")
        lines.append(json.dumps(tools, ensure_ascii=False, indent=2))

    lines.append("")
    lines.append("--- messages ---")

    for idx, message in enumerate(messages):
        if not isinstance(message, dict):
            lines.append(f"\n[{idx}] {message!r}")
            continue

        lines.append(f"\n[{idx}] {message.get('role', '?')}:")
        content = message.get("content")

        if content is not None:
            lines.append(
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False, indent=2)
            )

        for extra in ("reasoning_content", "name", "tool_call_id", "tool_calls"):
            if message.get(extra) is not None:
                lines.append(f"  {extra}: {json.dumps(message[extra], ensure_ascii=False)}")

    return "\n".join(lines)


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
