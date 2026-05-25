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
