## Findings

### High — (a) The native `BtBms` fix does not reach the actual client

`BtBms.__init__` canonicalizes only the public attribute:

- [`self.address = normalize_ble_address(address)`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/bt.py:408)

But it later creates the client with the original constructor argument:

- [`self.client = self._create_client(address)`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/bt.py:467)

`HaBleakClientWrapper.__init__` merely calls `str()` and preserves case; it performs no normalization ([habluetooth wrappers.py:294](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:294), [wrappers.py:305](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:305)).

A focused probe captured:

```text
self.address: D1:1A:04:01:01:B7
client target: d1:1a:04:01:01:b7
```

Consequently, every native `BtBms` model—`daly`, `daly2`, `jk`, `jbd`, `snoop`, etc.—still gives habluetooth the lower-case lookup key and can reproduce the original failure. Commit `32de0b6`’s premise that `48d7315` “covers BtBms” is false, as is the CHANGELOG claim that both paths are fixed.

The required correction is to construct the client from `self.address`, not `address`.

### High — (c) Existing lower-case group references can now abort startup

Physical devices are indexed by the newly canonicalized `bms.address`:

- [`bms_by_name = {bms.address: bms ...}`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:233)

Group member references remain verbatim:

- [`self.address.split(',')`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/group.py:94)

Resolution is exact and raises when the spelling differs:

- [`if member_ref not in bms_by_name:`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:244)
- [`raise Exception("unknown bms ...")`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:246)

Thus a previously working BlueZ configuration such as:

```yaml
- address: d1:1a:04:01:01:b7
  alias: battery1
- type: group_parallel
  address: d1:1a:04:01:01:b7
```

now indexes the physical BMS under `D1:1A:04:01:01:B7`, fails the lower-case group lookup, and terminates the add-on. My direct reconstruction confirmed the lower-case reference no longer resolves.

Group keys need consistent normalization or case-insensitive MAC matching. Alias-based group references remain safe.

### Low — (c) Anonymous telemetry identity changes across the upgrade

Telemetry hashes `bms.address` ([sinks.py:446](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sinks.py:446)). Lower-case users therefore receive a new address hash and measurement name after upgrading. This does not affect Home Assistant, but it splits longitudinal telemetry into old and new device identities.

### Low — (c) The regression test suite does not protect the actual failure

The test file imports `BLE_BMS_wrap` at module collection time ([test_ble_address_case.py:22](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/test/test_ble_address_case.py:22)), which imports `aiobmsble` unconditionally ([BLE_BMS_wrap.py:6](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/BLE_BMS_wrap.py:6)). `aiobmsble` is omitted from the normal requirements ([requirements.txt:20](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/requirements.txt:20)).

Running the new file in this checkout failed during collection:

```text
ModuleNotFoundError: No module named 'aiobmsble'
```

With `aiobmsble` installed, `test_aiobmsble_wrapper_normalizes_too` is not vacuous: it really executes the wrapper constructor and checks the normalized attribute. It does not need habluetooth. But it never exercises `BLEDeviceResolver.resolve`, an uppercase backend key, or connection establishment.

More importantly, there is no native-path assertion that captures the argument passed to `BleakClient`; such a test would have caught the High-severity defect above.

`test_adapter_mac_regex_was_not_rebound` tests global pattern names rather than `normalize_adapter()` behavior ([test_ble_address_case.py:67](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/test/test_ble_address_case.py:67)). It could pass if `normalize_adapter()` were later changed to use another broad regex directly. A behavioral test should mock `_hci_index_for_mac` and assert that hyphenated/mixed forms are not treated as controller MACs.

## Claims that survive scrutiny

### Root cause: confirmed

No requested habluetooth layer folds case:

- `async_ble_device_from_address`: exact `histories.get(address)` ([manager.py:807](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:807)).
- `async_scanner_devices_by_address`: forwards the address unchanged ([manager.py:388](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:388)).
- `HaScanner.get_discovered_device_advertisement_data`: exact dict lookup ([base_scanner.py:725](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/base_scanner.py:725)).
- `HaBleakClientWrapper.__init__`: preserves the string verbatim ([wrappers.py:305](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:305)).

In bluetooth-data-tools 1.29.21:

- The Python fallback uses `f"{address:012X}"` ([utils.py:17](/private/tmp/bes/bluetooth_data_tools-1.29.21/src/bluetooth_data_tools/utils.py:17)).
- The C implementation uses the uppercase table `0123456789ABCDEF` ([utils_wrapper.h:59](/private/tmp/bes/bluetooth_data_tools-1.29.21/src/bluetooth_data_tools/utils_wrapper.h:59)); `_utils_impl.pyx` returns that C buffer directly ([\_utils_impl.pyx:11](/private/tmp/bes/bluetooth_data_tools-1.29.21/src/bluetooth_data_tools/_utils_impl.pyx:11)).

The incident log independently shows the decisive mismatch: proxy discovery reports `D1:1A:04:01:01:B7` ([batmon399.log:59](/tmp/batmon399.log:59)), while the client attempts `d1:1a:04:01:01:b7` ([batmon399.log:66](/tmp/batmon399.log:66)).

Caveat: the add-on pins bluetooth-data-tools `<1.29`, while the locally available downloaded source is 1.29.21, and the user log does not record the installed bluetooth-data-tools version. I could not inspect the exact runtime release. The actual uppercase advertisement in the log makes that version gap non-dispositive for this incident.

### Evidence against slot exhaustion: confirmed, with one assumption that holds — (b)

Habluetooth builds `sorted_devices` first, then logs “Found N connection path(s)” whenever it is non-empty and INFO is enabled ([wrappers.py:585](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:585), [wrappers.py:602](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:602)). Only afterward does it reject candidates whose connector has no slot ([wrappers.py:625](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:625), [wrappers.py:564](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:564)).

Therefore, a genuine slot shortage with a reachable candidate would still emit the INFO line before producing the same final error.

The log contains zero such lines across repeated failures, while INFO from numerous child loggers is present and `habluetooth.wrappers` warnings propagate through the same formatted handler ([batmon399.log:49](/tmp/batmon399.log:49), [batmon399.log:66](/tmp/batmon399.log:66)). Neither the repository nor habluetooth sets a `habluetooth` logger level, disables propagation, or installs a separate handler. `get_logger()` explicitly sets the root to INFO ([util.py:64](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/util.py:64)).

The unformatted startup warning shows `BluetoothManager` is constructed before `basicConfig`, but Python’s fallback warning handler does not install a root handler; subsequent formatted INFO lines show that the later configuration took effect.

The remaining assumption is that `/tmp/batmon399.log` is not selectively filtered or truncated. It appears to contain contiguous startup and failure sequences, so that assumption holds. Your public retraction of the slot-exhaustion theory is well supported.

### Breaking-change scope

MQTT topics, HA discovery IDs, and persisted meter/algorithm state do not derive from `bms.address`:

- MQTT prefix derives from `bms.name` ([sampling.py:157](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sampling.py:157)).
- HA unique IDs derive from that prefix ([mqtt_util.py:333](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/mqtt_util.py:333)).
- Meter and algorithm state are keyed by BMS name ([main.py:70](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:70), [store.py:43](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/store.py:43)).

So this change does not silently rename normal HA entities or orphan their history. The concrete regressions are group address references and telemetry identity.

`_user_config_migrate_addresses` remains case-sensitive ([store.py:78](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/store.py:78)), but it runs before BMS construction and the commits do not rewrite the config dictionary. Its potential case-duplicate behavior is pre-existing, not introduced here.

### Other address paths

The other requested comparisons are safe:

- Model discovery lookup is case-insensitive ([models/\_\_init\_\_.py:138](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/__init__.py:138)).
- `scan.resolve_address` is case-insensitive ([scan.py:84](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/scan.py:84)).
- The scanner retry membership test is case-insensitive ([bt.py:684](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/bt.py:684)).
- Snoop inherits `BtBms`, so it shares the native-client defect rather than adding a separate comparison bug.
- `BLEDeviceResolver` remains exact-keyed, but the aiobmsble wrapper now supplies a canonical address ([BLE_BMS_wrap.py:67](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/BLE_BMS_wrap.py:67), [BLE_BMS_wrap.py:117](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/BLE_BMS_wrap.py:117)).

The MAC regex cannot match a normal device name or CoreBluetooth UUID. A deliberately MAC-shaped device name is first resolved through `name2addr`, before wrapper normalization. UUIDs and `serial` remain unchanged.

### `_MAC_RE` rename: correct

Python resolves `_MAC_RE` when `normalize_adapter()` executes, not when the function is defined. Before `32de0b6`, the later module assignment therefore replaced the colon-only controller regex with the broader BLE regex. That broader expression matches:

- Hyphen-only: `0C-EF-15-47-4A-46`
- Mixed separators: `0C-EF:15-47:4A-46`

The restored controller regex matches neither. The original colon-only pattern predates the BLE normalization change, so there is no evidence the widening was deliberate. One naming correction: the function is `normalize_adapter()`, not `resolve_adapter()`.

### `_discover_service_info` override: cosmetic

The base method is itself a no-op ([manager.py:755](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:755)). Habluetooth assigns either the base or override to `_subclass_discover_info` and calls it unconditionally ([manager.py:200](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:200), [manager.py:739](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:739)). Advertisement histories are updated before that call ([manager.py:660](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:660)).

Replacing one no-op bound method with another changes only the constructor identity check that emits the warning. It does not enable extra advertisement work or alter connection behavior. The “harmless/cosmetic” claim is correct.

## Bottom line

Your public root-cause diagnosis and slot-exhaustion retraction are defensible. The public claim that the fix now covers both native and aiobmsble paths is not: the native client still receives the lower-case string. Before release, I would block on:

1. Passing `self.address` to `_create_client`.
2. Making group MAC references canonical or case-insensitive.
3. Adding a native-client argument test and a real `BLEDeviceResolver` uppercase-key test.

No repository files were changed.

## Source access log

| Source | Evidence state and scope | Route / validation |
|---|---|---|
| Local git objects `48d7315`, `32de0b6`, `98b9ed7` | Inspected relevant diffs and all requested downstream address consumers | Raw local git data; validated |
| [habluetooth 6.1.0](https://pypi.org/project/habluetooth/6.1.0/) | Inspected manager, scanner, base scanner, wrappers, logging, slot selection, and subclass hook | Local cached sdist; SHA-256 `9b5ac9cb9a07bb9690f04e8587abdb5ffebb69e66163f55fbedf99d90fc554c9`; validated by `PKG-INFO` |
| [bluetooth-data-tools 1.29.21](https://pypi.org/project/bluetooth-data-tools/1.29.21/) | Inspected Python fallback, Cython entry point, and C formatter | Local cached sdist; SHA-256 `2ccf1e90212092cb6af825fd93f3f2f392c543fe2a3a894e7831cbc6f6851c15`; validated by `PKG-INFO` |
| `/tmp/batmon399.log` | Inspected complete 321-line local artifact and all relevant startup/failure sequences | Raw local text; validated |
| Browser access | In-app browser unavailable; local Chromium launch denied by sandbox; existing Chrome CDP port inaccessible. No target web fetch was attempted because exact primary sources were already local | Route: none |

Exact `bleak_esphome` and `aiobmsble` sources were unavailable locally and were not guessed about. Neither is needed for the two principal conclusions: resolution fails before aiobmsble constructs its driver, and the incident log directly records the proxy’s uppercase address.
tokens used
213 635
The diagnosis is substantially correct: the user’s failure was an address-case lookup miss, not exhausted proxy connection slots. However, the fix remains incomplete on the native `BtBms` path, and it introduces a real breaking change for groups that reference members by lower-case MAC.

## Findings

### High — (a) The native `BtBms` fix does not reach the actual client

`BtBms.__init__` canonicalizes only the public attribute:

- [`self.address = normalize_ble_address(address)`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/bt.py:408)

But it later creates the client with the original constructor argument:

- [`self.client = self._create_client(address)`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/bt.py:467)

`HaBleakClientWrapper.__init__` merely calls `str()` and preserves case; it performs no normalization ([habluetooth wrappers.py:294](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:294), [wrappers.py:305](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:305)).

A focused probe captured:

```text
self.address: D1:1A:04:01:01:B7
client target: d1:1a:04:01:01:b7
```

Consequently, every native `BtBms` model—`daly`, `daly2`, `jk`, `jbd`, `snoop`, etc.—still gives habluetooth the lower-case lookup key and can reproduce the original failure. Commit `32de0b6`’s premise that `48d7315` “covers BtBms” is false, as is the CHANGELOG claim that both paths are fixed.

The required correction is to construct the client from `self.address`, not `address`.

### High — (c) Existing lower-case group references can now abort startup

Physical devices are indexed by the newly canonicalized `bms.address`:

- [`bms_by_name = {bms.address: bms ...}`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:233)

Group member references remain verbatim:

- [`self.address.split(',')`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/group.py:94)

Resolution is exact and raises when the spelling differs:

- [`if member_ref not in bms_by_name:`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:244)
- [`raise Exception("unknown bms ...")`](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:246)

Thus a previously working BlueZ configuration such as:

```yaml
- address: d1:1a:04:01:01:b7
  alias: battery1
- type: group_parallel
  address: d1:1a:04:01:01:b7
```

now indexes the physical BMS under `D1:1A:04:01:01:B7`, fails the lower-case group lookup, and terminates the add-on. My direct reconstruction confirmed the lower-case reference no longer resolves.

Group keys need consistent normalization or case-insensitive MAC matching. Alias-based group references remain safe.

### Low — (c) Anonymous telemetry identity changes across the upgrade

Telemetry hashes `bms.address` ([sinks.py:446](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sinks.py:446)). Lower-case users therefore receive a new address hash and measurement name after upgrading. This does not affect Home Assistant, but it splits longitudinal telemetry into old and new device identities.

### Low — (c) The regression test suite does not protect the actual failure

The test file imports `BLE_BMS_wrap` at module collection time ([test_ble_address_case.py:22](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/test/test_ble_address_case.py:22)), which imports `aiobmsble` unconditionally ([BLE_BMS_wrap.py:6](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/BLE_BMS_wrap.py:6)). `aiobmsble` is omitted from the normal requirements ([requirements.txt:20](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/requirements.txt:20)).

Running the new file in this checkout failed during collection:

```text
ModuleNotFoundError: No module named 'aiobmsble'
```

With `aiobmsble` installed, `test_aiobmsble_wrapper_normalizes_too` is not vacuous: it really executes the wrapper constructor and checks the normalized attribute. It does not need habluetooth. But it never exercises `BLEDeviceResolver.resolve`, an uppercase backend key, or connection establishment.

More importantly, there is no native-path assertion that captures the argument passed to `BleakClient`; such a test would have caught the High-severity defect above.

`test_adapter_mac_regex_was_not_rebound` tests global pattern names rather than `normalize_adapter()` behavior ([test_ble_address_case.py:67](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/test/test_ble_address_case.py:67)). It could pass if `normalize_adapter()` were later changed to use another broad regex directly. A behavioral test should mock `_hci_index_for_mac` and assert that hyphenated/mixed forms are not treated as controller MACs.

## Claims that survive scrutiny

### Root cause: confirmed

No requested habluetooth layer folds case:

- `async_ble_device_from_address`: exact `histories.get(address)` ([manager.py:807](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:807)).
- `async_scanner_devices_by_address`: forwards the address unchanged ([manager.py:388](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:388)).
- `HaScanner.get_discovered_device_advertisement_data`: exact dict lookup ([base_scanner.py:725](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/base_scanner.py:725)).
- `HaBleakClientWrapper.__init__`: preserves the string verbatim ([wrappers.py:305](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:305)).

In bluetooth-data-tools 1.29.21:

- The Python fallback uses `f"{address:012X}"` ([utils.py:17](/private/tmp/bes/bluetooth_data_tools-1.29.21/src/bluetooth_data_tools/utils.py:17)).
- The C implementation uses the uppercase table `0123456789ABCDEF` ([utils_wrapper.h:59](/private/tmp/bes/bluetooth_data_tools-1.29.21/src/bluetooth_data_tools/utils_wrapper.h:59)); `_utils_impl.pyx` returns that C buffer directly ([\_utils_impl.pyx:11](/private/tmp/bes/bluetooth_data_tools-1.29.21/src/bluetooth_data_tools/_utils_impl.pyx:11)).

The incident log independently shows the decisive mismatch: proxy discovery reports `D1:1A:04:01:01:B7` ([batmon399.log:59](/tmp/batmon399.log:59)), while the client attempts `d1:1a:04:01:01:b7` ([batmon399.log:66](/tmp/batmon399.log:66)).

Caveat: the add-on pins bluetooth-data-tools `<1.29`, while the locally available downloaded source is 1.29.21, and the user log does not record the installed bluetooth-data-tools version. I could not inspect the exact runtime release. The actual uppercase advertisement in the log makes that version gap non-dispositive for this incident.

### Evidence against slot exhaustion: confirmed, with one assumption that holds — (b)

Habluetooth builds `sorted_devices` first, then logs “Found N connection path(s)” whenever it is non-empty and INFO is enabled ([wrappers.py:585](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:585), [wrappers.py:602](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:602)). Only afterward does it reject candidates whose connector has no slot ([wrappers.py:625](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:625), [wrappers.py:564](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/wrappers.py:564)).

Therefore, a genuine slot shortage with a reachable candidate would still emit the INFO line before producing the same final error.

The log contains zero such lines across repeated failures, while INFO from numerous child loggers is present and `habluetooth.wrappers` warnings propagate through the same formatted handler ([batmon399.log:49](/tmp/batmon399.log:49), [batmon399.log:66](/tmp/batmon399.log:66)). Neither the repository nor habluetooth sets a `habluetooth` logger level, disables propagation, or installs a separate handler. `get_logger()` explicitly sets the root to INFO ([util.py:64](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/util.py:64)).

The unformatted startup warning shows `BluetoothManager` is constructed before `basicConfig`, but Python’s fallback warning handler does not install a root handler; subsequent formatted INFO lines show that the later configuration took effect.

The remaining assumption is that `/tmp/batmon399.log` is not selectively filtered or truncated. It appears to contain contiguous startup and failure sequences, so that assumption holds. Your public retraction of the slot-exhaustion theory is well supported.

### Breaking-change scope

MQTT topics, HA discovery IDs, and persisted meter/algorithm state do not derive from `bms.address`:

- MQTT prefix derives from `bms.name` ([sampling.py:157](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sampling.py:157)).
- HA unique IDs derive from that prefix ([mqtt_util.py:333](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/mqtt_util.py:333)).
- Meter and algorithm state are keyed by BMS name ([main.py:70](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:70), [store.py:43](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/store.py:43)).

So this change does not silently rename normal HA entities or orphan their history. The concrete regressions are group address references and telemetry identity.

`_user_config_migrate_addresses` remains case-sensitive ([store.py:78](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/store.py:78)), but it runs before BMS construction and the commits do not rewrite the config dictionary. Its potential case-duplicate behavior is pre-existing, not introduced here.

### Other address paths

The other requested comparisons are safe:

- Model discovery lookup is case-insensitive ([models/\_\_init\_\_.py:138](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/__init__.py:138)).
- `scan.resolve_address` is case-insensitive ([scan.py:84](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/scan.py:84)).
- The scanner retry membership test is case-insensitive ([bt.py:684](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/bt.py:684)).
- Snoop inherits `BtBms`, so it shares the native-client defect rather than adding a separate comparison bug.
- `BLEDeviceResolver` remains exact-keyed, but the aiobmsble wrapper now supplies a canonical address ([BLE_BMS_wrap.py:67](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/BLE_BMS_wrap.py:67), [BLE_BMS_wrap.py:117](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/BLE_BMS_wrap.py:117)).

The MAC regex cannot match a normal device name or CoreBluetooth UUID. A deliberately MAC-shaped device name is first resolved through `name2addr`, before wrapper normalization. UUIDs and `serial` remain unchanged.

### `_MAC_RE` rename: correct

Python resolves `_MAC_RE` when `normalize_adapter()` executes, not when the function is defined. Before `32de0b6`, the later module assignment therefore replaced the colon-only controller regex with the broader BLE regex. That broader expression matches:

- Hyphen-only: `0C-EF-15-47-4A-46`
- Mixed separators: `0C-EF:15-47:4A-46`

The restored controller regex matches neither. The original colon-only pattern predates the BLE normalization change, so there is no evidence the widening was deliberate. One naming correction: the function is `normalize_adapter()`, not `resolve_adapter()`.

### `_discover_service_info` override: cosmetic

The base method is itself a no-op ([manager.py:755](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:755)). Habluetooth assigns either the base or override to `_subclass_discover_info` and calls it unconditionally ([manager.py:200](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:200), [manager.py:739](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:739)). Advertisement histories are updated before that call ([manager.py:660](/private/tmp/hablu/habluetooth-6.1.0/src/habluetooth/manager.py:660)).

Replacing one no-op bound method with another changes only the constructor identity check that emits the warning. It does not enable extra advertisement work or alter connection behavior. The “harmless/cosmetic” claim is correct.

## Bottom line

Your public root-cause diagnosis and slot-exhaustion retraction are defensible. The public claim that the fix now covers both native and aiobmsble paths is not: the native client still receives the lower-case string. Before release, I would block on:

1. Passing `self.address` to `_create_client`.
2. Making group MAC references canonical or case-insensitive.
3. Adding a native-client argument test and a real `BLEDeviceResolver` uppercase-key test.

No repository files were changed.

## Source access log

| Source | Evidence state and scope | Route / validation |
|---|---|---|
| Local git objects `48d7315`, `32de0b6`, `98b9ed7` | Inspected relevant diffs and all requested downstream address consumers | Raw local git data; validated |
| [habluetooth 6.1.0](https://pypi.org/project/habluetooth/6.1.0/) | Inspected manager, scanner, base scanner, wrappers, logging, slot selection, and subclass hook | Local cached sdist; SHA-256 `9b5ac9cb9a07bb9690f04e8587abdb5ffebb69e66163f55fbedf99d90fc554c9`; validated by `PKG-INFO` |
| [bluetooth-data-tools 1.29.21](https://pypi.org/project/bluetooth-data-tools/1.29.21/) | Inspected Python fallback, Cython entry point, and C formatter | Local cached sdist; SHA-256 `2ccf1e90212092cb6af825fd93f3f2f392c543fe2a3a894e7831cbc6f6851c15`; validated by `PKG-INFO` |
| `/tmp/batmon399.log` | Inspected complete 321-line local artifact and all relevant startup/failure sequences | Raw local text; validated |
| Browser access | In-app browser unavailable; local Chromium launch denied by sandbox; existing Chrome CDP port inaccessible. No target web fetch was attempted because exact primary sources were already local | Route: none |

Exact `bleak_esphome` and `aiobmsble` sources were unavailable locally and were not guessed about. Neither is needed for the two principal conclusions: resolution fails before aiobmsble constructs its driver, and the incident log directly records the proxy’s uppercase address.
