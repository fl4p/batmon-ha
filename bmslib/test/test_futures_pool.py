import asyncio

from bmslib import FuturesPool

pool = FuturesPool()


def test1():
    async def run():
        try:
            with pool.acquire(1):
                await pool.wait_for(1, 0.01)
        except asyncio.exceptions.TimeoutError:
            pass

        with pool.acquire(1):
            try:
                await pool.wait_for(1, 0.01)
            except asyncio.exceptions.TimeoutError:
                pass

        try:
            with pool.acquire(1):
                await pool.wait_for(1, 0.01)
        except asyncio.exceptions.TimeoutError:
            pass

    asyncio.run(run())


def test_result_is_returned_and_removed():
    async def run():
        p = FuturesPool()
        with p.acquire('a'):
            asyncio.get_running_loop().call_soon(p.set_result, 'a', 42)
            assert await p.wait_for('a', 1) == 42
            assert 'a' not in p._futures

    asyncio.run(run())


def test_timeout_raises_timeout_error():
    async def run():
        p = FuturesPool()
        with p.acquire('a'):
            fut = p._futures['a']
            try:
                await p.wait_for('a', 0.01)
                assert False, 'no timeout'
            except asyncio.TimeoutError:
                pass
            assert fut.cancelled() and 'a' not in p._futures

    asyncio.run(run())


def test_pool_clear_on_disconnect_is_reported_as_timeout():
    """bt.py clears the pool on disconnect; drivers retry/reconnect on TimeoutError."""
    async def run():
        p = FuturesPool()
        with p.acquire('a'):
            asyncio.get_running_loop().call_soon(p.clear)
            try:
                await p.wait_for('a', 5)
                assert False, 'no error'
            except asyncio.TimeoutError:
                pass

    asyncio.run(run())


def test_cancelling_the_waiting_task_propagates():
    async def run():
        p = FuturesPool()
        with p.acquire('a'):
            fut = p._futures['a']
            task = asyncio.ensure_future(p.wait_for('a', 5))
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
                assert False, 'cancellation swallowed'
            except asyncio.CancelledError:
                pass
            assert fut.cancelled() and 'a' not in p._futures

    asyncio.run(run())


def test_cancelling_a_tuple_wait_propagates():
    async def run():
        p = FuturesPool()
        with p.acquire(('a', 'b')):
            task = asyncio.ensure_future(p.wait_for(('a', 'b'), 5))
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
                assert False, 'cancellation swallowed'
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


def test_outer_wait_for_timeout_is_a_timeout():
    """asyncio.wait_for around a fetch cancels the waiter; that must end as the
    caller's TimeoutError, promptly, not after the pool's own timeout."""
    async def run():
        p = FuturesPool()
        with p.acquire('a'):
            t0 = asyncio.get_running_loop().time()
            try:
                await asyncio.wait_for(p.wait_for('a', 5), 0.02)
                assert False, 'no timeout'
            except asyncio.TimeoutError:
                pass
            assert asyncio.get_running_loop().time() - t0 < 1

    asyncio.run(run())
