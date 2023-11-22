# Home Assistant Add-on: BatMON

![Home Assistant Dashboard Screenshot](https://repository-images.githubusercontent.com/445289350/03f3d531-37cf-48be-84c8-e6c75270fc87)

Monitor and control various Battery management systems (BMS) over Bluetooth. This add-on reads the BMS and sends sensor
data through MQTT to Home Assistant. Using bluetooth on the Home Assistant host system, it does not need any additional
hardware (no USB/Serial/RS485).

I created this to compare BMS readings for a detailed evaluation of BMS reliability and accuracy.

## Features

* Uses Bluetooth Low-Energy (BLE) for wireless communication
* Captures SoC, Current, Power, individual cell voltages and temperatures
* Monitor multiple devices at the same time
* Energy consumption meters (using trapezoidal power integrators)
* Integrates with Home Assistant Energy dashboard and [Utility Meter](doc/HA%20Energy%20Dashboard.md) sensor helper
* Control BMS charging and discharging switches
* Home Assistant MQTT Discovery
* Can write data to [InfluxDB](doc/InfluxDB.md)
* Battery Groups, see [doc/Groups.md](doc/Groups.md)
* Charge Algorithms, see [doc/Algorithms.md](doc/Algorithms.md)
* Short delays for responsive automation (fast load shedding)

### Supported Devices (bluetooth low energy)

* JK BMS / jikong (JK02 protocol)
* Daly BMS
* JBD / Jiabaida/ Xiaoxiang / Overkill Solar BMS
* ANT BMS
* Supervolt BMS
* SOK BMS
* Victron SmartShunt (make sure to update to the latest firmware
  and [enable GATT](https://community.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html)
  in the VictronConnect app)

I tested the add-on on a Raspberry Pi 4 using Home Assistant Operating System.

## Installation

* Go to your Home Assistant Add-on store and add this
  repository: [`https://github.com/fl4p/home-assistant-addons`](https://github.com/fl4p/home-assistant-addons)
  [![Open your Home Assistant instance and show the dashboard of a Supervisor add-on.](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=2af0a32d_batmon&repository_url=https%3A%2F%2Fgithub.com%2Ffl4p%2Fhome-assistant-addons)
* Install Batmon add-on
* Install, configure and start Mosquito MQTT broker (don't forget to configure the MQTT integration)

## Configuration

The add-on can read multiple BMS at the same time.
Add an entry for each device, such as:

```
- address: CC:44:8C:F7:AD:BB
  type: jk
  alias: battery1
  pin: "12345"               # pairing PSK, victron only (optional)
  adapter: "hci0"            # switch the bluetooth hw adapter (optional)
  debug: true                # verbose log for this device only (optional)
  current_calibration: 1.0   # current [I] correction factor (optional)
```

`address` is the MAC address of the Bluetooth device. If you don't know the MAC address start the add-on, and you'll
find a list of visible Bluetooth devices in the add-on log. Alternatively you can enter the device name here as
displayed in the discovery list.

`type` can be `jk`, `jbd`, `ant`, `daly`, `supervolt`, `sok`, `victron` or `dummy`.

With the `alias` field you can set the name as displayed in Home Assistant. Otherwise, the name as found in Bluetooth
discovery is used.

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
  after it exists)
* Enable `install_newer_bleak` to install bleak 0.20.2, which is more stable than the default version. The default
  version is known to be working with Victron SmartShunt.

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
* Enable `verbose_log` and check the logs. If that is too noisy set `debug: true` in the BMS configuration as described
  above
* Toggle `install_newer_bleak` option
* Try to find the BMS with a BLE scan [linux](https://ukbaz.github.io/howto/beacon_scan_cmd_line.html)
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
* [doc/Downgrade.md](doc/Downgrade.md) to ab earlier version

## TODO

* Implement daly2 [#33](https://github.com/fl4p/batmon-ha/issues/33)
* Port to MicroPython for MCU (ESP32 etc.)
* make this a custom
  integration? [home-assistant-bms-tools-integration](https://github.com/ElD4n1/home-assistant-bms-tools-integration)
* use the new [Bluetooth integration since HA 2022.8 ](https://www.home-assistant.io/integrations/bluetooth/) ?
* Implement BMS data push (JK)
* Read device bt info [see](https://www.bluetooth.com/specifications/specs/device-information-service-1-1/)
* Implement RS485 [#22](https://github.com/fl4p/batmon-ha/issues/22)
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
