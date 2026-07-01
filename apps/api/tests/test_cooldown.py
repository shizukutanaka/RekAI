"""Tests for the per-provider cooldown registry."""

from __future__ import annotations

from rekai.cache import MemoryCache
from rekai.cooldown import Cooldown


def test_marks_and_expires() -> None:
    t = {"now": 0.0}
    cd = Cooldown(clock=lambda: t["now"])
    assert cd.active("p") is False
    cd.mark("p", 10)
    assert cd.active("p") is True
    assert cd.remaining("p") == 10
    t["now"] = 11
    assert cd.active("p") is False  # expired
    assert cd.remaining("p") == 0


def test_extends_to_the_later_deadline() -> None:
    t = {"now": 0.0}
    cd = Cooldown(clock=lambda: t["now"])
    cd.mark("p", 5)
    cd.mark("p", 3)  # shorter -> keep the existing later deadline
    assert cd.remaining("p") == 5
    cd.mark("p", 20)  # longer -> extend
    assert cd.remaining("p") == 20


def test_ignores_nonpositive() -> None:
    cd = Cooldown()
    cd.mark("p", 0)
    cd.mark("p", -5)
    assert cd.active("p") is False


async def test_shared_cooldown_visible_to_another_worker() -> None:
    # Two independent Cooldown instances (simulating two worker processes) share
    # one cache backend (simulating Redis). A cooldown marked by worker A must be
    # visible to worker B even though its local dict never saw it.
    shared_cache = MemoryCache()
    worker_a = Cooldown()
    worker_b = Cooldown()

    assert await worker_b.active_shared(shared_cache, "openai") is False
    await worker_a.mark_shared(shared_cache, "openai", seconds=10)

    assert worker_a.active("openai") is True  # local fast path
    assert "openai" not in worker_b._until  # worker B's local dict is untouched
    assert await worker_b.active_shared(shared_cache, "openai") is True  # ...but sees it via cache


async def test_shared_mark_is_noop_write_for_nonpositive_seconds() -> None:
    shared_cache = MemoryCache()
    cd = Cooldown()
    await cd.mark_shared(shared_cache, "p", 0)
    assert await cd.active_shared(shared_cache, "p") is False


async def test_active_shared_checks_local_before_cache() -> None:
    # Local-active short-circuits without touching the cache at all.
    class ExplodingCache(MemoryCache):
        async def get(self, key: str):
            raise AssertionError("should not be called when local cooldown is active")

    cd = Cooldown()
    cd.mark("p", 10)
    assert await cd.active_shared(ExplodingCache(), "p") is True
