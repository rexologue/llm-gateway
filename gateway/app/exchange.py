"""One proxied exchange, from request body to terminal bookkeeping.

Both response shapes and both outcomes converge here. A streaming answer and a
non-streaming one differ only in how the backend response is consumed; the
spans, the Loki events, the metrics and the transcript write are the same, and
they happen in exactly one place - ``_finalize``. That is what keeps a dialog
from being fully recorded by one branch and half-recorded by another.

Reading the backend is deliberately detached from delivering to the caller. The
task that talks to httpx is never the task a disconnect cancels, so a caller
hanging up cannot land a cancellation inside a backend read, and the gateway is
free to finish the generation it already paid for and record it whole.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Coroutine, Mapping

import anyio
from fastapi import Response
from fastapi.responses import StreamingResponse
from opentelemetry import trace

from app.backend import ChatResponseReader, ChatTurn
from app.http_utils import gateway_response_headers, utc_now_iso
from app.session_store import (
    DELIVERY_CLIENT_GONE,
    DELIVERY_COMPLETE,
    DELIVERY_DRAINED,
    DELIVERY_TRUNCATED,
    SessionExchange,
)
from app.state import AppState
from app.tracing import (
    SPAN_BACKEND_REQUEST,
    SPAN_GATEWAY_REQUEST,
    SPAN_STREAM_RESPONSE,
    TRACER_NAME,
    backend_request_span_attrs,
    backend_response_span_attrs,
    gateway_request_span_attrs,
    gateway_response_span_attrs,
    http_status_span_attrs,
    mark_error_if_needed,
    record_span_exception,
    set_span_attributes,
    stream_response_span_attrs,
    stream_ttft_span_attrs,
)

tracer = trace.get_tracer(TRACER_NAME)

QUEUE_MAX_CHUNKS = 64
ERROR_BODY_EXCERPT_CHARS = 2000

WARN_HISTORY_DIVERGED = "session_history_diverged"
WARN_DRAIN_TIMEOUT = "backend_drain_timeout"
WARN_DRAIN_OVERSIZED = "backend_drain_oversized"
WARN_TRANSCRIPT_NOT_SAVED = "session_transcript_not_saved"

PARAMS_SNAPSHOT_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "max_completion_tokens",
    "max_tokens",
    "n",
    "seed",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "chat_template_kwargs",
    "stream_options",
)


@dataclass(slots=True)
class ExchangeOutcome:
    """What the backend produced for one exchange, however it ended."""

    status_code: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_bytes: bytes = b""
    media_type: str | None = None
    ttft_sec: float | None = None
    turn: ChatTurn | None = None
    delivery: str = DELIVERY_COMPLETE
    error: BaseException | None = None
    warn_reason: str | None = None
    client_gone: bool = False


    @property
    def response_text(self) -> str:
        """Return the backend response body as text."""

        return self.response_bytes.decode("utf-8", errors="replace")


class ProxyExchange:
    """Own one gateway request end to end, including its terminal records."""

    def __init__(
        self,
        *,
        state: AppState,
        route: str,
        method: str,
        request_id: str,
        session_id: str | None,
        session_first_request: bool,
        headers_in: Mapping[str, str],
        raw_body: bytes,
        payload: Any,
        model: str = "unknown",
        stream: bool = False,
        fallback_params: dict[str, Any] | None = None,
        usage_forced: bool = False,
        record_transcript: bool = False,
        track_inflight: bool = False,
    ) -> None:
        """Bind everything one exchange needs to describe itself later."""

        self.state = state
        self.route = route
        self.method = method
        self.request_id = request_id
        self.session_id = session_id
        self.stream = stream
        self.model = model
        self.payload = payload
        self.raw_body = raw_body
        self.usage_forced = usage_forced
        self.record_transcript = record_transcript

        self.started_at = time.perf_counter()
        self.started_at_iso = utc_now_iso()

        self.log = state.loki.context(
            route=route,
            method=method,
            request_id=request_id,
            session_id=session_id,
            session_first_request=session_first_request,
            stream=stream,
            headers_in=headers_in,
            raw_body=raw_body,
            payload=payload,
            fallback_params=fallback_params,
        )
        self.metrics = state.metrics.context(
            route=route,
            method=method,
            stream=stream,
            model=model,
            session_id=session_id,
            session_first_request=session_first_request,
            track_inflight=track_inflight,
        )

        self.request_span = tracer.start_span(
            SPAN_GATEWAY_REQUEST,
            attributes=gateway_request_span_attrs(
                request_id=request_id,
                session_id=session_id,
                session_first_request=session_first_request,
                route=route,
                method=method,
                stream=stream,
                payload=payload if isinstance(payload, dict) else None,
                raw_body=raw_body,
            ),
        )

        self._finalized = False
        self._client_gone = asyncio.Event()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=QUEUE_MAX_CHUNKS)
        self._pump_task: asyncio.Task[Any] | None = None
        self._pump_done = False
        self._drained_after_disconnect = False
        self._deadline: Any = None


    async def open(self) -> None:
        """Emit the request-side records for this exchange."""

        await self.log.request()
        self.metrics.request()


    async def run_chat(self, *, backend_headers: Mapping[str, str], backend_url: str) -> Response:
        """Proxy one chat completion, streaming or not."""

        if self.stream:
            return await self._run_stream(
                backend_headers=backend_headers,
                backend_url=backend_url,
            )

        return await self._detached(
            self._complete_unary(
                backend_headers=backend_headers,
                backend_url=backend_url,
            ),
            name=f"chat:{self.request_id}",
        )


    async def run_passthrough(
        self,
        *,
        backend_headers: Mapping[str, str],
        backend_url: str,
        params: Any = None,
    ) -> Response:
        """Proxy one non-chat request, recording the same terminal records."""

        return await self._detached(
            self._complete_unary(
                backend_headers=backend_headers,
                backend_url=backend_url,
                params=params,
            ),
            name=f"proxy:{self.request_id}",
        )


    async def fail_locally(self, response: Response, *, response_text: str) -> Response:
        """Record a response the gateway produced itself, without a backend call."""

        outcome = ExchangeOutcome(
            status_code=response.status_code,
            response_headers=dict(response.headers),
            response_bytes=response_text.encode("utf-8"),
            media_type="application/json",
        )
        await self._finalize(outcome)

        return response


    async def _detached(self, coro: Coroutine[Any, Any, Response], *, name: str) -> Response:
        """Run the backend half in its own task and await it from the request.

        Shielding is what makes a disconnect survivable: the caller's
        cancellation stops the waiting, not the work, so the answer still gets
        read, logged and recorded. With draining disabled the task is awaited
        directly and a disconnect takes it down, which is the old behaviour.
        """

        task = self.state.tasks.spawn(coro, name=name)

        if self.state.settings.drain_after_disconnect:
            return await asyncio.shield(task)

        return await task


    async def _complete_unary(
        self,
        *,
        backend_headers: Mapping[str, str],
        backend_url: str,
        params: Any = None,
    ) -> Response:
        """Send one whole-body request, then record and return the answer.

        Chat completions and every other proxied route share this: the backend
        reads the body to its end before answering, so there is nothing to
        relay and the only difference is whether the answer carries a dialog
        turn to persist.
        """

        outcome = ExchangeOutcome()

        try:
            with trace.use_span(self.request_span, end_on_exit=False):
                with tracer.start_as_current_span(SPAN_BACKEND_REQUEST) as backend_span:
                    set_span_attributes(
                        backend_span,
                        backend_request_span_attrs(
                            method=self.method,
                            url=backend_url,
                            route=self.route,
                            model=self.model,
                            request_body_bytes=len(self.raw_body),
                        ),
                    )
                    backend_response = await self.state.backend.request(
                        method=self.method,
                        route=self.route,
                        headers=backend_headers,
                        content=self.raw_body,
                        params=params,
                    )
                    set_span_attributes(
                        backend_span,
                        backend_response_span_attrs(backend_response.status_code),
                    )
                    mark_error_if_needed(backend_span, backend_response.status_code)

        # Branch: no answer exists - the caller left or the call itself failed.
        except BaseException as exc:
            outcome.error = exc
            outcome.client_gone = isinstance(exc, asyncio.CancelledError)
            await self._finalize(outcome)
            raise

        outcome.status_code = backend_response.status_code
        outcome.response_headers = gateway_response_headers(
            backend_response.headers,
            request_id=self.request_id,
            session_id=self.session_id,
        )
        outcome.response_bytes = backend_response.content
        outcome.media_type = backend_response.headers.get("content-type")

        if self.record_transcript:
            outcome.turn = ChatResponseReader.turn_from_body(backend_response.text)

        await self._finalize(outcome)

        return Response(
            content=outcome.response_bytes,
            status_code=backend_response.status_code,
            headers=outcome.response_headers,
            media_type=outcome.media_type,
        )


    async def _run_stream(
        self,
        *,
        backend_headers: Mapping[str, str],
        backend_url: str,
    ) -> Response:
        """Start one streaming exchange and hand the caller a relayed stream."""

        try:
            with trace.use_span(self.request_span, end_on_exit=False):
                backend_request = self.state.backend.build_request(
                    method=self.method,
                    route=self.route,
                    headers=backend_headers,
                    content=self.raw_body,
                )

                with tracer.start_as_current_span(SPAN_BACKEND_REQUEST) as backend_span:
                    set_span_attributes(
                        backend_span,
                        backend_request_span_attrs(
                            method=self.method,
                            url=backend_url,
                            route=self.route,
                            model=self.model,
                            request_body_bytes=len(self.raw_body),
                        ),
                    )
                    backend_response = await self.state.backend.send(
                        backend_request,
                        stream=True,
                    )
                    set_span_attributes(
                        backend_span,
                        backend_response_span_attrs(backend_response.status_code),
                    )
                    mark_error_if_needed(backend_span, backend_response.status_code)

        # Branch: the caller left or the backend refused before a single byte of
        # body existed. There is no stream to relay, so this ends as an error.
        except BaseException as exc:
            outcome = ExchangeOutcome(error=exc)
            outcome.client_gone = isinstance(exc, asyncio.CancelledError)
            await self._finalize(outcome)
            raise

        status_code = backend_response.status_code
        response_headers = gateway_response_headers(
            backend_response.headers,
            request_id=self.request_id,
            session_id=self.session_id,
        )

        set_span_attributes(self.request_span, http_status_span_attrs(status_code))
        mark_error_if_needed(self.request_span, status_code)

        self._pump_task = self.state.tasks.spawn(
            self._pump(
                backend_response=backend_response,
                status_code=status_code,
                response_headers=response_headers,
            ),
            name=f"stream:{self.request_id}",
        )

        return StreamingResponse(
            self._deliver(),
            status_code=status_code,
            headers=response_headers,
            media_type=backend_response.headers.get("content-type"),
        )


    async def _pump(
        self,
        *,
        backend_response: Any,
        status_code: int,
        response_headers: dict[str, str],
    ) -> None:
        """Read the backend stream to its end, relaying and accumulating it.

        This task never belongs to the caller, so it keeps reading after a
        disconnect and the transcript gets the whole turn. Limits still apply:
        a drain that outlives its deadline or outgrows its buffer stops and the
        turn is recorded as truncated rather than silently partial.
        """

        reader = ChatResponseReader(drop_usage_events=self.usage_forced)
        outcome = ExchangeOutcome(
            status_code=status_code,
            response_headers=response_headers,
            media_type=response_headers.get("content-type"),
        )
        collected = bytearray()
        chunk_count = 0

        with trace.use_span(self.request_span, end_on_exit=False):
            stream_span = tracer.start_span(SPAN_STREAM_RESPONSE)

            try:
                async with asyncio.timeout(None) as deadline:
                    self._deadline = deadline

                    async for chunk in backend_response.aiter_bytes():
                        if chunk:
                            chunk_count += 1
                            collected += chunk

                            if outcome.ttft_sec is None:
                                outcome.ttft_sec = time.perf_counter() - self.started_at
                                set_span_attributes(
                                    stream_span,
                                    stream_ttft_span_attrs(outcome.ttft_sec),
                                )
                                self.metrics.ttft(
                                    status_code=status_code,
                                    cancelled=False,
                                    ttft_sec=outcome.ttft_sec,
                                )

                        await self._offer(reader.feed(chunk))

                        if self._over_drain_budget(len(collected)):
                            outcome.warn_reason = WARN_DRAIN_OVERSIZED
                            break

                    await self._offer(reader.flush())

            # Branch: the drain deadline expired after the caller had gone. The
            # turn recorded here is whatever the backend managed to produce.
            except TimeoutError:
                outcome.warn_reason = WARN_DRAIN_TIMEOUT

            except BaseException as exc:
                outcome.error = exc

            finally:
                self._pump_done = True
                outcome.response_bytes = bytes(collected)
                outcome.turn = reader.turn() if self.record_transcript else None
                outcome.client_gone = self._client_gone.is_set()
                outcome.delivery = self._delivery_of(outcome)

                set_span_attributes(
                    stream_span,
                    stream_response_span_attrs(
                        chunk_count=chunk_count,
                        response_body_bytes=len(outcome.response_bytes),
                        duration_sec=time.perf_counter() - self.started_at,
                        cancelled=outcome.client_gone,
                    ),
                )
                mark_error_if_needed(
                    stream_span,
                    status_code,
                    cancelled=outcome.client_gone,
                )

                with self._finalization_scope():
                    try:
                        self._queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass

                    try:
                        await self._finalize(outcome)
                    finally:
                        try:
                            await backend_response.aclose()
                        finally:
                            stream_span.end()


    async def _deliver(self) -> AsyncIterator[bytes]:
        """Yield relayed events to the caller until the backend stream ends."""

        try:
            while True:
                item = await self._queue.get()

                if item is None:
                    return

                yield item

        # Branch: the caller stopped reading. The pump is untouched by this, so
        # the decision is only whether to keep reading the backend for the
        # transcript or to stop it here.
        except (asyncio.CancelledError, GeneratorExit):
            self._on_client_gone()
            raise


    def _on_client_gone(self) -> None:
        """React to a caller that stopped consuming the stream."""

        self._client_gone.set()

        if self._pump_done:
            return

        if not self.state.settings.drain_after_disconnect:
            if self._pump_task is not None:
                self._pump_task.cancel()
            return

        self._drained_after_disconnect = True
        self.state.metrics.backend_drain("started")

        # From here the read is on borrowed time: nobody is waiting for it, so
        # it gets its own deadline instead of the caller's read timeout.
        if self._deadline is not None:
            self._deadline.reschedule(
                asyncio.get_running_loop().time() + self.state.settings.drain_timeout_sec
            )


    async def _offer(self, data: bytes) -> None:
        """Hand relayed bytes to the caller, unless the caller is gone.

        A slow caller must slow the backend read down rather than be buffered
        without bound, so this waits for queue room - but stops waiting the
        moment the caller disconnects, or the drain would block on a queue
        nobody drains.
        """

        if not data or self._client_gone.is_set():
            return

        try:
            self._queue.put_nowait(data)
            return

        except asyncio.QueueFull:
            pass

        put = asyncio.ensure_future(self._queue.put(data))
        gone = asyncio.ensure_future(self._client_gone.wait())

        try:
            await asyncio.wait({put, gone}, return_when=asyncio.FIRST_COMPLETED)

        finally:
            for pending in (put, gone):
                if not pending.done():
                    pending.cancel()


    def _over_drain_budget(self, collected_bytes: int) -> bool:
        """Return whether a post-disconnect drain has outgrown its buffer."""

        if not self._drained_after_disconnect:
            return False

        limit = self.state.settings.drain_max_bytes

        return limit > 0 and collected_bytes >= limit


    def _delivery_of(self, outcome: ExchangeOutcome) -> str:
        """Classify how the answer reached - or failed to reach - the caller."""

        if outcome.warn_reason in (WARN_DRAIN_TIMEOUT, WARN_DRAIN_OVERSIZED):
            return DELIVERY_TRUNCATED

        if not outcome.client_gone:
            return DELIVERY_COMPLETE

        return DELIVERY_DRAINED if self._drained_after_disconnect else DELIVERY_CLIENT_GONE


    async def _finalize(self, outcome: ExchangeOutcome) -> None:
        """Write every terminal record for this exchange, exactly once."""

        if self._finalized:
            return

        self._finalized = True
        duration_sec = time.perf_counter() - self.started_at

        with self._finalization_scope():
            if outcome.error is not None:
                record_span_exception(self.request_span, outcome.error)

            set_span_attributes(
                self.request_span,
                gateway_response_span_attrs(
                    status_code=outcome.status_code,
                    response_body_bytes=len(outcome.response_bytes),
                    duration_sec=duration_sec,
                    cancelled=outcome.client_gone,
                ),
            )
            mark_error_if_needed(
                self.request_span,
                outcome.status_code,
                cancelled=outcome.client_gone,
            )

            # Branch: the gateway never observed an answer, so the terminal
            # record is the failure itself. A caller that left after the backend
            # had answered still counts as a response: the answer exists.
            observed_answer = outcome.status_code is not None and (
                outcome.error is None or outcome.client_gone
            )

            if observed_answer:
                await self.log.response(
                    status_code=outcome.status_code,
                    response_headers=outcome.response_headers,
                    response_bytes=outcome.response_bytes,
                    response_text=outcome.response_text,
                    e2e_sec=duration_sec,
                    ttft_sec=outcome.ttft_sec,
                    cancelled=outcome.client_gone,
                )
            elif outcome.error is not None:
                await self.log.error(outcome.error, e2e_sec=duration_sec)

            if self._drained_after_disconnect:
                self.state.metrics.backend_drain(
                    "truncated" if outcome.delivery == DELIVERY_TRUNCATED else "completed"
                )

            await self._write_transcript(outcome, duration_sec)

            self.metrics.response(
                status_code=outcome.status_code if observed_answer else None,
                cancelled=outcome.client_gone,
                e2e_sec=duration_sec,
            )

            self.request_span.end()


    async def _write_transcript(
        self,
        outcome: ExchangeOutcome,
        duration_sec: float,
    ) -> None:
        """Append this exchange to its session transcript."""

        payload = self.payload if isinstance(self.payload, dict) else None

        if not self.record_transcript or self.session_id is None or payload is None:
            return

        messages = payload.get("messages")
        if not isinstance(messages, list):
            return

        tools = payload.get("tools")
        result = await self.state.session_store.record_exchange(
            SessionExchange(
                session_id=self.session_id,
                request_id=self.request_id,
                messages=messages,
                started_at=self.started_at_iso,
                finished_at=utc_now_iso(),
                tools=tools if isinstance(tools, list) else [],
                params=self._params_snapshot(payload),
                model=self.model,
                stream=self.stream,
                status_code=outcome.status_code,
                turn=outcome.turn,
                delivery=outcome.delivery,
                error=self._error_info(outcome),
                ttft_sec=outcome.ttft_sec,
                e2e_sec=duration_sec,
            )
        )

        warn_reason = self._transcript_warn_reason(outcome, result)
        self.state.metrics.session_write(
            saved=result.saved,
            delivery=outcome.delivery,
            warn_reason=warn_reason,
        )

        if warn_reason is not None:
            await self.log.session_write(
                warn_reason=warn_reason,
                delivery=outcome.delivery,
                message_cnt=result.message_cnt,
                turn_cnt=result.turn_cnt,
                dropped_cnt=result.dropped_cnt,
            )


    def _params_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the generation parameters this exchange actually used."""

        snapshot = {
            key: payload[key] for key in PARAMS_SNAPSHOT_KEYS if key in payload
        }

        if self.usage_forced:
            snapshot["usage_forced_by_gateway"] = True

        if self.log.fallback_params:
            snapshot["fallback_params"] = dict(self.log.fallback_params)

        return snapshot


    def _transcript_warn_reason(self, outcome: ExchangeOutcome, result: Any) -> str | None:
        """Return the one reason worth warning about for this write, if any."""

        if not result.saved:
            return WARN_TRANSCRIPT_NOT_SAVED

        if result.warn_reason is not None:
            return result.warn_reason

        if result.dropped_cnt:
            return WARN_HISTORY_DIVERGED

        if outcome.warn_reason is not None:
            return outcome.warn_reason

        return None


    @staticmethod
    def _error_info(outcome: ExchangeOutcome) -> dict[str, Any] | None:
        """Describe why an exchange failed, for the transcript audit."""

        if outcome.error is not None:
            return {
                "type": type(outcome.error).__name__,
                "message": str(outcome.error),
            }

        if outcome.status_code is None or outcome.status_code < 400:
            return None

        return {
            "type": "backend_status",
            "status_code": outcome.status_code,
            "message": outcome.response_text[:ERROR_BODY_EXCERPT_CHARS],
        }


    @staticmethod
    def _finalization_scope() -> anyio.CancelScope:
        """Return a shielded cancel scope for terminal request bookkeeping.

        A downstream consumer that stops reading cancels the ASGI task, and
        inside a cancelled scope every plain ``await`` re-raises immediately.
        Terminal work would then be silently dropped: no Loki event, no turn
        persisted, no backend connection closed.

        Shielding keeps that bookkeeping running to completion. It is bounded
        work: Loki submission only enqueues, and the session store runs against
        Valkey with socket timeouts, so a shielded scope cannot hold a cancelled
        request open indefinitely.
        """

        return anyio.CancelScope(shield=True)
