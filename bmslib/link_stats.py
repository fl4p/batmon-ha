"""Link diagnostics of one BMS connection: connects since start, age of the
current link and, where the BLE stack can read them, live RSSI and TX power.

RSSI/TX power of a live link come from bluek (ble_stack: bluek, the kernel's
mgmt GET_CONN_INFO) or, on ble_stack: esphome, from nimble-ble-proxy's
GET /links (bmslib/ble/esphome_proxy/links.py). bleak over BlueZ D-Bus stops
updating RSSI once connected and the ESPHome API has no such message, so with
those the two values stay None and nothing is published, rather than a stale
advert RSSI.
"""
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

CONN_INFO_TIMEOUT = 2.0
CONN_INFO_RETRY_S = 300


class LinkStats:
    def __init__(self, name: str):
        self.name = name
        self.connects = 0
        self._t_up: Optional[float] = None  # time.monotonic() of the current link
        self._client = None  # the client object of the current link, see observe()
        self._conn_info_retry_t = 0.0  # time.monotonic() before which a failed read is not retried
        self._conn_info_warned = False  # once per link
        self.rssi: Optional[int] = None
        self.tx_power: Optional[int] = None

    def on_connect(self):
        self.connects += 1
        self._t_up = time.monotonic()
        self._conn_info_retry_t = 0.0
        self._conn_info_warned = False
        self.rssi = None
        self.tx_power = None

    def observe(self, was_connected: bool, connect_time_before, connect_time_after, client=None) -> bool:
        """Count a new link after the sampler's `async with bms`. It is new when
        - the BMS was not connected before,
        - its connect_time moved: __aenter__ rebuilds a link that survived a
          connect() which raised half-way, and was_connected was True then (#391),
        - or its client is another object than last time: aiobmsble
          (BLE_BMS_wrap) reconnects inside async_update() with a new client and
          keeps connect_time fixed."""
        new_client = self._client is not None and client is not None and client is not self._client
        if client is not None:
            self._client = client
        if not was_connected or connect_time_after != connect_time_before or new_client:
            self.on_connect()
            return True
        return False

    def age_s(self) -> Optional[float]:
        return None if self._t_up is None else time.monotonic() - self._t_up

    async def poll_conn_info(self, client, address: Optional[str] = None) -> None:
        """Read RSSI/TX power of the live link, from bluek's get_conn_info() or,
        through an ESPHome proxy, from nimble-ble-proxy's GET /links.

        A stack that can't tell leaves both None. An error (not connected, no
        CAP_NET_ADMIN, a failed HCI or HTTP read, a timeout) clears both until a
        retry CONN_INFO_RETRY_S later; it is logged as a warning once per link."""
        read = _conn_info_reader(client, address)
        if read is None:
            self.rssi = self.tx_power = None
            return
        if time.monotonic() < self._conn_info_retry_t:
            return  # still None from the failed read
        try:
            ci = await asyncio.wait_for(read(), CONN_INFO_TIMEOUT)
            rssi, tx_power = (ci.rssi, ci.tx_power) if ci is not None else (None, None)
        except Exception as e:
            self._conn_info_retry_t = time.monotonic() + CONN_INFO_RETRY_S
            self.rssi = self.tx_power = None
            (logger.debug if self._conn_info_warned else logger.warning)(
                '%s: reading link RSSI failed, retry in %d s: %s',
                self.name, CONN_INFO_RETRY_S, str(e) or type(e).__name__)
            self._conn_info_warned = True
            return
        self.rssi, self.tx_power = rssi, tx_power

    def values(self, connected: bool) -> dict:
        """Topic suffix -> value. While disconnected only the connect count, so
        the age and RSSI entities expire instead of holding the last link's."""
        v = {'link/connects': self.connects}
        age = self.age_s()
        if connected and age is not None:
            v['link/age'] = int(age)
            if self.rssi is not None:
                v['link/rssi'] = self.rssi
            if self.tx_power is not None:
                v['link/tx_power'] = self.tx_power
        return v


def _conn_info_reader(client, address):
    """A coroutine function returning the link's (rssi, tx_power) namespace, or
    None when this client's stack can't read them."""
    get = getattr(client, 'get_conn_info', None)
    if get is not None:  # bluek
        return get
    if address:
        from bmslib.ble.esphome_proxy import links
        if links.proxy_host(client) is not None:
            return lambda: links.conn_info(client, address)
    return None
