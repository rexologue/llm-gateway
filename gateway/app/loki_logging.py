"""Gateway-domain Loki logging."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

from app.http_utils import SENSITIVE_KEYS, parse_json_maybe, sanitize_headers, utc_now_iso
from app.route_paths import CHAT_COMPLETIONS_ROUTE
from app.tools.loki import LokiEventPublisher
from app.tracing import current_trace_context


@dataclass(slots=True)
class LokiRequestContext:
    """Bound Loki logging context for one gateway request."""

    logger: GatewayLokiLogger
    route: str
    method: str
    request_id: str
    session_id: str | None
    session_first_request: bool
    stream: bool
    headers_in: Mapping[str, str]
    raw_body: bytes
    payload: Any | None


    async def request(self) -> None:
        """Write the request event for this gateway request."""

        await self.logger.log_request(self)


    async def response(
        self,
        *,
        status_code: int,
        response_headers: Mapping[str, str],
        response_bytes: bytes,
        response_text: str,
        e2e_sec: float,
        ttft_sec: float | None = None,
    ) -> None:
        """Write the response event for this gateway request."""

        await self.logger.log_response(
            self,
            status_code=status_code,
            response_headers=response_headers,
            response_bytes=response_bytes,
            response_text=response_text,
            e2e_sec=e2e_sec,
            ttft_sec=ttft_sec,
        )


    async def error(self, error: BaseException, *, e2e_sec: float) -> None:
        """Write the terminal error event for this gateway request."""

        await self.logger.log_error(self, error, e2e_sec=e2e_sec)


class GatewayLokiLogger:
    """Build gateway Loki events and submit them to the event publisher."""

    def __init__(self, publisher: LokiEventPublisher) -> None:
        """Initialize the domain logger with a low-level Loki publisher."""

        self.publisher = publisher


    async def start(self) -> None:
        """Start the underlying Loki publisher."""

        await self.publisher.start()


    async def stop(self) -> None:
        """Stop the underlying Loki publisher."""

        await self.publisher.stop()


    def context(
        self,
        *,
        route: str,
        method: str,
        request_id: str,
        session_id: str | None,
        session_first_request: bool,
        stream: bool,
        headers_in: Mapping[str, str],
        raw_body: bytes,
        payload: Any | None,
    ) -> LokiRequestContext:
        """Bind repeated request metadata once for later request/response/error logs."""

        return LokiRequestContext(
            logger=self,
            route=route,
            method=method,
            request_id=request_id,
            session_id=session_id,
            session_first_request=session_first_request,
            stream=stream,
            headers_in=headers_in,
            raw_body=raw_body,
            payload=payload,
        )


    async def log_request(self, context: LokiRequestContext) -> None:
        """Write a request event for a bound request context."""

        event = self._base_event(
            bucket=self._request_bucket(context.route),
            level="info",
            event_type="request",
            context=context,
        )
        event.update(
            self._compact(
                {
                    "body_bytes": len(context.raw_body),
                    **self._request_header_summary(context.headers_in),
                    "tool_call_count": self._tool_call_count(context.payload),
                    "request_json": self._request_json(context.route, context.payload),
                }
            )
        )

        await self._submit(event)


    async def log_response(
        self,
        context: LokiRequestContext,
        *,
        status_code: int,
        response_headers: Mapping[str, str],
        response_bytes: bytes,
        response_text: str,
        e2e_sec: float,
        ttft_sec: float | None = None,
    ) -> None:
        """Write a response event for a bound request context."""

        level = self._response_level(status_code)
        event = self._base_event(
            bucket=self._response_bucket(context.route),
            level=level,
            event_type="response",
            context=context,
        )
        event.update(
            self._compact(
                {
                    "status_code": status_code,
                    "warn_reason": self._warn_reason(status_code) if level == "warn" else None,
                    "e2e_sec": round(e2e_sec, 6),
                    "ttft_sec": round(ttft_sec, 6) if ttft_sec is not None else None,
                    "body_bytes": len(response_bytes),
                    "response_content_type": self._header_value(
                        response_headers,
                        "content-type",
                    ),
                    "assistant_text": self._assistant_text(
                        context.route,
                        context.stream,
                        response_text,
                    ),
                    **self._response_body_fields(
                        context.route,
                        context.stream,
                        response_text,
                    ),
                }
            )
        )

        await self._submit(event)


    async def log_error(
        self,
        context: LokiRequestContext,
        error: BaseException,
        *,
        e2e_sec: float,
    ) -> None:
        """Write a terminal gateway error event for a bound request context."""

        event = self._base_event(
            bucket="gateway_error",
            level="error",
            event_type="error",
            context=context,
        )
        event.update(
            {
                "e2e_sec": round(e2e_sec, 6),
                "error_type": type(error).__name__,
                "error_message": str(error),
            }
        )

        await self._submit(event)


    async def _submit(self, event: dict[str, Any]) -> None:
        """Attach common envelope fields and enqueue a Loki event."""

        record = {
            "ts": utc_now_iso(),
            "ts_unix_ns": time.time_ns(),
            **current_trace_context(),
            **event,
        }

        await self.publisher.submit(record)


    @staticmethod
    def _base_event(
        *,
        bucket: str,
        level: str,
        event_type: str,
        context: LokiRequestContext,
    ) -> dict[str, Any]:
        """Return fields shared by every gateway Loki event."""

        return {
            "level": level,
            "bucket": bucket,
            "event_type": event_type,
            "route": context.route,
            "method": context.method,
            "request_id": context.request_id,
            "session_id": context.session_id,
            "session_first_request": context.session_first_request,
            "stream": context.stream,
        }


    @staticmethod
    def _request_bucket(route: str) -> str:
        """Return the logical Loki bucket for a request event."""

        return (
            "request_generation"
            if route == CHAT_COMPLETIONS_ROUTE
            else "request_non_generation"
        )


    @staticmethod
    def _response_bucket(route: str) -> str:
        """Return the logical Loki bucket for a response event."""

        return (
            "response_generation"
            if route == CHAT_COMPLETIONS_ROUTE
            else "response_non_generation"
        )


    @staticmethod
    def _response_level(status_code: int) -> str:
        """Return the log level implied by an HTTP response status."""

        if status_code >= 500:
            return "error"

        if status_code >= 400:
            return "warn"

        return "info"


    @staticmethod
    def _warn_reason(status_code: int) -> str:
        """Return a precise warning reason for warning-level response events."""

        return f"http_status_{status_code}"


    @staticmethod
    def _request_header_summary(headers: Mapping[str, str]) -> dict[str, Any]:
        """Return compact request header fields useful in operational logs."""

        sanitized = sanitize_headers(headers)

        return GatewayLokiLogger._compact(
            {
                "request_content_type": GatewayLokiLogger._header_value(
                    sanitized,
                    "content-type",
                ),
                "request_user_agent": GatewayLokiLogger._header_value(
                    sanitized,
                    "user-agent",
                ),
                "authorization_present": any(
                    key.lower() == "authorization" for key in headers
                ),
            }
        )


    @staticmethod
    def _header_value(headers: Mapping[str, str], name: str) -> str | None:
        """Return one header value by case-insensitive name."""

        lowered_name = name.lower()
        for key, value in headers.items():
            if key.lower() == lowered_name:
                return value

        return None


    @staticmethod
    def _request_json(route: str, payload: Any | None) -> Any | None:
        """Return sanitized request JSON without message bodies."""

        if not isinstance(payload, dict):
            return None

        if route == CHAT_COMPLETIONS_ROUTE:
            return GatewayLokiLogger._strip_messages(
                GatewayLokiLogger._sanitize_payload(payload)
            )

        return GatewayLokiLogger._sanitize_payload(payload)


    @staticmethod
    def _response_body_fields(route: str, stream: bool, response_text: str) -> dict[str, Any]:
        """Return parsed response body fields for a Loki response event."""

        parsed_response = parse_json_maybe(response_text)

        if route == CHAT_COMPLETIONS_ROUTE:
            if stream:
                return {"response_json": GatewayLokiLogger._stream_response_json(response_text)}

            if parsed_response is not None:
                return {"response_json": parsed_response}

            if response_text:
                return {"response_text": response_text}

            return {}

        if parsed_response is not None:
            return {"response_json": GatewayLokiLogger._sanitize_payload(parsed_response)}

        if response_text:
            return {"response_text": response_text}

        return {}


    @staticmethod
    def _assistant_text(route: str, stream: bool, response_text: str) -> str | None:
        """Return generated assistant text for generation response events."""

        if route != CHAT_COMPLETIONS_ROUTE:
            return None

        parsed_response = parse_json_maybe(response_text)

        return (
            GatewayLokiLogger._stream_assistant_text(response_text)
            if stream
            else GatewayLokiLogger._json_assistant_text(parsed_response)
        )


    @staticmethod
    def _tool_call_count(payload: Any | None) -> int | None:
        """Count tool calls in a chat request payload."""

        if not isinstance(payload, dict):
            return None

        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None

        count = 0
        for message in messages:
            if not isinstance(message, dict):
                continue

            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                count += sum(1 for tool_call in tool_calls if isinstance(tool_call, dict))

        return count


    @staticmethod
    def _sanitize_payload(value: Any) -> Any:
        """Return a payload copy with common credential-bearing fields redacted."""

        if isinstance(value, dict):
            return {
                key: "***REDACTED***"
                if GatewayLokiLogger._is_sensitive_payload_key(key)
                else GatewayLokiLogger._sanitize_payload(item)
                for key, item in value.items()
            }

        if isinstance(value, list):
            return [GatewayLokiLogger._sanitize_payload(item) for item in value]

        return value


    @staticmethod
    def _strip_messages(value: Any) -> Any:
        """Return a payload copy with message arrays removed from mappings."""

        if isinstance(value, dict):
            return {
                key: GatewayLokiLogger._strip_messages(item)
                for key, item in value.items()
                if key != "messages"
            }

        if isinstance(value, list):
            return [GatewayLokiLogger._strip_messages(item) for item in value]

        return value


    @staticmethod
    def _stream_response_json(response_text: str) -> dict[str, Any]:
        """Represent an SSE response body as a valid JSON-compatible object."""

        events: list[dict[str, Any]] = []
        data_lines: list[str] = []
        event_name: str | None = None
        done = False

        def flush_event() -> None:
            nonlocal done, event_name

            if not data_lines:
                event_name = None
                return

            payload_text = "\n".join(data_lines).strip()
            data_lines.clear()

            if payload_text == "[DONE]":
                done = True
                event_name = None
                return

            parsed_payload = parse_json_maybe(payload_text)
            event: dict[str, Any] = {
                "data": parsed_payload if parsed_payload is not None else payload_text,
            }

            if event_name is not None:
                event["event"] = event_name

            events.append(event)
            event_name = None

        for line in response_text.splitlines():
            if not line.strip():
                flush_event()
                continue

            if line.startswith("event:"):
                event_name = line[6:].strip()

            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        flush_event()

        return {
            "format": "sse",
            "done": done,
            "event_count": len(events),
            "events": events,
        }


    @staticmethod
    def _stream_assistant_text(response_text: str) -> str | None:
        """Extract assistant text by concatenating streamed SSE payload chunks."""

        fragments: list[str] = []
        data_lines: list[str] = []

        def flush_event() -> None:
            if not data_lines:
                return

            payload_text = "\n".join(data_lines).strip()
            data_lines.clear()

            if not payload_text or payload_text == "[DONE]":
                return

            payload = parse_json_maybe(payload_text)
            if not isinstance(payload, dict):
                return

            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                fragments.append(delta)

            choices = payload.get("choices")
            if not isinstance(choices, list):
                return

            for choice in choices:
                if not isinstance(choice, dict):
                    continue

                choice_text = choice.get("text")
                if isinstance(choice_text, str) and choice_text:
                    fragments.append(choice_text)

                delta_text = GatewayLokiLogger._message_text(choice.get("delta"))
                if delta_text:
                    fragments.append(delta_text)

        for line in response_text.splitlines():
            if not line.strip():
                flush_event()
                continue

            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        flush_event()

        text = "".join(fragments)
        return text or None


    @staticmethod
    def _json_assistant_text(payload: Any) -> str | None:
        """Extract assistant text from a non-stream OpenAI-compatible response."""

        if not isinstance(payload, dict):
            return None

        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text:
            return output_text

        choices = payload.get("choices")
        if isinstance(choices, list):
            text = "".join(
                fragment
                for choice in choices
                if isinstance(choice, dict)
                for fragment in (
                    choice.get("text") if isinstance(choice.get("text"), str) else "",
                    GatewayLokiLogger._message_text(choice.get("message")),
                )
                if fragment
            )

            if text:
                return text

        output = payload.get("output")
        if isinstance(output, list):
            text = "".join(
                GatewayLokiLogger._response_output_text(item) for item in output
            )

            if text:
                return text

        return None


    @staticmethod
    def _response_output_text(value: Any) -> str:
        """Return assistant text from a Responses API output item."""

        if not isinstance(value, dict):
            return ""

        text = GatewayLokiLogger._content_text(value.get("content"))
        if text:
            return text

        return GatewayLokiLogger._content_text(value.get("text"))


    @staticmethod
    def _message_text(value: Any) -> str:
        """Return assistant text from a chat-style message or delta payload."""

        if not isinstance(value, dict):
            return ""

        content_text = GatewayLokiLogger._content_text(value.get("content"))
        if content_text:
            return content_text

        refusal = value.get("refusal")
        return refusal if isinstance(refusal, str) else ""


    @staticmethod
    def _content_text(value: Any) -> str:
        """Flatten OpenAI-compatible text content blocks into plain text."""

        if isinstance(value, str):
            return value

        if isinstance(value, list):
            return "".join(GatewayLokiLogger._content_text(item) for item in value)

        if not isinstance(value, dict):
            return ""

        text = value.get("text")
        if isinstance(text, str):
            return text

        if isinstance(text, dict):
            return GatewayLokiLogger._content_text(text)

        return GatewayLokiLogger._content_text(value.get("content"))


    @staticmethod
    def _is_sensitive_payload_key(key: str) -> bool:
        """Return whether a payload field name likely contains a secret."""

        lowered = key.lower()

        return (
            lowered in SENSITIVE_KEYS
            or lowered.endswith("_api_key")
            or lowered.endswith("_password")
            or lowered.endswith("_secret")
            or lowered.endswith("_token")
        )


    @staticmethod
    def _compact(values: Mapping[str, Any]) -> dict[str, Any]:
        """Drop None values from a mapping to keep Loki payloads compact."""

        return {key: value for key, value in values.items() if value is not None}
