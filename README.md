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

## Setup

* Install the add on the usual way
* In the add-on configration set either daly_address or jbd_address MAC address or both. If you don't know the MAC
  address, just put any random characters, start the add-on and you'll find a list of visible Bluetooth devices in the
  add-on log.
* Set MQTT user and password. MQTT broker is usually `core-mosquitto`.

## Links

* [Daly: similar add-on](https://github.com/MindFreeze/dalybms)
* [DALY-Daly_RS485_UART_Protocol.pdf](https://github.com/jblance/mpp-solar/blob/master/docs/protocols/DALY-Daly_RS485_UART_Protocol.pdf)