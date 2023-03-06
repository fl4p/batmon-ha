# Changelog

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