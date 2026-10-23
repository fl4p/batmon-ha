"""JK settings frame (type 0x01) decode tests.

Fixtures are real 300-byte frames captured from a JK-B2A8S30P (4S LiFePO4)
on firmware 15.26 (jk1) and 17.06 (jk2). Expected values are what the JK app
showed at capture time.
"""

import os

import pytest

from bmslib.models.jk_settings import (
    CRITICAL,
    SETTINGS,
    SettingError,
    UNVERIFIED_WRITE,
    decode_settings,
)

DATA = os.path.join(os.path.dirname(__file__), "data")


def _load(name):
    with open(os.path.join(DATA, name), "rb") as f:
        return f.read()


JK1 = "jk_settings_b2a8s30p_fw15.26.bin"
JK2 = "jk_settings_b2a8s30p_fw17.06.bin"

COMMON = {
    "cell_uvpr": 2.70,
    "cell_ovp": 3.65,
    "cell_ovpr": 3.448,
    "cell_soc100_voltage": 3.449,
    "cell_soc0_voltage": 2.69,
    "cell_rcv": 3.45,
    "cell_rfv": 3.35,
    "power_off_voltage": 2.5,
    "max_charge_current": 140.0,
    "charge_ocp_delay": 60,
    "charge_ocpr_time": 3,
    "max_discharge_current": 300.0,
    "discharge_ocp_delay": 300,
    "discharge_ocpr_time": 60,
    "scp_delay": 5,
    "scpr_time": 30,
    "max_balance_current": 2.0,
    "charge_otp": 50.0,
    "charge_otpr": 45.0,
    "discharge_otp": 70.0,
    "discharge_otpr": 60.0,
    "charge_utp": 2.0,
    "charge_utpr": 5.0,
    "cell_count": 4,
    "total_capacity": 280.0,
    "balance_trigger_voltage": 0.005,
    "balance_start_voltage": 3.45,
    "smart_sleep_delay": 24,
    # rcv_time / rfv_time / emergency_duration: not in the 0x01 frame (read=None)
    "charging_float_mode": True,
    "smart_sleep": False,
    "display_always_on": False,
    "disable_temperature_sensors": False,
    "timed_stored_data": False,
    "emergency_button_trigger": False,
    "dry_contact_alarm_intermittent": False,
    "disable_pcl_module": True,
}


def _check(decoded, expected):
    for name, exp in expected.items():
        assert name in decoded, name
        if isinstance(exp, bool):
            assert decoded[name] is exp, name
        else:
            assert decoded[name] == pytest.approx(exp, abs=1e-6), name


def test_decode_jk1_fw1526():
    d = decode_settings(_load(JK1))
    _check(d, COMMON)
    _check(d, {
        "cell_uvp": 2.65,
        "mosfet_otp": 100.0,
        "mosfet_otpr": 80.0,
        "smart_sleep_voltage": 3.285,
    })


def test_decode_jk2_fw1706():
    d = decode_settings(_load(JK2))
    _check(d, COMMON)
    _check(d, {
        "cell_uvp": 2.60,  # the user's change to 2.65 did not stick on jk2
        "mosfet_otp": 80.0,
        "mosfet_otpr": 70.0,
        "smart_sleep_voltage": 3.287,
        "discharge_ocp2": False,
        "discharge_ocp3": False,
    })


def test_decode_only_readable_settings():
    d = decode_settings(_load(JK1))
    assert set(d) == {n for n, s in SETTINGS.items() if s.read is not None}
    for name in ("voltage_calibration", "current_calibration"):
        assert SETTINGS[name].read is None
        assert name not in d


def test_decode_signed_temperatures():
    frame = bytearray(_load(JK1))
    frame[98:102] = (-200).to_bytes(4, "little", signed=True)
    frame[284] = 0xFB
    d = decode_settings(bytes(frame))
    assert d["charge_utp"] == pytest.approx(-20.0, abs=1e-6)
    assert d["heating_start_temp"] == -5


def test_decode_rejects_short_or_wrong_frame():
    frame = _load(JK1)
    with pytest.raises(SettingError):
        decode_settings(frame[:299])
    wrong_type = bytearray(frame)
    wrong_type[4] = 0x02
    with pytest.raises(SettingError):
        decode_settings(bytes(wrong_type))
    wrong_header = bytearray(frame)
    wrong_header[0] = 0x00
    with pytest.raises(SettingError):
        decode_settings(bytes(wrong_header))


def test_table_has_no_duplicate_registers_or_offsets():
    registers = [s.register for s in SETTINGS.values()]
    assert len(registers) == len(set(registers))

    # every byte a number reads must be read by exactly one setting
    number_bytes = []
    for s in SETTINGS.values():
        if s.kind == "number" and s.read is not None:
            offset, width, _signed = s.read
            number_bytes += range(offset, offset + width)
    assert len(number_bytes) == len(set(number_bytes))

    switch_bits = [s.read for s in SETTINGS.values() if s.kind == "switch"]
    assert len(switch_bits) == len(set(switch_bits))
    assert not {offset for offset, _mask in switch_bits} & set(number_bytes)


def test_read_offset_is_not_register():
    coincidences = [n for n, s in SETTINGS.items()
                    if s.kind == "number" and s.read is not None and s.read[0] == s.register]
    assert coincidences == []


def test_table_shape():
    assert CRITICAL == frozenset({
        "cell_count", "scp_delay", "voltage_calibration", "current_calibration",
        "disable_temperature_sensors", "gps_locked_charging", "gps_locked_discharging",
    })
    assert CRITICAL == frozenset(n for n, s in SETTINGS.items() if s.danger == "critical")
    assert UNVERIFIED_WRITE == {
        "heating_start_temp", "heating_stop_temp",
        "discharge_utp", "discharge_utpr", "precharge_time",
    }
    for name, s in SETTINGS.items():
        assert s.name == name
        assert s.kind in ("number", "switch")
        assert s.danger in ("low", "med", "high", "critical")
        assert s.length in (1, 4)
        assert s.min <= s.max
        if s.kind == "switch":
            assert (s.min, s.max, s.step) == (0, 1, 1)
        else:
            assert s.step == pytest.approx(1 / s.factor)
    for name in ("charging", "discharging", "balancer", "soc_calibration",
                 "soh_calibration", "emergency"):
        assert name not in SETTINGS
