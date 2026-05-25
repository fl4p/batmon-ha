"""Helpers for running BMS decoders against canned BLE frames without real I/O.

A decoder test typically:
  1. Constructs the BMS with a non-special MAC (avoids the dummy-client routing).
  2. Patches the per-instance `_q` coroutine to return a canned response.
  3. Runs `fetch()` (or model-specific decode entry points) through ``asyncio.run``.

The constructor accepts the address kwarg, but never calls `connect()`, so no
BLE I/O ever happens.
"""

import asyncio


def run_fetch_with_response(bms, response_bytes):
    """Patch the BMS instance's ``_q`` to return ``response_bytes`` for any cmd.

    Returns the awaited ``BmsSample`` from ``bms.fetch()``.
    """
    async def fake_q(*args, **kwargs):
        return response_bytes

    bms._q = fake_q
    return asyncio.run(bms.fetch())


def run_fetch_with_responses(bms, response_map):
    """Patch ``_q`` so each command gets a specific response.

    ``response_map``: ``{cmd_byte: bytes}``. Falls back to the first entry for
    unknown commands so callers don't need to enumerate all possibilities.
    """
    fallback = next(iter(response_map.values()))

    async def fake_q(cmd, *args, **kwargs):
        return response_map.get(cmd, fallback)

    bms._q = fake_q
    return asyncio.run(bms.fetch())
