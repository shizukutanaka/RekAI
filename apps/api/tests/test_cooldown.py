"""Tests for the per-provider cooldown registry."""

from __future__ import annotations

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
