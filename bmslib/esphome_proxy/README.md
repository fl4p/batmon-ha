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

### Proxy firmware should disable the GATT service cache

```yaml
bluetooth_proxy:
  active: true
  cache_services: false   # see below
```

`cache_services: true` (the ESPHome default) writes the discovered
GATT tree to ESP32 NVS and reuses it on reconnect. Discovered during
development:

- The `bluetooth_device_clear_cache` API opcode crashes the firmware
  with `LoadProhibited` on ESP32-S3 (esphome/esphome `bluetooth_proxy.cpp`
  CLEAR_CACHE handler — calls `esp_ble_gattc_cache_clean` without
  ensuring the BDA has an initialized `gattc_if`). So the only way to
  evict a stale cache is a full `esptool erase_flash`.
- For some BMSes the cached attribute table aliases handle permissions
  (notably the CCC at handle 17 of `0xffe1`), so the first CCC write
  after reconnect fails with `Insufficient authorization (8)` even
  though the BMS doesn't actually require auth.

Setting `cache_services: false` forces fresh GATT discovery on every
connect and clears the cache on disconnect. Connect time is ~100ms
slower per reconnect but reliability is dramatically better.

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

## Known incompatibility: ANT BMS

The ANT-BLE20PHUB BMS firmware mishandles ESPHome/Bluedroid-style GATT
flows: the proxy connects, GATT discovery completes, but the CCC write
for `0xffe1` (handle 17) gets no reply, times out at 8s, and surfaces
as `error 133` (`ESP_GATT_INTERNAL_ERROR`). Verified that:

- The same BMS works fine through stock `bleak` on macOS/Linux, BlueZ
  on a Raspberry Pi, and a custom micropython aioble relay.
- The difference is in the BLE host stack — Bluedroid auto-initiates
  MTU exchange to 517 right after connect; aioble doesn't. ANT
  firmware appears to silently drop ATT ops after the MTU exchange.
- We tried every workaround (cache off, fresh NVS via erase_flash,
  spoofed BT MAC, removed `pair()`, sdkconfig+runtime MTU caps) —
  none made the proxy succeed.

If you have an ANT BMS, dedicate a local Bluetooth adapter to it
(`ble_stack: bleak` or `bluek`) until upstream ESPHome adds an option
to skip MTU exchange or switches the proxy to the NimBLE host stack.

## Status — not in this scaffold

- **mDNS discovery of proxies.** Explicit `bluetooth_proxies:` list
  required.
- **Connection-slot accounting.** ESP32 holds ~3 active BLE
  connections. With more BMSes than slots on one proxy, some will
  fail. Mitigation today: add a second proxy near each cluster of
  BMSes and let `habluetooth` pick by RSSI.
- **Pairing (`pin:`/PSK passkey).** Tested via `client.pair()` but
  found it actively harmful for BMSes that don't speak SMP — leaves
  Bluedroid in "encryption pending" and breaks all subsequent ATT
  ops. The PSK callback path in `BtBms._connect_client` is still in
  place for BMSes that genuinely passkey-pair (JK with PSK, etc.) but
  has not been validated through the proxy. Prefer `ble_stack: bleak`
  for PSK-bonded BMSes until that's exercised.
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
