"""#391: a connect() that raised half-way left the BMS half-initialized forever.

BtBms.connect() does more than open the link — for a JK it discovers the write and
notify characteristics, subscribes, and queries the 0x03/0x01 one-shot frames, the
latter being where `num_cells` comes from. Under proxy congestion the 0x01 reply can
time out, so connect() raises *after* the BLE link is up.

`__aenter__` then short-circuited on `keep_alive and is_connected`, so connect() was
never re-run: the link stayed up, the streaming 0x02 frames kept producing usable
samples, and the missing post-connect state was never filled in. The reporter's log
shows the result — a good sample every cycle, and next to it, forever:

    14:56:01 ERROR [sampling] JK BMS 2 error fetching voltage
      File "/app/bmslib/models/jikong.py", line 423, in fetch_voltages
        raise Exception("num_cells not set")
    14:56:01 INFO  [sampling] JK BMS 2 volt=[None] temp=[28.9, 28.5, nan, nan]

The sampler's own escape hatch ("disconnecting due to too many errors") cannot help
here: the cycle does not fail, so the error count never grows.
"""

import asyncio

from bmslib.bt import BtBms


class _Client:
    def __init__(self):
        self.is_connected = False
        self.num_connects = 0
        self.num_disconnects = 0
        self.events = []

    async def connect(self, *a, **kw):
        self.is_connected = True
        self.num_connects += 1
        self.events.append('connect')

    async def disconnect(self):
        self.is_connected = False
        self.num_disconnects += 1
        self.events.append('disconnect')


class _Bms(BtBms):
    """A BMS whose connect() opens the link and only then may fail, like the JK."""

    def __init__(self, fail_connect=False):
        self.fail_connect = fail_connect
        self.num_cells = None
        super().__init__('AA:BB:CC:DD:EE:FF', 'JK BMS 2', keep_alive=True)

    def _create_client(self, addr_or_device):
        return _Client()

    async def connect(self, timeout=20):
        await self.client.connect()
        if self.fail_connect:
            # the 0x01 query in jikong.connect(), which sets num_cells right after
            raise TimeoutError('timeout waiting for 1')
        self.num_cells = 8


async def _enter(bms):
    """One sampling cycle's `async with bms`, returning the exception if any."""
    try:
        await bms.__aenter__()
    except Exception as e:
        return e
    return None


def test_a_half_finished_connect_is_redone_not_kept():
    async def run():
        bms = _Bms(fail_connect=True)

        assert isinstance(await _enter(bms), TimeoutError)
        # the link itself came up, which is exactly why this was never noticed
        assert bms.num_cells is None

        bms.fail_connect = False
        assert await _enter(bms) is None

        return bms

    bms = asyncio.run(run())

    # pre-fix, __aenter__ returned early here and num_cells stayed None forever
    assert bms.num_cells == 8
    assert bms.client.num_connects == 2


def test_the_stale_link_is_dropped_before_reconnecting():
    async def run():
        bms = _Bms(fail_connect=True)
        await _enter(bms)
        bms.fail_connect = False
        await _enter(bms)
        return bms

    bms = asyncio.run(run())

    # re-running connect() on a link that is already up would re-subscribe a
    # characteristic that is still subscribed, so the old link has to go first
    assert bms.client.events == ['connect', 'disconnect', 'connect']


def test_a_completed_connect_is_not_repeated():
    async def run():
        bms = _Bms()
        await _enter(bms)
        await _enter(bms)
        await _enter(bms)
        return bms

    bms = asyncio.run(run())

    assert bms.client.num_connects == 1, 'keep_alive must still keep the link alive'
    assert bms.client.num_disconnects == 0


def test_an_externally_dropped_link_reconnects_cleanly():
    async def run():
        bms = _Bms()
        await _enter(bms)

        # the peripheral or the proxy drops us
        bms.client.is_connected = False
        bms._on_disconnect(bms.client)

        await _enter(bms)
        return bms

    bms = asyncio.run(run())

    assert bms.num_cells == 8
    assert bms.client.num_connects == 2
    # nothing to tear down: the link was already gone
    assert bms.client.num_disconnects == 0


def test_disconnect_invalidates_the_completed_connect():
    async def run():
        bms = _Bms()
        await _enter(bms)
        await bms.disconnect()
        assert not bms._connect_complete
        await _enter(bms)
        return bms

    bms = asyncio.run(run())
    assert bms.client.num_connects == 2
