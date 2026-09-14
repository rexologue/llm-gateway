"""Tests for the Valkey breaker and the settings that configure it.

The breaker exists so an unreachable Valkey costs microseconds instead of
``socket_connect_timeout`` seconds on every request. The assertions are about
*whether the dialer was called at all*, not about timing: a breaker that still
dialed but returned quickly would pass a latency test while failing its purpose.

``asyncio.run`` is used rather than a pytest async plugin, so the suite keeps
running on the project's two dev dependencies.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, WatchError

from app.settings import Settings
from app.tools.valkey_store import ValkeyJsonStore, ValkeyUnavailable

T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Run one coroutine to completion."""

    return asyncio.run(coro)


class FakeRedis:
    """A Valkey client stand-in that counts dials and fails on demand."""

    def __init__(self) -> None:
        """Start unreachable, with no dials recorded."""

        self.calls = 0
        self.error: BaseException | None = RedisConnectionError("unreachable")

    async def get(self, key: str) -> None:
        """Record one dial, then fail with whatever the test asked for."""

        self.calls += 1

        if self.error is not None:
            raise self.error

        return None


def make_store(**kwargs: Any) -> tuple[ValkeyJsonStore, FakeRedis]:
    """Return a store whose dialer is replaced by a countable fake."""

    store = ValkeyJsonStore(
        api_url="redis://127.0.0.1:6379/0",
        prefix="test:",
        default_ttl_sec=60,
        **kwargs,
    )
    fake = FakeRedis()
    store.redis = fake

    return store, fake


def test_breaker_opens_after_threshold_and_stops_dialing() -> None:
    """Once open, the breaker must not reach the network at all."""

    store, fake = make_store(breaker_failures=3, breaker_cooldown_sec=30.0)

    for _ in range(3):
        with pytest.raises(RedisError):
            run(store.get("id"))

    assert fake.calls == 3
    assert store.available is False

    with pytest.raises(ValkeyUnavailable):
        run(store.get("id"))

    assert fake.calls == 3, "an open breaker must not dial Valkey"


def test_breaker_probes_once_after_cooldown_and_closes_on_success() -> None:
    """A recovered Valkey must be picked up again without a restart."""

    store, fake = make_store(breaker_failures=2, breaker_cooldown_sec=30.0)

    for _ in range(2):
        with pytest.raises(RedisError):
            run(store.get("id"))

    assert store.available is False

    # Expire the cooldown instead of sleeping through it.
    store._open_until = 0.0
    fake.error = None

    assert run(store.get("id")) is None
    assert fake.calls == 3
    assert store.available is True


def test_watch_error_does_not_count_towards_the_breaker() -> None:
    """Write contention means Valkey answered, so it is not an outage."""

    store, fake = make_store(breaker_failures=2, breaker_cooldown_sec=30.0)
    fake.error = WatchError("record changed underneath the write")

    for _ in range(5):
        with pytest.raises(WatchError):
            run(store.get("id"))

    assert fake.calls == 5
    assert store.available is True


def test_unavailable_is_a_redis_error() -> None:
    """Callers degrade on ``RedisError``; an open breaker must reuse that path."""

    assert issubclass(ValkeyUnavailable, RedisError)


def test_engine_id_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway without an engine id must refuse to start."""

    monkeypatch.delenv("GATEWAY_ENGINE_ID", raising=False)

    with pytest.raises(ValueError, match="GATEWAY_ENGINE_ID"):
        Settings.from_env()


def test_engine_id_reaches_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured engine id is what labels this gateway everywhere."""

    monkeypatch.setenv("GATEWAY_ENGINE_ID", "  rtx6000a-8001  ")

    assert Settings.from_env().engine_id == "rtx6000a-8001"


@pytest.mark.parametrize("value, expected", [("false", False), ("true", True)])
def test_sessions_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    """Session persistence is optional: the observability stack may be absent."""

    monkeypatch.setenv("GATEWAY_ENGINE_ID", "rtx6000a-8001")
    monkeypatch.setenv("GATEWAY_SESSIONS_ENABLED", value)

    assert Settings.from_env().sessions_enabled is expected


def make_session_store(*, enabled: bool) -> "Any":
    """Return a session store wired to an address nothing listens on."""

    from app.session_store import SessionStore

    return SessionStore(
        api_url="redis://127.0.0.1:1/1",
        prefix="test-store:",
        ttl_sec=60,
        max_connections=1,
        enabled=enabled,
    )


def test_disabled_session_store_never_dials() -> None:
    """A gateway deployed without observability must not wait on Valkey."""

    from app.session_store import SessionExchange

    store = make_session_store(enabled=False)
    assert store.available is False

    result = run(
        store.record_exchange(
            SessionExchange(
                session_id="s1",
                request_id="r1",
                messages=[{"role": "user", "content": "ping"}],
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:00:01Z",
            )
        )
    )

    assert result.saved is False


def test_disabled_session_store_reads_fail_as_unavailable() -> None:
    """The viewer routes answer 503 on RedisError, so reads reuse that path."""

    store = make_session_store(enabled=False)

    with pytest.raises(ValkeyUnavailable):
        run(store.get_session("s1"))

    with pytest.raises(ValkeyUnavailable):
        run(store.list_sessions())
