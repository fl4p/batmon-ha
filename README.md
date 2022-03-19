# Home Assistant Add-on: BatMON

![Home Assistant Dashboard Screenshot](https://repository-images.githubusercontent.com/445289350/03f3d531-37cf-48be-84c8-e6c75270fc87)

Monitor various Battery management systems (BMS). This add on reads the BMS and sends sensor data through MQTT to Home
Assistant.

I created this to compare BMS readings for a detailed evaluation of BMS reliability and accuracy.

## Features

* Records SoC, Current, Power, individual cell voltages and temperatures
* Monitor multiple devices at the same time
* MQTT Discovery

### Supported Devices

* Daly BMS (bluetooth)
* JBD / Xiaoxiang BMS (bluetooth)
* Victron SmartShunt (bluetooth)

I tested the add on on a Raspberry Pi 4 using Home Assistant Operating System.

## Installation

* Go to your Home Assistant Add-on store and add this repository: `https://github.com/fl4p/home-assistant-addons`
* Install Batmon add-on

## Configuration

The add on can either fetch a Daly BMS, JBD (xiaoxiang) BMS or both at the same time.

* In the add-on configration set either `daly_address` or `jbd_address` MAC address or both. If you don't know the MAC
  address, just put any random characters, start the add-on and you'll find a list of visible Bluetooth devices in the
  add-on log. For verbose logs of a particular BMS append `?` to the address, e.g.  `'A4:E1:93:44:52:C8?'`
* Set MQTT user and password. MQTT broker is usually `core-mosquitto`.
* `concurrent_sampling` tries to read all BMSs at the same time (instead of a serial read one after another). This can
  increase sampling rate for more timely-accurate data. Might cause Bluetooth connection issues if `keep_alive` is
  disabled.
* `keep_alive` will never close the bluetooth connection. Use for higher sampling rate. You will not be able to connect
  to the BMS from your phone anymore while the add-on is running.
* `sample_period` is the time in seconds to wait between BMS reads. Small periods generate more data points per time.

## Kown Issues

* After a long-lasting bluetooth connection is lost both Daly and JBD dongles occasionally refuse to accept new
  connections and disappear from bluetooth discovery. Remove wires from the dongle and reconnect for a restart.

## Links

* [dalybms: similar add-on](https://github.com/MindFreeze/dalybms)
* [Daly_RS485_UART_Protocol.pdf](https://github.com/jblance/mpp-solar/blob/master/docs/protocols/DALY-Daly_RS485_UART_Protocol.pdf)
