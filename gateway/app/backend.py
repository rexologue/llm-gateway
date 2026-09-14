"""Boundary with an OpenAI-compatible backend API.

Two classes share this boundary because they are two halves of the same
conversation: ``OpenAICompatibleBackend`` sends requests, and
``ChatResponseReader`` reads the answers back and rebuilds the assistant turn
the backend produced. Keeping the reader here means the response is parsed
exactly once - the bytes forwarded to the caller and the turn persisted in the
session store come out of the same pass.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.http_utils import parse_json_maybe, strip_hop_by_hop_headers

SSE_DATA_PREFIX = "data:"
SSE_DONE_PAYLOAD = "[DONE]"
EVENT_SEPARATORS = (b"\r\n\r\n", b"\n\n")


class OpenAICompatibleBackend:
    """HTTP client wrapper for backend routes under the OpenAI-compatible API."""

    def __init__(self, *, base_url: str, http: httpx.AsyncClient) -> None:
        """Initialize the backend client boundary."""

        self.base_url = base_url.rstrip("/")
        self.http = http


    def url_for(self, route: str) -> str:
        """Return the absolute backend URL for a gateway route."""

        path = route if route.startswith("/") else f"/{route}"
        return f"{self.base_url}{path}"


    def forwarded_headers(
        self,
        headers: Mapping[str, str],
        *,
        request_id: str,
        session_id: str | None,
    ) -> dict[str, str]:
        """Return caller headers that are safe and useful to forward."""

        forwarded = strip_hop_by_hop_headers(headers)
        forwarded["x-request-id"] = request_id

        if session_id is not None:
            forwarded["x-session-id"] = session_id

        return forwarded


    def build_request(
        self,
        *,
        method: str,
        route: str,
        headers: Mapping[str, str],
        content: bytes,
    ) -> httpx.Request:
        """Build a backend request without sending it."""

        return self.http.build_request(
            method=method,
            url=self.url_for(route),
            headers=headers,
            content=content,
        )


    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        """Send a prebuilt backend request."""

        return await self.http.send(request, stream=stream)


    async def post(
        self,
        *,
        route: str,
        headers: Mapping[str, str],
        content: bytes,
    ) -> httpx.Response:
        """Send a POST request to a backend route."""

        return await self.http.post(
            self.url_for(route),
            headers=headers,
            content=content,
        )


    async def request(
        self,
        *,
        method: str,
        route: str,
        headers: Mapping[str, str],
        content: bytes,
        params: Any = None,
    ) -> httpx.Response:
        """Send an arbitrary HTTP request to a backend route."""

        return await self.http.request(
            method=method,
            url=self.url_for(route),
            headers=headers,
            content=content,
            params=params,
        )


@dataclass(slots=True)
class ChatChoice:
    """One choice of a chat response, rebuilt into a complete message."""

    index: int
    message: dict[str, Any]
    finish_reason: str | None = None


@dataclass(slots=True)
class ChatTurn:
    """The assistant turn one backend chat response produced.

    Both response shapes reduce to this: a non-streaming body and an SSE stream
    differ only in how the choices are assembled, never in what a turn is.
    ``truncated`` marks a stream the gateway never saw end, so a partial turn is
    never mistaken for a complete one.
    """

    choices: list[ChatChoice] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    model: str | None = None
    response_id: str | None = None
    created: int | None = None
    truncated: bool = False


    @property
    def assistant_message(self) -> dict[str, Any] | None:
        """Return the message that continues the dialog, or None when empty.

        The transcript is a linear dialog, so the lowest-indexed choice is the
        turn that continues it. With ``n > 1`` the remaining choices are kept in
        ``choices`` for the turn audit instead of being merged into one message,
        which is what previously produced concatenated content.
        """

        if not self.choices:
            return None

        return self.choices[0].message


    @property
    def finish_reason(self) -> str | None:
        """Return the finish reason of the dialog-continuing choice."""

        return self.choices[0].finish_reason if self.choices else None


class ChatResponseReader:
    """Rebuild the assistant turn of one backend chat response.

    Streaming is handled as a state machine over SSE *events*, not over raw
    transport chunks: one event can arrive split across chunks, and the
    usage-only event has to be droppable independently of the content event
    sitting next to it in the same chunk. ``feed``/``flush`` return the bytes to
    forward downstream, so a caller relays and accumulates in a single pass.

    The reader is deliberately synchronous. Awaiting belongs to whoever owns the
    request lifetime; keeping the parsing pure makes it testable without a
    backend, an event loop, or a client.
    """

    def __init__(self, *, drop_usage_events: bool = False) -> None:
        """Initialize a reader for one streaming chat response.

        ``drop_usage_events`` hides the usage-only event from the downstream
        consumer. The gateway asks the backend for usage on every stream so the
        transcript can record it, and must then withhold that event from a
        caller who never requested it.
        """

        self.drop_usage_events = drop_usage_events
        self.usage: dict[str, Any] | None = None
        self.model: str | None = None
        self.response_id: str | None = None
        self.created: int | None = None
        self.saw_done = False

        self._buffer = bytearray()
        self._choices: dict[int, dict[str, Any]] = {}


    def feed(self, chunk: bytes) -> bytes:
        """Consume one transport chunk and return the bytes to forward."""

        if not chunk:
            return b""

        self._buffer += chunk
        forward = bytearray()

        while True:
            event = self._take_event()
            if event is None:
                break

            if self._consume_event(event):
                forward += event

        return bytes(forward)


    def flush(self) -> bytes:
        """Consume whatever is left in the buffer as one final event."""

        if not self._buffer:
            return b""

        event = bytes(self._buffer)
        self._buffer.clear()

        return event if self._consume_event(event) else b""


    def turn(self) -> ChatTurn:
        """Return the assistant turn accumulated so far."""

        choices = [
            ChatChoice(
                index=index,
                message=self._message_from_accumulator(self._choices[index]),
                finish_reason=self._choices[index]["finish_reason"],
            )
            for index in sorted(self._choices)
        ]

        return ChatTurn(
            choices=choices,
            usage=self.usage,
            model=self.model,
            response_id=self.response_id,
            created=self.created,
            truncated=bool(choices) and not self.saw_done,
        )


    def _take_event(self) -> bytes | None:
        """Remove and return the next complete SSE event from the buffer."""

        end = -1
        for separator in EVENT_SEPARATORS:
            found = self._buffer.find(separator)
            if found != -1 and (end == -1 or found < end):
                end = found + len(separator)

        if end == -1:
            return None

        event = bytes(self._buffer[:end])
        del self._buffer[:end]

        return event


    def _consume_event(self, event: bytes) -> bool:
        """Accumulate one SSE event and return whether to forward it."""

        data_lines = [
            line[len(SSE_DATA_PREFIX):].lstrip()
            for line in event.decode("utf-8", errors="replace").splitlines()
            if line.startswith(SSE_DATA_PREFIX)
        ]

        # Comments and keepalives carry no data lines. They mean nothing to the
        # transcript but are part of the protocol, so they pass through.
        if not data_lines:
            return True

        payload_text = "\n".join(data_lines).strip()

        if payload_text == SSE_DONE_PAYLOAD:
            self.saw_done = True
            return True

        payload = parse_json_maybe(payload_text)

        # An event the gateway cannot parse is still the backend's answer to the
        # caller, so it is relayed untouched rather than swallowed.
        if not isinstance(payload, dict):
            return True

        self._absorb_envelope(payload)
        choices = payload.get("choices")
        has_choices = isinstance(choices, list) and bool(choices)

        if has_choices:
            for position, choice in enumerate(choices):
                self._absorb_choice(choice, position)

        # Branch: the usage-only event that closes a stream when usage was
        # requested. It has no choices, so dropping it costs the caller no
        # dialog content - only the token counts it never asked for.
        return has_choices or not self.drop_usage_events


    def _absorb_envelope(self, payload: dict[str, Any]) -> None:
        """Record response-level fields carried by a chunk."""

        usage = payload.get("usage")
        if isinstance(usage, dict):
            self.usage = usage

        response_id = payload.get("id")
        if self.response_id is None and isinstance(response_id, str) and response_id:
            self.response_id = response_id

        model = payload.get("model")
        if self.model is None and isinstance(model, str) and model:
            self.model = model

        created = payload.get("created")
        if self.created is None and isinstance(created, int):
            self.created = created


    def _absorb_choice(self, choice: Any, position: int) -> None:
        """Accumulate one streamed choice delta into its own accumulator."""

        if not isinstance(choice, dict):
            return

        raw_index = choice.get("index")
        index = raw_index if isinstance(raw_index, int) else position
        accumulator = self._choices.setdefault(index, self._new_accumulator())

        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            accumulator["finish_reason"] = finish_reason

        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return

        role = delta.get("role")
        if isinstance(role, str) and role:
            accumulator["role"] = role

        for source, target in (
            ("content", "content"),
            ("reasoning_content", "reasoning_content"),
            ("reasoning", "reasoning_content"),
            ("refusal", "refusal"),
        ):
            fragment = delta.get(source)
            if isinstance(fragment, str) and fragment:
                accumulator[target].append(fragment)

        self._accumulate_tool_calls(accumulator["tool_calls"], delta.get("tool_calls"))


    @staticmethod
    def turn_from_body(response_text: str | bytes) -> ChatTurn:
        """Return the turn carried by a non-streaming chat completion body."""

        payload = parse_json_maybe(
            response_text.decode("utf-8", errors="replace")
            if isinstance(response_text, bytes)
            else response_text
        )

        if not isinstance(payload, dict):
            return ChatTurn()

        raw_choices = payload.get("choices")
        choices: list[ChatChoice] = []

        if isinstance(raw_choices, list):
            for position, choice in enumerate(raw_choices):
                if not isinstance(choice, dict):
                    continue

                message = choice.get("message")
                if not isinstance(message, dict):
                    continue

                raw_index = choice.get("index")
                finish_reason = choice.get("finish_reason")
                choices.append(
                    ChatChoice(
                        index=raw_index if isinstance(raw_index, int) else position,
                        message=message,
                        finish_reason=finish_reason
                        if isinstance(finish_reason, str)
                        else None,
                    )
                )

        choices.sort(key=lambda item: item.index)
        usage = payload.get("usage")
        created = payload.get("created")
        model = payload.get("model")
        response_id = payload.get("id")

        return ChatTurn(
            choices=choices,
            usage=usage if isinstance(usage, dict) else None,
            model=model if isinstance(model, str) and model else None,
            response_id=response_id if isinstance(response_id, str) and response_id else None,
            created=created if isinstance(created, int) else None,
        )


    @staticmethod
    def _new_accumulator() -> dict[str, Any]:
        """Return an empty per-choice accumulator."""

        return {
            "role": "assistant",
            "content": [],
            "reasoning_content": [],
            "refusal": [],
            "tool_calls": {},
            "finish_reason": None,
        }


    @staticmethod
    def _message_from_accumulator(accumulator: dict[str, Any]) -> dict[str, Any]:
        """Build one OpenAI-style message from an accumulated choice."""

        message: dict[str, Any] = {"role": accumulator["role"]}
        content = "".join(accumulator["content"])
        message["content"] = content or None

        for key in ("reasoning_content", "refusal"):
            joined = "".join(accumulator[key])
            if joined:
                message[key] = joined

        tool_calls = accumulator["tool_calls"]
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]

        return message


    @staticmethod
    def _accumulate_tool_calls(
        tool_calls: dict[int, dict[str, Any]],
        delta_tool_calls: Any,
    ) -> None:
        """Merge streamed tool call fragments into per-index accumulators."""

        if not isinstance(delta_tool_calls, list):
            return

        for position, fragment in enumerate(delta_tool_calls):
            if not isinstance(fragment, dict):
                continue

            raw_index = fragment.get("index")
            index = raw_index if isinstance(raw_index, int) else position
            entry = tool_calls.setdefault(
                index,
                {"type": "function", "function": {"name": "", "arguments": ""}},
            )

            call_id = fragment.get("id")
            if isinstance(call_id, str) and call_id:
                entry["id"] = call_id

            call_type = fragment.get("type")
            if isinstance(call_type, str) and call_type:
                entry["type"] = call_type

            function_fragment = fragment.get("function")
            if not isinstance(function_fragment, dict):
                continue

            function = entry["function"]
            name = function_fragment.get("name")

            # A name can arrive whole in the first fragment, or split across
            # fragments like arguments are. Appending unconditionally would turn
            # a backend that repeats the full name into "get_weatherget_weather",
            # so an identical repeat is treated as the same name.
            if isinstance(name, str) and name and name != function["name"]:
                function["name"] += name

            arguments = function_fragment.get("arguments")
            if isinstance(arguments, str) and arguments:
                function["arguments"] += arguments
