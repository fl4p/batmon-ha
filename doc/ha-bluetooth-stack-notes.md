# Home Assistant Bluetooth Stack — Notes

## 1. How does Home Assistant Core access Bluetooth devices? Is it through `home-assistant-bluetooth==2.0.0`?

No — `home-assistant-bluetooth` is a small data‑structures package (it defines `BluetoothServiceInfo` etc.) and isn't actually what does the I/O. The real stack in the `bluetooth` integration's manifest is:

- **`bleak`** — the actual cross‑platform BLE client. On Linux/HAOS it talks to BlueZ over D‑Bus (via `dbus-fast`).
- **`habluetooth`** — Home Assistant's wrapper layer around Bleak: manages a single shared `BleakScanner`, multi‑adapter coordination, passive/active scanning, remote scanners (ESPHome Bluetooth proxies, Shelly, etc.), and per‑source advertisement routing.
- **`bleak-retry-connector`** — retry/connect helpers on top of Bleak (handles the flaky connect errors you'd recognize).
- **`bluetooth-adapters`** — adapter enumeration / capabilities (HCI index, address, manufacturer).
- **`bluetooth-auto-recovery`** — power‑cycles/resets a stuck HCI adapter.
- **`bluetooth-data-tools`** — fast parsers for advertisement payloads, manufacturer data, etc.
- **`dbus-fast`** — the async D‑Bus transport Bleak uses on Linux.

So the call path is roughly: integration → `bluetooth.async_get_scanner()` / `async_ble_device_from_address()` → `habluetooth` wrapper → `bleak` → `dbus-fast` → BlueZ. `home-assistant-bluetooth` only supplies the `BluetoothServiceInfo` dataclass that gets handed to integrations.

For the `bluek` project (a kernel‑socket BlueZ central), the relevant comparison point is `bleak`'s BlueZ backend — but HA goes through D‑Bus, not raw HCI/L2CAP sockets.

Sources:
- [home-assistant/core bluetooth manifest.json](https://github.com/home-assistant/core/blob/dev/homeassistant/components/bluetooth/manifest.json)
- [Bluetooth APIs — HA Developer Docs](https://developers.home-assistant.io/docs/core/bluetooth/api/)
- [Bluetooth integration — Home Assistant](https://www.home-assistant.io/integrations/bluetooth/)
- [home-assistant-bluetooth on PyPI](https://pypi.org/project/home-assistant-bluetooth/)

---

## 2. What is `dbus-fast`? Is it faster than normal D‑Bus?

Short answer: **`dbus-fast` is a performance‑oriented fork of `dbus-next`** (a pure‑Python asyncio D‑Bus client). It is *not* faster than "real" D‑Bus the protocol — it's faster than other Python D‑Bus client libraries.

A few points worth disentangling:

- **"D‑Bus" isn't one thing.** There's the protocol/daemon (`dbus-daemon` or `dbus-broker`), the reference C library (`libdbus`), the GLib binding (`gdbus`), and various language bindings. They all speak the same wire protocol.
- **`dbus-fast` is a client library**, written in Python with optional Cython acceleration. The bottleneck it attacks is not the bus itself but the cost of **marshalling/unmarshalling** D‑Bus messages in Python — turning bytes on the socket into typed Python values and back. `dbus-next` did this in pure Python; `dbus-fast` rewrote the hot paths in Cython.
- **What it is faster than:** `dbus-next`, the old `python-dbus` (libdbus bindings, blocking + GIL‑unfriendly), and `pydbus`. Numbers in the project's own benchmarks land in the "several × faster" range for parsing advertisement/property messages — which is the workload that matters for HA, because BlueZ emits a constant stream of `PropertiesChanged` signals as BLE advertisements come in.
- **Why HA cares:** with dozens of BLE devices advertising, the Bluetooth integration is decoding thousands of D‑Bus messages per minute. `dbus-fast` is essentially what makes that practical in Python without pegging a CPU core. It's also async/asyncio‑native, which `python-dbus` is not.

So: faster than other Python D‑Bus libraries, same wire protocol underneath. For the `bluek` work this is the relevant comparison point — Bleak's BlueZ backend pays the D‑Bus serialization tax that you sidestep entirely by going straight to HCI/L2CAP sockets.

Sources:
- [Bluetooth-Devices/dbus-fast on GitHub](https://github.com/Bluetooth-Devices/dbus-fast)
- [dbus-fast releases](https://github.com/Bluetooth-Devices/dbus-fast/releases)

---

## 3. What is `bleak-esphome`?

`bleak-esphome` is a Bleak *backend* that lets a Python process do BLE over a remote ESP32 instead of a local Bluetooth adapter. It's the glue between Home Assistant's Bluetooth stack and **ESPHome Bluetooth Proxies**.

How the pieces fit:

- An **ESP32 running ESPHome** with the `bluetooth_proxy` component scans for BLE advertisements and exposes a connection API over the ESPHome native API (a binary protocol over TCP, not MQTT, not HTTP).
- **`aioesphomeapi`** is the Python client for that protocol.
- **`bleak-esphome`** wraps `aioesphomeapi` and exposes it as something that *looks* like a Bleak `BleakClient` / scanner. So code written against Bleak can connect to a peripheral via "ESP32 in the kitchen" exactly as if it were a local HCI adapter.
- **`habluetooth`** in Home Assistant registers these as additional "scanners" / "sources" alongside any local USB adapter, picks the best path per device (usually the proxy with the strongest RSSI), and routes advertisements + connection requests through it.

Why it exists: a single USB BLE dongle in a server closet only reaches ~10 m through walls. With proxies, you scatter $5 ESP32s around the house and Home Assistant transparently uses whichever one is closest to a given BLE thermometer, lock, etc. The proxy supports both **passive** (just relay advertisements — unlimited devices) and **active GATT connections** (limited to ~3 simultaneous "connection slots" per ESP32, used for things like locks and authenticated reads).

Relevant to the `bluek` work only tangentially — it's the *other* end of the Bleak abstraction: where `bluek` is replacing Bleak's BlueZ backend with raw kernel sockets, `bleak-esphome` replaces it with a network protocol to a remote radio.

Sources:
- [Bluetooth-Devices/bleak-esphome on GitHub](https://github.com/Bluetooth-Devices/bleak-esphome)
- [ESPHome Bluetooth Proxy component](https://esphome.io/components/bluetooth_proxy/)
