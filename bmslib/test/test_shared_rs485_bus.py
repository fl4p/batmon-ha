"""Several BMS on ONE RS485 bus (#398).

The reporter has 5 Daly and does not want 5 USB adapters. Daly supports it —
the board number is in every request — but batmon gave each configured BMS its
own copy of the serial port, so two on one path meant two reader threads racing
for the same bytes.

The failure modes this pins down, all of which produce *plausible wrong data*
rather than an error:
  * one BMS decoding its neighbour's reply and publishing it as its own
  * the second BMS never registering its notify callback (BtBms.__aenter__
    skips connect() when is_connected, so a shared connected-flag hides it)
  * one BMS disconnecting and closing the port under its bus siblings
  * two units configured with the same board number, silently stealing each
    other's replies
"""
import asyncio

import pytest

import bmslib.wired
from bmslib.models import daly_uart as du
from bmslib.test.data import daly_uart_fixtures as fx

PORT = '/dev/ttyFAKE0'


class _FakeTransport:
    """In-memory stand-in for SerialTransport; records writes, replays reads."""
    instances = []

    def __init__(self, port, baudrate=9600, **kw):
        self.port = port
        self.baudrate = baudrate
        self.kw = kw
        self.is_open = False
        self.writes = []
        self.rx_bytes = 0
        self.opens = 0
        self.closes = 0
        _FakeTransport.instances.append(self)

    def open(self):
        self.opens += 1
        self.is_open = True

    def close(self):
        self.closes += 1
        self.is_open = False

    def write(self, data):
        self.writes.append(bytes(data))

    def read(self):
        return None  # tests push bytes in directly via the port's fan-out


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _FakeTransport.instances = []
    bmslib.wired._reset_shared_ports()
    monkeypatch.setattr(bmslib.wired, 'SerialTransport', _FakeTransport)
    yield
    bmslib.wired._reset_shared_ports()


def _bms(board, port=PORT, name=None):
    return du.DalyUart('serial', name=name or ('bat%d' % board), adapter=port,
                       type_spec=str(board))


def _reply(board, cmd=0x93, payload=None):
    f = bytearray([0xA5, board, cmd, 0x08])
    f.extend(payload if payload is not None else bytes(range(8)))
    f.append(sum(f) & 0xFF)
    return bytes(f)


def _deliver(bms, data):
    """Push bytes onto the shared bus, as the reader thread would."""
    for _owner, cb in list(bms.client.port.callback.values()):
        cb(None, data)


# === the port is shared, not duplicated =====================================

def test_two_bms_on_one_path_share_one_transport():
    a, b = _bms(1), _bms(2)
    assert a.client.port is b.client.port
    assert len(_FakeTransport.instances) == 1, 'the port was opened twice'


def test_different_paths_do_not_share():
    a, b = _bms(1), _bms(1, port='/dev/ttyFAKE1')
    assert a.client.port is not b.client.port


def test_incompatible_link_settings_are_rejected():
    """jk_uart (115200) and daly_uart (9600) on one adapter is physically
    impossible. Say so, instead of opening the device twice."""
    from bmslib.models.jikong_uart import JKUart

    _bms(1)
    with pytest.raises(bmslib.wired.SharedSerialPortError) as e:
        JKUart('serial', name='jk', adapter=PORT)
    assert '9600' in str(e.value) and '115200' in str(e.value)


def test_incompatible_settings_skip_only_that_device(monkeypatch, caplog):
    """...and that error must stay contained to the offending device."""
    from bmslib.models import construct_bms

    _bms(1)
    dev = dict(address='serial', adapter=PORT, type='jk_uart', alias='jk')
    with caplog.at_level('ERROR'):
        assert construct_bms(dev, False, []) is None


# === per-BMS connected state ================================================

def test_second_bms_still_connects_after_the_first():
    """BtBms.__aenter__ returns early when is_connected, so a shared flag would
    leave the second BMS never calling start_notify -- connected, and deaf."""
    a, b = _bms(1), _bms(2)
    asyncio.run(a.client.connect())
    assert a.client.is_connected
    assert not b.client.is_connected, 'B looks connected without ever connecting'
    asyncio.run(b.client.connect())
    assert b.client.is_connected
    assert _FakeTransport.instances[0].opens == 1, 'port opened twice'


def test_disconnect_does_not_close_the_port_under_a_sibling():
    a, b = _bms(1), _bms(2)
    asyncio.run(a.client.connect())
    asyncio.run(b.client.connect())
    t = _FakeTransport.instances[0]

    asyncio.run(a.client.disconnect())
    assert t.is_open, 'A disconnecting took the bus down for B'
    assert b.client.is_connected

    asyncio.run(b.client.disconnect())
    assert not t.is_open, 'port never closed after the last user left'


def test_reconnect_after_full_release_reopens():
    a = _bms(1)
    asyncio.run(a.client.connect())
    asyncio.run(a.client.disconnect())
    asyncio.run(a.client.connect())
    assert a.client.is_connected
    assert _FakeTransport.instances[0].opens == 2


# === reply routing ==========================================================

def _connect_both():
    a, b = _bms(1), _bms(2)
    for x in (a, b):
        asyncio.run(x.connect())
    return a, b


def test_each_bms_decodes_only_its_own_board():
    """The dangerous one: without filtering, battery1 publishes battery2's
    voltages. That is wrong data that looks entirely healthy."""
    a, b = _connect_both()
    _deliver(a, _reply(2, 0x93))

    assert a._uart_stats.get('other_board') == 1, "A did not skip B's frame"
    assert a._uart_stats.get('frames') == 1     # it was a valid frame, just not ours
    assert b._last_response is not None, 'B never got its own reply'
    assert a._last_response is None, 'A decoded a frame addressed to board 2'


def test_both_bms_get_their_own_replies_from_one_stream():
    a, b = _connect_both()
    _deliver(a, _reply(1, 0x93, bytes([1] * 8)) + _reply(2, 0x93, bytes([2] * 8)))
    assert a._last_response == bytes([1] * 8)
    assert b._last_response == bytes([2] * 8)


def test_shared_bus_does_not_warn_about_the_other_board(caplog):
    """On a shared bus a foreign board number is normal traffic, not a
    misconfiguration -- warning once per sibling would be pure noise."""
    a, _b = _connect_both()
    with caplog.at_level('WARNING'):
        _deliver(a, _reply(2))
    assert 'answered' not in caplog.text


def test_exclusive_port_still_warns_and_accepts(caplog):
    """...but with only one BMS on the port, a mismatch is still the 'wrong
    board configured' hint, and the frame is still accepted so the add-on works
    while the user fixes it."""
    a = _bms(1)
    asyncio.run(a.connect())
    with caplog.at_level('WARNING'):
        _deliver(a, _reply(3))
    assert 'board 3 answered' in caplog.text
    assert a._last_response is not None


# === misconfiguration =======================================================

def test_duplicate_board_number_on_one_bus_is_rejected():
    """Two units answering to board 2 would steal each other's replies and
    publish each other's data. Fail loudly at connect."""
    a, b = _bms(2), _bms(2, name='dup')
    asyncio.run(a.connect())
    with pytest.raises(bmslib.wired.SharedSerialPortError) as e:
        asyncio.run(b.connect())
    assert 'daly_uart:<board>' in str(e.value)


def test_same_board_on_different_ports_is_fine():
    a = _bms(2)
    b = _bms(2, port='/dev/ttyFAKE1')
    asyncio.run(a.connect())
    asyncio.run(b.connect())  # must not raise


def test_reconnect_does_not_self_collide():
    """A BMS reconnecting re-registers the same key; that is itself, not a
    duplicate, and must not raise."""
    a = _bms(2)
    asyncio.run(a.connect())
    asyncio.run(a.connect())
    asyncio.run(a.disconnect())
    asyncio.run(a.connect())


# === bus arbitration ========================================================

def test_requests_are_serialized_across_the_bus():
    """RS485 is half duplex: with concurrent_sampling two BMS would transmit at
    once and both replies would be lost. The lock is per port, so siblings
    exclude each other."""
    a, b = _connect_both()
    assert a.client.bus_lock() is b.client.bus_lock()

    async def scenario():
        lock = a.client.bus_lock()
        async with lock:
            # b must not be able to take the bus while a holds it
            assert lock.locked()
            got = []

            async def other():
                async with b.client.bus_lock():
                    got.append('b ran')

            task = asyncio.create_task(other())
            await asyncio.sleep(0.02)
            assert not got, 'B transmitted while A held the bus'
        await task
        assert got == ['b ran']

    asyncio.run(scenario())


def test_separate_ports_do_not_block_each_other():
    a = _bms(1)
    b = _bms(1, port='/dev/ttyFAKE1')
    assert a.client.bus_lock() is not b.client.bus_lock()


# === writes still reach the wire ============================================

def test_write_goes_to_the_shared_transport():
    a, b = _connect_both()
    asyncio.run(a.client.write_gatt_char(None, du.build_command(0x93, board=1)))
    asyncio.run(b.client.write_gatt_char(None, du.build_command(0x93, board=2)))
    t = _FakeTransport.instances[0]
    assert len(t.writes) == 2
    assert t.writes[0][1] == 0x40 and t.writes[1][1] == 0x41


# === replies must wake the event loop =======================================

def test_reply_from_the_reader_thread_resolves_promptly():
    """The reader is a plain thread; FuturesPool.set_result calls
    Future.set_result directly, which does NOT wake a sleeping loop. Decoding on
    the reader thread therefore left the loop asleep in its selector until its
    next timer -- the request's own 12 s timeout -- so every reply was correct
    but arrived 12 s late, and on a shared bus each BMS pays that in turn.

    Handing the bytes over with call_soon_threadsafe is what makes this fast;
    if that regresses, this test takes DalyBt.TIMEOUT seconds and fails.
    """
    import threading
    import time as _time

    a = _bms(4)

    async def scenario():
        await a.connect()

        def feed():
            _time.sleep(0.05)
            a.client.port._dispatch(_reply(4, 0x90, bytes([9] * 8)))

        threading.Thread(target=feed, daemon=True).start()
        t0 = _time.monotonic()
        got = await a._q(0x90)
        return got, _time.monotonic() - t0

    got, dt = asyncio.run(asyncio.wait_for(scenario(), du.DalyUart.TIMEOUT + 5))
    assert got == bytes([9] * 8)
    assert dt < 2.0, ('reply took %.1fs -- the loop was not woken, it timed out '
                      'and only then saw the result' % dt)


# === end to end over a real pty =============================================

@pytest.mark.skipif(not hasattr(__import__('os'), 'openpty'), reason='needs a pty')
def test_two_daly_on_one_real_serial_port(monkeypatch):
    """The whole stack against two simulated Daly on ONE tty: real
    SerialTransport, real reader thread, real framing and filtering.

    This is the setup the reporter is wiring up (5 units, one adapter). The
    boards are deliberately 4 and 7 -- neither is the default 1 -- and each
    answers with a distinguishable payload, so a routing mistake shows up as
    swapped data rather than as an error.
    """
    import os
    import pty
    import threading

    # undo the fake transport: this test wants the real one
    monkeypatch.setattr(bmslib.wired, 'SerialTransport',
                        bmslib.wired.transport.SerialTransport)
    bmslib.wired._reset_shared_ports()

    BOARDS = {4: 0x44, 7: 0x77}
    master, slave = pty.openpty()
    stop = threading.Event()

    def sim():
        buf = bytearray()
        while not stop.is_set():
            try:
                buf.extend(os.read(master, 256))
            except OSError:
                return
            while True:
                while buf and buf[0] != 0xA5:
                    del buf[0]
                if len(buf) < 13:
                    break
                frame = bytes(buf[:13])
                del buf[:13]
                if (sum(frame[:12]) & 0xFF) != frame[12]:
                    continue
                board = frame[1] - 0x3F          # 0x43 -> board 4
                if board not in BOARDS:
                    continue
                os.write(master, _reply(board, frame[2],
                                        bytes([BOARDS[board]] * 8)))

    threading.Thread(target=sim, daemon=True).start()
    try:
        port = os.ttyname(slave)
        a = _bms(4, port=port, name='bat4')
        b = _bms(7, port=port, name='bat7')
        assert a.client.port is b.client.port

        async def scenario():
            await a.connect()
            await b.connect()
            # interleave, the way concurrent_sampling would
            ra, rb, ra2 = await asyncio.gather(a._q(0x90), b._q(0x90), a._q(0x94))
            return ra, rb, ra2

        ra, rb, ra2 = asyncio.run(asyncio.wait_for(scenario(), 30))
    finally:
        # Release the tty before closing the pty: the reader thread holds the
        # slave open, and closing the master under it blocks.
        bmslib.wired._reset_shared_ports()
        stop.set()
        os.close(slave)
        os.close(master)

    assert ra == bytes([0x44] * 8), 'board 4 got the wrong payload: %r' % (ra,)
    assert rb == bytes([0x77] * 8), 'board 7 got the wrong payload: %r' % (rb,)
    assert ra2 == bytes([0x44] * 8)
    assert _FakeTransport.instances == [], 'fake transport leaked into the e2e test'
