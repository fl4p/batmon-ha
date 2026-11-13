# Home Assistant Add-on: BatMON

[![Analytics][install-shield]]()

![Home Assistant Dashboard Screenshot](https://repository-images.githubusercontent.com/445289350/03f3d531-37cf-48be-84c8-e6c75270fc87)

Monitor and control various Battery management systems (BMS) over Bluetooth. This add-on reads the BMS and sends sensor
data through MQTT to Home Assistant. Using bluetooth on the Home Assistant host system, it does not need any additional
hardware (no USB, Serial, RS485 or ESP32). It can also run without HA on Linux, macOS and Windows.

I created this to compare BMS readings for a detailed evaluation of BMS reliability and accuracy.

## Features

* Uses Bluetooth Low-Energy (BLE) for wireless communication
* Captures SoC, Current, Power, individual cell voltages and temperatures
* Monitor multiple devices at the same time
* Energy consumption meters (using trapezoidal power integrators)
* Integrates with Home Assistant Energy dashboard and [Utility Meter](doc/HA%20Energy%20Dashboard.md) sensor helper
* Control BMS charging and discharging switches
* Set the SOC gauge from HA via a `set SOC` number entity, e.g. to reset a drifted gauge to 0 % after a low-voltage cut-off: Daly (`daly`, `daly_uart`) and JK with firmware >= 11 (`jk`, `jk_32s`) (#144)
* Home Assistant MQTT Discovery
* Can run as stand-alone app without Home-Assistant and directly write to [InfluxDB](doc/InfluxDB.md)
* Battery Groups for parallel batteries, see [doc/Groups.md](doc/Groups.md)
* Charge Algorithms, see [doc/Algorithms.md](doc/Algorithms.md)
* Low latency for responsive automation (fast load shedding)
* Experimental serial communication for JK and Daly BMS
* Current sensor gain calibration
* Custom bluetooth stack for increased reliability

### Supported BLE Devices

Batmon comes with connectors for some popular BMS. It also wraps `aiobmsble`, which includes many other BMS for
read-only access.

batmon device connectors:

* JK BMS / jikong with JK02 protocol (`jk` over BLE, `jk_uart` over RS485 — see [Serial / RS485](#serial--rs485))
* JK-PB inverter BMS (PB1A16S, PB2A16S, ...; `jk` over BLE, `jk_pb_uart` over RS485)
* Daly BMS (`daly`, `daly2`, `daly_ble` over BLE, `daly_uart` over RS485 — see [Serial / RS485](#serial--rs485))
* JBD / Xiaoxiang / LLT BMS (`jbd` over BLE, `jbd_uart` over TTL UART or RS485)
* Seplos V2 BMS, 48 V rack packs (`seplos_ble`, `seplos_v2_ble` over BLE, `seplos_uart` over RS485)
* Renogy smart lithium batteries (`renogy_ble` over BLE via the BT-2 dongle, `renogy_uart` over RS485)
* Felicity Solar 48 V LiFePo4 packs (FLB48314TG1, FLB48100, LPBF48/LPBA48; `felicity` over BLE, `felicity_uart` over RS485)
* JBD / Jiabaida/ Xiaoxiang / Overkill Solar BMS (`jbd`)
* ANT BMS (`ant` over BLE, `ant_uart` / `ant_old_uart` over UART)
* Supervolt BMS (`supervolt`)
* SOK / ABC-BMS (`sok`, routed through the aiobmsble `abc_bms` decoder; use `sok_legacy` for the pre-2023 native driver)
* Basen BMS (`basen`), ported from [syssi/esphome-basen-bms](https://github.com/syssi/esphome-basen-bms)
* LiTime BMS (`litime`)
* Legacy Offgridtec OGT-12200 / Topband batteries (`offgridtec`): unsolicited FFE4 ASCII-hex protocol
* BM6 Bluetooth car battery monitor and the Ancel BM200 (`bm6`): voltage, temperature, SOC and charging/low-voltage state, no current (#160, #41)
* Quicklynks BM2 and rebrands (`bm2`): voltage and SOC. An Ancel BM200 reads with `bm6`, not `bm2` (#41)
* Generic no-name Chinese BMS with Modbus RTU over Nordic UART (`noname_modbus`) — see [issue #131](https://github.com/fl4p/batmon-ha/issues/131)
* Victron SmartShunt (make sure to update to the latest firmware
  and [enable GATT](https://community.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html)
  in the VictronConnect app) (`victron`)

More `aiobmsble` device connectors:

* CBT Power / Creabest smart BMS (`cbtpwr`)
* Seplos smart BMS V3 (`seplos`), Seplos smart BMS V2 (`seplos_v2`)
* TianPwr smart BMS (`tianpwr`)
* ATORCH CW20 DC Meter (`cw20`)
* TDT smart BMS (`tdt`; `tdt_nocrc` for units with broken CRC firmware, e.g. Humsienk golf-cart batteries — see [issue #394](https://github.com/fl4p/batmon-ha/issues/394))
* E&J Technology smart BMS (`ej`)
* Chunguang Song ABC-BMS (`abc`)
* D-powercore smart BMS (`dpwrcore`)
* ECO-WORTHY BW02 (`ecoworthy`)
* Ective smart BMS (`ective`)
* Felicity Solar LiFePo4 battery (`felicity`)
* Offgridtec LiFePo4 Smart Pro (`ogt`)
* Redodo Bluetooth battery (`redodo`)
* RoyPow smart BMS (`roypow`)
* Braun Power smart BMS (`braunpwr`)
* Neey Balancer (`neey`)
* Pro BMS Smart Shunt (`pro`)
* Renogy Bluetooth battery (`renogy`), Renogy BT battery pro (`renogy_pro`)
* all other devices [aiobmsble](https://github.com/patman15/aiobmsble/?tab=readme-ov-file#supported-devices) supports

You can switch from the batmon to the aiobmsble connectors, just append a `_ble` to the `type` field, e.g. instead
of `type: daly` (batmon), write `type: daly_ble` (aiobmsble). This can help if you experience connection issues, because
some of the `aiobmsble` connectors are more up to date.

I tested the add-on on a Raspberry Pi 4 and 5 using Home Assistant Operating System.

## Installation

* Go to your Home Assistant Add-on store and add this
  repository: [`https://github.com/fl4p/home-assistant-addons`](https://github.com/fl4p/home-assistant-addons)
  [![Open your Home Assistant instance and show the dashboard of a Supervisor add-on.](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=2af0a32d_batmon&repository_url=https%3A%2F%2Fgithub.com%2Ffl4p%2Fhome-assistant-addons)
* Install Batmon add-on
* Install, configure and start Mosquitto MQTT broker (don't forget to configure the MQTT integration)

## Configuration

Add an entry under `devices` for each BMS. The add-on reads all of them.

```
- address: CC:44:8C:F7:AD:BB
  type: jk
  alias: battery1            # MQTT topic prefix (regex [\w_.-/])
  pin: "12345"               # pairing PIN, victron and newer JBD firmware (optional)
  adapter: "hci0"            # switch the bluetooth hw adapter (optional)
  debug: true                # verbose log for this device only (optional)
  current_calibration: 1.0   # current [I] correction factor (optional)
  display_name: House battery     # device name in HA; topics stay (optional)
  temperature_names: Cell 1,Cell 2 # probe names in HA, in order (optional)
```

### Per device

* `address`: the Bluetooth MAC address, or the device name as shown in discovery. Start the add-on to see a list of
  visible devices in the log.
* `type`: `jk`, `jk_24s`, `jk_32s`, `jk_uart`, `jk_pb_uart`, `jbd`, `jbd_uart`, `ant`, `ant_uart`, `ant_old_uart`, `daly`, `daly2`, `daly_ble`,
  `daly_uart`, `pace_uart`, `seplos_uart`, `braunpwr_uart`, `renogy_uart`, `felicity_uart`, `supervolt`, `sok`,
  `sok_legacy`, `basen`, `basen_uart`, `litime`, `offgridtec`, `bm6`, `bm2`, `victron`, or any tag listed under
  [Supported BLE Devices](#supported-ble-devices). `dummy` is a mock BMS.
  * JK: `jk_24s` for the older 24s version (firmware < 11), `jk_32s` for the 32s version (firmware ≥ 11), `jk` to
    auto-detect (may give invalid data if detection fails).
  * Daly: `daly` for classic `A5` frames (J/T/A/U/W series), `daly2` for Modbus over BLE (H/K/M/S series, frames start
    with `D2`, [#356](https://github.com/fl4p/batmon-ha/issues/356)). Both look the same over GATT; if one only times
    out, try the other, or run `type: snoop:daly` and check whether replies start with `a5`
    ([#416](https://github.com/fl4p/batmon-ha/issues/416)).
  * `auto` (Bluetooth only) probes the device at every start and logs the `type:` to use. It knows `daly`, `daly2`,
    `jbd`, `jk`, `ant`, `bm2` and `bm6`; a device that doesn't answer is skipped with an error, never guessed.
* `alias`: MQTT topic prefix and HA name. Defaults to the Bluetooth name.
* `display_name`, `temperature_names`: device name and temperature probe names (comma list, in order) in HA. Topics and
  unique IDs don't change, so history is kept.
* `pin`: pairing PIN, for the Victron SmartShunt, newer JBD firmware (Liontron and other packs whose app asks for a
  6-digit password, [#217](https://github.com/fl4p/batmon-ha/issues/217)) and Felicity packs renamed from `F07…` to
  `SolarB_…` ([#415](https://github.com/fl4p/batmon-ha/issues/415)). BlueZ bonds the device before sampling starts
  (like `bluetoothctl pair`). This needs a local adapter; an ESPHome proxy cannot answer a PIN prompt.
* `adapter`: Bluetooth adapter to use, e.g. `hci1`. See [`adapter:` per BMS](#adapter-per-bms).
* `current_calibration`: factor applied to the current reading. `-1` flips the sign.
* `capacity`: nameplate capacity of this pack in Ah, needed by `soh_estimator`.
* `settings_write`: JK settings this device may change, see `jk_settings`.
* `debug: true`: verbose log for this device only.

### Global

* MQTT: set `mqtt_user` and `mqtt_password`. The broker is usually `core-mosquitto`.
* `sample_period`: seconds between BMS reads.
* `publish_period`: seconds between MQTT publishes; samples in between are averaged. Set it above `sample_period` to
  throttle MQTT while keeping energy meters accurate. Below 2 s, HA history plots get slow.
* `keep_alive`: never close the Bluetooth connection. Allows faster sampling, but your phone app can't connect while
  the add-on runs.
* `concurrent_sampling`: read all BMS at the same time instead of one after another. Can cause connection problems
  without `keep_alive`.
* `ble_request_timeout`: seconds an `aiobmsble` device (`felicity`, `daly_ble`, any tag from
  [Supported BLE Devices](#supported-ble-devices)) gets to answer, default 5. Raise it (e.g. `12`) if a device times
  out in `_await_msg`. Applies to all such devices; a silent device can take up to twice this value to give up.
* `invert_current`: flip the current sign. Normally discharge is positive.
* `expire_values_after`: seconds after which a sensor shows "Unavailable".
* `watchdog`: exit on too many errors. Enable the HA watchdog so the add-on restarts.
* `jk_settings`: show JK settings (BLE, firmware ≥ 11) in HA. A device's `settings_write` (`all` and/or a comma list of
  names; `all` leaves out critical settings) makes them writable, with backup and read-back check. See
  [doc/JK-Settings.md](doc/JK-Settings.md).
* Experimental estimators, all off by default:
  * `impedance_estimator`: per-cell resistance of LiFePO4 packs, as `Cell Resistance` (mΩ). See
    [doc/Cell Resistance.md](doc/Cell%20Resistance.md).
  * `soh_estimator`: present capacity of LiFePO4 packs as `Qmax (est.)` and `SoH (est.)`, against the device's
    `capacity:`. With the built-in OCV curve it gives no value yet, see [doc/SoH.md](doc/SoH.md).
  * `pack_temp_estimator`: cell temperature for packs that only report a MOSFET temperature, as
    `Pack Temp (RC est.)`. Optional `pack_temp_room_topic` / `pack_temp_outdoor_topic` take ambient temperatures from
    MQTT. See [doc/PackTemp.md](doc/PackTemp.md).

## Serial / RS485

Some BMS have an RS485 (or TTL UART) port. Batmon reads it through a USB-to-RS485 adapter, independent of Bluetooth
and `ble_stack`.

```yaml
- address: serial
  adapter: /dev/ttyUSB0   # serial port path; required when address=serial
  type: jk_uart
  alias: battery1
```

`address: serial` selects the wired transport and `adapter` is the port (`/dev/ttyUSB0`, `/dev/ttyAMA0`, `COM3`, …).
The add-on maps the host's serial devices into the container; prefer the stable `/dev/serial/by-id/...` path.
Standalone on Linux, add your user to the `dialout` group. A network serial server (ser2net, an ESP USB gateway, …)
works as `adapter: tcp://192.168.1.108:8880` (raw TCP, set the baud rate on the server) or `rfc2217://host:port`.

Types (baud rate in parentheses). `:<addr>` after the type sets the bus address; several packs on one bus share the
same `adapter:`, one entry each.

* `jk_uart` (115200): JK / Jikong UART protocol. Not for PB models.
* `jk_pb_uart` (115200): JK-PB inverter BMS (PB1A16S, PB2A16S, …) on RS485-1. Address as set in the JK app, default 1.
* `daly_uart` (9600): Daly. Address is the board number, see below.
* `jbd_uart` (9600): JBD / Xiaoxiang / LLT, TTL header or RS485.
* `jbd_hv_uart` (9600): JBD / LWS high-voltage BMS (HVBMS, up to 200 cells). Default address 1
  ([#424](https://github.com/fl4p/batmon-ha/issues/424)).
* `pace_uart` (9600): PACE server-rack packs (SOK, SunGoldPower, Sunsynk rebrands, PbmsTools)
  ([#276](https://github.com/fl4p/batmon-ha/issues/276)).
* `basen_uart` (9600): Basen.
* `seplos_uart` (19200): Seplos V2 (BMS 1.0/2.0 hardware). Default address 0; `seplos_uart:<addr>:9600` for the
  RS232 console port. Seplos V3 is not supported.
* `braunpwr_uart` (9600): BraunPWR packs with the KS48100 rack BMS. Set the BMS inverter protocol to Voltronic.
  Default address 1; if nothing answers, try 1 to 16. Use the RS485 port, not the internal 4-pin UART header: its
  GND is battery ground and has burned out USB-TTL adapters
  ([#403](https://github.com/fl4p/batmon-ha/discussions/403)).
* `renogy_uart` (9600): Renogy smart lithium (RBT100LFP12, RBT200LFP12S, …). Default id `0x30`; the RBT100LFP12SH-G1
  uses `0xF7`.
* `felicity_uart` (9600): Felicity Solar 48 V packs (FLB48314TG1, FLB48100, LPBF48/LPBA48), RS485 on RJ45 pins 5/6.
  Packs without DIP switches number themselves from 1, not in wiring order: add entries from 1 until one stops
  answering, and tell packs apart by their readings ([#415](https://github.com/fl4p/batmon-ha/issues/415)).
* `ant_uart` (19200): ANT BMS from 2021 on (16ZMB, 24AHA, 24BH, …); `ant_old_uart` (19200) for older ANT boards.
  3.3 V TTL on the 4-pin comm port; connect VCC too, or the BMS does not answer. `ant_uart:9600` sets another baud rate.

Except for `jk_uart` and `daly_uart`, these are not yet confirmed on live hardware. Feedback welcome.

### Daly board number

A Daly BMS only answers requests carrying its board number ("Board No" in Daly's PC tool, default 1). Append it to
the type, e.g. `type: daly_uart:2`.

Several Daly on one bus need distinct board numbers and share one adapter:

```yaml
- address: serial
  adapter: /dev/ttyUSB0
  type: daly_uart:1
  alias: battery1
- address: serial
  adapter: /dev/ttyUSB0     # same port
  type: daly_uart:2
  alias: battery2
```

This is safe with `concurrent_sampling`. Two entries with the same board number, or different BMS families on one
port, are rejected.

### Troubleshooting a silent wired link

`timeout awaiting result ... 0 bytes received` is a wiring, adapter or port problem. Bytes received but `0 valid
frames` points at the baud rate or the address.

For Daly, `tools/daly_serial_probe.py` sweeps board numbers and line settings and dumps the raw bytes:

```
python3 tools/daly_serial_probe.py /dev/serial/by-id/usb-FTDI_...-port0
```

Daly wiring ([#398](https://github.com/fl4p/batmon-ha/issues/398)): the supplied XH 5-pin RS485/CAN cable is pin 1 =
B−, pin 2 = A+, pin 3 = GND, pins 4–5 = CAN. **Connect GND**, without it nothing works. Daly's TTL UART and RS485 are
separate ports; on some boards they share a UART with the Bluetooth module, so unplug the BT module if in doubt.

To request another BMS family over RS485, open an issue with a captured frame.

## Adding a new BMS

If your BMS isn't supported yet, set `type: snoop` (optionally
`type: snoop:jbd,jk,daly,ant,sok,supervolt` to also write known probe
frames) on the device entry. Batmon will connect, dump the GATT tree, and
log every notification byte the device sends — enough to reverse-engineer
the protocol or share with us in an issue. See
[doc/SNOOP.md](doc/SNOOP.md).

## BLE Stack

The global `ble_stack` option picks the Bluetooth backend:

* `bleak` (default): BlueZ over D-Bus. Shares the adapter with Home Assistant's Bluetooth integration. Start here.
* `bumble`: own HCI stack ([bumble](https://github.com/google/bumble)), no BlueZ. Takes the adapter exclusively:
  dedicate one to it and disable it in HA under Integrations / Bluetooth. Most reliable with many BMS.
* `bluek`: kernel BlueZ sockets without D-Bus ([fl4p/bluek](https://github.com/fl4p/bluek/)). Shares the adapter with
  HA. Linux only. Has helped with connection timeouts.
* `esphome`: connects through [ESPHome Bluetooth Proxies](https://esphome.io/components/bluetooth_proxy.html), so
  BMS can be anywhere an ESP32 reaches. See below.

`bumble`, `bluek` and `esphome` are experimental.

### ESPHome proxies

```yaml
ble_stack: esphome
bluetooth_proxies:
  - host: garage-proxy.local
    noise_psk: "<base64 Noise key from the proxy's ESPHome config>"
    name: "garage"          # log label only
  - host: 192.168.1.43
    noise_psk: "<another key>"
```

* A stock ESPHome proxy serves one API client. If HA uses it too, HA and the add-on take it from each other on every
  reconnect. Remove the proxy from HA, or flash [nimble-ble-proxy](https://fl4p.github.io/nimble-ble-proxy-esphome/)
  ≥ 0.2.0, which serves up to 4 clients.
* Each proxy has 3 connection slots by default.
* Set `bluetooth_proxy: { active: true, cache_services: false }` in the proxy firmware. Details and a known
  incompatibility (ANT-BLE20PHUB) in [bmslib/ble/esphome_proxy/README.md](bmslib/ble/esphome_proxy/README.md).

### `adapter:` per BMS

| `ble_stack` | `adapter:` | If omitted |
|---|---|---|
| `bleak`, `bluek` | adapter name, e.g. `hci1` | default adapter |
| `bumble` | adapter name; bumble takes it exclusively | first available |
| `esphome` | leave unset; the best-RSSI proxy is picked per connect | |

For wired BMS, `adapter:` is the serial port, see [Serial / RS485](#serial--rs485).

A top-level `adapter:` is the default for every device without its own: a controller (`hci1` or its MAC) for BLE
devices, a port path for wired ones. The log lists which devices inherited it.

## Energy Meters

Batmon implements energy metering by computing the integral of power values from the BMS with the trapezoidal rule. You
can add theses meters to your Home Assistant Energy Dashboard or use them with the HA Helper *Utility Meter*,
see [doc/HA Energy Dashboard.md](doc/HA%20Energy%20Dashboard.md).

* `Total Energy Discharge` Meter: total Energy out of the battery (increasing only, use this for the Energy Dashboard)
* `Total Energy Charge`: total Energy into the battery (increasing only, use this for the Energy Dashboard)
* `Total Energy`: The total energy flow into and out of the battery (decreasing and increasing).
  This equals to `(Total Energy Charge) - (Total Energy Discharge)`. It will increase over time because batteries are
  not ideal. You can create a derivative helper to compute energy flow within e.g. 24h.
* `Total Cycles`: Total full cycles of the battery. One complete discharge and charge is a full cycle: SoC 100%-0%-100%.
  This is not a value provided by the BMS, Batmon computes this by differentiating the SoC (
  e.g. `integrate(abs(diff(SoC% / 100 / 2)))`).

The accuracy depends on the accuracy of the voltage and current readings from the BMS.
Consider these having an error of 2~5%. Some BMS do not detect small currents (<200mA) and can miss high frequency
peaks, leading to even greater error.

## Troubleshooting

* Power cycle (turn off and on) the BMS Bluetooth hardware/dongle (or BMS)
* Enable `bt_power_cycle`. If it doesn't work, manually power cycle Bluetooth on the host you are running batmon
  on [#91](https://github.com/fl4p/batmon-ha/discussions/91).
* When experiencing unstable connection enable `keep_alive`
* `TimeoutError: timeout waiting`: put BT devices closer, disable inverters and other EMI sources
* Try another `ble_stack`: `bumble` for exclusive adapter access (you need to remove it from HA Integration first), or
  `bluek` to bypass D-Bus on Linux (has helped with timeouts)
* Enable `verbose_log` and check the logs. If that is too noisy set `debug: true` in the BMS configuration as described
  above
* Try to find the BMS with a BLE
  scan ([Chrome Browser](chrome://bluetooth-internals/#devices), [linux](https://ukbaz.github.io/howto/beacon_scan_cmd_line.html))
* After a long-lasting bluetooth connection is lost both Daly and JBD dongles occasionally refuse to accept new
  connections and disappear from bluetooth discovery. Remove wires from the dongle and reconnect for a restart.
* Some users reported unstable Bluetooth connection with Raspberry Pi 4 onboard bluetooth hardware and WiFi enabled. It
  appears that disabling WiFi helps. ([#42](https://github.com/fl4p/batmon-ha/issues/42))
* Cheap inverters might cause heavy EMI (electromagnetic interference). Turn them off or keep them away from the
  bluetooth
  hardware
* Either bleak or bluetooth support in HA docker seems unstable. see related
  issues [106](https://github.com/fl4p/batmon-ha/issues/106) [109](https://github.com/fl4p/batmon-ha/issues/109)
* A bonded (`pin:`) device that still fails with `failed to discover services, device disconnected`: disable Home
  Assistant's own Bluetooth integration to free the adapter from scan contention
  ([#415](https://github.com/fl4p/batmon-ha/issues/415)).
* Try another bluetooth hardware. Note you can choose the adapter with `adapter` parameter for each BMS individually
* [doc/Downgrade.md](doc/Downgrade.md) to an earlier version
* to see more log entries, run this in the Terminal add-on: `ha host logs --identifier addon_<slug>_batmon`. You'll find
  the slug in the URL of the add-on page.
* to see logs during installation: Settings / System / Logs / Supervisor (choose from the menu at the top-right
  corner), [link](`http://homeassistant.local:8123/config/logs?provider=supervisor`)

## TODO

* Implement daly2 [#33](https://github.com/fl4p/batmon-ha/issues/33)
* Port to MicroPython for MCU (ESP32 etc.)
* make this a custom
  integration? [home-assistant-bms-tools-integration](https://github.com/ElD4n1/home-assistant-bms-tools-integration)
* use the new [Bluetooth integration since HA 2022.8 ](https://www.home-assistant.io/integrations/bluetooth/) ?
* Implement BMS data push (JK)
* Read device bt info [see](https://www.bluetooth.com/specifications/specs/device-information-service-1-1/)
* Implement RS485 for more BMS families [#22](https://github.com/fl4p/batmon-ha/issues/22) — JK (`jk_uart`) and Daly (
  `daly_uart`) are done; JBD, ANT still TODO
* Implement old JK04?
* web interface (export, import bms meter data)

## Stand-alone

You can run the add-on outside of Home Assistant (e.g. on a remote RPI sending MQTT data of WiFI).
All you need is an operating system supported by [bleak](https://pypi.org/project/bleak/).
See [doc/Standalone.md](doc/Standalone.md)

To run it in a container instead, see [doc/Docker.md](doc/Docker.md): prebuilt
multi-arch images (`ghcr.io/fl4p/batmon-ha`), compose examples, and a Helm chart.

# Contribute / Donate

* [PayPal](https://www.paypal.com/donate/?hosted_button_id=6LACACFHQMR3C)
* [Patreon](patreon.com/user?u=88448325) (Donations & News)

## References

* [daly bms: similar add-on](https://github.com/MindFreeze/dalybms)
* [JK-BMS: similar add-on using ESP-Home](https://github.com/syssi/esphome-jk-bms) (needs extra hardware)
* [Daly_RS485_UART_Protocol.pdf](https://github.com/jblance/mpp-solar/blob/master/docs/protocols/DALY-Daly_RS485_UART_Protocol.pdf)
* [JK-bms esphome](https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L336)
* [JK02 protocol](https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jk02.py)
* [ANT BMS 7E A1 protocol spec](https://github.com/yosle/ant-bms-ble/blob/main/PROTOCOL.md)

[install-shield]: https://img.shields.io/badge/dynamic/json?style=for-the-badge&color=green&label=Analytics&suffix=%20Installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/addons.json&query=$.2af0a32d_batmon.total
