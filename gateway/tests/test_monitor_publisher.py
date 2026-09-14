"""Tests for Monitor delivery and for the record the gateway sends it.

Monitor is an optional sink for another team's dashboards, so the properties
worth pinning are the ones that keep it optional: a request never waits on it,
a failing Monitor never reaches the caller, and a Monitor that is refusing
records is distinguishable from one that is merely down.

``asyncio.run`` is used rather than a pytest async plugin, so the suite keeps
running on the project's two dev dependencies.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

import httpx
import orjson
import pytest

from app.backend import ChatTurn
from app.exchange import ExchangeOutcome, ProxyExchange
from app.metrics import GatewayMetrics
from app.route_paths import CHAT_COMPLETIONS_ROUTE
from app.settings import Settings
from app.state import create_app_state
from app.tools.monitor import MONITOR_CONV_PATH, MonitorPublisher

T = TypeVar("T")

CONV_ID = "3f2a9c1e-0000-4000-8000-000000000001"


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Run one coroutine to completion."""

    return asyncio.run(coro)


class RecordingMetrics(GatewayMetrics):
    """A metrics facade that remembers Monitor outcomes instead of exporting them."""

    def __init__(self) -> None:
        """Initialize empty outcome logs."""

        super().__init__()
        self.pushes: list[str] = []
        self.drops: list[str] = []


    def monitor_push(self, status: str) -> None:
        """Remember one delivery outcome."""

        self.pushes.append(status)


    def monitor_record_dropped(self, reason: str) -> None:
        """Remember one record dropped before delivery."""

        self.drops.append(reason)


async def make_publisher(
    handler: Any,
    *,
    metrics: RecordingMetrics,
    queue_max_size: int = 10,
    concurrency: int = 1,
) -> MonitorPublisher:
    """Return a started publisher whose HTTP client is answered by ``handler``."""

    publisher = MonitorPublisher(
        enabled=True,
        url=f"http://monitor.invalid{MONITOR_CONV_PATH}",
        timeout_sec=1.0,
        queue_max_size=queue_max_size,
        concurrency=concurrency,
        metrics=metrics,
    )
    await publisher.start()

    # The workers are already running against a real client; swapping it for a
    # mock transport keeps the queue-and-drain path under test while nothing
    # leaves the process.
    await publisher._client.aclose()
    publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return publisher


def test_record_is_posted_in_the_monitor_envelope() -> None:
    """The gateway sends ``convId`` plus an opaque ``data`` object, as specified."""

    metrics = RecordingMetrics()
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(orjson.loads(request.content))
        return httpx.Response(204)

    async def scenario() -> None:
        publisher = await make_publisher(handler, metrics=metrics)
        publisher.submit(conv_id=CONV_ID, data={"service": "llm-gateway"})
        await publisher.stop()

    run(scenario())

    assert seen == [{"convId": CONV_ID, "data": {"service": "llm-gateway"}}]
    assert metrics.pushes == ["success"]


def test_queued_records_are_drained_before_shutdown_completes() -> None:
    """Shutdown flushes what is queued: the last turn of a call still counts."""

    metrics = RecordingMetrics()
    delivered: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        delivered.append(orjson.loads(request.content)["data"]["request_id"])
        return httpx.Response(204)

    async def scenario() -> None:
        publisher = await make_publisher(handler, metrics=metrics)

        for index in range(5):
            publisher.submit(conv_id=CONV_ID, data={"request_id": f"req-{index}"})

        await publisher.stop()

    run(scenario())

    assert sorted(delivered) == [f"req-{index}" for index in range(5)]


def test_unreachable_monitor_does_not_raise_into_the_request() -> None:
    """A Monitor that fails is a lost record, never a failed exchange."""

    metrics = RecordingMetrics()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("monitor is down")

    async def scenario() -> None:
        publisher = await make_publisher(handler, metrics=metrics)
        publisher.submit(conv_id=CONV_ID, data={"service": "llm-gateway"})
        await publisher.stop()

    run(scenario())

    assert metrics.pushes == ["error"]


def test_full_queue_drops_instead_of_waiting() -> None:
    """Submitting never blocks: a backed-up queue drops and says so.

    The assertion is that the drop was *counted*. A publisher that silently
    discarded records would pass a "did not block" test while leaving an
    operator unable to tell an idle gateway from a losing one.
    """

    metrics = RecordingMetrics()
    publisher = MonitorPublisher(
        enabled=True,
        url=f"http://monitor.invalid{MONITOR_CONV_PATH}",
        timeout_sec=1.0,
        queue_max_size=1,
        concurrency=1,
        metrics=metrics,
    )

    publisher.submit(conv_id=CONV_ID, data={"n": 1})
    publisher.submit(conv_id=CONV_ID, data={"n": 2})

    assert metrics.drops == ["queue_full"]


def test_disabled_publisher_never_queues() -> None:
    """A gateway deployed without Monitor pays nothing for it."""

    metrics = RecordingMetrics()
    publisher = MonitorPublisher(
        enabled=False,
        url="",
        timeout_sec=1.0,
        queue_max_size=10,
        concurrency=1,
        metrics=metrics,
    )

    publisher.submit(conv_id=CONV_ID, data={"service": "llm-gateway"})

    assert publisher.queue.empty()
    assert metrics.drops == []


@pytest.mark.parametrize(
    "status_code, expected",
    [(204, "success"), (422, "rejected"), (500, "error")],
)
def test_rejected_records_are_not_filed_as_monitor_failures(
    status_code: int,
    expected: str,
) -> None:
    """422 means our record is wrong - most likely a convId that is not a UUID.

    It is kept apart from 5xx because only one of the two is ours to fix.
    """

    assert MonitorPublisher.push_status(status_code) == expected


def make_exchange(state: Any, *, session_id: str | None) -> ProxyExchange:
    """Return an exchange for one streamed chat completion."""

    return ProxyExchange(
        state=state,
        route=CHAT_COMPLETIONS_ROUTE,
        method="POST",
        request_id="req-8812",
        session_id=session_id,
        session_first_request=True,
        headers_in={},
        raw_body=b"{}",
        payload={"messages": []},
        model="qwen2.5-32b-instruct",
        stream=True,
    )


def finished_outcome() -> ExchangeOutcome:
    """Return the outcome of a stream that completed with usage reported."""

    return ExchangeOutcome(
        status_code=200,
        ttft_sec=0.21,
        turn=ChatTurn(
            usage={
                "prompt_tokens": 1840,
                "completion_tokens": 96,
                "total_tokens": 1936,
            }
        ),
    )


def monitor_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return settings for a gateway wired to a Monitor that nothing answers."""

    monkeypatch.setenv("GATEWAY_ENGINE_ID", "rtx6000a-8001")
    monkeypatch.setenv("GATEWAY_MONITOR_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MONITOR_API_URL", "http://monitor.invalid")

    return Settings.from_env()


def test_record_names_the_engine_that_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Which GPU host served the request is the field Monitor asked for by name.

    One gateway runs in front of one engine process, so the engine id answers
    it - and it has to survive into the record, not just into Prometheus.
    """

    async def scenario() -> dict[str, Any]:
        state = create_app_state(monitor_settings(monkeypatch))

        try:
            exchange = make_exchange(state, session_id=CONV_ID)
            return exchange._monitor_data(finished_outcome(), 1.567)

        finally:
            await state.http.aclose()

    data = run(scenario())

    assert data["gpu_node"] == "rtx6000a-8001"
    assert data["conv_id"] == CONV_ID
    assert data["request_id"] == "req-8812"
    assert data["service"] == "llm-gateway"


def test_record_splits_time_to_first_token_from_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Throughput is measured over generation, not over the whole request.

    Prefill and engine queueing are already reported as ``ttft_ms``; counting
    them again inside ``tokens_per_sec`` would make a busy queue read as a slow
    GPU.
    """

    async def scenario() -> dict[str, Any]:
        state = create_app_state(monitor_settings(monkeypatch))

        try:
            exchange = make_exchange(state, session_id=CONV_ID)
            return exchange._monitor_data(finished_outcome(), 1.567)

        finally:
            await state.http.aclose()

    data = run(scenario())

    assert data["e2e_ms"] == 1567.0
    assert data["ttft_ms"] == 210.0
    assert data["generation_ms"] == 1357.0
    assert data["tokens_in"] == 1840
    assert data["tokens_out"] == 96
    assert data["tokens_per_sec"] == pytest.approx(70.7, abs=0.1)


def test_record_omits_what_was_never_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-streamed failure has no TTFT and no usage; it must not invent them."""

    async def scenario() -> dict[str, Any]:
        state = create_app_state(monitor_settings(monkeypatch))

        try:
            exchange = make_exchange(state, session_id=CONV_ID)
            outcome = ExchangeOutcome(status_code=500)

            return exchange._monitor_data(outcome, 0.4)

        finally:
            await state.http.aclose()

    data = run(scenario())

    assert "ttft_ms" not in data
    assert "tokens_out" not in data
    assert "tokens_per_sec" not in data
    assert data["result"] == "error"
    assert data["status_code"] == 500


def test_finished_exchange_is_queued_under_its_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exchange hands Monitor a record without awaiting delivery."""

    async def scenario() -> Any:
        state = create_app_state(monitor_settings(monkeypatch))

        try:
            exchange = make_exchange(state, session_id=CONV_ID)
            exchange._report_to_monitor(finished_outcome(), 1.567)

            return state.monitor.queue.get_nowait()

        finally:
            await state.http.aclose()

    conv_id, data = run(scenario())

    assert conv_id == CONV_ID
    assert data["event"] == "inference_finished"
    assert data["gpu_node"] == "rtx6000a-8001"


def test_exchange_without_a_session_is_not_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monitor files records under a conversation, so one without it is skipped."""

    async def scenario() -> bool:
        state = create_app_state(monitor_settings(monkeypatch))

        try:
            exchange = make_exchange(state, session_id=None)
            exchange._report_to_monitor(finished_outcome(), 1.567)

            return state.monitor.queue.empty()

        finally:
            await state.http.aclose()

    assert run(scenario()) is True


def test_monitor_url_is_required_when_monitor_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled sink with no address would report nothing, quietly."""

    monkeypatch.setenv("GATEWAY_ENGINE_ID", "rtx6000a-8001")
    monkeypatch.setenv("GATEWAY_MONITOR_ENABLED", "true")
    monkeypatch.delenv("GATEWAY_MONITOR_API_URL", raising=False)

    with pytest.raises(ValueError, match="GATEWAY_MONITOR_API_URL"):
        Settings.from_env()


def test_monitor_stays_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway must start and proxy without Monitor being configured."""

    monkeypatch.setenv("GATEWAY_ENGINE_ID", "rtx6000a-8001")
    monkeypatch.delenv("GATEWAY_MONITOR_ENABLED", raising=False)
    monkeypatch.delenv("GATEWAY_MONITOR_API_URL", raising=False)

    assert Settings.from_env().monitor_enabled is False
