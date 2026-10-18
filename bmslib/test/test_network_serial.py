"""`adapter: tcp://host:port` for network serial servers (#422).

The gateway in #422 (esp-ha-usb-gateway) bridges a USB serial adapter to raw
TCP, like ser2net. Before, the wrapper split the adapter on ':' and tried to
open a file named after the TCP port ("could not open port 8880").
"""
import asyncio
import socket
import threading
import time

import pytest

import bmslib.wired as wired
from bmslib.store import _user_config_apply_global_adapter
from bmslib.wired.transport import serial_url


@pytest.fixture(autouse=True)
def _clean_ports():
    yield
    wired._reset_shared_ports()


def test_serial_url_mapping():
    assert serial_url('tcp://192.168.1.108:8880') == 'socket://192.168.1.108:8880'
    assert serial_url('TCP://gw:8880') == 'socket://gw:8880'
    assert serial_url('socket://gw:8880') == 'socket://gw:8880'
    assert serial_url('rfc2217://gw:2217') == 'rfc2217://gw:2217'
    assert serial_url('/dev/ttyUSB0') is None
    assert serial_url('COM3') is None
    with pytest.raises(ValueError):
        serial_url('udp://gw:8880')


def test_legacy_colon_prefix_still_strips():
    w = wired.SerialBleakClientWrapper('serial:/dev/ttyUSB0', baudrate=9600)
    assert w.port.t.port == '/dev/ttyUSB0'


def test_url_is_a_port_for_the_global_adapter():
    conf = dict(adapter='tcp://gw:8880',
                devices=[dict(address='serial', type='jk_uart'),
                         dict(address='AA:BB:CC:DD:EE:FF', type='jk')])
    _user_config_apply_global_adapter(conf)
    assert conf['devices'][0]['adapter'] == 'tcp://gw:8880'
    assert 'adapter' not in conf['devices'][1]


class _EchoServer:
    """Answers every request with b'reply:' + request, like a BMS behind a gateway."""

    def __init__(self, port=0):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', port))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.conns = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn):
        while True:
            try:
                data = conn.recv(1024)
            except OSError:
                return
            if not data:
                return
            self.received.append(data)
            conn.sendall(b'reply:' + data)

    def drop_clients(self):
        for c in self.conns:
            c.shutdown(socket.SHUT_RDWR)
            c.close()
        self.conns.clear()

    def close(self):
        self.sock.close()


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wrapper(port, **kw):
    return wired.SerialBleakClientWrapper('tcp://127.0.0.1:%d' % port, baudrate=9600,
                                          eol=None, timeout=0.2, **kw)


async def _wait_reader_dead(w):
    for _ in range(200):
        if w.rx_thread_error is not None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError('reader never gave up')


@pytest.fixture
def fast_giveup(monkeypatch):
    monkeypatch.setattr(wired._SharedSerialPort, 'RX_MAX_ERRORS', 2)
    monkeypatch.setattr(wired._SharedSerialPort, 'RX_ERROR_SLEEP', 0.01)


async def _roundtrip(w, payload):
    """Raw mode hands over whatever is buffered, so a reply can arrive in pieces."""
    want = len(b'reply:' + payload)
    buf = bytearray()
    done = asyncio.Event()

    def on_rx(_sender, data):
        buf.extend(data)
        if len(buf) >= want:
            done.set()

    await w.start_notify('rx', on_rx)
    await w.write_gatt_char('rx', payload)
    try:
        await asyncio.wait_for(done.wait(), 3)
        return bytes(buf)
    finally:
        await w.stop_notify('rx')


def test_tcp_adapter_roundtrip_and_reconnect(fast_giveup):
    srv = _EchoServer()

    async def run():
        w = wired.SerialBleakClientWrapper('tcp://127.0.0.1:%d' % srv.port, baudrate=9600,
                                           eol=None, timeout=0.2)
        await w.connect()
        assert w.is_connected
        assert await _roundtrip(w, b'\xdd\xa5\x03') == b'reply:\xdd\xa5\x03'

        # gateway reboots: the reader gives up, sampling disconnects and reconnects
        srv.drop_clients()
        for _ in range(100):
            if w.rx_thread_error is not None:
                break
            await asyncio.sleep(0.02)
        assert w.rx_thread_error is not None
        await w.disconnect()
        await w.connect()
        assert await _roundtrip(w, b'again') == b'reply:again'
        await w.disconnect()

    try:
        asyncio.run(run())
    finally:
        srv.close()


def test_long_reply_arrives_at_once():
    """pyserial's socket:// in_waiting is 0 or 1. Reading it once per reader pass
    took 31 s for 300 bytes (one byte per 100 ms pass), so every raw-mode model
    (daly_uart, jbd_uart, jk_pb_uart, ...) timed out over tcp://."""
    srv = _EchoServer()

    async def run():
        w = _wrapper(srv.port)
        await w.connect()
        payload = bytes(range(256)) + bytes(44)
        t0 = time.monotonic()
        assert await _roundtrip(w, payload) == b'reply:' + payload
        assert time.monotonic() - t0 < 1
        await w.disconnect()

    try:
        asyncio.run(run())
    finally:
        srv.close()


def test_gateway_down_at_start_then_reboot_recovers(fast_giveup):
    """A failed first connect leaked a port reference, so the port never closed
    again and reconnects after a later gateway reboot reused the dead socket."""
    port = _free_port()

    async def run():
        w = _wrapper(port)
        with pytest.raises(Exception):
            await w.connect()           # nothing listening yet
        assert w.port.users == 0
        assert not w.is_connected

        srv = _EchoServer(port)
        try:
            await w.connect()
            assert w.port.users == 1
            assert await _roundtrip(w, b'one') == b'reply:one'

            srv.drop_clients()          # gateway reboots
            await _wait_reader_dead(w)
            await w.disconnect()
            await w.connect()
            assert await _roundtrip(w, b'two') == b'reply:two'
            await w.disconnect()
        finally:
            srv.close()

    asyncio.run(run())


def test_dead_reader_closes_port_so_shared_handles_reconnect(fast_giveup):
    """Two BMS on one gateway: after a drop neither must keep reporting a dead
    tcp:// socket as connected, and connect() alone must bring both back."""
    srv = _EchoServer()

    async def run():
        a = _wrapper(srv.port)
        b = _wrapper(srv.port)
        assert a.port is b.port
        await a.connect()
        await b.connect()

        srv.drop_clients()
        await _wait_reader_dead(a)
        assert not a.is_connected and not b.is_connected

        await a.connect()
        await b.connect()
        assert a.is_connected and b.is_connected
        assert await _roundtrip(b, b'back') == b'reply:back'
        await a.disconnect()
        await b.disconnect()

    try:
        asyncio.run(run())
    finally:
        srv.close()


def test_connect_does_not_block_the_event_loop(monkeypatch):
    """An unreachable gateway takes pyserial 5 s to give up; on the loop thread
    that stalled every other BMS and MQTT."""
    import serial

    def slow_unreachable(*a, **kw):
        time.sleep(0.5)
        raise serial.SerialException('timed out')

    monkeypatch.setattr(serial, 'serial_for_url', slow_unreachable)

    async def run():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        t = asyncio.create_task(ticker())
        with pytest.raises(serial.SerialException):
            await _wrapper(1).connect()
        t.cancel()
        assert ticks >= 10

    asyncio.run(run())


def test_url_spellings_share_one_connection():
    a = wired.SerialBleakClientWrapper('tcp://gw:8880', baudrate=9600)
    b = wired.SerialBleakClientWrapper('socket://gw:8880', baudrate=9600)
    c = wired.SerialBleakClientWrapper('TCP://gw:8880', baudrate=9600)
    assert a.port is b.port is c.port


def test_unsupported_scheme_skips_only_that_device():
    from bmslib.models import construct_bms
    dev = dict(address='serial', type='jbd_uart', alias='gw', adapter='udp://gw:8880')
    assert construct_bms(dev, False, []) is None
