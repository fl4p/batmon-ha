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

import pytest

import bmslib.bt
from bmslib.bt import normalize_ble_address

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
    # Imported here, not at module scope: BLE_BMS_wrap imports aiobmsble
    # unconditionally, and aiobmsble is installed with --no-deps outside
    # requirements.txt, so a module-level import fails *collection* of this
    # whole file in a checkout without it -- taking the other cases with it.
    pytest.importorskip('aiobmsble')
    from bmslib.models.BLE_BMS_wrap import BMS as WrappedBMS

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


def test_group_member_referenced_by_lowercase_mac_still_resolves():
    """Regression: canonicalizing the address must not orphan a group's members.

    `group_parallel` names its members in `address:` as a comma-separated list.
    That list is not an address, so it is never normalized -- but each real BMS is
    indexed under its canonical address, so a group written with the lower-case
    spelling (the only one that worked before #399) missed the lookup. main()
    raises on a miss, which aborts the whole add-on rather than just the group.
    """
    from bmslib.group import resolve_member_ref

    class _Dev:
        def __init__(self, name):
            self.name = name

    battery1, battery2 = _Dev('battery1'), _Dev('battery2')
    bms_by_name = {
        'D1:1A:04:01:01:B7': battery1, 'battery1': battery1,
        'E2:2B:15:02:02:C8': battery2, 'battery2': battery2,
    }

    # the group's own config spelling, verbatim, including a space after the comma
    for ref, want in (('d1:1a:04:01:01:b7', battery1),
                      (' e2:2b:15:02:02:c8', battery2),
                      ('d1-1a-04-01-01-b7', battery1),
                      ('battery1', battery1),
                      ('D1:1A:04:01:01:B7', battery1)):
        assert resolve_member_ref(bms_by_name, ref) is want, ref

    # a genuinely unknown member must still be reported, not silently dropped
    assert resolve_member_ref(bms_by_name, 'aa:bb:cc:dd:ee:ff') is None
    assert resolve_member_ref(bms_by_name, 'battery9') is None


def test_an_alias_wins_over_normalization():
    """A MAC-shaped *alias* must match exactly rather than being canonicalized
    onto some other device."""
    from bmslib.group import resolve_member_ref

    class _Dev:
        def __init__(self, name):
            self.name = name

    aliased, other = _Dev('aliased'), _Dev('other')
    bms_by_name = {'d1:1a:04:01:01:b7': aliased, 'D1:1A:04:01:01:B7': other}
    assert resolve_member_ref(bms_by_name, 'd1:1a:04:01:01:b7') is aliased
