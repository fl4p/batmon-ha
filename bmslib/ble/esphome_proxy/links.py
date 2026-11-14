"""Live RSSI/TX power of a BLE link that runs through an ESPHome proxy.

The ESPHome native API has no message for this. nimble-ble-proxy (0.2.5+)
serves it over HTTP: GET /links lists every link with its rssi and tx_power
(null when the controller can't tell). A stock ESPHome proxy, or an older
nimble, answers 404 or has no web server; such a proxy is not asked again for
UNSUPPORTED_RECHECK_S, and the link has no RSSI.
"""
import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Dict, Optional

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 1.5
UNSUPPORTED_RECHECK_S = 3600

_unsupported_until: Dict[str, float] = {}  # proxy host -> time.monotonic()


class NoLinkInfo(Exception):
    """The proxy answered but can't report this link."""


def proxy_host(client) -> Optional[str]:
    """The proxy host a habluetooth client is connected through, or None.

    HaBleakClientWrapper._backend is bleak_esphome's ESPHomeClient once
    connected; its _client is the aioesphomeapi APIClient of that proxy.
    Private attributes, so anything unexpected means "not a proxy link"."""
    api = getattr(getattr(client, '_backend', None), '_client', None)
    if api is None:
        return None
    host = getattr(api, 'connected_address', None) or getattr(api, 'address', None)
    return host if isinstance(host, str) and host else None


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
        return r.read()


async def conn_info(client, address: str):
    """SimpleNamespace(rssi, tx_power) for `address` from the proxy `client`
    is connected through. None when this is no proxy link or the proxy has no
    /links; raises when the proxy should know but the read failed."""
    host = proxy_host(client)
    if host is None:
        return None
    if time.monotonic() < _unsupported_until.get(host, 0):
        return None
    url = 'http://%s/links' % ('[%s]' % host if host.count(':') > 1 else host)  # bracket IPv6
    try:
        body = await asyncio.get_running_loop().run_in_executor(None, _get, url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _unsupported_until[host] = time.monotonic() + UNSUPPORTED_RECHECK_S
            logger.info('proxy %s has no /links (stock ESPHome or nimble-ble-proxy < 0.2.5): no link RSSI', host)
            return None
        raise
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):  # no web server at all
            _unsupported_until[host] = time.monotonic() + UNSUPPORTED_RECHECK_S
            logger.info('proxy %s has no web server: no link RSSI', host)
            return None
        raise
    links = json.loads(body).get('links') or []
    want = address.upper()
    for link in links:
        if str(link.get('address', '')).upper() == want and link.get('state') == 'connected':
            return SimpleNamespace(rssi=_dbm(link.get('rssi')), tx_power=_dbm(link.get('tx_power')))
    raise NoLinkInfo('proxy %s lists no connected link to %s' % (host, address))


def _dbm(v) -> Optional[int]:
    # null, or anything that isn't a plain int, is "unknown", never a number
    return v if isinstance(v, int) and not isinstance(v, bool) else None
