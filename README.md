# Home Assistant Add-on: BatMON

Monitor various Battery management systems (BMS). This add on reads the BMS and sends sensor data through MQTT to Home
Assistant.

I created this to compare BMS readings for a detailed evaluation of BMS reliability and accuracy.

## Features

* Records SoC, Current, Power and individual cell voltages
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
  add-on log.
* Set MQTT user and password. MQTT broker is usually `core-mosquitto`.
* `concurrent_sampling` tries to read all BMSs at the same time (instead of a serial read one after another). This can increase sampling rate for more timely-accurate data. Might cause Bluetooth connection issues.
* `keep_alive` will never close the bluetooth connection. Use for higher sampling rate. You will not be able to connect to the BMS from your phone anymore while the add-on is running.

## Links

* [dalybms: similar add-on](https://github.com/MindFreeze/dalybms)
* [Daly_RS485_UART_Protocol.pdf](https://github.com/jblance/mpp-solar/blob/master/docs/protocols/DALY-Daly_RS485_UART_Protocol.pdf)
