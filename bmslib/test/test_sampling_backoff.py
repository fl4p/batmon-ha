"""#391: the device-not-found retry backoff escalated on polling cadence instead of
on failures.

`__call__` bumps `_num_errors` on *every* invocation, and `_sample_inner` returns None
(without raising) for each cycle spent inside the retry window. So the cycles spent
waiting counted as errors and inflated the next wait, which bought more waiting cycles.
In the reporter's log that reached the 1.5**14 == 291 s cap after only three failed
connects: "retry in 7 seconds" -> "retry in 25 seconds" -> "retry in 291 seconds".
"""

import asyncio
import time

import pytest

import bmslib.bt
from bmslib.sampling import BmsSampler


class _FakeBms:
    name = "fake"
    address = 'serial'  # skips the bt_diagnostics BLE scan in the error handler
    is_virtual = False
    is_connected = False
    connect_time = 0
    verbose_log = False

    def __str__(self):
        return "FakeBms(fake)"


def _make_sampler():
    return BmsSampler(_FakeBms(), mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60)


def _run(sampler, num_skipped_cycles_per_failure):
    """Drive __call__ through 3 failed connects, each preceded by some cycles that are
    skipped because we are still inside the retry window. Returns the waits in seconds."""
    waits = []
    state = dict(skip=0)

    async def _sample_inner():
        if state["skip"] > 0:
            state["skip"] -= 1
            return None  # inside the retry window, _sample_inner() bails out early
        state["skip"] = num_skipped_cycles_per_failure
        raise bmslib.bt.BleakDeviceNotFoundError("fake not found")

    sampler._sample_inner = _sample_inner

    for _ in range(500):  # bounded so a regression fails instead of hanging the suite
        if len(waits) == 3:
            return waits
        t0 = time.time()
        before = sampler._time_next_retry
        asyncio.run(sampler())
        if sampler._time_next_retry != before:
            waits.append(int(sampler._time_next_retry - t0))  # truncate like the log's %d
    raise AssertionError("only got %d waits, _time_next_retry stopped updating" % len(waits))


def test_backoff_counts_failures_not_waiting_cycles():
    # three consecutive failures -> 1.5**5, 1.5**6, 1.5**7
    assert _run(_make_sampler(), 0) == [7, 11, 17]

    # the same three failures must back off identically no matter how many cycles the
    # sampler burned waiting in between (this is what a short sample_period changes)
    assert _run(_make_sampler(), 5) == [7, 11, 17]
    assert _run(_make_sampler(), 40) == [7, 11, 17]


def test_backoff_resets_on_a_successful_connect():
    """A BMS whose fetch_voltages() keeps failing publishes samples but _sample_inner
    returns None for it (`err`), so "reset on a good sample" alone would carry the old
    not-found streak forever and turn the next single missed scan into a 291 s blackout.
    Drives the real _sample_inner() far enough to reach the connect."""

    class _ReachableBms(_FakeBms):
        is_connected = False

        async def __aenter__(self):
            self.is_connected = True
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch(self):
            # stop here: everything past the connect is publishing, which needs mqtt
            raise TimeoutError("connected, but no data")

        def debug_data(self):  # used by the error handler
            return None

        async def disconnect(self):
            self.is_connected = False

    sampler = BmsSampler(_ReachableBms(), mqtt_client=None, dt_max_seconds=120,
                         expire_after_seconds=60)
    sampler.num_samples = 1  # skip the device-info fetch on the first sample

    sampler._num_not_found = 9
    sampler._time_next_retry = 0  # pretend the 195 s wait already elapsed

    with pytest.raises(TimeoutError):  # __call__ re-raises, main.fetch_loop catches it
        asyncio.run(sampler())  # connects, then fails to fetch

    assert sampler._num_not_found == 0


def test_backoff_resets_after_a_good_sample():
    sampler = _make_sampler()
    assert _run(sampler, 3) == [7, 11, 17]

    async def _ok():
        return object()

    sampler._sample_inner = _ok
    asyncio.run(sampler())

    assert _run(sampler, 3) == [7, 11, 17]
