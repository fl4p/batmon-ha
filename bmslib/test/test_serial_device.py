"""Wired (`address: serial`) devices must stay out of the Bluetooth code paths (#380)."""

import pytest

from bmslib.models import device_address, is_serial_device


@pytest.mark.parametrize("address", ["serial", "serial ", " serial", "\tserial\n"])
def test_whitespace_padded_serial_is_recognized(address):
    """The HA add-on types `address:` as free text, so users paste stray whitespace.
    construct_bms() has always stripped it; every other `== 'serial'` check must
    agree, or a tty path leaks into the BLE scanner."""
    assert is_serial_device({"address": address})


@pytest.mark.parametrize("address", ["C8:47:80:39:A6:E2", "", "serialx", "not-serial"])
def test_non_serial_addresses(address):
    assert not is_serial_device({"address": address})


def test_missing_and_none_address():
    assert device_address({}) == ""
    assert not is_serial_device({})
    assert not is_serial_device({"address": None})


def test_serial_device_excluded_from_bt_controller_set():
    """Mirrors main.py's bl_ctrls comprehension: a serial device's `adapter:` is a
    tty path, not a BT controller, and must never reach bt_discovery()."""
    devices = [
        {"address": "serial ", "adapter": "/dev/ttyUSB0", "alias": "wired"},
        {"address": "C8:47:80:39:A6:E2", "adapter": "hci0", "alias": "ble"},
    ]
    adapters = {dev["adapter"] for dev in devices
                if dev.get("adapter") and not is_serial_device(dev)}
    assert adapters == {"hci0"}
