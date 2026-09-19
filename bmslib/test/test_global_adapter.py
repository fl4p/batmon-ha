"""#414: a top-level `adapter:` in options.json was read by nobody.

The reporter runs batmon in Docker with two controllers and wants `hci1`. A
top-level `adapter: "hci1"` changed nothing at all -- no device saw it, nothing
was logged -- which is indistinguishable from "hci1 was selected and failed",
the state they were actually trying to debug.

`adapter:` now falls through to every device that doesn't set its own, and a
value that reaches no device says so.
"""

import logging

from bmslib.store import _user_config_apply_global_adapter

BLE = dict(address='CC:44:8C:F7:AD:BB', type='jk', alias='battery1')
SERIAL = dict(address='serial', type='daly_uart', alias='wired1')


def test_top_level_adapter_falls_through_to_ble_devices():
    conf = dict(adapter='hci1', devices=[dict(BLE), dict(BLE, alias='battery2')])
    _user_config_apply_global_adapter(conf)
    assert [d['adapter'] for d in conf['devices']] == ['hci1', 'hci1']


def test_per_device_adapter_wins():
    conf = dict(adapter='hci1', devices=[dict(BLE, adapter='hci0'), dict(BLE, alias='battery2')])
    _user_config_apply_global_adapter(conf)
    assert [d['adapter'] for d in conf['devices']] == ['hci0', 'hci1']


def test_controller_is_not_handed_to_a_wired_device():
    """`adapter:` on a serial BMS is a tty path; "hci1" would open nothing."""
    conf = dict(adapter='hci1', devices=[dict(SERIAL), dict(BLE)])
    _user_config_apply_global_adapter(conf)
    assert 'adapter' not in conf['devices'][0]
    assert conf['devices'][1]['adapter'] == 'hci1'


def test_port_is_not_handed_to_a_ble_device():
    conf = dict(adapter='/dev/ttyUSB0', devices=[dict(SERIAL), dict(BLE)])
    _user_config_apply_global_adapter(conf)
    assert conf['devices'][0]['adapter'] == '/dev/ttyUSB0'
    assert 'adapter' not in conf['devices'][1]


def test_serial_address_with_whitespace_still_counts_as_wired():
    """device_address() strips, so the transport test must too (#380)."""
    conf = dict(adapter='/dev/ttyUSB0', devices=[dict(SERIAL, address=' serial ')])
    _user_config_apply_global_adapter(conf)
    assert conf['devices'][0]['adapter'] == '/dev/ttyUSB0'


def test_adapter_reaching_nothing_is_reported(caplog):
    conf = dict(adapter='hci1', devices=[dict(SERIAL)])
    with caplog.at_level(logging.WARNING):
        _user_config_apply_global_adapter(conf)
    assert 'no effect' in caplog.text and 'hci1' in caplog.text
    assert 'adapter' not in conf['devices'][0]


def test_no_devices_at_all_is_reported(caplog):
    conf = dict(adapter='hci1')
    with caplog.at_level(logging.WARNING):
        _user_config_apply_global_adapter(conf)
    assert 'no effect' in caplog.text


def test_inheritance_is_logged(caplog):
    conf = dict(adapter='hci1', devices=[dict(BLE)])
    with caplog.at_level(logging.INFO):
        _user_config_apply_global_adapter(conf)
    assert 'hci1' in caplog.text and 'battery1' in caplog.text


def test_no_top_level_adapter_changes_nothing():
    for value in (None, '', '   ', 0, True):
        conf = dict(adapter=value, devices=[dict(BLE)])
        _user_config_apply_global_adapter(conf)
        assert 'adapter' not in conf['devices'][0], value
