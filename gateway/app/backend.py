"""Client boundary for an OpenAI-compatible backend API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from app.http_utils import strip_hop_by_hop_headers


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
