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
