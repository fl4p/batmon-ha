"""#399: a lower-case `address:` in the add-on config could never connect over an
ESPHome proxy.

habluetooth resolves an address with plain dict lookups and folds no case
anywhere (`BluetoothManager.async_ble_device_from_address` is
`histories.get(address)`; `HaScanner.get_discovered_device_advertisement_data` is
`self._previous_service_info.get(address)`), while every address it stores comes
from `bluetooth_data_tools.int_to_bluetooth_address`, which formats `%012X` --
upper case. So `d1:1a:04:01:01:b7` missed every lookup. BlueZ accepts either
case, which is why only the proxy stack was affected.

The reporter hit it twice with two different-looking errors and one root cause:
`type: daly` died in habluetooth with "No backend with an available connection
slot that can reach address ..." (a *lookup miss*, not slot exhaustion -- the
"Found N connection path(s)" INFO line habluetooth logs whenever it has any
candidate never appeared), and `type: daly_ble` died in
`BLEDeviceResolver.resolve()` with BleakDeviceNotFoundError.
"""

import bmslib.bt
from bmslib.bt import normalize_ble_address
from bmslib.models.BLE_BMS_wrap import BMS as WrappedBMS

REPORTED = 'd1:1a:04:01:01:b7'  # verbatim from the #399 config
CANONICAL = 'D1:1A:04:01:01:B7'  # what a proxy-sourced advertisement is keyed by


def test_lowercase_mac_is_upper_cased():
    assert normalize_ble_address(REPORTED) == CANONICAL


def test_already_canonical_is_unchanged():
    assert normalize_ble_address(CANONICAL) == CANONICAL


def test_mixed_case_and_hyphens():
    assert normalize_ble_address('d1:1A:04:01:01:B7') == CANONICAL
    assert normalize_ble_address('d1-1a-04-01-01-b7') == CANONICAL
    assert normalize_ble_address('  %s  ' % REPORTED) == CANONICAL


def test_non_mac_addresses_pass_through():
    # `serial` must survive verbatim (is_serial_device compares against it), a
    # macOS/CoreBluetooth identifier is a UUID rather than a MAC, and an
    # `address:` may be a device *name* resolved against a scan.
    for keep in ('serial', '2f2f2ba0-9c8b-4f0e-9a0e-000000000001',
                 'JK-B2A20S20P', '#d1:1a:04:01:01:b7', '', None):
        assert normalize_ble_address(keep) == keep


def test_malformed_macs_pass_through():
    for bad in ('d1:1a:04:01:01', 'd1:1a:04:01:01:b7:c8', 'd1:1a:04:01:01:zz'):
        assert normalize_ble_address(bad) == bad, bad


def test_aiobmsble_wrapper_normalizes_too():
    """The `_ble` path does not subclass BtBms, so it needs its own call.

    `BLEDeviceResolver.resolve()` keys its cache on `(adapter, d.address)` with
    the backend's own spelling and looks it up exactly, so without this the
    lookup misses and connect() raises BleakDeviceNotFoundError forever.
    """
    bms = WrappedBMS(address=REPORTED, type='daly_bms', name='battery1')
    assert bms.address == CANONICAL


def test_adapter_mac_regex_was_not_rebound():
    """Regression: `normalize_ble_address` originally reused the module global
    `_MAC_RE`, silently rebinding the stricter colon-only pattern that
    `resolve_adapter()` uses to recognize a controller MAC. They are separate
    names now; keep them that way."""
    assert bmslib.bt._MAC_RE.pattern != bmslib.bt._BLE_MAC_RE.pattern
    assert not bmslib.bt._MAC_RE.match('0C-EF-15-47-4A-46')
    assert bmslib.bt._BLE_MAC_RE.match('0C-EF-15-47-4A-46')


def test_the_client_is_built_with_the_canonical_address():
    """Normalizing `self.address` is worthless if the client gets the raw spelling.

    habluetooth keeps the address the client was constructed with and looks it up
    verbatim (`async_scanner_devices_by_address`), so a canonical `self.address`
    next to a lower-case BleakClient argument leaves the native path exactly as
    broken as before -- while every address-level test still passes.
    """
    seen = []

    class _Bms(bmslib.bt.BtBms):
        def _create_client(self, addr_or_device):
            seen.append(addr_or_device)
            return object()

    bms = _Bms(REPORTED, 'battery1')

    assert bms.address == CANONICAL
    assert seen == [CANONICAL]
