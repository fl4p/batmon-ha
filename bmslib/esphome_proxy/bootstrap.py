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
aioesphomeapi ReconnectLogic). Scanner registration with habluetooth is
handled by APIConnectionManager internally.

If any dependency is missing (`habluetooth`, `bleak_esphome`,
`aioesphomeapi`), install_bleak_shim() logs a warning and returns False so
the addon can fall back to plain bleak rather than crash.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List

logger = logging.getLogger(__name__)

# Populated by install_bleak_shim() and consumed by start_proxies().
_manager: Any = None
_conns: List[Any] = []


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

    _manager = BluetoothManager()
    set_manager(_manager)
    logger.info(
        "esphome_proxy: bleak shim installed (BleakClient/BleakScanner -> "
        "habluetooth wrappers)"
    )
    return True


async def start_proxies(proxies: Iterable[dict]) -> None:
    """Start the BluetoothManager and bring up an APIConnectionManager per proxy.

    `proxies` is an iterable of dicts with keys: host (str), noise_psk
    (str|None), name (str|None — diagnostic label only).

    Per-proxy `start()` failures are logged and skipped; the rest are
    brought up. APIConnectionManager keeps reconnecting on its own once
    started, so transient WiFi blips are recovered without addon restart.
    """
    if _manager is None:
        logger.warning("esphome_proxy: start_proxies() called without "
                       "install_bleak_shim(); nothing to do")
        return

    from bleak_esphome import APIConnectionManager, ESPHomeStartAborted

    await _manager.async_setup()

    for proxy in proxies:
        host = proxy.get("host")
        if not host:
            logger.warning("esphome_proxy: proxy entry missing host: %r", proxy)
            continue
        label = proxy.get("name") or host
        config = {"address": host, "noise_psk": proxy.get("noise_psk")}

        conn = APIConnectionManager(config)
        try:
            await conn.start()
        except ESPHomeStartAborted:
            logger.warning("esphome_proxy: %s: start aborted", label)
            continue
        except Exception as exc:
            logger.error("esphome_proxy: %s: start failed: %s", label, exc)
            continue

        _conns.append(conn)
        logger.info("esphome_proxy: %s up at %s", label, host)

    if not _conns:
        logger.warning("esphome_proxy: no proxies connected; "
                       "BleakScanner will return no devices")


async def stop_proxies() -> None:
    """Best-effort teardown for clean shutdown."""
    for conn in _conns:
        try:
            await conn.stop()
        except Exception:
            pass
    _conns.clear()
    if _manager is not None:
        try:
            await _manager.async_stop()
        except Exception:
            pass
