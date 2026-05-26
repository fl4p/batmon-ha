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
* Daly BMS (`daly`, `daly2`, `daly_ble` over BLE, `daly_uart` over RS485 — see [Serial / RS485](#serial--rs485))
* JBD / Jiabaida/ Xiaoxiang / Overkill Solar BMS (`jbd`)
* ANT BMS (`ant`)
* Supervolt BMS (`supervolt`)
* SOK BMS (`sok`)
* LiTime BMS (`litime`)
* Victron SmartShunt (make sure to update to the latest firmware
  and [enable GATT](https://community.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html)
  in the VictronConnect app) (`victron`)

More `aiobmsble` device connectors:

* CBT Power / Creabest smart BMS (`cbtpwr`)
* Seplos smart BMS V3 (`seplos`), Seplos smart BMS V2 (`seplos_v2`)
* TianPwr smart BMS (`tianpwr`)
* ATORCH CW20 DC Meter (`cw20`)
* TDT smart BMS (`tdt`)
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

The add-on can read multiple BMS at the same time.
Add an entry for each device, such as:

```
- address: CC:44:8C:F7:AD:BB
  type: jk
  alias: battery1            # MQTT topic prefix (regex [\w_.-/])
  pin: "12345"               # pairing PSK, victron only (optional)
  adapter: "hci0"            # switch the bluetooth hw adapter (optional)
  debug: true                # verbose log for this device only (optional)
  current_calibration: 1.0   # current [I] correction factor (optional)
```

`address` is the MAC address of the Bluetooth device. If you don't know the MAC address start the add-on, and you'll
find a list of visible Bluetooth devices in the add-on log. Alternatively you can enter the device name here as
displayed in the discovery list.

`type` can be `jk`, `jk_24s`, `jk_32s`, `jk_uart`, `jbd`, `ant`, `daly`, `daly2`, `daly_ble`, `daly_uart`,
`supervolt`, `sok`, `litime`, `victron`, or any tag listed under [Supported BLE Devices](#supported-ble-devices).
For a mock BMS use `dummy`.

With the `alias` field you can set the MQTT topic prefix and the name as displayed in Home Assistant.
Otherwise, the name as found in Bluetooth discovery is used.

If the device requires a PIN when pairing (currently Victron SmartShunt only) add `pin: "123456"` (and replace 123456
with device's PIN).

Add `adapter: "hci1"` to select a bluetooth adapter other than the default one.

With `current_calibration` you can calibrate the current sensor. The current reading is multiplied by this factor. Set
it to `-1` to flip the sign if you experience wrong charge/discharge meters.

For verbose logs of particular BMS add `debug: true`.

* Set MQTT user and password. MQTT broker is usually `core-mosquitto`.
* `concurrent_sampling` tries to read all BMSs at the same time (instead of a serial read one after another). This can
  increase sampling rate for more timely-accurate data. Might cause Bluetooth connection issues if `keep_alive` is
  disabled.
* `keep_alive` will never close the bluetooth connection. Use for higher sampling rate. You will not be able to connect
  to the BMS from your phone anymore while the add-on is running.
* `sample_period` is the time in seconds to wait between BMS reads. Small periods generate more data points per time.
* Set `publish_period` to a higher value than `sample_period` to throttle MQTT data, while sampling BMS for accurate
  energy meters. On publish, samples since previous publish are averaged. Periods shorter than 2s can slow down history
  plots in HA.
* `invert_current` changes the sign of the current. Normally it is positive during discharge, inverted its negative.
* `expire_values_after` time span in seconds when sensor values become "Unavailable"
* `watchdog` stops the program on too many errors (make sure to enable the Home Assistant watchdog to restart the add-on
  after it exits)
* For JK bms: set `type` to `jk_24s` for the older 24s version (firmware<11.x), `jk_32s` for the newer 32s version (fw>
  =11.x), or `jk` if you don't know (might cause invalid battery data when detection fails)
* type `daly2` is for a newer Daly BMS version which is untested

## Serial / RS485

Some BMS expose an RS485 (or TTL UART) port in addition to BLE. Batmon can
read those directly using a USB-to-RS485 adapter, no Bluetooth needed.

Currently supported:

* `jk_uart` — JK / Jikong BMS over RS485. Speaks the genuine UART TLV
  protocol (`4E 57 …`), which is a different wire format from the BLE one
  (`55 AA EB 90 …`). Cross-referenced against `syssi/esphome-jk-bms`,
  `jblance/mpp-solar`, and `Louisvdw/dbus-serialbattery`.

* `daly_uart` — Daly BMS over RS485 / USB-UART (**9600 8N1**, per the Daly
  protocol PDF + `maland16/daly-bms-uart`). Same `A5 …` 13-byte frame
  format as Daly BLE; the only on-wire difference is the host-address byte
  (4 = USB/RS485, 8 = BLE). Cross-referenced against
  `maland16/daly-bms-uart`, `dreadnought/python-daly-bms`, and
  `syssi/esphome-daly-bms`.

Example config:

```yaml
- address: serial
  adapter: /dev/ttyUSB0   # serial port path; required when address=serial
  type: jk_uart
  alias: battery1
```

Notes:

* `address: serial` tells batmon to use the wired transport instead of
  Bluetooth. `adapter` is then the serial port path (`/dev/ttyUSB0`,
  `/dev/ttyAMA0`, `COM3`, …) rather than a Bluetooth HCI index.
* The baud rate is picked per BMS — `jk_uart` uses 115200, `daly_uart`
  uses 9600 8N1 (both match the respective vendor protocol docs).
* On Linux you may need to add your user to the `dialout` group or run the
  HA add-on with privileged access to read `/dev/ttyUSB*`.
* This path is independent of the BLE backend selected by `ble_stack`, so
  it works even when Bluetooth is disabled.

If you'd like another BMS family added over RS485, open an issue with a
captured frame (`tcpdump` of the USB-serial line, or a wireshark log from
the vendor's PC tool).

## Adding a new BMS

If your BMS isn't supported yet, set `type: snoop` (optionally
`type: snoop:jbd,jk,daly,ant,sok,supervolt` to also write known probe
frames) on the device entry. Batmon will connect, dump the GATT tree, and
log every notification byte the device sends — enough to reverse-engineer
the protocol or share with us in an issue. See
[doc/SNOOP.md](doc/SNOOP.md).

## BLE Stack

Batmon can talk to your BMS through one of four Bluetooth backends. Pick one with the global
`ble_stack` option:

* **`bleak`** (default) — uses [bleak](https://pypi.org/project/bleak/), a cross-platform Python
  BLE library that wraps the OS's native stack: BlueZ on Linux, CoreBluetooth on macOS, WinRT on
  Windows. On Linux (and therefore inside the HA add-on) it talks to `bluetoothd` over D-Bus and
  coexists with Home Assistant's Bluetooth integration — the adapter stays in the HA Bluetooth
  pool and is shared. This is the most compatible option and what you want unless you're chasing a
  specific problem.
* **`bumble`** — uses [bumble](https://github.com/google/bumble), a pure-Python BLE stack that
  talks HCI directly (no BlueZ, no D-Bus). Cross-platform (Linux/macOS/Windows via HCI socket,
  USB dongle, or serial transports). Always needs **exclusive HCI access** to its controller —
  on Linux that means bumble brings the BlueZ-managed adapter down, so it leaves the HA
  Bluetooth pool. You need to dedicate one adapter to it and disable it in HA under Integrations / Bluetooth;
  Use it for best reliability and if you have many BMS.
* **`bluek`** — talks to the kernel BlueZ stack directly over L2CAP and `mgmt` sockets (no D-Bus).
  Coexists with `bluetoothd`, so the adapter stays in the HA Bluetooth pool. Useful when D-Bus is
  the bottleneck but you don't want to take the adapter away from HA. **Linux only** (BlueZ is
  Linux-specific). [fl4p/bluek](https://github.com/fl4p/bluek/)
* **`esphome`** — routes all BLE through one or more
  [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html) devices
  (typically a cheap ESP32 flashed with the upstream
  [bluetooth-proxies](https://github.com/esphome/bluetooth-proxies) firmware). The add-on
  doesn't touch any local Bluetooth adapter — you can extend BLE reach to wherever your BMSes
  physically live (other rooms, outdoors) just by powering an ESP32 there. Coexists with HA's
  Bluetooth integration; each proxy can host multiple clients but BLE connection slots are shared
  (default 3 per proxy, see [esphome_proxy/README.md](bmslib/esphome_proxy/bootstrap.py) for
  notes on bumping). Configure the proxies under the `bluetooth_proxies:` add-on option:

  ```yaml
  ble_stack: esphome
  bluetooth_proxies:
    - host: garage-proxy.local
      noise_psk: "<base64 Noise key from the proxy's ESPHome config>"
      name: "garage"          # diagnostic label only
    - host: 192.168.1.43
      noise_psk: "<another key>"
  ```

  Proxy firmware should set `bluetooth_proxy: { active: true, cache_services: false }` — see
  [bmslib/esphome_proxy/README.md](bmslib/esphome_proxy/README.md) for the rationale and a known
  incompatibility (ANT-BLE20PHUB BMS).

`bumble`, `bluek` and `esphome` are experimental — try `bleak` first. Users have already reported
that `bluek` helps in case of connection timeouts.

### `adapter:` per BMS

The per-device `adapter:` option only applies to backends that have a notion of a local Bluetooth
adapter:

| `ble_stack` | What `adapter:` accepts | If omitted |
|---|---|---|
| `bleak` | A BlueZ adapter name like `hci0`, `hci1`. | Uses BlueZ's `[default]` adapter |
| `bluek` | Same as `bleak` — kernel adapter name. | Uses the first available |
| `bumble` | Same — pick one adapter (bumble will take it exclusively). | First available |
| `esphome` | **Leave unset.** There's no local adapter; the proxy host stack picks the best-RSSI proxy automatically per connect. | n/a — auto-routed |

(For `address: serial` / RS-485 BMSes, `adapter:` is the serial port path like `/dev/ttyUSB0`,
independent of `ble_stack`.)

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

# Contribute / Donate

* [PayPal](https://www.paypal.com/donate/?hosted_button_id=6LACACFHQMR3C)
* [Patreon](patreon.com/user?u=88448325) (Donations & News)

## References

* [daly bms: similar add-on](https://github.com/MindFreeze/dalybms)
* [JK-BMS: similar add-on using ESP-Home](https://github.com/syssi/esphome-jk-bms) (needs extra hardware)
* [Daly_RS485_UART_Protocol.pdf](https://github.com/jblance/mpp-solar/blob/master/docs/protocols/DALY-Daly_RS485_UART_Protocol.pdf)
* [JK-bms esphome](https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L336)
* [JK02 protocol](https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jk02.py)

[install-shield]: https://img.shields.io/badge/dynamic/json?style=for-the-badge&color=green&label=Analytics&suffix=%20Installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/addons.json&query=$.2af0a32d_batmon.total
