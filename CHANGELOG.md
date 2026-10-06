## [2.24]

* Fix: 2.23's `daly` connect could fail every time on a module that drops the first request after connecting, because the learned address was asked once; now it is retried (#416).
* Fix (`soh_estimator`): a restart with the BMS's counter held at its full end could still pass with a whole-number SoC. The stop rule now also checks the reported SoC.
* Fix (`soh_estimator`): saved segments survived a change of `capacity:`, so a replaced 100 Ah pack showed SoH 34.8 % of the new 280 Ah one. Such state is now discarded.
* Fix (`soh_estimator`): garbled frames around a caught current glitch were counted (101–109 Ah for 97.5). Now the 5 readings either side must agree with their level (doc/SoH.md).
* `soh_estimator`: `capacity:` is one pack's nameplate; a bank's capacity there passed (SoH 48.8 %). Nothing is published now when it differs from the BMS's reported capacity by over 1.25×.
* Experimental estimators: saved state was kept after changes to code the fingerprint missed (closures, function attributes, module/class lists or dicts, BmsSample's SoC derivation). The fingerprint now covers them.
* `impedance_estimator`: the first start after updating from 2.23 discards its saved state once (the code fingerprint changed), so the warm-up starts again.
* Fix (`soh_estimator`): a published Qmax/SoH stayed in Home Assistant for up to a year after it stopped being valid (corrected `capacity:`, mismatch, estimator disabled). It is now cleared to unknown.
* Fix (`soh_estimator`): a BMS counter stopped before a restart went unnoticed when ±22 A netted to zero (49 Ah published for 98). It now counts the charge moved either way.

## [2.23]

* Fix: 2.22's `daly` host-address fallback could stick on `0x40` for a module answering only `0x80`. `daly` now settles layout and address on connect; `daly_uart` no longer logs address switches (#416).

## [2.22]

* Experimental `impedance_estimator` (off by default): publishes per-cell resistance of LiFePO4 packs as a `Cell Resistance` sensor in mΩ; small packs may never draw the current steps it needs (doc/Cell Resistance.md).
* The cell resistance estimator saves its state per BMS (`impedance_<name>.json`), so a restart keeps the collected windows instead of starting the warm-up again.
* Experimental `soh_estimator` (off by default): estimates each LiFePO4 pack's capacity as `Qmax (est.)` and `SoH (est.)` sensors from the per-device `capacity:`. It publishes nothing yet; doc/SoH.md says why.
* Experimental `pack_temp_estimator` (off by default): a `Pack Temp (RC est.)` sensor estimating cell temperature from the MOSFET temperature and optional room/outdoor MQTT topics, for BMSes reporting only MOSFET temperature.
* New wired type `braunpwr_uart` for BraunPWR packs with the KS48100 rack BMS, whose FC41D module keeps dropping BLE: a USB-TTL adapter on the module's header (9600 baud) instead (#403).
* New `type: auto` for Bluetooth devices: batmon asks which protocol it speaks (`daly`, `daly2`, `jbd`, `jk`, `ant`) with read-only requests and logs the `type:` to set (#416).
* Fix: `daly` never connected to modules that refuse an empty write on `fff2` (GATT error 258). `daly` now keeps the `fff1/fff2` layout and alternates host address `0x80`/`0x40` (#416).
* Fix: `snoop` probing wrote into SIG characteristics, so via an ESPHome proxy a Daly module took the probe frame as its new name. It now probes vendor characteristics only (#416).
* `daly2` timeouts now say to try `type: daly`: Daly modules that speak the classic `A5` protocol have the same `fff0` GATT layout and silently ignore Modbus requests (#416).
* Fix: telemetry uploaded either a device's pack samples or its cell voltages, rarely both, because the two shared one 15 s slot. Each now has its own.

## [2.21]

* New `ble_request_timeout`: how long an aiobmsble (`_ble`) device gets to answer one request, default 5 s. Raise it for a pack that times out in `_await_msg` (#415).
* `pin:` now works for the aiobmsble types (`felicity`, `daly_ble`, ...): it was dropped, so packs serving GATT only to bonded centrals never connected; batmon now bonds via BlueZ (#415).
* A syntax error in `options.json` now stops batmon, quoting file, line and column, instead of a warning and a misleading `No such file or directory: 'options.json'` (#414).
* A top-level `adapter:` is no longer silently ignored: it is now the default for every device that doesn't set its own, and the log names devices that inherited it (#414).
* Add `offgridtec` (legacy Offgridtec OGT-12200 / Topband): voltage, current, SOC, cycles, temperature, cell voltages and alarm bits. Decoder and fixtures contributed by @travel-and-cache (#413).
* Fix (`bm6`): the Battery Guard variant answers an empty `d155 07 ff …` sentinel unless the realtime request precedes the FFE4 subscribe; batmon now switches ordering. Thanks @travel-and-cache (#412).
* Android app for JK: `/jk.html` talks to a JK BMS directly from Chrome on Android over Web Bluetooth, no add-on or MQTT. See doc/GUI.md for the secure context Chrome needs.
* New built-in read-only web GUI showing every pack, its BMS data and group topology: an ingress panel in Home Assistant, `http://<host>:8099/` standalone. See doc/GUI.md and doc/options.json.gui-demo.
* New `type: group_serial` for battery strings wired in series, alongside `group_parallel`. Voltage sums; charge/capacity/SOC/SOH come from the pack with the least remaining charge. See doc/Groups.md.
* A group no longer publishes a partial sum: it could emit plausible but wrong totals while a member was still connecting. It now waits for every member.
* A group now reports its members' alarms: it hid a failure any of its BMSes was flagging. Group devices gain a `problem` binary sensor; unknown stays unknown, not "no problem".
* Fix: with `watchdog: true` and no MQTT broker configured, batmon killed itself a few minutes after start, because nothing was ever published. The watchdog now only applies with a broker.
* Fix: sampling groups after their members never took effect, so a `group_parallel` could be sampled before its members and waste the cycle raising `GroupNotReady`.

* `bm6`/`bm2`: write the realtime request with `response=True` and clear an orphan subscription first. Thanks @travel-and-cache, who confirmed a BM6 / intAct Battery Guard on BlueZ 5.86 (#408, #160).
* Fix (`ble_stack: bumble`): every aiobmsble BMS was skipped as `Unknown device type`, because the bundled `bleak_retry_connector` shim lacked names aiobmsble >= 0.25 imports. Same bug as #385 (#407).
* `ble_stack: bluek`: a dropped BLE link now logs `link lost (…)` with the kernel's reason, e.g. `Connection timed out`, instead of a later causeless `TX BLE request error` (#403).

## [2.20]

* New wired types: `jbd_uart` (RS485), `jk_pb_uart` (JK-PB, RS485), `seplos_uart` (Seplos V2, RS485), `renogy_uart` (Modbus RTU). Bus address via `type: <name>:<addr>`. Tested against reference frames only.
* `set SOC` number entity to correct a drifted SOC gauge from HA. Daly: command 0x21. JK firmware >= 11: register 0x6E. Untested on hardware (#144).
* Add `bm2` (Quicklynks BM2, Ancel BM200): voltage and SOC from the AES-encrypted pushed frames. Untested on hardware (#41).
* Add `bm6` car battery monitor: voltage, temperature, SOC and charging/low-voltage state over its AES-encrypted protocol. Untested on hardware (#160).
* JBD: newer firmware (Liontron etc.) only answers after passkey pairing; `pin:` now works for `type: jbd` (#217).
* MQTT discovery: `state_class` on capacity, remaining charge, throughput and the meters, so HA keeps long-term statistics for them (#232).
* MQTT: a refused login is reported once as `refused the connection: Not authorized` instead of endless `publish failed: 4` and a watchdog exit (#269).

## [2.19]

* JBD: report which cells the balancer is bleeding, as a `balancing` binary sensor and a `balancing cells` list (e.g. `1,5,18`) (#283).
* Fix (JBD): a basic-info frame whose NTC count byte overruns the payload is rejected instead of creating hundreds of phantom -273 °C temperature sensors in HA (#321).
* `type: snoop`: add the `ej` probe family (E&J Technology `:`…`~` ASCII framing, incl. the Fogstar Drift app's poll) and fingerprint its replies (#351).
* Telemetry: `doc/Telemetry.md` now states it is on by default, what is sent and how to opt out; the log says `Anonymous telemetry is ON`. Uploads now use HTTPS (#379).
* Fix (JK): a status frame missing a BLE packet (busy ESPHome proxy) could publish values like 1,216,000 V, 107 kA and SOC 0%. Such frames are now rejected (#391).
* `ble_stack: esphome`: a fresh 2.17 build failed every connect through ESPHome proxies, because a `bluetooth-data-tools<1.29` pin held habluetooth at 6.1.0. The packages are now pinned together (#401).
* New optional `reconnect_interval_minutes`: drop each `keep_alive` BLE link every N minutes so the next connect re-picks the ESPHome proxy; otherwise a device stays on the first. Off by default (#406).
* `concurrent_sampling`: a device that keeps failing now backs off up to 10 min (was 60 s), so its retries stop starving healthy neighbours on the same ESPHome proxy (#405).
* Fix: a BMS failing every cycle for ~5 days crashed its fetch loop with `OverflowError` from the `1.1 ** n` error backoff; the exponent is now clamped.

## [2.18]

* Fix (`ble_stack: bluek`): endless `[Errno 16] Resource busy`, reported as `device not found`, when bluetoothd held the device's ATT channel. bluek now drops that link before retrying (#403).

## [2.17]

* Bundled aiobmsble updated 0.25.0 → 0.27.0: fixes a permanent bogus Seplos v2 `problem_code` (#400, aiobmsble#98/#240), adds the `pwrboozt_bms` type and a TDT firmware-v1.1 fix.

## [2.16]

* Several Daly can share one RS485 bus: same `adapter:`, a board number each (`type: daly_uart:1`, `daly_uart:2`, …). Duplicate board numbers and mixed baud rates are rejected (#398).
* Fix: every wired BMS reply (`daly_uart`, `jk_uart`, `pace_uart`, `basen_uart`) arrived a full command timeout late (12 s for Daly) when the event loop was idle. BLE was unaffected.
* Fix: `daly_uart`'s 20 ms inter-command gap was taken before acquiring the bus, so with several units on one bus there was no gap at all (#398).
* Serial reader threads now stop on shutdown instead of holding the port open.
* Fix: a BMS whose connect was interrupted after the link came up reported `num_cells not set` and no cell voltages every cycle until restart, with `keep_alive` (#391).
* Fix: a lower-case `address:` never connected through an ESPHome proxy, failing with a misleading `No backend with an available connection slot` or `BleakDeviceNotFoundError`. Only `ble_stack: esphome` was affected (#399).
* Fix: a `group_parallel` referencing members by lower-case MAC aborted the add-on at start-up. Members now match a name exactly or an address case-insensitively (#399).
* Anonymous telemetry still hashes the address as written in the config, not the canonicalized form, so a device's telemetry identity does not move at this upgrade (#399).
* With `ble_stack: esphome`, `adapter:` is no longer reported as used (habluetooth picks a proxy per connection), and start-up discovery scans once instead of once per adapter (#391).
* With `ble_stack: esphome`, start-up no longer prints `BluetoothManager: does not implement _discover_service_info …`, which read like a fault but is harmless (#399).
* New `bt_power_cycle_on_error` (off by default): power-cycles the Bluetooth controller when reconnects keep failing with `Operation already in progress`. At most once per 10 minutes; drops all BLE connections (#392).
* `tdt_nocrc`: a TDT/XiaoXiang variant accepting frames with a wrong firmware CRC, shipping since 2.14. Its invalid-CRC warning is now logged once per device, not per connection (#394).
* Docs: Daly RS485 wiring that works (XH 5-pin: 1 = B−, 2 = A+, 3 = GND — GND is required), and UART/Bluetooth port sharing differs per Daly (#398).

## [2.15]

* Fix: `daly_uart` addressed RS485 board 1 unconditionally, so a Daly whose board number was changed from the factory default answered nothing at all. Set it with `type: daly_uart:2` (#398).
* Fix: wired Daly requests went unanswered because their unused payload was padded with `0x00`; they now use `0xAA` like dbus-serialbattery, plus a 20 ms gap between commands (#398).
* A wired timeout now reports raw bytes received, separating a dead link from a mis-framed one. New `tools/daly_serial_probe.py` finds which settings a BMS answers (#398).
* Fix: a wired BMS stayed dark until add-on restart if its serial reader thread died (e.g. a USB adapter re-enumerating). It now logs, and restarts on the next reconnect.
* Fix: a bad option for one device (e.g. `daly_uart:0`) aborted the whole add-on before any battery started. Such a device is now skipped with an error.
* Fix: Daly MOSFET switch writes always used the BLE address byte, so charge/discharge toggles were ignored over UART/RS485.

## [2.14]

* Fix: the add-on appeared to hang for minutes after a failed BLE subscribe, reading every characteristic at 30 s each over an ESPHome proxy. Reads are now capped (#391).
* Install failing with `Could not find a version that satisfies the requirement bleak==2.0.0` means python < 3.10 or unreachable PyPI; the build now checks both (#397).
* Fix: `daly_uart` got no response over RS485 (`got 0/1 responses`): the serial transport ignored per-model framing. Same bug affected `basen_uart` and `pace_uart` (#398, #396).

## [2.13]

* MQTT discovery now sets `state_class: measurement` on temperature (`temperatures_1..N`) and cell-voltage sensors, so HA keeps long-term statistics for them (#395).
* `bt_diagnostics` with `ble_stack: esphome` now names the registered proxy scanners instead of the host's hci adapters, and no longer passes `adapter:` to the proxy scanner (#391).

## [2.12]

* Fix: the watchdog's error counter never reset during serial sampling (the default), so errors eventually stalled both BMS a minute each and sampling aborted at 200 (#391).

# Changelog


## [2.11]

* Fix: the retry backoff from 2.10 was only cleared by a good sample, so a BMS failing `fetch_voltages` kept its not-found streak. It now clears on connect (#391).


## [2.10]

* Fix: the device-not-found retry backoff escalated on polling cadence, not failures, so an unreachable BMS dropped to one retry per 5 minutes after 3 failed connects (#391).


## [2.09]

* `sok` now routes to the aiobmsble `abc_bms` decoder; fixes `timeout waiting for 193` on current SOK/ABC firmware (#390, #222, #178). Old native driver stays as `sok_legacy`.
* Fix: JK `set_switch` never awaited `has_float_charger()`, so `float_charge` was offered on every model and each switch write leaked a RuntimeWarning (#391).
* JK: the junk-byte and CRC-failure log lines now name the BMS, so two JK devices can be told apart (#391).
* `telemetry` is now a documented add-on option (default on). Set `telemetry: false` to opt out of anonymous sample uploads.


## [2.08]

* Add `pace_uart` for PACE BMS over RS232/RS485 — SOK, SunGoldPower, Sunsynk and other PACE rebrands (#276).
* Add `basen_uart` for Basen BMS over RS232/RS485.
* Add `basen` for Basen BMS over BLE.
* Fix: serial BMS (`pace_uart`, `daly_uart`, `jk_uart`) stacked a duplicate notify callback on every reconnect, corrupting frame reassembly.
* `pace_uart`: reject responses with a bad return code or mismatched address/CID.
* Fix: algorithms crashed on a JK BMS reporting `balance`/`float_charge` switches (#234).
* Fix: temperature sensors flickered to "unavailable" every ~20–30s (#207).
* Fix: a BMS without temperatures (e.g. SOK) crashed HA discovery publish (`len(None)`).
* bluek → `60d1c77`: fix a JK MTU stray-`0x03` crash-loop in service discovery (#386).


## [2.07]

* Removed per-device `ble_stack` (2.05 Approach A): it forced global `bleak`, whose pairing pre-step lacks `bluek`/`bumble` and crash-looped those devices. Use `ble_stack: bluek` globally (#386).


## [2.06]

* Fix: aiobmsble BMS wedged on `NotPermitted: Notify acquired` after a dropped keep-alive, failing every reconnect until restart. Now disconnects the stale instance first (#384).


## [2.05]

* Per-device `ble_stack`: override the global stack per device for mixed setups — only when global is `bleak`; `esphome`/aiobmsble stay global (#385).
* bluek → `bd9070d`: answer a peripheral that server-initiates the ATT MTU exchange (JK failed with `unexpected ATT opcode 0x02`) (#385)
* bluek `bleak_retry_connector` shim: add missing symbols so aiobmsble BMS (Daly `daly_ble`) load on bluek instead of "Unknown device type" (#385)


## [2.04]

* Standalone Docker: prebuilt multi-arch images at `ghcr.io/fl4p/batmon-ha` + a Helm chart in `charts/batmon-ha`. See [doc/Docker.md](doc/Docker.md) (#120)
* `addon_main.sh` no longer needs bashio, so `ble_stack` (`bumble`/`bluek`/`esphome`) works outside Home Assistant
* Fix: `MQTT_HOST`/`MQTT_PORT`/`MQTT_USER`/`MQTT_PASSWORD` from the environment no longer overwritten with empty strings when no Supervisor MQTT service is present
* Fix: an `options.json` without `ble_stack` now defaults to `bleak` instead of skipping the pairing pre-step
* `docker stop` now terminates batmon promptly (entrypoint `exec`s python)
* Add `.dockerignore` so a local `docker build` no longer bakes `options.json` credentials into the image
* JK BLE: resync framing on the header instead of clearing the buffer, fixing `timeout waiting 2/3` / `crc check failed` after reconnect (#377, #370).
* All BMS: estimated time remaining (`bms/runtime`) from remaining capacity / smoothed discharge current (#381)
* Fix: skip BLE discovery and `bt_diagnostics` for serial devices (`address: serial`) (#380)
* Fix: keep `bleak` at 2.x — `aiobmsble` (dep `bleak>=3.0.2`) silently upgraded it, overriding the `bleak==2.0.0` pin for #275. Install `aiobmsble==0.25.0` with `--no-deps` (#383)


## [2.03]

* Daly v2: fix MOSFET switch control — write regs `0x00A5` (charge) / `0x00A6` (discharge), confirmed by official-app HCI snoop; no password write needed (#356)


## [2.02]

* `type: snoop`: fingerprint incoming notifications against known protocol framing and suggest a `type:` (#375)


## [2.01]

* Daly v2: switch write that gets no echo logs a warning instead of blocking the mqtt queue 8s (#356)


## [2.00]

* Daly v2: cell voltages, MOSFET switch control, and correct charge/discharge MOSFET state (#356)


## [1.99]

* Daly v2 (`daly2`, Modbus-over-BLE): the I/O path was a stub that never sent the request. Adds CRC-16 framing, notification reassembly, and a `fff1/fff2`→`ff01/ff02` UUID fallback (#356).
* SOC sensor: add `state_class: measurement` so HA records long-term statistics and it shows in the energy dashboard's battery-level selector (#374).


## [1.98]

* JK: throttle the per-packet `crc check failed` log when the UART carries junk (e.g. JK-PB firmware flooding `AT\r\n`, #370) — one line per 30s instead of an ERROR per packet.
* Add `uart: true` to the manifest so wired BMSes (`address: serial`, e.g. `jk_uart`/`daly_uart`) work — host serial devices are now mapped in; `privileged:` alone never exposed them (#22, #225).
* Keep `publish_period`/`expire_values_after` as visible required fields again — under the collapsed "unused optional" section the HA frontend silently dropped edits on save (#225)


## [1.97]

* Add `translations/{en,de,es}.yaml` in English, German and Spanish
* Add `snoop` BMS to explore unknown types — passive read-out or active probe writes via a `:families` suffix on `type:` (e.g. `type: snoop:jbd,jk,daly`); see [doc/SNOOP.md](doc/SNOOP.md).
* Add `noname_modbus` for generic Chinese BMSes that speak Modbus RTU over the Nordic UART Service (#131) — needs verification with a real device
* Restore multi-arch Docker builds (aarch64/amd64/armhf/armv7/i386) — re-add `ARG BUILD_FROM` consumed by `build.yaml`, which 1.96 had dropped (#365)
* JK: restore sub-1% SOC precision lost in 1.95 by recomputing from `charge / aged_capacity`, keeping 1.95's `capacity` fix for aged 11.x packs (#369)


## [1.96]

* Add `esphome` BLE backend for [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html) devices — see README *BLE Stack*. Incompatible with ANT BMS.
* Fix HA discovery topics when device alias contains `/` (#366)
* Throttle telemetry writes to 15s and suffix InfluxDB measurement with address hash
* InfluxDB: flush in a background task to avoid stalling the sample loop


## [1.95]

* Fix crash when   active balancers/meters report no `battery_level`/`current` (EK-24S4EB #357, CW20 #338)
* Fix `UnicodeDecodeError` crash on JK BMS device info with non-UTF8 bytes (#349)
* Harden ANT BMS device-info decode against non-UTF8 bytes
* DALY: support newer firmware GATT layout (service 0000ff00, ff01/ff02) (#356)
* JK: read pack capacity from settings frame, not BMS-aged value (#365)
* CW20: fix against `aiobmsble` 0.23
* Expose `soh` and `aged_capacity` (JK, ANT, Supervolt)
* Fix `*_ble` current/power sign (charge/discharge meters were swapped)
* Rename `cycle_capacity` → `total_charge_throughput`
* Document the three BLE stacks (`bleak` / `bumble` / `bluek`) in the README
* Add pack-temperature RC pipeline (estimate pack temp from cell + ambient)
* Add `bt_diagnostics` BLE health snapshot
* Collapse multi-page asyncio tracebacks on BLE errors into a one-line cause chain (#367)
* Pin `bluek` to a known-good commit


## [1.93]

* Fixd Dockerfile BUILD_ARGS
* Add new bluetooth backend `bumble` for exclusive adapter access (use a dedicated ble adapter)
* Add new bluetooth backend `bluek` for direct BlueZ access (Linux only, bypass DBus)
* Reverted connection logic back to v1.90 (most stable)

## [1.92]

* Connecting logic using shared scanner
* Rename meters sensor names
* Changed order of options schema so BMS alias is now visible in visual editor
* Show RSSI during ble scanﬂ

## [1.91]

* `aioblebms` v0.12
* fix numeric precision
* implement shared scanners and connect lock
* bleak v1.1.1
* Add Atorch CW20
* strip whitespaces from device addresses
* reduce logging verbosity
* lazy imports of bms specific code

## [1.90]

* fix `mqtt_util` import

## [1.89]

* fix `NoneType doesn't define round method`

## [1.88]

* Add litime BMS (thanks @KOSSOII)
* Fix temperature reading on first sample
* Fix: add `adapter` to options schema

## [1.87]

* Fix PSK (pin) pairing (victron)

## [1.86]

* Fix display precision in HA (you might need to remove the device from MQTT integration and restart batmon)

## [1.85]

* Fix `'BleakClient' object has no attribute 'get_services'`
* Fix `MQTT_HOST: unbound variable` (https://github.com/fl4p/batmon-ha/issues/314)

## [1.84]

* Upgrade Bleak version 1.1.0
* Fix MQTT port not being queried from Supervisor API
* Fix cell voltages for BMS connected through BMS_BLE
* Fix add-on startup with older versions of `bashio` (https://github.com/fl4p/batmon-ha/issues/296)
* JK: increase timeout to 12 seconds
* JK: fix char specifier for newer version (https://github.com/fl4p/batmon-ha/issues/310)
* Add exponential wait on sampling error

## [1.83]

* Fix supervolt characteristic specifiers
* JK-PB2A16S20: add float_charge switch
* Add wrapper for ([BMS_BLE-HA](https://github.com/patman15/BMS_BLE-HA) wrapper) to enable support for Seplos, CBT BMS
  and many more
* Ignore influxdb setup error
* Ignore pip return code when installing special pairing version of bleak
* Fix add-on start-up bashio script if supervisor API is not reachable
* Improved logging output on BLE connection issues
* Pin python to version 3.12 in Dockerfile

## [1.82]

* Rollback bleak version to 0.20.2 (https://github.com/fl4p/batmon-ha/issues/275)
* Fix JK frame version detection
* Supervolt: add char UUIDs for newer version

## [1.81]

* Create separate venv with a modified bleak version for pairing.
  Speeds up start-up and doesn't break with lost internet connection
* Pin bleak version to  `0.22.3`
* Remove `install_newer_bleak` version
* Capture fetch_voltage errors and re-connect
* Add `jk_24s` and `jk_32s` BMS types to explicitly set the JK version (disable auto-detect)
* Add `daly2` type

## [1.80] - 2024-12-28

* Fix JK firmware detection (merge #267)

## [1.79] - 2024-12-09

* Fix #259 OverflowError
* pin paho version 2.1
* fix daly2 @patman15
* changed jk frame version detection

## [1.78] - 2024-02-18

* Fix `NameError: name 'logger' is not defined`

## [1.77] - 2024-02-16

* Fix meter rounding (https://github.com/fl4p/batmon-ha/issues/169)
* Fix paho 2.0 compatibility (https://github.com/fl4p/batmon-ha/issues/195)
* JK add temperature sensor 3 and 4

## [1.76] - 2024-01-17

* Add JK Balance switch
* allow forward slash `/` in mqtt topic prefix

## [1.75] - 2023-12-16

* fix HA install error (https://github.com/fl4p/batmon-ha/issues/175)
* fix meter expiry
* optimized sampling schedule
* add temperature noise filter

## [1.74] - 2023-11-26

* Add Daly checksum verification (https://github.com/fl4p/batmon-ha/issues/158)
* Fix daly bms num_responses (https://github.com/fl4p/batmon-ha/issues/163)
* Ignore voltage fetch errors and continue (https://github.com/fl4p/batmon-ha/issues/163)
* Fix JK 11.x FW mos temp zero (https://github.com/fl4p/batmon-ha/issues/157)
* Copy meter states file before write
* JK fix bouncing switches
* Reduce log verbosity
* Reduce temp fetch and meter publish interval
* Cleanup error handling
* Continue on errors with pip in install_bleak
* InfluxDB add GZIP compression

## [1.73] - 2023-11-10

this is a rather big update. I've set version num to 1.0, so it looks more tidy.

* add manufacturer to device info
* start scanner if device not discovered
* influxdb publish meters
* add additional watchdog thread (to detect event loop issues)
* incr meter integral recovery time to 10 minutes
* add unit tests
* futures pool add  `acquire_timeout`
* BmsSample rename `num` to `num_samples` (breaking change)
* JK capture timestamps
* daly assert soc range (https://github.com/fl4p/batmon-ha/issues/158)
* import mqtt credentials from hassio service
* fix soc group bms (https://github.com/fl4p/batmon-ha/issues/155)
* sample expiry fix
* fix uptime (https://github.com/fl4p/batmon-ha/issues/157)
* fix temperature sensor discovery
* fix JK switches
* fix ANT switches
* victron fixes
* soc algo fix and assert thresholds

## [0.0.72] - 2023-10-19

* Fix JK BLE characteristic handles
* Remove `influxdb` requirement and install if needed
* Fix SoC for groups
* Implement down-sampler (mean)
* Initial device fetch in random order
* Fix victron sampling, add timestamp

## [0.0.71] - 2023-10-04

* Add SOK Bms (initial work by @mdshw5)
* Fix battery groups with missing temperature values
* Daly enumerate services
* Add `influxdb` dependency
* Implement calibration for Soc Algorithm
* fix `MQTT entity name starts with the device name` (#142)

## [0.0.70] - 2023-09-20

* Fix Supvervolt voltages
* Add suppport for Supervolt BMS sending chunked strings
* Try to fetch BMS device info first (for debugging)

## [0.0.69] - 2023-09-02

* Add restart loop to prevent batmon from stopping (#127)
* Fix ANT BMS cell voltages
* Add watchdog timer
* Add InfluxDB sink (undocumented)
* Fix `add_parallel` with empty temperatures

## [0.0.68] - 2023-08-22

* Add ANT BMS switches (untested)
* Fix `current_calibration_factor` and energy dashboard meters
* Fix mqtt names (#126)

## [0.0.67] - 2023-08-22

* Add ANT BMS
* Add Supervolt BMS
* offset cell min_index max_index by +1
* MQTT: Hide empty / nan fields
* Allow nan SoC
* Minor fixes

## [0.0.64] - 2023-07-24

* fix `JSON result was not a dictionary` (remove `json_attributes_topic`)
* Add `current_calibration`
* Daly BMS: Fix bug on timeout

## [0.0.63] - 2023-05-09

* Add option `bt_power_cycle` to power cycle the Bluetooth hardware on start-up
* Add info about bleak version and BMS device info on failures
* Fix `InvalidStateError`
* Fix `adapter` setting being ignored
* Strip spaces from BMS name for MQTT topics
* Parse port number from MQTT host

## [0.0.62] - 2023-04-22

Due to a mistake with git branching, I pushed this update multiple times (v0.0.60, v0.0.61).

* Add `num_cycles`
* Add cell voltage statistics `min`, `max`, `delta`, `average`, `median`, `min_i`, `max_i`
* Add Algorithms feature (experimental) [doc](https://github.com/fl4p/batmon-ha/blob/master/doc/Algorithms.md)
* Add BMS Groups (experimental) [doc](https://github.com/fl4p/batmon-ha/blob/master/doc/Groups.md)

* Increase JBD timeout
* Fix mqtt topic names (remove whitespaces)
* Fix initializing meter states
* Fix JK charge/discharge switches
* Fix JK `Multiple Characteristics with this UUID` error [#83](https://github.com/fl4p/batmon-ha/issues/83)
* Fix `bleak.exc.BleakError: Not connected` [#85](https://github.com/fl4p/batmon-ha/issues/85)

## [0.0.57] - 2023-04-07

* Fix JBD charge/discharge switch
* Change warning if meter states file not found

## [0.0.56] - 2023-04-01

- Add apparmor.txt @MariusHerget
- Fix pin pairing (Victron SmartShunt)
- Fix circular import

## [0.0.54] - 2023-03-06

- Fix main loop exception handling and possible watchdog issue
- Change Daly connecting code to use BT scanner
- Add HA Energy Dashboard support
- Add `adapter` option to choose the BT hardware adapter

## [0.0.53] - 2023-03-06

### Added

- Victron SmartShunt GATT notify
- Add dummy JBD device for testing
- Timestamps in logs
- Total cycle meter

### Changed

- Fix Daly bug
- Fix Victron SmartShunt pairing
- Fix meters
- Fix JK connection bug
- Device name now includes alias

## [0.0.52] - 2023-03-01

### Added

- Add support for JK 11.x firmware
- Add dummy JBD device for testing

### Changed

- Fix JBD unsigned values (negative capacity)

## [0.0.51] - 2023-02-25

### Added

- Add dummy JK device for testing
- Add Daly num_cycles

### Changed

- Fix debug log on error
- Fix BLE discovery with empty device names
- Fix JK soc issue (now using SoC the BMS provides instead of computing it)

## [0.0.50] - 2023-02-03

### Added

- Option `expire_values_after`
- Option `publish_period`
- Log BMS debug data on failure
- Energy meters using trapezoidal power integrators

### Changed

- Serially install apk packages for error tracking
- Switch states have now class `power`
- fix `float division by zero`
- Dummy BMS now reports AC current
- Fix spinning loop in `background_loop` causing high CPU usage

## [0.0.46] - 2022-11-04

### Added

* Add charge/discharge switches for JK, JBD and Daly
* Add watchdog option (disable to prevent program exit on too many errors)
* Add dummy BMS for testing
* Add JK BMS uptime readout

## [0.0.45] - 2022-09-20

### Changed

- Sensor value now use availability status so status expires when BMS is not available
- Fix zero negative current
- Set Keep alive and invert_current default value to true
- Fix Daly zero SoC issue
- Fix JK current direction
- Add `fetch_device_info`
- Add Icons for some sensors
- Fix MQTT connection timeout

## [0.0.44] - 2022-09-06

### Changed

- JK protocol fix
- Daly fix `Characteristic with UUID 17 could not be found`

## [0.0.39] - 2022-08-21

### Changed

- Fix number rounding
- JK try simple connect before scanning
- Periodically send MQTT sensor discovery messages

### Added

- Add capacity sensor

## [0.0.37] - 2022-08-18

### Added

- Added user option `invert_current` to change the direction of battery current

### Changed

- Fixed `already waiting` error
- Increase max_errors before exit to 40

## [0.0.36] - 2022-08-08

### Added

- Support for multiple BMS
- Added cycle_capacity

### Changed

- Changed options schema for MAC addresses. You need to re-enter all addresses after the update. The new schema allows
  adding multiple BMS of the same type.
- JK use nominal capacity instead of user-set capacity

## [0.0.28] - 2022-08-06

### Added

- Support for JK-BMS (Jikong) using JK02 protocol