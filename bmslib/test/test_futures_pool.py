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
