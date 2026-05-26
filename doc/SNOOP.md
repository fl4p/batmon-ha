# `type: snoop` — GATT dumper for new BMSes

`snoop` is a pseudo-BMS that doesn't decode anything. It connects, enumerates
the GATT tree, subscribes to every notify/indicate characteristic, and logs
every byte the device pushes. Useful for adding support for a BMS family
batmon-ha doesn't know yet.

## When to use it

* You have a BMS that isn't in the supported list and you want to capture its
  wire protocol.
* An existing BMS family added a firmware variant whose frames look different
  from the decoder's expectations, and you want to see the raw bytes.
* You want to verify a BMS is reachable over BLE at all, separately from any
  protocol-level decoding issue.

## Configure

Add a device entry pointing at the BMS MAC, with `type: snoop`. Most BMSes
only push notifications in response to a poll, so passive subscribe usually
sits silent — append a comma-separated list of known BMS families after a `:`
to make snoop also write probe frames for each. Example:

```yaml
devices:
  - address: AA:BB:CC:DD:EE:FF
    type: snoop:jbd,jk,daly,ant,sok,supervolt
    alias: unknown_bms
    debug: true
```

Notes:

* **Kill any vendor app first.** BLE GATT is single-master — if your phone is
  connected to the BMS, the add-on can't be.
* `debug: true` raises this BMS's log level so individual notify payloads
  show up.
* If you only want a passive capture (no probe writes), use the plain
  `type: snoop` form with no `:families` suffix.

## Probe families

Each family in the `:families` suffix expands to one or more known poll
frames, which snoop writes to every writable characteristic on the device.
Currently shipped probes (see `bmslib/models/snoop.py` `PROBE_FRAMES` for
the exact bytes):

| Family       | Source / target BMS                                        |
|--------------|------------------------------------------------------------|
| `jbd`        | JBD / Xiaoxiang (basic info + cells + hw)                  |
| `jk`         | JK / Jikong (device info + subscribe)                      |
| `daly`       | Daly (SOC + cells + status)                                |
| `ant`        | ANT (legacy `a5 a5 a5 a5` + newer `7e a1 01 …`)            |
| `sok`        | SOK                                                        |
| `supervolt`  | SuperVolt (JBD-compatible)                                 |
| `abc`        | ABC family (aiobmsble `abc_bms`)                           |
| `ant_leg`    | Legacy ANT register read                                   |
| `braunpwr`   | BraunPower BMS                                             |
| `cbtpwr`     | CBTPWR                                                     |
| `cbtpwr_vb`  | CBTPWR-VB (Seplos V2 framing variant)                      |
| `felicity`   | Felicity racks (ASCII `wifilocalMonitor:…`)                |
| `lipower`    | Lipower (Modbus read)                                      |
| `neey`       | Neey balancer                                              |
| `pace`       | Pace BMS                                                   |
| `pro`        | Pro BMS (init handshake + trigger)                         |
| `redodo`     | Redodo / LiTime fixed 8-byte poll                          |
| `renogy`     | Renogy (Modbus read 0x13B2/7 + 0x1388/34)                  |
| `renogy_pro` | Renogy Pro                                                 |
| `roypow`     | RoyPow                                                     |
| `seplos`     | Seplos V3 (Modbus)                                         |
| `seplos_v2`  | Seplos V2 (`7e … 0d` ASCII-hex framing)                    |
| `tdt`        | TDT (`7e 00 01 03 00 8c/8d …`)                             |
| `tianpwr`    | Tianpower                                                  |
| `vatrer`     | Vatrer (Modbus)                                            |

Probe frames for the `aiobmsble`-backed families were snapshotted by
directly calling each plugin's `_cmd()` builder (or hand-built from its
protocol constants), so their CRC/LRC bytes match what the actual decoder
expects. Unknown family names are logged with the list of known ones
and skipped.

## Reading the log

After connect you'll see, in order:

1. The GATT map: each service + characteristic UUID with properties.
2. `[snoop] subscribed <uuid> (notify,…)` — one line per char we subscribed to.
3. If `:families` is set: `[snoop] probing N families across M writable chars`,
   followed by `[snoop] probe <fam> → <uuid> : <hex>` for each frame written.
4. Each notification arrives as:
   ```
   [snoop + 0.42s] 0000ff01-…  h=17  len=20  dd 03 00 1d 17 00 …  | ......
   ```
   The `+s` is seconds since connect, `h=` is the GATT handle, then length,
   hex bytes, and an ASCII rendering for the printable bytes.

## Sharing a capture for new-BMS support

If you want a BMS family added to batmon-ha:

1. Capture at least one full request/response cycle for each frame type the
   vendor app shows (cells, status, settings, …).
2. Copy the `[snoop] …` lines into a GitHub issue under
   [fl4p/batmon-ha](https://github.com/fl4p/batmon-ha/issues).
3. Include the BMS make/model and ideally a link to any public protocol doc
   the vendor (or a similar BMS) publishes.

A passive capture taken alongside the vendor app running on a **second phone**
on a **different bonded controller** can also be useful — kernel-level
btsnoop tracing on Android/Linux records the full exchange without snoop
needing to write anything. See [doc/dev/BT Sniffing.md](dev/BT%20Sniffing.md)
for that workflow.

## Limitations

* Snoop writes probe frames to **every** writable characteristic, not just the
  one that family normally uses. That's intentional — vendor-app captures
  show the right char, but if you don't have one, brute-forcing every
  writable char is the fastest way to find a responder. Expect a flurry of
  failed writes in `debug` log for chars that reject the frame.
* `0.5s` is hard-coded between probe writes. Plenty for normal GATT.
* Snoop produces no MQTT entities (it returns `voltage=NaN` and empty
  cell/temperature arrays). Don't use it as a long-running monitor — it's a
  diagnostic tool.
