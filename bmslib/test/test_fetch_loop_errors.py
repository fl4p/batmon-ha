"""#391: fetch_loop's error counter never reset in serial mode, so the watchdog
eventually aborted a healthy add-on.

`fetch_loop` reset the counter with `if await fn(): num_errors_row = 0`, but the
serial fetch fn in main() returns None on success (it only *raises* on failure).
So the reset never ran and `num_errors_row` counted errors for the lifetime of
the process instead of consecutive failures:

  * the error backoff `min(1.1 ** n, 60)` hit its 60 s cap after ~44 lifetime
    errors, so from then on every single error stalled *both* BMS for a minute
  * at 200 it logged "too many errors, abort" and stopped sampling for good

The reporter's log shows the signature plainly: "Error (num 11)", then a healthy
half hour with dozens of good samples, and the next error is "num 12", not "num 1".
"""

import asyncio

import pytest

import bmslib.sampling
from bmslib.sampling import fetch_loop


@pytest.fixture
def slept(monkeypatch):
    """Neutralize the backoff sleeps and record their durations."""
    durations = []

    async def _fake_sleep(d):
        durations.append(d)

    monkeypatch.setattr(bmslib.sampling.asyncio, 'sleep', _fake_sleep)
    return durations


def _run(fail_pattern, max_errors, max_cycles, **kw):
    """Run fetch_loop with fn failing according to fail_pattern (cycled).
    Stops after max_cycles unless the loop aborts on its own first."""
    calls = []
    stop = dict(v=False)

    async def fn():
        calls.append(1)
        if len(calls) >= max_cycles:
            stop['v'] = True
        if fail_pattern[(len(calls) - 1) % len(fail_pattern)]:
            raise TimeoutError('boom')

    asyncio.run(fetch_loop(fn, period=0, max_errors=max_errors,
                           should_stop=lambda: stop['v'], **kw))
    return len(calls)


def test_intermittent_errors_never_abort(slept):
    # fail, ok, fail, ok ... with a watchdog that aborts at 3 consecutive errors.
    # Before the fix the count accumulated across the good cycles and tripped the
    # abort on the 4th failure, i.e. after 7 cycles.
    assert _run([True, False], max_errors=3, max_cycles=40) == 40

    # and the backoff must stay at the first step instead of escalating to the 60 s cap
    error_sleeps = [d for d in slept if d != 0]
    assert error_sleeps and max(error_sleeps) == pytest.approx(1.1)


def test_rare_errors_over_a_long_run_never_abort(slept):
    # the reporter's shape: a healthy add-on with an occasional BLE timeout
    assert _run([True] + [False] * 49, max_errors=3, max_cycles=500) == 500


def test_watchdog_still_aborts_on_sustained_failure(slept):
    # the guard must still fire: max_errors+1 consecutive failures and it gives up
    assert _run([True], max_errors=3, max_cycles=500) == 4


def test_backoff_escalates_while_failing(slept):
    _run([True], max_errors=3, max_cycles=500)
    error_sleeps = [d for d in slept if d != 0]
    assert error_sleeps == [pytest.approx(1.1 ** n) for n in (1, 2, 3)]


def test_backoff_exponent_is_clamped(slept):
    # 1.1 ** 7448 raises OverflowError, which used to kill the loop after ~5 days
    # of a permanently failing device; the exponent is clamped so it just hits the cap
    assert _run([True], max_errors=0, max_cycles=7500) == 7500
    assert max(slept) == 60


def test_backoff_cap_is_configurable(slept):
    # concurrent mode passes 600 so a permanently dead device stops hammering
    # its proxy; the serial loop keeps the 60 s default
    _run([True], max_errors=0, max_cycles=200, max_backoff=600)
    assert max(slept) == 600
