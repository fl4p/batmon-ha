# Home Assistant Add-on: BatMON

![Home Assistant Dashboard Screenshot](https://repository-images.githubusercontent.com/445289350/03f3d531-37cf-48be-84c8-e6c75270fc87)

Monitor and control various Battery management systems (BMS) over Bluetooth. This add-on reads the BMS and sends sensor
data through MQTT to Home Assistant. Using bluetooth on the Home Assistant host system, it does not need any additional
hardware.

I created this to compare BMS readings for a detailed evaluation of BMS reliability and accuracy.

## Features

* Uses Bluetooth Low-Energy (BLE) for wireless communication
* Records SoC, Current, Power, individual cell voltages and temperatures
* Monitor multiple devices at the same time
* Energy consumption meters (using trapezoidal power integrators)
* Control BMS charging and discharging switches
* Home Assistant MQTT Discovery

### Supported Devices (bluetooth)

* JK BMS (jikong) (JK02 protocol)
* Daly BMS
* JBD / Xiaoxiang BMS
* Victron SmartShunt (make sure to update to latest firmware and [enable GATT](https://community.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html) in the VictronConnect app)

I tested the add-on on a Raspberry Pi 4 using Home Assistant Operating System.

## Installation

* Go to your Home Assistant Add-on store and add this repository: `https://github.com/fl4p/home-assistant-addons` 
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
```

`address` is the MAC address of the Bluetooth device. If you don't know the MAC address start the add-on, and you'll
find a list of visible Bluetooth devices in the add-on log. Alternatively you can enter the device name here as
displayed in the discovery list.

`type` can be `jk`, `jbd`, `daly`, `victron` or `dummy`.

With the `alias` field you can set the name as displayed in Home Assistant. Otherwise, the name as found in Bluetooth
discovery is used.

If the device requires a PIN when pairing add `pin: 123456` (and replace 123456 with device's PIN)

For verbose logs of particular BMS add `debug: true`.

* Set MQTT user and password. MQTT broker is usually `core-mosquitto`.
* `concurrent_sampling` tries to read all BMSs at the same time (instead of a serial read one after another). This can
  increase sampling rate for more timely-accurate data. Might cause Bluetooth connection issues if `keep_alive` is
  disabled.
* `keep_alive` will never close the bluetooth connection. Use for higher sampling rate. You will not be able to connect
  to the BMS from your phone anymore while the add-on is running.
* `sample_period` is the time in seconds to wait between BMS reads. Small periods generate more data points per time.
* Set `publish_period` to a higher value than `sample_period` to throttle MQTT data, while sampling BMS for accurate
  energy meters.
* `invert_current` changes the sign of the current. Normally it is positive during discharge, inverted its negative.
* `expire_values_after` time span in seconds when sensor values become "Unavailable"
* `watchdog` stops the program on too many errors (make sure to enable the Home Assistant watchdog to restart the add-on
  after it exists)

## Troubleshooting
* When experiencing connection issues enable `keep_alive`
* Enable `verbose_log` and check the logs. If that is too noisy set `debug: true` in the BMS configuration as described above
* Power cycle the BMS Bluetooth dongle (or BMS)
* Try another Bluetooth hardware
* Try to find the BMS with a BLE scan [linux](https://ukbaz.github.io/howto/beacon_scan_cmd_line.html)


## Known Issues

* After a long-lasting bluetooth connection is lost both Daly and JBD dongles occasionally refuse to accept new
  connections and disappear from bluetooth discovery. Remove wires from the dongle and reconnect for a restart.
* Raspberry PI's bluetooth can be buggy. If you experience errors and timeouts try to install an external Bluetooth
  dongle.

## TODO

* use the new Bluetooth integration since HA 2022.8 https://www.home-assistant.io/integrations/bluetooth/

## Stand-alone

You can run the add-on outside of Home Assistant (e.g. on a remote RPI sending MQTT data of WiFI).
See [doc/Standalone.md](doc/Standalone.md)

## References

* [dalybms: similar add-on](https://github.com/MindFreeze/dalybms)
* [JK-BMS: similar add-on using ESP-Home](https://github.com/syssi/esphome-jk-bms) (needs extra hardware)
* [Daly_RS485_UART_Protocol.pdf](https://github.com/jblance/mpp-solar/blob/master/docs/protocols/DALY-Daly_RS485_UART_Protocol.pdf)
* [JK-bms esphome](https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L336)
* [JK02 protocol](https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jk02.py)