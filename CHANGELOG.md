# Changelog

## [1.81]
* Create separate venv with a modified bleak version for pairing.
  Speeds up start-up and doesn't break with lost internet connection
* Pin bleak version to  `0.22.3`
* Remove `install_newer_bleak` version
* Capture fetch_voltage errors and re-connect
* Add `jk_24s` and `jk_32s` BMS types to explicitly set the JK version (disable auto-detect)
* Add `daly2` type

## [1.80] - 2024-12-28

* Fix JK firmware detection (merge #267)

## [1.79] - 2024-12-09

* Fix #259 OverflowError
* pin paho version 2.1
* fix daly2 @patman15
* changed jk frame version detection

## [1.78] - 2024-02-18

* Fix `NameError: name 'logger' is not defined`

## [1.77] - 2024-02-16

* Fix meter rounding (https://github.com/fl4p/batmon-ha/issues/169)
* Fix paho 2.0 compatibility (https://github.com/fl4p/batmon-ha/issues/195)
* JK add temperature sensor 3 and 4

## [1.76] - 2024-01-17

* Add JK Balance switch
* allow forward slash `/` in mqtt topic prefix

## [1.75] - 2023-12-16

* fix HA install error (https://github.com/fl4p/batmon-ha/issues/175)
* fix meter expiry
* optimized sampling schedule
* add temperature noise filter

## [1.74] - 2023-11-26

* Add Daly checksum verification (https://github.com/fl4p/batmon-ha/issues/158)
* Fix daly bms num_responses (https://github.com/fl4p/batmon-ha/issues/163)
* Ignore voltage fetch errors and continue (https://github.com/fl4p/batmon-ha/issues/163)
* Fix JK 11.x FW mos temp zero (https://github.com/fl4p/batmon-ha/issues/157)
* Copy meter states file before write
* JK fix bouncing switches
* Reduce log verbosity
* Reduce temp fetch and meter publish interval
* Cleanup error handling
* Continue on errors with pip in install_bleak
* InfluxDB add GZIP compression

## [1.73] - 2023-11-10

this is a rather big update. I've set version num to 1.0, so it looks more tidy.

* add manufacturer to device info
* start scanner if device not discovered
* influxdb publish meters
* add additional watchdog thread (to detect event loop issues)
* incr meter integral recovery time to 10 minutes
* add unit tests
* futures pool add  `acquire_timeout`
* BmsSample rename `num` to `num_samples` (breaking change)
* JK capture timestamps
* daly assert soc range (https://github.com/fl4p/batmon-ha/issues/158)
* import mqtt credentials from hassio service
* fix soc group bms (https://github.com/fl4p/batmon-ha/issues/155)
* sample expiry fix
* fix uptime (https://github.com/fl4p/batmon-ha/issues/157)
* fix temperature sensor discovery
* fix JK switches
* fix ANT switches
* victron fixes
* soc algo fix and assert thresholds

## [0.0.72] - 2023-10-19

* Fix JK BLE characteristic handles
* Remove `influxdb` requirement and install if needed
* Fix SoC for groups
* Implement down-sampler (mean)
* Initial device fetch in random order
* Fix victron sampling, add timestamp

## [0.0.71] - 2023-10-04

* Add SOK Bms (initial work by @mdshw5)
* Fix battery groups with missing temperature values
* Daly enumerate services
* Add `influxdb` dependency
* Implement calibration for Soc Algorithm
* fix `MQTT entity name starts with the device name` (#142)

## [0.0.70] - 2023-09-20

* Fix Supvervolt voltages
* Add suppport for Supervolt BMS sending chunked strings
* Try to fetch BMS device info first (for debugging)

## [0.0.69] - 2023-09-02

* Add restart loop to prevent batmon from stopping (#127)
* Fix ANT BMS cell voltages
* Add watchdog timer
* Add InfluxDB sink (undocumented)
* Fix `add_parallel` with empty temperatures

## [0.0.68] - 2023-08-22

* Add ANT BMS switches (untested)
* Fix `current_calibration_factor` and energy dashboard meters
* Fix mqtt names (#126)

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