"""Bring up habluetooth + ESPHome Bluetooth Proxy backends.

Two-step contract for the caller (main.py):

  1. install_bleak_shim()      - synchronous; called BEFORE `import bmslib.bt`
                                  so that `from bleak import BleakClient,
                                  BleakScanner` inside bt.py resolves to the
                                  habluetooth wrappers.
  2. await start_proxies(...)  - asynchronous; called inside the asyncio loop.
                                  Brings up the habluetooth manager and a
                                  bleak_esphome.APIConnectionManager per proxy.

Reconnect/backoff is handled by APIConnectionManager (which owns an
aioesphomeapi ReconnectLogic), including for a proxy that is offline at
start-up: start_proxies() waits only STARTUP_WAIT_S for it (#430).
Scanner registration with habluetooth is handled by APIConnectionManager
internally.

If any dependency is missing (`habluetooth`, `bleak_esphome`,
`aioesphomeapi`), install_bleak_shim() logs a warning and returns False so
the addon can fall back to plain bleak rather than crash.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Iterable, List, Set

logger = logging.getLogger(__name__)

# Populated by install_bleak_shim() and consumed by start_proxies().
_manager: Any = None
_conns: List[Any] = []
# start() tasks of proxies that have not connected yet; strong refs keep them alive
_start_tasks: Set["asyncio.Task"] = set()


def install_bleak_shim() -> bool:
    """Patch `bleak.BleakClient`/`BleakScanner` to habluetooth wrappers.

    MUST run before any module imports those symbols by name. Returns True
    on success, False if a dependency is missing (caller should treat that
    as "fall back to plain bleak").
    """
    global _manager

    try:
        import bleak
        from habluetooth import (
            BluetoothManager,
            HaBleakClientWrapper,
            HaBleakScannerWrapper,
            set_manager,
        )
    except ImportError as exc:
        logger.warning(
            "esphome_proxy: missing dependency (%s); ble_stack=esphome will "
            "not activate, falling back to plain bleak", exc,
        )
        return False

    bleak.BleakClient = HaBleakClientWrapper  # type: ignore[assignment]
    bleak.BleakScanner = HaBleakScannerWrapper  # type: ignore[assignment]

    class _Manager(BluetoothManager):
        """Only exists to answer habluetooth's subclass hook.

        `BluetoothManager.__init__` warns "does not implement
        _discover_service_info, subclasses must implement this method to consume
        discovery data" whenever that method is still the base one -- which it is
        if you instantiate BluetoothManager directly, as we did. The hook is an
        optional per-advertisement callback for consumers that react to
        discoveries (Home Assistant's integration matching); batmon polls a
        configured address instead, so there is genuinely nothing to consume and
        the advertisement history the connect path uses is kept regardless.
        Overriding it with a no-op silences a line that read like a fault on
        every start-up (#399).
        """

        def _discover_service_info(self, service_info) -> None:
            pass

    _manager = _Manager()
    set_manager(_manager)
    logger.info(
        "esphome_proxy: bleak shim installed (BleakClient/BleakScanner -> "
        "habluetooth wrappers)"
    )
    return True


# How long start-up waits for the proxies before going on to discovery and
# polling. A reachable proxy is up in 1-2 s; one that is offline would
# otherwise hold start-up forever (#430).
STARTUP_WAIT_S = 10.0


async def _start_one(conn: Any, label: str, host: str) -> bool:
    from bleak_esphome import ESPHomeStartAborted

    try:
        # returns only after the first successful connect; until then
        # ReconnectLogic keeps retrying with backoff
        await conn.start()
    except ESPHomeStartAborted:
        logger.info("esphome_proxy: %s: start aborted", label)
        return False
    except Exception as exc:
        logger.error("esphome_proxy: %s: start failed: %s", label, exc)
        return False
    logger.info("esphome_proxy: %s up at %s", label, host)
    return True


async def start_proxies(proxies: Iterable[dict]) -> None:
    """Start the BluetoothManager and bring up an APIConnectionManager per proxy.

    `proxies` is an iterable of dicts with keys: host (str), noise_psk
    (str|None), name (str|None — diagnostic label only).

    All proxies start concurrently and this returns after at most
    STARTUP_WAIT_S seconds. `APIConnectionManager.start()` does not return
    until its first successful connect, so a proxy that is offline at boot is left connecting
    in the background: its ReconnectLogic keeps retrying and registers the
    scanner whenever the device shows up (#430). Once connected,
    APIConnectionManager also recovers from later disconnects on its own.
    """
    if _manager is None:
        logger.warning("esphome_proxy: start_proxies() called without "
                       "install_bleak_shim(); nothing to do")
        return

    from bleak_esphome import APIConnectionManager

    await _manager.async_setup()

    pending = {}
    for proxy in proxies:
        host = proxy.get("host")
        if not host:
            logger.warning("esphome_proxy: proxy entry missing host: %r", proxy)
            continue
        label = proxy.get("name") or host
        config = {"address": host, "noise_psk": proxy.get("noise_psk")}

        conn = APIConnectionManager(config)
        # registered before start so stop_proxies() also ends a proxy that never connected
        _conns.append(conn)
        task = asyncio.create_task(_start_one(conn, label, host), name="esphome_proxy:%s" % label)
        _start_tasks.add(task)
        task.add_done_callback(_start_tasks.discard)
        pending[task] = label

    if not pending:
        logger.warning("esphome_proxy: no proxies configured")
        return

    wait = STARTUP_WAIT_S
    done, not_done = await asyncio.wait(pending, timeout=wait)
    up = sum(1 for t in done if not t.cancelled() and t.exception() is None and t.result())
    for t in not_done:
        logger.warning("esphome_proxy: %s not reachable after %.0f s; continuing without it, "
                       "it keeps reconnecting in the background", pending[t], wait)
    if not up:
        logger.warning("esphome_proxy: no proxies connected yet; "
                       "BMS connections will fail until one comes up")


def proxy_sources() -> List[str] | None:
    """Best-effort list of the proxy scanners currently registered.

    Returns None when the set cannot be determined (manager not up, habluetooth
    API changed) — that is NOT the same as "no proxies", and callers must not
    report it as such. Returns [] only when the manager genuinely has none.
    """
    if _manager is None:
        return None
    try:
        scanners = _manager.async_current_scanners()
    except Exception:
        return None
    out = []
    for s in scanners:
        name = getattr(s, 'name', None) or getattr(s, 'source', None) or type(s).__name__
        out.append('%s%s' % (name, '' if getattr(s, 'scanning', True) else ' (not scanning)'))
    return out


async def stop_proxies() -> None:
    """Best-effort teardown for clean shutdown."""
    for conn in _conns:
        try:
            await conn.stop()
        except Exception:
            pass
    _conns.clear()
    for task in list(_start_tasks):
        task.cancel()
    if _manager is not None:
        try:
            # habluetooth names it async_stop() but it is a plain function (6.1 and 6.26)
            r = _manager.async_stop()
            if inspect.isawaitable(r):
                await r
        except Exception:
            pass
