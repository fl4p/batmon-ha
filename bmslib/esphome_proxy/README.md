# ESPHome Bluetooth-Proxy stack (`ble_stack: esphome`)

Routes BLE GATT through one or more ESPHome devices running the
[Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html)
component, so the batmon-ha addon does not need a local Bluetooth adapter.

## How it boots

1. `addon_main.sh` sees `ble_stack: esphome` and logs the choice — no
   `PYTHONPATH` shadow is set (unlike `bumble`/`bluek`, this stack does
   not replace the `bleak` package wholesale).
2. `main.py` calls `install_bleak_shim()` *before* importing
   `bmslib.bt`. The shim monkey-patches `bleak.BleakClient` and
   `bleak.BleakScanner` to `habluetooth.HaBleakClientWrapper` and
   `HaBleakScannerWrapper`. From then on, every `from bleak import ...`
   inside the addon (and inside `aiobmsble`) resolves to the wrappers.
3. Inside the asyncio loop, `start_manager(proxies)` brings up the
   `habluetooth.BluetoothManager`, opens an `aioesphomeapi.APIClient`
   per configured proxy, fetches its `device_info`, and calls
   `bleak_esphome.connect_scanner(...)` which registers a connectable
   scanner per proxy with the manager.
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
    # port: 6053          # default
    noise_psk: "base64-encoded-encryption-key"   # if Noise-encrypted (default in ESPHome)
    # password: "..."     # legacy API password (mutually exclusive with noise_psk)
    name: "garage-proxy"  # diagnostic label only
  - host: 192.168.1.43
    noise_psk: "another-key"
```

The proxy needs ESPHome firmware with **active connections** enabled
(`bluetooth_proxy.active: true`), otherwise scans work but GATT does
not.

## Dependencies

Installed best-effort in the Dockerfile (`pip install aioesphomeapi
habluetooth bleak-esphome`). If any are missing at runtime, the shim
logs a warning and the addon falls back to plain `bleak`.

## Status — not in this scaffold

- **Reconnect / backoff.** A proxy losing its WiFi link is currently
  not retried. Restart the addon.
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
- **Tests.** None — this scaffold is the smallest plausible thing that
  could work; first validation is to run it against a real proxy.

## Validation plan (next pass)

1. Deploy with one proxy (`ble_stack: esphome`, single
   `bluetooth_proxies:` entry).
2. Configure one non-PSK BMS (e.g. Daly, SOK).
3. Watch addon log for `esphome_proxy: registered <name>` and a
   subsequent BMS `connected` line.
4. Check sample latency vs. local `bleak` baseline — expect +5–20ms
   per GATT round-trip.
5. Pull the proxy's power; confirm addon logs reflect lost samples
   (and decide whether explicit reconnect is needed).
