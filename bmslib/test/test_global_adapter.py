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


def test_controller_mac_is_a_bluetooth_adapter():
    """normalize_adapter() resolves a controller MAC to hciN, so it is a BLE value."""
    conf = dict(adapter='0C:EF:15:47:4A:46', devices=[dict(BLE), dict(SERIAL)])
    _user_config_apply_global_adapter(conf)
    assert conf['devices'][0]['adapter'] == '0C:EF:15:47:4A:46'
    assert 'adapter' not in conf['devices'][1]


def test_relative_port_name_is_refused_not_given_to_ble(caplog):
    """`ttyUSB0` is a port serial.Serial accepts. "not a path -> Bluetooth" would
    have handed it to every BLE device instead, which is the silent breakage the
    whole change is about."""
    conf = dict(adapter='ttyUSB0', devices=[dict(BLE), dict(SERIAL)])
    with caplog.at_level(logging.WARNING):
        _user_config_apply_global_adapter(conf)
    assert 'ttyUSB0' in caplog.text and 'ignoring' in caplog.text
    assert not any('adapter' in d for d in conf['devices'])


def test_every_device_overriding_the_default_is_not_a_warning(caplog):
    """A default that every device overrides is a legitimate config."""
    conf = dict(adapter='hci1', devices=[dict(BLE, adapter='hci0')])
    with caplog.at_level(logging.INFO):
        _user_config_apply_global_adapter(conf)
    assert 'hci1' in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_disabled_and_group_devices_do_not_inherit():
    """They open no link; an adapter on them only pollutes the discovery sweep
    (main.py builds it from the per-device `adapter:` keys)."""
    conf = dict(adapter='hci1', devices=[
        dict(BLE, address='#CC:44:8C:F7:AD:BB', alias='disabled'),
        dict(address='', type='jk', alias='empty'),
        dict(type='group_parallel', address='group1', alias='pack'),
        dict(type='group_serial:2', address='group2', alias='string'),
        dict(BLE),
    ])
    _user_config_apply_global_adapter(conf)
    assert ['adapter' in d for d in conf['devices']] == [False, False, False, False, True]


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


def test_no_top_level_adapter_changes_nothing(caplog):
    for value in (None, ''):
        conf = dict(adapter=value, devices=[dict(BLE)])
        with caplog.at_level(logging.WARNING):
            _user_config_apply_global_adapter(conf)
        assert 'adapter' not in conf['devices'][0], value
    assert not caplog.records


def test_a_non_string_adapter_is_reported(caplog):
    """The failure mode of #414 was silence, so an unusable value must not be
    dropped either -- it is a value the user did set."""
    for value in (0, 1, True, ['hci1'], dict(hci1=1), '   '):
        conf = dict(adapter=value, devices=[dict(BLE)])
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            _user_config_apply_global_adapter(conf)
        assert 'adapter' not in conf['devices'][0], value
        assert caplog.records, value
