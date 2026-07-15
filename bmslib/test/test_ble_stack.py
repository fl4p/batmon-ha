"""Per-device ble_stack selection (Approach A, docs/per-device-ble-stack.md)."""
import pytest

import bmslib.bt as bt
from bmslib.models import check_ble_stack_config


def test_resolve_default_is_module_global():
    # None / 'bleak' must yield the module-global classes, i.e. a no-op vs the
    # previous behaviour.
    for name in (None, 'bleak'):
        bc, bs = bt._resolve_stack(name)
        assert bc is bt.BleakClient
        assert bs is bt.BleakScanner


def test_resolve_esphome_rejected_per_device():
    # esphome needs its own venv, so it is not selectable per device.
    with pytest.raises(ValueError):
        bt._resolve_stack('esphome')


def test_resolve_unknown_rejected():
    with pytest.raises(ValueError):
        bt._resolve_stack('nope')


def test_resolve_missing_package_is_loud_not_silent():
    # bluek/bumble aren't installed in the test env; a request must raise a clear
    # error, never fall back to bleak silently (absence-of-evidence rule).
    for name in ('bluek', 'bumble'):
        try:
            bc, _ = bt._resolve_stack(name)
        except RuntimeError as e:
            assert name in str(e)
        else:
            # If the package *is* installed, it must not be the stock bleak class.
            assert bc is not bt.BleakClient


def test_btbms_default_stack_wires_through():
    b = bt.BtBms('test_jk', name='t', ble_stack=None)
    assert b.ble_stack is None
    assert b._BleakClient is bt.BleakClient
    assert b._BleakScanner is bt.BleakScanner


def test_model_forwards_ble_stack_kwarg():
    # A native model must forward ble_stack through **kwargs to BtBms.
    from bmslib.models.jikong import JKBt
    j = JKBt('test_jk', name='jk', ble_stack='bleak')
    assert j._BleakClient is bt.BleakClient


def test_bt_stack_version_accepts_client_cls():
    # Passing a class must not raise; default (None) uses the module global.
    assert isinstance(bt.bt_stack_version(bt.BleakClient), str)
    assert isinstance(bt.bt_stack_version(None), str)


# --- config validation: override legal only when global stack is bleak ---

def test_validation_allows_override_under_bleak_global():
    cfg = {'ble_stack': 'bleak', 'devices': [
        {'address': 'D', 'ble_stack': 'bluek'},
        {'address': 'E'},  # inherits global
    ]}
    overriders = check_ble_stack_config(cfg)
    assert [d['address'] for d in overriders] == ['D']


def test_validation_allows_no_override_under_nonbleak_global():
    # Global bluek with no per-device override is fine (the shadow path).
    cfg = {'ble_stack': 'bluek', 'devices': [{'address': 'D'}]}
    assert check_ble_stack_config(cfg) == []


def test_validation_rejects_override_under_nonbleak_global():
    cfg = {'ble_stack': 'bluek', 'devices': [
        {'address': 'D', 'ble_stack': 'bleak'},
    ]}
    with pytest.raises(ValueError):
        check_ble_stack_config(cfg)


def test_validation_default_global_is_bleak():
    # ble_stack absent -> treated as bleak, so an override is allowed.
    cfg = {'devices': [{'address': 'D', 'ble_stack': 'bluek'}]}
    assert len(check_ble_stack_config(cfg)) == 1


# --- known-bad calibration: the gate must fire on invalid per-device values even
# --- under a bleak global (the case that previously slipped through to an
# --- uncaught traceback deep in construction).

@pytest.mark.parametrize('bad', ['esphome', 'typo_stack', ' bluek', 'Bluek', 'BLEAK'])
def test_validation_rejects_invalid_per_device_value_under_bleak_global(bad):
    cfg = {'ble_stack': 'bleak', 'devices': [{'address': 'D', 'ble_stack': bad}]}
    with pytest.raises(ValueError):
        check_ble_stack_config(cfg)


def test_validation_rejects_esphome_at_device_level():
    # esphome needs its own venv; it is never per-device selectable.
    cfg = {'ble_stack': 'esphome', 'devices': [{'address': 'D', 'ble_stack': 'esphome'}]}
    with pytest.raises(ValueError):
        check_ble_stack_config(cfg)


def test_serial_and_test_device_ignore_stray_stack():
    # A stray ble_stack on a test_/serial device must not resolve the stack (and
    # so must not crash even if that package isn't installed) — the device has no
    # BLE client. Uses 'bluek', which is not installed in the test env.
    b = bt.BtBms('test_jk', name='t', ble_stack='bluek')
    assert b._BleakClient is bt.BleakClient  # unchanged default, not resolved
