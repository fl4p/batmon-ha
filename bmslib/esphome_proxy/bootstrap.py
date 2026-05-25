"""Bring up habluetooth + ESPHome Bluetooth Proxy backends.

Two-step contract for the caller (main.py):

  1. install_bleak_shim()   - synchronous; called BEFORE `import bmslib.bt`
                              so that `from bleak import BleakClient,
                              BleakScanner` inside bt.py resolves to the
                              habluetooth wrappers.
  2. await start_manager(proxies)
                            - asynchronous; called inside the asyncio loop.
                              Brings up the habluetooth manager, opens an
                              APIClient per proxy and registers each
                              proxy's scanner with the manager.

If any dependency is missing (`habluetooth`, `bleak_esphome`,
`aioesphomeapi`), install_bleak_shim() logs a warning and returns False so
the addon can fall back to plain bleak rather than crash.

What's deliberately NOT here yet (see README.md):
  - reconnect / backoff on a proxy losing its WiFi link
  - mDNS discovery of proxies (require explicit list for now)
  - per-device routing override (manager picks best-RSSI connectable proxy)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Populated by install_bleak_shim() and consumed by start_manager().
_manager: Any = None
_clients: List[Any] = []


def install_bleak_shim() -> bool:
    """Patch `bleak.BleakClient`/`BleakScanner` to habluetooth wrappers.

    MUST run before any module imports those symbols by name, otherwise the
    bound reference still points at the stock backend. Returns True on
    success, False if a dependency is missing (caller should treat that as
    "fall back to plain bleak").
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


async def start_manager(proxies: Iterable[dict]) -> None:
    """Start the BluetoothManager and connect each configured proxy.

    `proxies` is an iterable of dicts with keys: host (str), port (int,
    default 6053), noise_psk (str|None), password (str|None), name (str|
    None — diagnostic label only).

    Survives per-proxy failures: a proxy that can't be reached is logged
    and skipped; the rest are brought up. Aborts cleanly if the shim was
    not installed.
    """
    if _manager is None:
        logger.warning("esphome_proxy: start_manager() called without "
                       "install_bleak_shim(); nothing to do")
        return

    from aioesphomeapi import APIClient
    from bleak_esphome import connect_scanner
    from bleak_esphome.backend.cache import ESPHomeBluetoothCache

    await _manager.async_setup()
    cache = ESPHomeBluetoothCache()

    for proxy in proxies:
        host = proxy.get("host")
        if not host:
            logger.warning("esphome_proxy: proxy entry missing host: %r",
                           proxy)
            continue
        port = int(proxy.get("port", 6053))
        label = proxy.get("name") or host

        cli = APIClient(
            address=host,
            port=port,
            password=proxy.get("password"),
            noise_psk=proxy.get("noise_psk"),
            client_info="batmon-ha",
        )

        try:
            await cli.connect(login=True)
            device_info = await cli.device_info()
        except Exception as exc:
            logger.error("esphome_proxy: %s: connect failed: %s", label, exc)
            continue

        client_data = connect_scanner(
            cli=cli,
            device_info=device_info,
            cache=cache,
            available=True,
        )
        scanner = client_data.scanner
        await scanner.async_setup()
        _manager.async_register_scanner(scanner, connectable=True)

        _clients.append(cli)
        logger.info(
            "esphome_proxy: registered %s (%s) feature_flags=0x%x",
            label, host,
            device_info.bluetooth_proxy_feature_flags_compat(cli.api_version),
        )

    if not _clients:
        logger.warning("esphome_proxy: no proxies connected; "
                       "BleakScanner will return no devices")


async def stop_manager() -> None:
    """Best-effort teardown for clean shutdown."""
    for cli in _clients:
        try:
            await cli.disconnect()
        except Exception:
            pass
    _clients.clear()
    if _manager is not None:
        try:
            await _manager.async_stop()
        except Exception:
            pass
