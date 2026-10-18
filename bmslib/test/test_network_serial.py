"""`adapter: tcp://host:port` for network serial servers (#422).

The gateway in #422 (esp-ha-usb-gateway) bridges a USB serial adapter to raw
TCP, like ser2net. Before, the wrapper split the adapter on ':' and tried to
open a file named after the TCP port ("could not open port 8880").
"""
import asyncio
import socket
import threading

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

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
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


def test_tcp_adapter_roundtrip_and_reconnect(monkeypatch):
    monkeypatch.setattr(wired._SharedSerialPort, 'RX_MAX_ERRORS', 2)
    monkeypatch.setattr(wired._SharedSerialPort, 'RX_ERROR_SLEEP', 0.01)
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
