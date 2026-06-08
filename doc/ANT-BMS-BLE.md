# ANT BMS — BLE protocol notes

Low-level BLE notes for the ANT BMS family, captured while building
a NimBLE-based proxy and verifying against the `aiobmsble/bms/ant_bms`
client. These are the things that aren't documented anywhere obvious
but bite if you skip them.

Test unit was an ANT-BLE20PHUB at MAC `20:A1:11:02:23:45`. Mileage
will vary across firmware revisions — variants noted where known.

## Advertising

* **Address type**: Public (`addr_type=0`). Stable across power
  cycles, no rotation.
* **PDU type**: `ADV_IND` (legacy, connectable, scannable).
  `connectable=1 scannable=1 legacy=1` per NimBLE's classification.
* **Adv interval**: ~100 ms idle. Comfortable to catch with any
  reasonable scan window.
* **Adv payload** (21 bytes, AD-structured):
  | Bytes | Type | Meaning |
  |---|---|---|
  | `02 01 06` | `0x01` Flags | LE General Discoverable + BR/EDR not supported |
  | `05 02 e0ff e7fe` | `0x02` Incomplete 16-bit UUIDs | **0xFFE0** (BMS service) + **0xFEE7** (Tuya/IoT cloud beacon, unused by us) |
  | `0b ff 5706 88a0 20a111012345` | `0xFF` Manufacturer Specific | company **0x0657**, then 8 bytes vendor payload |

* **Manufacturer ID mismatch with upstream matcher**: this unit
  advertises company ID `0x0657`. The matcher in
  `aiobmsble/bms/ant_bms.py` expects `0x2313` — so HA auto-discovery
  using that matcher won't pick this up. Use the `ant_leg_bms`
  variant or extend the matcher. (Untangled which firmware revision
  emits which company ID — empirically both exist in the wild.)

* **Embedded MAC byte is off-by-one**: the vendor payload ends with
  `20 a1 11 01 23 45` — looks like the device's own MAC, but with
  byte 4 = `0x01` instead of `0x02` (the real MAC's fourth octet).
  Probably a firmware bug. Harmless because nothing routes by it;
  just don't trust embedded copies of the address inside the adv.

## Connection behaviour

* **Single client**. Goes "stealth" while connected: stops advertising
  entirely until the active link drops. Useful debugging heuristic —
  if a BMS that *was* advertising suddenly disappears from your scan,
  the first hypothesis should be "someone else connected" before
  "out of range" or "battery dead."
* **No auth, no pairing**. Just `gatt_connect`. The official phone
  app likewise connects unauthenticated; the closest thing to a
  security layer is the BMS-side protocol itself.
* **MTU negotiates to 136** (from a 247-byte request — that's the
  BMS-side cap). Plenty of headroom for ANT's frames (max ~80 B).
* **Connect latency** at close range: ~290 ms from `gap_connect` to
  `on_connect`. Fast.

## Range

| RSSI         | Behaviour                                  |
|--------------|--------------------------------------------|
| > -65 dBm    | Reliable connect + steady link             |
| -65 to -75   | Connect usually works; link mostly stable  |
| -75 to -80   | Connect intermittent; link drops under load |
| < -85        | `BLE_HS_ETIMEOUT` is the normal outcome    |

Note the asymmetry — once connected the link tolerates much weaker
signal than the connect handshake itself does. ANT's default
supervision_timeout (≈ 2.56 s) is generous.

## GATT layer

Service `0xFFE0` exposes a single characteristic `0xFFE1` that carries
both directions:

* **Notify** for streaming state from the BMS
* **Write Without Response** for commands from the host

The application protocol frames (from `aiobmsble/bms/ant_bms.py`):

```
HEAD = 7e a1
TAIL = aa 55
```

Frame layout: `HEAD | cmd | … payload … | CRC16-Modbus | TAIL`

* `cmd 0x01` — status (cell voltages, current, SoC, etc.)
* `cmd 0x02` — device info
* Replies have `cmd | 0x10` (i.e. status reply is `0x11`, device-info
  reply is `0x12`).

CRC is standard Modbus (`crc_modbus` in the same module). Field
offsets and scaling are in the `_FIELDS` table in `ant_bms.py` — that
parser is the canonical reference; replicate from there rather than
rolling your own.

## Coexistence with the vendor app

If the user has the ANT phone app open, it grabs the connection (and
the BMS stops advertising — see above). There's no graceful way for
batmon-ha to take over: the BMS only accepts one client.

For users running both, the practical guidance is:

1. Don't run the phone app in parallel. It steals advertising too,
   not just the connect, so even passive monitoring breaks.
2. If batmon-ha can't connect and you can see the device in the
   phone app, force-close the app and wait a few seconds for
   adverts to resume.

## Variants observed

* `ANT-BLE20PHUB` family with mfr ID **0x0657** — what this doc was
  written against. `ant_leg_bms` matcher should apply.
* `ANT-BLE…` family with mfr ID **0x2313** — what
  `aiobmsble/bms/ant_bms.py` was originally written for. Same GATT
  shape (FFE0/FFE1), same frame protocol, same `_FIELDS` layout
  works — only the discovery matcher differs.

If you find a third variant, the diagnostic recipe is:

1. nRF Connect or `bluetoothctl` to confirm: service `0xFFE0`, single
   characteristic `0xFFE1` with notify + write.
2. Subscribe to `0xFFE1` notify, write `7e a1 01 …<CRC>… aa 55` to
   the same characteristic, and the response should look like
   `7e a1 11 … aa 55`.
3. If those frames flow, the existing `ant_bms` parser will work;
   only the matcher needs updating.

## Stack compatibility (NimBLE-on-ESP32-S3 proxy doesn't work)

Cross-stack test results (May 2026, against an ANT-BLE20PHUB at
firmware `20PHUB00-211026A`, MAC `20:A1:11:02:23:45`):

| Central stack | Hardware | Result |
|---|---|---|
| Linux BlueZ direct (`bluek`) | Pi 5 + USB BT dongle (Realtek) | ✅ works |
| Linux BlueZ direct (`bluek`) | Pi 5 + onboard UART BT (Cypress) | ✅ works |
| macOS CoreBluetooth | M-series Mac (Broadcom) | ✅ works |
| **MicroPython aioble** | **ESP32-S3 (Espressif controller)** | **✅ works** |
| ESPHome Bluedroid proxy | ESP32-S3 (Espressif controller) | ❌ silent |
| **nimble-ble-proxy** (own) | **ESP32-S3 (same physical chip as the aioble row)** | **❌ silent** |

The two bolded rows are the critical pair: same chip, same NimBLE
host C library, different host-side code → only one works. That
rules out the controller, the radio, and the NimBLE library; the
issue lives in NimBLE-Arduino / our usage of it / the
WiFi+TCP+HTTP-server environment around it.

The NimBLE-proxy investigation got far enough to confirm this is not
an ATT-layer issue:

* **ATT bytes are byte-for-byte identical to BlueZ.** Captured
  HCI ACL dumps from both sides — same Write Request to handle `0x0011`
  with value `0x0001`, same Write Command to handle `0x0010` with the
  same query payload. NimBLE host receives the BMS's Write Response
  (`status=0`) for both CCCD writes.
* **After CCCD is enabled, the BMS reads back `0x0001` on reconnect.**
  So the write *did* take effect server-side.
* **BMS sends zero LL notify PDUs over the proxy.** Verified by
  registering a NimBLE host-level `BLE_GAP_EVENT_NOTIFY_RX` listener
  via `ble_gap_event_listener_register` and exposing the count at
  `/stats.json` on the proxy. Stays at 0 for the entire session.

Things tried on the proxy side that did **not** unblock notifies:

| Knob | Tested values |
|---|---|
| CCCD response flag | `response=true` (ATT_WRITE_REQ) — matches BlueZ |
| CCCD value | `0x0001` (notify) and `0x0003` (notify+indicate) |
| Duplicate CCCD write (subscribe + explicit write_descriptor) | Patched NimBLE-Cpp to expose `setNotifyCallback()` for register-without-write; got to a single CCCD write matching BlueZ — still no notifies |
| BLE 5 PHY features | `CONFIG_BT_NIMBLE_LL_CFG_FEAT_LE_2M_PHY=n` + `..._CODED_PHY=n` — removed an `UNSUPP_REM_FEATURE` HCI error during MTU exchange but didn't change notify behaviour |
| Initial connection interval | NimBLE defaults (30–50 ms initial → 15–25 ms after BMS-initiated L2CAP CPU); also forced to 75–150 ms |
| ATT reads of "junk" handles | `verbose_log: false` (skip BMS attribute reads from bt.py) — no change |
| Service Changed indication subscribe (BlueZ does this; NimBLE proxy doesn't) | Inconclusive — BMS entered a stuck state mid-test before the variable could be isolated |

The differences between BlueZ and the proxy that *aren't* ATT-level:

* **HCI Create Connection command.** BlueZ uses LE Extended Create
  Connection (`0x08|0x0043`) with `Initiating PHYs: 0x01` = LE 1M only.
  NimBLE uses the legacy LE Create Connection (`0x08|0x000d`).
  Semantically equivalent for a 1M-only initiator.
* **Initial supervision timeout.** BlueZ: 6000 ms. NimBLE-Cpp default: 2560 ms.
  Both end up at 6000 ms after the BMS's L2CAP Connection Parameter Update
  Request goes through; no notify-behaviour difference observed.
* **Service Changed CCCD.** BlueZ writes `0x0002` to handle `0x000d`
  (Service Changed indications enable) early in the connection. The
  NimBLE proxy never subscribes to Service Changed. This is the
  best remaining hypothesis but couldn't be isolated cleanly before
  the BMS entered a stuck-discovery state.

If you want to chase the final answer:

1. Power-cycle the BMS first (it gets sticky after many failed proxy
   sessions; gets back to working state after a clean reset).
2. Run the bring-up probe directly via `aioesphomeapi` from a host
   with no other clients on the proxy — that's the cleanest stack to
   reason about.
3. The exact sequence to compare against: write `0x0002` to handle
   `0x000d` (Service Changed indicate), wait one connection interval,
   write `0x0001` to handle `0x0011` (ffe1 notify), wait, then write
   the query to handle `0x0010`. That matches BlueZ exactly.

Until that's resolved, **do not configure ANT BMS variants through
`ble_stack: esphome`**. Use `ble_stack: bluek` (direct kernel BlueZ)
or `ble_stack: bleak` — both confirmed working with this BMS family.

### Update 2026-05-26 — chip is NOT the problem

Flashed MicroPython v1.24.1 onto the exact same ESP32-S3 module that
was running the proxy, ran a minimal aioble script (connect →
`disc_svc_by_uuid` → `disc_chrs_by_uuid` → `subscribe(notify=True)` →
write query → wait for notify). **It works**, returning the expected
`7e a1 12 6c 02 20 …20PHB0TB120A…` response within ~200 ms.

So the same Espressif BLE controller and the same underlying NimBLE
host C library *do* talk to the ANT BMS correctly. The bug is in
**our proxy stack's discovery flow** — specifically the wrapper layer
plus bleak-esphome doing ~20 ATT operations between connect and
CCCD-write where aioble does 5.

Leading hypothesis: the rapid string of ATT operations during full
descriptor enumeration (especially reads of `0x2803` characteristic-
declaration attributes that return `ATT_ERR_INVALID_HANDLE`) puts the
ANT BMS into a state where it accepts subsequent CCCD writes (we see
status=0 acks, the CCCD value reads back as `0x0001` on reconnect)
but silently doesn't transmit any notify PDUs. Confirmed via a
NimBLE host-level `BLE_GAP_EVENT_NOTIFY_RX` counter: zero notify
PDUs reach the controller after our proxy's discovery completes.

Fix candidates, in increasing effort:

1. **Make the proxy's `gatt_discovery::run()` minimal** — call
   `ble_gattc_disc_svc_by_uuid(ffe0)` and `ble_gattc_disc_all_chrs`
   targeted at the BMS service only, skip the GAP/GATT (`0x1800` /
   `0x1801`) services and their descriptors entirely. Build the
   services-response proto by hand from a curated subset rather than
   from `NimBLEClient::discoverAttributes()`. The trade-off is the
   proxy then needs a "known device profile" table to know which
   services to look up; or the addon can pass the service UUID via
   the `BluetoothDeviceConnectionRequest.flags` extension. Untrivial
   integration with bleak-esphome which expects a full service list.
2. **Bypass `discoverAttributes()` in NimBLE-Arduino** and call the
   raw `ble_gattc_disc_*` functions directly, but only for the
   services we care about — same end state, less invasive than (1).
3. **Replace NimBLE-Arduino with raw NimBLE C calls throughout the
   proxy.** Mirrors what micropython's `modbluetooth_nimble.c` does.
   Bigger refactor.

The working MicroPython test script + scan script are in
`/Users/fab/dev/mpy-ant/` for future reproducers. Recipe:

```
# erase + flash micropython (FLASH_4M variant for this 4MB chip)
.../venv/bin/python -m esptool --chip esp32s3 -p /dev/cu.usbmodem1101 erase-flash
.../venv/bin/python -m esptool --chip esp32s3 -p /dev/cu.usbmodem1101 write-flash 0 esp32s3.bin
# push aioble + test script
.../mpremote cp -r aioble/ :lib/
.../mpremote cp ant_test.py :
.../mpremote run ant_test.py
```

### Side observations worth retaining

* **MTU=23 is enough.** aioble doesn't do an ATT MTU exchange, so the
  link stays at the BLE default 23-byte MTU. ANT BMS's notify payloads
  fit fine — the first frame is exactly 20 ATT bytes (the
  `7e a1 12 …20PHB0TB120A…` device-info reply), the second is 8
  bytes (`ff 0b 00 00 41 f2 aa 55` — the frame trailer with the ANT
  `aa 55` end marker). Both fit in MTU=23 (3-byte ATT header + ≤20
  byte payload).
* **Reply framing.** Every multi-PDU response from the BMS ends with
  a short `ff … aa 55` trailer PDU. Parsers should accumulate notify
  bytes until they see `aa 55` rather than counting up to a fixed
  size — the BMS will split a logical reply across two or more
  notify PDUs (one ≈20 bytes data + one ≈8-byte trailer).
* **First-connect grace window is unreliable.** After a session that
  ends with a hard disconnect (or a series of failed connects from
  our proxy), the BMS sometimes refuses the next discovery for a
  few seconds — `discoverAttributes` returns false / GATT timeout.
  Wait ≥10 s, or power-cycle the BMS, before retrying.
* **CCCD persists across reconnect in some firmware revisions.** On
  the test unit, after our proxy enabled notify, a re-connect saw
  the CCCD read back as `0x0001` — implying the BMS *thinks* notify
  is still subscribed. So failed-notify symptoms aren't "the CCCD
  write isn't sticking"; the write took, the BMS just doesn't emit
  PDUs.

Until the proxy bug is fixed, ANT BMS users should stay on
`ble_stack: bluek` or `bleak`.

### Update 2026-05-26 (cont.) — patched proxy to match aioble byte-for-byte; still fails

After confirming the chip works via micropython aioble, patched the
proxy to mirror aioble's ATT-op sequence exactly:

* `exchangeMTU=false` in NimBLEClient::connect — skip the auto MTU
  exchange aioble doesn't do.
* `gatt_discovery::run()` rewritten to use targeted disc
  (`getService(NimBLEUUID(0xFFE0))` → triggers `disc_svc_by_uuid`;
  per-service `getCharacteristics(refresh=true)`; per-char-with-notify
  `getDescriptors(refresh=true)`). Skips full
  `discoverAttributes()` enumeration of `0x1800` / `0x1801`.
* `chr->subscribe()` in `handle_notify` replaced with a
  `setNotifyCallback()` shim that registers the callback only
  (no CCCD write), so bleak-esphome's explicit `write_descriptor`
  is the single CCCD write. One write, matching aioble.
* `scanner::resume()` removed from `onConnect` — proxy now keeps the
  scanner stopped during an active connection, matching aioble's
  single-connection focus.

Captured the HCI ACL TX bytes from the proxy. Resulting wire sequence
post-connect, exact match against aioble:

```
06 01 00 ff ff 00 28 e0 ff             FIND_BY_TYPE_VALUE ffe0 (svc disc)
06 14 00 ff ff 00 28 e0 ff             ditto continuation
08 0e 00 13 00 03 28                    READ_BY_TYPE (char disc on ffe0 range)
08 13 00 13 00 03 28                    continuation
04 11 00 12 00                          FIND_INFORMATION (desc disc on ffe1 range)
04 12 00 12 00                          continuation
12 11 00 01 00                          WRITE_REQ handle 17 = 0x0001 (CCCD)
52 10 00 7e a1 02 6c 02 20 58 c4 aa 55  WRITE_CMD handle 16 (ANT query)
```

Byte-for-byte identical to aioble. **BMS still doesn't notify** —
`BLE_GAP_EVENT_NOTIFY_RX` count stays at 0 over 50 s of active
connection.

That isolates the cause to **environmental / scheduling** rather than
ATT protocol, ruling out everything we can directly observe and patch
from host code. The remaining differences between aioble-on-ESP32-S3
and our-proxy-on-the-same-ESP32-S3 are:

* **WiFi+BT coexistence load.** Our proxy keeps WiFi up + a TCP API
  server + an HTTP server during the BLE session. aioble runs with
  no concurrent WiFi traffic. ESP32-S3 schedules WiFi and BT on the
  shared 2.4 GHz radio; under sustained WiFi load the BLE
  connection-event windows can be squeezed enough to miss notify PDUs.
* **Task scheduling.** ESP-IDF preemptive FreeRTOS scheduling vs
  micropython's single-thread asyncio. Different priority pressures
  on the NimBLE host task.

Neither is testable from this codebase without significant
infrastructure changes (running aioble *with* WiFi+API server load,
or instrumenting the BT/WiFi coexistence scheduler), and both are
plausibly the root cause.

### Definitive next step requires an air sniffer

To make further progress we need to observe what's actually on the
2.4 GHz radio during a failing session — specifically whether the
BMS transmits any data-channel PDUs (notify or empty) that the
ESP32-S3 controller silently drops, vs whether the BMS genuinely
stops emitting after our connect/subscribe sequence.

**Get an nRF52840 dongle** (Nordic PCA10059, ~$10 from Mouser/Digi-
Key, ~2 days shipping). Reasons it's the right tool:

* Mature **nRF Sniffer for Bluetooth LE** firmware ships from Nordic;
  flashes via the bundled `nrfutil` in ~30 s.
* Follows a chosen MAC through the 37-channel hopping pattern
  automatically — no per-channel-fix dance like ESP32 / CC2340
  sniffers.
* **Wireshark extcap integration** on macOS works out of the box —
  point Wireshark at the sniffer interface, select the ANT MAC from
  the live device list, hit record.
* Tested across BLE 4.x + 5.x peripherals; doesn't get confused by
  ANT's BLE 4-only profile the way newer-only tools sometimes do.

Other gear that does NOT work for this:

* TI CC2340R5 LaunchPad — supported in TI SmartRF Sniffer 2 as of
  v1.11 *no*, not at all yet. The CC23xx F3 family is too new; sniffer
  firmware only targets the older CC13xx/CC26xx parts.
* ESP32 family (any variant) — controller firmware doesn't expose a
  promiscuous "follow connection" mode. Open-source ESP32 sniffers
  capture advertising channels only.
* Generic USB BT dongles (CSR, Realtek) — host adapters, no
  promiscuous mode at all.
* Ubertooth One — works but $120, slower hop tracking than Nordic.

Once a capture is in hand, the diff against the working
BlueZ-on-Pi capture (already on hand at
`havan:/tmp/ant_hci1.btsnoop`) should expose the LL-level
difference. Until then, the proxy ships with ANT documented as
unsupported.

The proxy patches that matched aioble's ATT sequence were reverted
before shipping — they would break BMSes with 128-bit-UUID-heavy
profiles or non-FFE0 vendor services. Keeping the targeted-discovery
hack as ANT-specific dead code isn't worth the maintenance.

**Current shipping state of the proxy:** restored to full
`discoverAttributes()` + `exchangeMTU=true` + scanner resumed during
connections. Works for everything we've tested except ANT. ANT users
must use `ble_stack: bluek` or `bleak`.

The legitimate proxy fixes that **do** stay shipped (all
`nimble-ble-proxy` improvements that came out of this debugging
session):

* Address byte-swap fix in scanner adv and notify-rx paths.
* Heap-alloc `ServicesEncodeCtx` (~25 KiB) to avoid 8 KiB-stack
  overflow during `bluetooth_gatt_get_services`.
* CCCD subscribe `response=true` (spec-recommended ATT_WRITE_REQ
  for CCCD writes, not ATT_WRITE_CMD).
* `setNotifyCallback()` shim — register notify callback without
  redundant CCCD write (avoids the duplicate-CCCD pattern with
  bleak-esphome's explicit `write_descriptor`).
* `BLE_NIMBLE_LL_CFG_FEAT_LE_2M_PHY=n` + `…_LE_CODED_PHY=n` — silence
  `BLE_ERR_UNSUPP_REM_FEATURE` HCI noise; safe for BLE 4.x peers.
* `/log` HTTP endpoint with chunked response, 64 KiB ring when
  NimBLE-DEBUG is compiled in; `/level` runtime knob to dial NimBLE
  log verbosity.
* Diagnostic GAP listener counting raw `BLE_GAP_EVENT_NOTIFY_RX` at
  the NimBLE host level (`notify_rx` / `last_notify_handle` in
  `/stats.json`).
