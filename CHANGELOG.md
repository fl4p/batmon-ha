# Changelog

## [0.0.45] - 2022-09-20
### Changed
- Sensor value now use availability status so status expires when BMS is not available
- Fix zero negative current
- Set Keep alive and invert_current default value to true
- Fix Daly zero SoC issue
- Fix JK current direction
- Add `fetch_device_info`
- Add Icons for some sensors
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
- Changed options schema for MAC addresses. You need to re-enter all addresses after the update. The new schema allows adding multiple BMS of the same type.
- JK use nominal capacity instead of user-set capacity

## [0.0.28] - 2022-08-06
### Added
- Support for JK-BMS (Jikong) using JK02 protocol