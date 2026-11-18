from types import SimpleNamespace

from bmslib import circuit_breaker
from bmslib.circuit_breaker import CircuitBreaker


def test_disabled_always_attempts():
    cb = CircuitBreaker(0)
    assert cb.enabled is False
    assert cb.should_attempt(now=100) is True
    cb.on_failure(now=100)
    # disabled: failure changes nothing
    assert cb.should_attempt(now=101) is True
    assert cb.keep_batch_on_failure is False


def test_enabled_blocks_for_interval_after_failure():
    cb = CircuitBreaker(3600)
    assert cb.should_attempt(now=100) is True
    cb.on_failure(now=100)
    assert cb.should_attempt(now=200) is False        # within backoff window
    assert cb.should_attempt(now=100 + 3600) is True  # window elapsed


def test_buffer_only_after_a_success():
    cb = CircuitBreaker(3600)
    assert cb.keep_batch_on_failure is False          # never succeeded -> drop
    cb.on_success(now=100)
    assert cb.ever_succeeded is True
    assert cb.keep_batch_on_failure is True            # proven server -> buffer
    cb.on_failure(now=200)
    assert cb.keep_batch_on_failure is True            # stays buffering


def test_success_resets_backoff():
    cb = CircuitBreaker(3600)
    cb.on_failure(now=100)
    assert cb.should_attempt(now=200) is False
    cb.on_success(now=250)
    assert cb.should_attempt(now=251) is True


def test_backoff_follows_elapsed_time_not_wall_time(monkeypatch):
    """A wall-clock correction must not stretch or shorten the backoff window."""
    state = SimpleNamespace(wall=10000., elapsed=100.)
    clock = SimpleNamespace(time=lambda: state.wall, monotonic=lambda: state.elapsed)
    monkeypatch.setattr(circuit_breaker, 'time', clock)

    cb = CircuitBreaker(60)
    cb.on_failure()                       # no explicit now: uses the clock
    state.elapsed += 30
    state.wall -= 3600                    # backwards correction
    assert cb.should_attempt() is False   # still inside the window
    state.elapsed += 31
    assert cb.should_attempt() is True    # elapsed time decides, not wall time
