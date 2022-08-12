import asyncio
from typing import Dict


class FuturesPool:

    def __init__(self):
        self._futures: Dict[int, asyncio.Future] = {}

    def acquire(self, name):
        assert name not in self._futures, "already waiting for %s" % name
        fut = asyncio.Future()
        self._futures[name] = fut
        return fut

    def set_result(self, name, value):
        fut = self._futures.pop(name, None)
        if fut:
            fut.set_result(value)

    def clear(self):
        for fut in self._futures.values():
            fut.cancel()
        self._futures.clear()

    async def wait_for(self, name, timeout):
        try:
            return await asyncio.wait_for(self._futures[name], timeout)
        except asyncio.TimeoutError:
            del self._futures[name]
            raise
