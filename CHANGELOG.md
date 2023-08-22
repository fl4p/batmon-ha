# Changelog

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