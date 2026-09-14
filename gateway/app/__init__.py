"""OpenAI-compatible gateway application package.

Intentionally empty: the ASGI entrypoint is ``app.main:app`` (see the
Dockerfile ``CMD``). Re-exporting the application here would make every
submodule import build the whole FastAPI app as a side effect, which
prevents importing one module in isolation - in tests, for instance.
"""
