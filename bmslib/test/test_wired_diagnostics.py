"""The wired path must be able to say WHY nothing decoded (#398).

"timeout awaiting result for cmd=0x93, got 0/1 responses" was emitted for two
completely different failures -- no bytes on the wire at all (wiring, adapter,
wrong port) and plenty of bytes that don't frame (wrong baud, wrong board
number) -- which is why #398 needed two rounds of guessing. These tests pin the
diagnostics that separate them, and check the reader thread reports its own
death instead of leaving every command to time out forever.
"""
import time

import pytest

import bmslib.wired
from bmslib.models import daly_uart as du
from bmslib.models.daly import daly_command_message
from bmslib.test.data import daly_uart_fixtures as fx


# === feed_buffer accounting =================================================

def test_feed_buffer_counts_good_frames():
    stats = {}
    frame = fx.STATUS_CHARGING["frame"]
    out = du.feed_buffer(bytearray(), frame, stats)
    assert out == frame
    assert stats == dict(rx=13, frames=1)


def test_feed_buffer_counts_bad_crc():
    """A frame dropped for a bad checksum must be counted, not vanish. Silent
    drops are what made 'wrong baud' indistinguishable from 'dead link'."""
    stats = {}
    frame = bytearray(fx.STATUS_CHARGING["frame"])
    frame[12] ^= 0xFF
    assert du.feed_buffer(bytearray(), bytes(frame), stats) == b''
    assert stats.get('bad_crc') == 1
    assert stats.get('frames', 0) == 0
    assert stats['rx'] == 13


def test_feed_buffer_counts_resync_skips():
    """Leading garbage is skipped to resync -- and reported."""
    stats = {}
    noise = b'\x01\x02\x03\x04'
    frame = fx.STATUS_CHARGING["frame"]
    assert du.feed_buffer(bytearray(), noise + frame, stats) == frame
    assert stats['skipped'] == 4
    assert stats['frames'] == 1
    assert stats['rx'] == 4 + 13


def test_feed_buffer_counts_pure_garbage():
    """The 'bytes arrive but nothing parses' case: rx > 0 and frames == 0."""
    stats = {}
    assert du.feed_buffer(bytearray(), bytes(range(0x60, 0x80)), stats) == b''
    assert stats['rx'] == 32
    assert stats.get('frames', 0) == 0
    assert stats['skipped'] == 32


def test_feed_buffer_stats_are_optional():
    """Existing callers pass no stats dict and must keep working."""
    frame = fx.STATUS_CHARGING["frame"]
    assert du.feed_buffer(bytearray(), frame) == frame


# === timeout context ========================================================

class _NullWrapper:
    def __init__(self, address, **kwargs):
        self.address = address
        self.services = []
        self.rx_thread_error = None
        self.rx_bytes = 0


@pytest.fixture
def uart_bms(monkeypatch):
    monkeypatch.setattr(bmslib.wired, 'SerialBleakClientWrapper', _NullWrapper)
    return lambda **kw: du.DalyUart('serial', name='t', adapter='/dev/ttyUSB0', **kw)


def test_timeout_context_reports_a_silent_link(uart_bms):
    ctx = uart_bms()._q_timeout_context()
    assert '0 bytes received' in ctx
    assert 'wiring' in ctx
    assert '/dev/ttyUSB0' in ctx
    assert 'board=1' in ctx


def test_timeout_context_distinguishes_garbage_from_silence(uart_bms):
    """Bytes arriving but not framing must NOT read as a dead link -- that would
    send the user off checking cables when the baud rate is the problem."""
    bms = uart_bms(type_spec='2')
    bms._wrap_notify(None, bytes(range(0x60, 0x80)))
    ctx = bms._q_timeout_context()
    assert '32 bytes received' in ctx
    assert '0 valid frames' in ctx
    assert 'baud' in ctx
    assert 'board=2' in ctx
    assert '0 bytes received' not in ctx


def test_timeout_context_survives_a_good_frame(uart_bms):
    """Sanity: after a decodable frame the context reports it as valid, so the
    counter cannot be read as evidence of breakage."""
    bms = uart_bms()
    bms._wrap_notify(None, fx.STATUS_CHARGING["frame"])
    ctx = bms._q_timeout_context()
    assert '1 valid frames' in ctx


def test_timeout_context_reports_a_dead_reader_thread_first(uart_bms):
    """A dead reader thread freezes _uart_stats, so the byte counts would keep
    asserting a confident-but-stale diagnosis forever. The thread's death has to
    outrank them, and the staleness has to be admitted."""
    bms = uart_bms()
    bms._wrap_notify(None, fx.STATUS_CHARGING["frame"])  # stats say "1 valid frame"
    bms.client.rx_thread_error = OSError('adapter went away')
    ctx = bms._q_timeout_context()
    assert 'reader thread died' in ctx
    assert 'adapter went away' in ctx
    assert 'stale' in ctx
    # must NOT still be advertising the frozen counts as a live diagnosis
    assert 'bytes are arriving' not in ctx


def test_timeout_context_uses_the_port_level_counter(uart_bms):
    """Bytes that arrived before the notify callback was registered are invisible
    to feed_buffer but visible to the transport. Reporting only the callback's
    view would call such a link dead."""
    bms = uart_bms()
    bms.client.rx_bytes = 26
    ctx = bms._q_timeout_context()
    assert '0 bytes received' not in ctx
    assert '26 bytes received' in ctx
    assert '0 reached the decoder' in ctx


def test_wrap_notify_warns_when_another_board_answers(uart_bms, caplog):
    """Addressing board 1 while board 3 replies is the 'wrong board number'
    case. It must be called out, because the data still decodes and the setup
    would otherwise look healthy."""
    bms = uart_bms()
    frame = bytearray(fx.STATUS_CHARGING["frame"])
    frame[1] = 3
    frame[12] = sum(frame[:12]) & 0xFF
    with caplog.at_level('WARNING'):
        bms._wrap_notify(None, bytes(frame))
    assert 'board 3 answered' in caplog.text
    assert 'daly_uart:3' in caplog.text


def test_wrap_notify_does_not_warn_on_the_addressed_board(uart_bms, caplog):
    bms = uart_bms()
    frame = bytearray(fx.STATUS_CHARGING["frame"])
    frame[1] = 1
    frame[12] = sum(frame[:12]) & 0xFF
    with caplog.at_level('WARNING'):
        bms._wrap_notify(None, bytes(frame))
    assert 'answered' not in caplog.text


# === reader thread =========================================================

class _FlakyTransport:
    """Transport whose read() raises. Models a USB adapter that disappears."""

    def __init__(self, *a, errors=10**9, **kw):
        self.port = '/dev/fake'
        self.is_open = True
        self.reads = 0
        self._errors = errors

    def read(self):
        self.reads += 1
        if self.reads <= self._errors:
            raise OSError('read failed')
        return b'\xa5'

    def open(self):
        pass


def _wrapper_with(monkeypatch, transport_factory, max_errors=3):
    monkeypatch.setattr(bmslib.wired, 'SerialTransport', transport_factory)
    monkeypatch.setattr(bmslib.wired.SerialBleakClientWrapper, 'RX_MAX_ERRORS', max_errors)
    monkeypatch.setattr(bmslib.wired.SerialBleakClientWrapper, 'RX_ERROR_SLEEP', 0.001)
    return bmslib.wired.SerialBleakClientWrapper('/dev/fake')


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_reader_thread_records_permanent_failure(monkeypatch):
    """A dying reader thread used to be invisible: the port still reported
    connected and every command timed out identically, forever."""
    w = _wrapper_with(monkeypatch, _FlakyTransport)
    assert _wait_until(lambda: w.rx_thread_error is not None), 'error never recorded'
    assert isinstance(w.rx_thread_error, OSError)
    assert _wait_until(lambda: not w._rx_thread.is_alive())


def test_reader_thread_rides_out_transient_errors(monkeypatch):
    """...but a couple of failed reads (USB re-enumeration) must not kill it."""
    got = []
    w = _wrapper_with(monkeypatch, lambda *a, **kw: _FlakyTransport(errors=2), max_errors=5)
    w.callback['x'] = lambda _s, data: got.append(data)
    assert _wait_until(lambda: got), 'reader gave up on a transient error'
    assert w.rx_thread_error is None


def test_reader_thread_is_respawned_on_reconnect(monkeypatch):
    """Recording the death is useless if nothing acts on it: the wrapper is built
    once per BtBms and never replaced, so without a respawn a transient USB
    dropout left that battery dark until the add-on process restarted."""
    import asyncio

    w = _wrapper_with(monkeypatch, lambda *a, **kw: _FlakyTransport(errors=3))
    assert _wait_until(lambda: w.rx_thread_error is not None)
    assert _wait_until(lambda: not w._rx_thread.is_alive())

    got = []
    w.callback['x'] = lambda _s, data: got.append(data)
    asyncio.run(w.connect())

    assert w.rx_thread_error is None, 'stale error survived the reconnect'
    assert _wait_until(lambda: got), 'reader thread was not respawned'


def test_reader_thread_survives_a_raising_callback(monkeypatch):
    """One BMS's decoder blowing up must not take the shared reader down."""
    calls = []

    def boom(_s, _data):
        calls.append(1)
        raise ValueError('decoder bug')

    w = _wrapper_with(monkeypatch, lambda *a, **kw: _FlakyTransport(errors=0))
    w.callback['x'] = boom
    assert _wait_until(lambda: len(calls) >= 3)
    assert w.rx_thread_error is None
    assert w._rx_thread.is_alive()


# === one bad device must not take down the others ===========================

@pytest.mark.parametrize('bad_type', ['daly_uart:0', 'daly_uart:abc', 'daly_uart:99'])
def test_bad_board_number_skips_only_that_device(monkeypatch, bad_type, caplog):
    """`type: daly_uart:0` is a plausible typo (the board number is 1-based).
    It used to raise out of main()'s device loop and abort the whole add-on, so
    every other configured battery never started. construct_bms already skips
    unknown types with a warning; a rejecting constructor must do the same."""
    from bmslib.models import construct_bms

    monkeypatch.setattr(bmslib.wired, 'SerialBleakClientWrapper', _NullWrapper)
    dev = dict(address='serial', adapter='/dev/ttyUSB0', type=bad_type, alias='bad')
    with caplog.at_level('ERROR'):
        assert construct_bms(dev, False, []) is None
    assert 'skipping' in caplog.text.lower()


def test_good_board_number_still_constructs(monkeypatch):
    """Calibration for the above: the skip path must not swallow valid configs."""
    from bmslib.models import construct_bms

    monkeypatch.setattr(bmslib.wired, 'SerialBleakClientWrapper', _NullWrapper)
    dev = dict(address='serial', adapter='/dev/ttyUSB0', type='daly_uart:2', alias='ok')
    bms = construct_bms(dev, False, [])
    assert bms is not None and bms.board == 2


# === the standalone probe tool must not drift from the real builder =========

def test_probe_tool_frames_match_the_library_builder():
    """tools/daly_serial_probe.py inlines the frame builder so it can run with
    nothing but pyserial. That copy must stay byte-identical to the real one, or
    the tool would clear a setup the add-on then fails on."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / 'tools' / 'daly_serial_probe.py'
    spec = importlib.util.spec_from_file_location('daly_serial_probe', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for board in (1, 2, 8):
        for cmd in (0x90, 0x93, 0x94, 0x95):
            for fill in (0x00, 0xAA):
                assert mod.build_request(cmd, mod.BOARD1_ADDR + board - 1, fill) == \
                       bytes(daly_command_message(cmd, addr_byte=0x3F + board, fill=fill)), \
                       f'probe tool drifted: cmd={cmd:#02x} board={board} fill={fill:#02x}'
    # and the tool's default sweep must cover what the add-on actually sends
    assert du.UART_FILL in (0x00, 0xAA)
    assert mod.BOARD1_ADDR == 0x40
    assert du.build_command(0x90, board=1) == mod.build_request(0x90, 0x40, du.UART_FILL)


def test_probe_tool_rejects_an_echo_of_its_own_request():
    """A stuck RS485 transceiver loops our request back. It is checksummed by
    construction, so counting it as a reply would announce "BMS answers" on a bus
    with no BMS -- a false PASS on the exact fault the tool exists to find."""
    mod = _load_probe_tool()
    req = mod.build_request(0x90, 0x40, 0xAA)
    assert mod.is_echo(req, req)
    # even a *different* command echoed back is still a host-addressed frame
    other = mod.build_request(0x93, 0x42, 0x00)
    assert mod.is_echo(other, req)
    # a genuine reply carries the board NUMBER in byte 1, not the host address
    reply = bytearray([0xA5, 3, 0x90, 0x08]) + bytearray(range(8))
    reply.append(sum(reply) & 0xFF)
    assert not mod.is_echo(bytes(reply), req)
    for board in range(1, 17):
        r = bytearray([0xA5, board, 0x90, 0x08]) + bytearray(range(8))
        r.append(sum(r) & 0xFF)
        assert not mod.is_echo(bytes(r), req), f'board {board} reply misread as echo'


def _load_probe_tool():
    import importlib.util
    path = pathlib_path() / 'tools' / 'daly_serial_probe.py'
    spec = importlib.util.spec_from_file_location('daly_serial_probe', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not hasattr(__import__('os'), 'openpty'), reason='needs a pty')
@pytest.mark.parametrize('answering_board, need_fill, expect_exit', [
    (3, 0xAA, 0),     # a BMS that is NOT board 1 and needs the 0xAA fill
    (1, None, 0),     # the plain factory case
    (None, None, 1),  # nothing on the bus
    ('echo', None, 1),  # stuck RS485 transceiver: our own bytes come back
])
def test_probe_tool_end_to_end(answering_board, need_fill, expect_exit, tmp_path):
    """Drive the probe tool against a simulated Daly on a pty. This is the only
    check that the tool's verdict matches reality -- a diagnostic that has never
    been seen to fire on a known input is not a diagnostic."""
    import os
    import pty
    import subprocess
    import sys
    import threading

    master, slave = pty.openpty()
    stop = threading.Event()

    def sim():
        buf = bytearray()
        while not stop.is_set():
            try:
                chunk = os.read(master, 256)
            except OSError:
                return
            buf.extend(chunk)
            while True:
                while buf and buf[0] != 0xA5:
                    del buf[0]
                if len(buf) < 13:
                    break
                frame = bytes(buf[:13])
                del buf[:13]
                if answering_board == 'echo':
                    os.write(master, frame)  # stuck transceiver: verbatim loopback
                    continue
                if answering_board is None:
                    continue
                if frame[1] != 0x3F + answering_board:
                    continue
                if (sum(frame[:12]) & 0xFF) != frame[12]:
                    continue
                if need_fill is not None and set(frame[4:12]) != {need_fill}:
                    continue
                r = bytearray([0xA5, answering_board, frame[2], 0x08]) + bytearray(range(8))
                r.append(sum(r) & 0xFF)
                os.write(master, bytes(r))

    t = threading.Thread(target=sim, daemon=True)
    t.start()
    try:
        tool = pathlib_path() / 'tools' / 'daly_serial_probe.py'
        r = subprocess.run([sys.executable, str(tool), os.ttyname(slave),
                            '--listen', '0', '--wait', '0.08', '--boards', '1-3'],
                           capture_output=True, text=True, timeout=180)
    finally:
        stop.set()
        os.close(slave)
        os.close(master)

    assert r.returncode == expect_exit, r.stdout + r.stderr
    assert not r.stderr.strip(), r.stderr  # no traceback: the tool must not crash
    if answering_board == 'echo':
        assert 'our OWN request looped back' in r.stdout, r.stdout
        assert 'direction-control' in r.stdout, r.stdout
        assert 'BMS answers' not in r.stdout, 'false positive on a loopback!'
        assert 'configure batmon with' not in r.stdout, r.stdout
    elif answering_board:
        assert 'type: daly_uart:%d' % answering_board in r.stdout, r.stdout
        if need_fill == 0xAA:
            # a 0x00-only sweep would have found nothing here
            assert 'working fill byte(s): 0xaa' in r.stdout, r.stdout
    else:
        assert 'NO bytes received' in r.stdout, r.stdout


def pathlib_path():
    import pathlib
    return pathlib.Path(__file__).resolve().parents[2]
