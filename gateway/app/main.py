"""FastAPI application factory for the vLLM gateway."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.routes import create_router
from app.settings import Settings
from app.state import create_app_state
from app.tracing import configure_tracing


def create_app() -> FastAPI:
    """Assemble the FastAPI application and wire lifespan-managed resources."""

    settings = Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Create and tear down shared clients and sinks."""

        state = create_app_state(settings)
        app.state.gateway_state = state
        await state.loki_sink.start()
        try:
            yield
        finally:
            await state.loki_sink.stop()
            await state.session_tracker.close()
            await state.http.aclose()

    application = FastAPI(title="vLLM Gateway", lifespan=lifespan)
    configure_tracing(application, settings)
    application.include_router(create_router())
    return application


app = create_app()
