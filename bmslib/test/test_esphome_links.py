"""Link RSSI through an ESPHome proxy: nimble-ble-proxy's GET /links."""
import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from bmslib.ble.esphome_proxy import links
from bmslib.link_stats import LinkStats

ADDR = '20:A1:11:02:23:45'


class _Proxy:
    """A local HTTP server standing in for the proxy's web server."""

    def __init__(self, status=200, body=None):
        self.status, self.body, self.hits = status, body, 0
        proxy = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                proxy.hits += 1
                if self.path != '/links' or proxy.status != 200:
                    self.send_response(proxy.status if self.path == '/links' else 404)
                    self.end_headers()
                    self.wfile.write(b'Nothing matches the given URI')
                    return
                data = json.dumps(proxy.body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(('127.0.0.1', 0), H)
        self.host = '127.0.0.1:%d' % self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(links, '_unsupported_until', {})


def _client(host):
    """HaBleakClientWrapper -> ESPHomeClient -> aioesphomeapi APIClient."""
    return SimpleNamespace(_backend=SimpleNamespace(_client=SimpleNamespace(connected_address=host, address='cfg')))


def _links(*entries):
    return {'slots': 2, 'ble_off': False, 'links': list(entries)}


def _link(address=ADDR, state='connected', rssi=-57, tx_power=9):
    return dict(address=address, state=state, owner=3, rssi=rssi, tx_power=tx_power)


def test_proxy_host_of_a_habluetooth_client():
    assert links.proxy_host(_client('192.168.1.231')) == '192.168.1.231'
    c = _client(None)
    c._backend._client.address = '192.168.1.231'  # not connected yet: the configured host
    assert links.proxy_host(c) == '192.168.1.231'
    assert links.proxy_host(object()) is None  # bleak/BlueZ
    assert links.proxy_host(None) is None


def test_rssi_and_tx_power_of_the_matching_link():
    p = _Proxy(body=_links(_link('AA:BB:CC:DD:EE:FF', rssi=-90), _link(ADDR.lower(), rssi=-57, tx_power=9)))
    try:
        ci = asyncio.run(links.conn_info(_client(p.host), ADDR))
    finally:
        p.close()
    assert (ci.rssi, ci.tx_power) == (-57, 9)


def test_null_values_stay_unknown():
    p = _Proxy(body=_links(_link(rssi=None, tx_power=None)))
    try:
        ci = asyncio.run(links.conn_info(_client(p.host), ADDR))
    finally:
        p.close()
    assert (ci.rssi, ci.tx_power) == (None, None)


def test_a_link_that_is_not_connected_is_an_error_not_a_value():
    p = _Proxy(body=_links(_link(state='connecting')))
    try:
        with pytest.raises(links.NoLinkInfo):
            asyncio.run(links.conn_info(_client(p.host), ADDR))
    finally:
        p.close()


def test_a_proxy_without_links_is_not_asked_again():
    p = _Proxy(status=404)  # stock ESPHome web server, or nimble < 0.2.5
    try:
        assert asyncio.run(links.conn_info(_client(p.host), ADDR)) is None
        assert asyncio.run(links.conn_info(_client(p.host), ADDR)) is None
    finally:
        p.close()
    assert p.hits == 1


def test_a_proxy_without_a_web_server_is_not_asked_again():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    host = '127.0.0.1:%d' % s.getsockname()[1]
    s.close()  # nothing listens: connection refused
    assert asyncio.run(links.conn_info(_client(host), ADDR)) is None
    assert host in links._unsupported_until


def test_a_server_error_is_raised_for_a_retry():
    p = _Proxy(status=500)
    try:
        with pytest.raises(Exception):
            asyncio.run(links.conn_info(_client(p.host), ADDR))
    finally:
        p.close()
    assert not links._unsupported_until


def test_link_stats_reads_rssi_through_the_proxy():
    p = _Proxy(body=_links(_link(rssi=-61, tx_power=3)))
    link = LinkStats('ant24')
    link.on_connect()
    try:
        asyncio.run(link.poll_conn_info(_client(p.host), ADDR))
    finally:
        p.close()
    v = link.values(connected=True)
    assert (v['link/rssi'], v['link/tx_power']) == (-61, 3)


def test_link_stats_on_a_stock_proxy_publishes_no_rssi():
    p = _Proxy(status=404)
    link = LinkStats('ant24')
    link.on_connect()
    try:
        asyncio.run(link.poll_conn_info(_client(p.host), ADDR))
    finally:
        p.close()
    assert set(link.values(connected=True)) == {'link/connects', 'link/age'}


def test_bluek_wins_over_the_proxy_path():
    async def get_conn_info():
        return SimpleNamespace(rssi=-44, tx_power=None)

    c = _client('127.0.0.1:9')
    c.get_conn_info = get_conn_info
    link = LinkStats('x')
    link.on_connect()
    asyncio.run(link.poll_conn_info(c, ADDR))
    assert link.rssi == -44
