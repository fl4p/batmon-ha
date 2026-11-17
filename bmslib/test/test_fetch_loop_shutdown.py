"""Stop requests must not wait out the sampling interval or retry backoff."""

import asyncio
from types import SimpleNamespace

import pytest

import bmslib.sampling as sampling


@pytest.mark.parametrize('phase', ['period', 'backoff', 'capped_backoff'])
def test_stop_during_sleep_returns_promptly(monkeypatch, phase):
    now = 0.0
    stop_at = None
    calls = 0
    target_call = 44 if phase == 'capped_backoff' else 1

    async def sleep(delay):
        nonlocal now
        now += delay

    async def fetch():
        nonlocal calls, stop_at
        calls += 1
        if calls == target_call:
            stop_at = now + 0.25
        if phase != 'period':
            raise TimeoutError('offline')

    monkeypatch.setattr(sampling.asyncio, 'sleep', sleep)
    monkeypatch.setattr(sampling, 'time', SimpleNamespace(monotonic=lambda: now))
    asyncio.run(sampling.fetch_loop(
        fetch, period=20 if phase == 'period' else 0, max_errors=0,
        should_stop=lambda: stop_at is not None and now >= stop_at))

    assert calls == target_call
    assert now - stop_at <= 0.5


@pytest.mark.parametrize('failed', [False, True])
def test_stop_during_fetch_does_not_start_another_sleep(monkeypatch, failed):
    stopped = False
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    async def fetch():
        nonlocal stopped
        stopped = True
        if failed:
            raise TimeoutError('link dropped during shutdown')

    monkeypatch.setattr(sampling.asyncio, 'sleep', sleep)
    asyncio.run(sampling.fetch_loop(
        fetch, period=20, max_errors=0, should_stop=lambda: stopped))
    assert not sleeps


def test_stop_with_real_event_loop():
    async def run():
        entered = asyncio.Event()
        stopped = False
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            entered.set()

        task = asyncio.create_task(sampling.fetch_loop(
            fetch, period=20, max_errors=0, should_stop=lambda: stopped))
        await entered.wait()
        stopped = True
        # A real sleep must wake up to observe the stop; no Bluetooth required.
        await asyncio.wait_for(task, timeout=2)
        assert calls == 1

    asyncio.run(run())


def test_sampling_interval_is_preserved_without_stop(monkeypatch):
    now = 0.0
    calls_at = []

    async def sleep(delay):
        nonlocal now
        now += delay

    async def fetch():
        calls_at.append(now)

    monkeypatch.setattr(sampling.asyncio, 'sleep', sleep)
    monkeypatch.setattr(sampling, 'time', SimpleNamespace(monotonic=lambda: now))
    asyncio.run(sampling.fetch_loop(
        fetch, period=2.25, max_errors=0, should_stop=lambda: len(calls_at) == 3))
    assert calls_at == [0, 2.25, 4.5]


@pytest.mark.parametrize('stop_callback', [None, lambda: False])
def test_task_cancellation_still_propagates(stop_callback):
    async def run():
        entered = asyncio.Event()

        async def fetch():
            entered.set()

        task = asyncio.create_task(sampling.fetch_loop(
            fetch, period=20, max_errors=0, should_stop=stop_callback))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
