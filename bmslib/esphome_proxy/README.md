# ESPHome Bluetooth-Proxy stack (`ble_stack: esphome`)

Routes BLE GATT through one or more ESPHome devices running the
[Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html)
component, so the batmon-ha addon does not need a local Bluetooth
adapter.

## How it boots

1. `addon_main.sh` sees `ble_stack: esphome`, picks `/app/venv_esphome`
   (separate venv: `habluetooth` requires `bleak>=3.0.2` which conflicts
   with the `bleak==2.0.0` pin in the default `venv` — see issue #275).
   No `PYTHONPATH` shadow is set; this stack does not replace `bleak`
   wholesale.
2. `main.py` calls `install_bleak_shim()` *before* importing
   `bmslib.bt`. The shim monkey-patches `bleak.BleakClient` and
   `bleak.BleakScanner` to `habluetooth.HaBleakClientWrapper` and
   `HaBleakScannerWrapper`, then constructs a `BluetoothManager` and
   registers it with `habluetooth.set_manager`. From then on, every
   `from bleak import ...` inside the addon (and inside `aiobmsble`)
   resolves to the wrappers.
3. Inside the asyncio loop, `start_proxies(proxies)` awaits
   `manager.async_setup()` then, for each configured proxy, constructs
   a `bleak_esphome.APIConnectionManager({"address": host, "noise_psk":
   psk})` and awaits its `start()`. APIConnectionManager wraps
   `APIClient` + `ReconnectLogic` + `connect_scanner` +
   `async_register_scanner` internally, so reconnect-on-WiFi-blip is
   handled for free.
4. BMS connect paths (`bmslib/bt.py`, `bmslib/models/BLE_BMS_wrap.py`)
   then `BleakClient(addr_or_device).connect()` as normal. The
   `HaBleakClientWrapper` asks the manager for the best-RSSI
   connectable scanner that has heard `addr` recently and routes the
   GATT calls through that proxy's `APIClient`.

## Configuration

```yaml
ble_stack: esphome
bluetooth_proxies:
  - host: 192.168.1.42
    noise_psk: "base64-encoded-encryption-key"   # if Noise-encrypted (default in ESPHome)
    name: "garage-proxy"                          # diagnostic label only
  - host: 192.168.1.43
    noise_psk: "another-key"
```

The proxy needs ESPHome firmware with **active connections** enabled
(`bluetooth_proxy.active: true`), otherwise scans work but GATT does
not. `APIConnectionManager` always dials port 6053 (the ESPHome
default) and does not use the legacy API password; only `noise_psk` is
respected.

## Dependencies (installed best-effort in Dockerfile)

In a dedicated `venv_esphome`:

- `bleak >= 3.0.2`
- `habluetooth` (BluetoothManager + bleak wrappers)
- `bleak-esphome` (ESPHomeClient/Scanner + APIConnectionManager)
- `aioesphomeapi` (ESPHome transport)
- `bluetooth-data-tools < 1.29` (upstream regression — 1.29.x ships
  only x86_64 wheels)

If `venv_esphome` is missing or importing one of those packages fails,
`addon_main.sh` logs a warning and falls back to plain `bleak` (stock
BlueZ stack via the regular `venv`).

## Status — not in this scaffold

- **mDNS discovery of proxies.** Explicit `bluetooth_proxies:` list
  required.
- **Connection-slot accounting.** ESP32 holds ~3 active BLE
  connections. With more BMSes than slots on one proxy, some will
  fail. Mitigation today: add a second proxy near each cluster of
  BMSes and let `habluetooth` pick by RSSI.
- **Pairing (`pin:`).** ESPHome BT proxy added `bluetooth_device_pair`
  in recent firmwares but `bleak-esphome`'s `pair()` path has not been
  exercised here. For PIN-pairing BMSes (JK with PSK, etc.), prefer
  `ble_stack: bleak` until this is verified.
- **`bt_diagnostics`/`bt_discovery` reporting.** Will surface devices
  the proxy has heard, but the "adapter" column will show the proxy
  source MAC rather than a local `hciN`. Expected.
- **armv7 image.** `cryptography`/`dbus-fast`/`bleak-esphome` lack
  musl-armv7 wheels — building from sdist needs Rust (~400 MB). We
  don't pull that toolchain in; on armv7 `venv_esphome` typically
  won't build and the addon falls back to bleak. Aarch64 and x86_64
  install cleanly.
- **Tests.** None yet — first validation is to run against a real proxy.

## Validation plan (next pass)

1. Deploy with one proxy (`ble_stack: esphome`, single
   `bluetooth_proxies:` entry).
2. Configure one non-PSK BMS (e.g. Daly, SOK).
3. Watch addon log for `esphome_proxy: <name> up at <host>` and a
   subsequent BMS `connected` line.
4. Check sample latency vs. local `bleak` baseline — expect +5–20ms
   per GATT round-trip.
5. Pull the proxy's power; confirm addon logs reflect lost samples
   followed by automatic recovery (APIConnectionManager reconnects).
