"""Publish conversation metric records to the Monitor service."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import orjson

from app.metrics import GatewayMetrics

# Fixed by the Monitor OpenAPI contract, not by deployment: the gateway reports
# conversations, so only this one path is ever used. Configuration carries the
# host, which is the part that actually differs per environment.
MONITOR_CONV_PATH = "/api/v1/conv"


class MonitorPublisher:
    """Deliver one conversation record per exchange to Monitor, off the request path.

    Monitor collects call-platform metrics; it is not part of answering a
    request, so a request must neither wait for it nor fail with it. Records go
    into a bounded queue that a pool of background workers drains, which leaves
    the request path paying a queue put and nothing else. A Monitor that stops
    answering therefore costs dropped records, not latency.

    Unlike Loki, Monitor takes one record per POST - the endpoint has no batch
    shape - so throughput comes from several workers rather than from batching.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        url: str,
        timeout_sec: float,
        queue_max_size: int,
        concurrency: int,
        metrics: GatewayMetrics,
    ) -> None:
        """Initialize a background Monitor record publisher."""

        self.metrics = metrics
        self.enabled = enabled
        self.url = url
        self.timeout_sec = timeout_sec
        self.queue_max_size = max(0, queue_max_size)
        self.concurrency = max(1, concurrency)
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue(
            maxsize=self.queue_max_size
        )

        self._workers: list[asyncio.Task[None]] = []
        self._client: httpx.AsyncClient | None = None


    async def start(self) -> None:
        """Create the HTTP client and the background worker pool."""

        if not self.enabled:
            return

        self._client = httpx.AsyncClient(timeout=self.timeout_sec)
        self._workers = [
            asyncio.create_task(self._run(), name=f"monitor-publisher-{index}")
            for index in range(self.concurrency)
        ]


    async def stop(self) -> None:
        """Drain queued records and close the HTTP client."""

        if not self.enabled:
            return

        for _worker in self._workers:
            await self.queue.put(None)

        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers = []

        if self._client is not None:
            await self._client.aclose()
            self._client = None


    def submit(self, *, conv_id: str, data: dict[str, Any]) -> None:
        """Queue one conversation record for asynchronous delivery.

        Deliberately not a coroutine: it never waits. A full queue drops the
        record and says so through a metric, because a gateway that blocks on
        its own telemetry sink has turned an optional dependency into a
        required one.
        """

        if not self.enabled:
            return

        try:
            self.queue.put_nowait((conv_id, data))

        except asyncio.QueueFull:
            self.metrics.monitor_record_dropped("queue_full")


    async def _run(self) -> None:
        """Drain the queue until the shutdown sentinel arrives."""

        while True:
            item = await self.queue.get()

            if item is None:
                break

            conv_id, data = item
            await self._publish(conv_id, data)


    async def _publish(self, conv_id: str, data: dict[str, Any]) -> None:
        """POST one record and classify the outcome for the metrics."""

        if self._client is None:
            return

        body = {"convId": conv_id, "data": data}

        try:
            response = await self._client.post(
                self.url,
                content=orjson.dumps(body),
                headers={"Content-Type": "application/json"},
            )

        except Exception:
            self.metrics.monitor_push("error")
            return

        self.metrics.monitor_push(self.push_status(response.status_code))


    @staticmethod
    def push_status(status_code: int) -> str:
        """Return the metric status label for one Monitor response.

        ``rejected`` is kept apart from ``error`` because the two have
        different fixes and only one of them is ours: Monitor answers 422 when
        the record itself is wrong - most likely a ``convId`` that is not a
        UUID - while 5xx and transport failures mean Monitor is unwell. Folding
        them together would hide a gateway that is rejected on every single
        request behind what looks like a flaky sink.
        """

        if status_code < 300:
            return "success"

        if status_code < 500:
            return "rejected"

        return "error"
