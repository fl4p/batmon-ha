# Changelog

## [0.0.37] - 2022-07-18
### Added
- Added user option `invert_current` to change the direction of battery current

### Changed
- Fixed `already waiting` error
- Increase max_errors before exit to 40

## [0.0.36] - 2022-07-08
### Added
- Support for multiple BMS
- Added cycle_capacity

### Changed
- Changed options schema for MAC addresses. You need to re-enter all addresses after the update. The new schema allows adding multiple BMS of the same type.
- JK use nominal capacity instead of user-set capacity

## [0.0.28] - 2022-07-06
### Added
- Support for JK-BMS (Jikong) using JK02 protocol