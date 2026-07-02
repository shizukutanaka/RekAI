"""Unit tests for the consecutive-failure tracker behind the 5xx circuit breaker."""

from __future__ import annotations

from rekai.circuit_breaker import ConsecutiveFailureTracker


def test_counts_increment_per_failure() -> None:
    tracker = ConsecutiveFailureTracker()
    assert tracker.record_failure("p") == 1
    assert tracker.record_failure("p") == 2
    assert tracker.record_failure("p") == 3


def test_success_resets_the_count() -> None:
    tracker = ConsecutiveFailureTracker()
    tracker.record_failure("p")
    tracker.record_failure("p")
    tracker.record_success("p")
    assert tracker.record_failure("p") == 1  # starts over, not 3


def test_success_on_an_unknown_key_is_a_noop() -> None:
    tracker = ConsecutiveFailureTracker()
    tracker.record_success("never-failed")  # must not raise
    assert tracker.record_failure("never-failed") == 1


def test_counts_are_independent_per_key() -> None:
    tracker = ConsecutiveFailureTracker()
    tracker.record_failure("a")
    tracker.record_failure("a")
    tracker.record_failure("b")
    assert tracker.record_failure("a") == 3
    assert tracker.record_failure("b") == 2


def test_clear_resets_everything() -> None:
    tracker = ConsecutiveFailureTracker()
    tracker.record_failure("a")
    tracker.record_failure("b")
    tracker.clear()
    assert tracker.record_failure("a") == 1
    assert tracker.record_failure("b") == 1
