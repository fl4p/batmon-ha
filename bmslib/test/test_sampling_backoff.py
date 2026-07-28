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

import bmslib.bt
from bmslib.sampling import BmsSampler


class _FakeBms:
    name = "fake"
    is_virtual = False
    is_connected = False
    connect_time = 0

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

    while len(waits) < 3:
        t0 = time.time()
        before = sampler._time_next_retry
        asyncio.run(sampler())
        if sampler._time_next_retry != before:
            waits.append(int(sampler._time_next_retry - t0))  # truncate like the log's %d
    return waits


def test_backoff_counts_failures_not_waiting_cycles():
    # three consecutive failures -> 1.5**5, 1.5**6, 1.5**7
    assert _run(_make_sampler(), 0) == [7, 11, 17]

    # the same three failures must back off identically no matter how many cycles the
    # sampler burned waiting in between (this is what a short sample_period changes)
    assert _run(_make_sampler(), 5) == [7, 11, 17]
    assert _run(_make_sampler(), 40) == [7, 11, 17]


def test_backoff_resets_after_a_good_sample():
    sampler = _make_sampler()
    assert _run(sampler, 3) == [7, 11, 17]

    async def _ok():
        return object()

    sampler._sample_inner = _ok
    asyncio.run(sampler())

    assert _run(sampler, 3) == [7, 11, 17]
