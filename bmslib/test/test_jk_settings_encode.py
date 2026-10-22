"""JK settings write tests: encoding, write whitelist and cross-field validation."""

import os

import pytest

from bmslib.models.jk_settings import (
    CRITICAL,
    LABELS,
    SETTINGS,
    SettingError,
    UNVERIFIED_WRITE,
    decode_settings,
    encode_setting,
    parse_write_option,
    validate_change,
)

DATA = os.path.join(os.path.dirname(__file__), "data")
JK1 = "jk_settings_b2a8s30p_fw15.26.bin"


def _load(name):
    with open(os.path.join(DATA, name), "rb") as f:
        return f.read()


WRITABLE = [n for n, s in SETTINGS.items() if s.read is not None and n not in UNVERIFIED_WRITE]


def test_encode_rounds_float_noise():
    assert encode_setting("cell_uvp", 2.6499999) == (0x02, [0x5A, 0x0A, 0x00, 0x00])


def test_encode_negative_temp():
    assert encode_setting("charge_utp", -5.0) == (0x18, [0xCE, 0xFF, 0xFF, 0xFF])


def test_encode_one_byte():
    assert encode_setting("smart_sleep_delay", 24) == (0x39, [24])


def test_encode_smart_sleep_voltage_four_bytes():
    # Verified on a JK-B2A8S30P fw 15.26 (2026-10-17) and in the JK app capture of
    # syssi/esphome-jk-bms#421: register 0x01 takes 4 bytes, mV.
    assert encode_setting("smart_sleep_voltage", 3.286) == (0x01, [0xD6, 0x0C, 0x00, 0x00])


def test_encode_switch():
    assert encode_setting("display_always_on", True) == (0x2B, [1, 0, 0, 0])
    assert encode_setting("display_always_on", False) == (0x2B, [0, 0, 0, 0])


@pytest.mark.parametrize("name,value", [
    ("nope", 1),
    ("max_charge_current", 1e6),
    ("max_charge_current", 0.5),
    ("cell_uvp", "x"),
    ("cell_uvp", True),
    ("cell_uvp", None),
    ("cell_uvp", float("nan")),
    ("cell_uvp", 2.6495),  # not on the 0.001 V grid
    ("display_always_on", 1),
    ("display_always_on", "on"),
    ("voltage_calibration", 13.2),
    ("rcv_time", 1.0),
    ("smart_sleep_voltage", 3.7),  # above max
    ("cell_count", 25),  # JKBt.connect() rejects more than 24 cells
    ("heating_start_temp", 10),
    ("heating_stop_temp", 20),
    ("discharge_utp", -10),
    ("discharge_utpr", -5),
    ("precharge_time", 5),
])
def test_encode_rejects(name, value):
    with pytest.raises(SettingError):
        encode_setting(name, value)


def _mid(s):
    return round(((s.min + s.max) / 2) * s.factor) / s.factor


@pytest.mark.parametrize("name", WRITABLE)
def test_roundtrip_all(name):
    s = SETTINGS[name]
    base = _load(JK1)
    values = [True, False] if s.kind == "switch" else [s.min, s.max, _mid(s)]
    for value in values:
        register, data = encode_setting(name, value)
        assert register == s.register
        assert len(data) == s.length
        frame = bytearray(base)
        if s.kind == "switch":
            offset, mask = s.read
            assert data == [1 if value else 0, 0, 0, 0]
            frame[offset] = (frame[offset] | mask) if value else (frame[offset] & ~mask & 0xFF)
        else:
            offset, width, signed = s.read
            raw = int.from_bytes(bytes(data), "little", signed=s.signed)
            frame[offset:offset + width] = raw.to_bytes(width, "little", signed=signed)
        decoded = decode_settings(bytes(frame))
        if s.kind == "switch":
            assert decoded[name] is value, (name, value)
        else:
            assert decoded[name] == pytest.approx(value, abs=1e-9), (name, value)


def test_parse_write_option():
    assert parse_write_option("") == frozenset()
    assert parse_write_option(None) == frozenset()
    assert parse_write_option("  ") == frozenset()

    everything = parse_write_option("all")
    assert "charge_utp" in everything
    assert "smart_sleep_voltage" in everything
    assert not everything & CRITICAL
    assert not everything & UNVERIFIED_WRITE
    assert everything == frozenset(WRITABLE) - CRITICAL
    assert "voltage_calibration" not in everything

    assert "cell_count" in parse_write_option("all,cell_count")
    assert parse_write_option("charge_utp, charge_utpr") == {"charge_utp", "charge_utpr"}

    for bad in ("nope", "all,nope", "heating_start_temp", "rcv_time"):
        with pytest.raises(SettingError):
            parse_write_option(bad)


@pytest.fixture
def jk1():
    return decode_settings(_load(JK1))


def test_validate_pairs(jk1):
    with pytest.raises(SettingError) as e:
        validate_change("charge_utp", 6.0, jk1)
    msg = str(e.value)
    assert "charge_utpr" in msg and "first" in msg
    assert msg == "charge_utp 6°C must be below charge_utpr (5°C); change charge_utpr first"

    validate_change("charge_utpr", 7.0, jk1)

    with pytest.raises(SettingError) as e:
        validate_change("charge_otp", 40.0, jk1)
    assert str(e.value) == "charge_otp 40°C must be above charge_otpr (45°C); change charge_otpr first"

    with pytest.raises(SettingError) as e:
        validate_change("cell_uvp", 2.70, jk1)
    assert str(e.value) == "cell_uvp 2.7V must be below cell_uvpr (2.7V); change cell_uvpr first"

    with pytest.raises(SettingError) as e:
        validate_change("cell_ovp", 3.40, jk1)
    assert "cell_ovpr" in str(e.value) or "cell_rcv" in str(e.value)


def test_validate_ignores_unrelated_inconsistent_pair(jk1):
    assert jk1["discharge_utp"] == 0 and jk1["discharge_utpr"] == 0
    validate_change("charge_utpr", 7.0, jk1)
    # but changing one of them does check the pair
    with pytest.raises(SettingError):
        validate_change("discharge_utp", 0, jk1)


def test_validate_chain(jk1):
    # soc0 2.69 < uvpr 2.70 is fine (not enforced)
    validate_change("cell_soc0_voltage", 2.69, jk1)
    with pytest.raises(SettingError) as e:
        validate_change("power_off_voltage", 2.65, jk1)
    assert "cell_uvp" in str(e.value)
    with pytest.raises(SettingError):
        validate_change("cell_soc0_voltage", 3.5, jk1)  # above soc100 3.449
    validate_change("cell_soc100_voltage", 3.45, jk1)  # == rcv is allowed
    with pytest.raises(SettingError):
        validate_change("cell_soc100_voltage", 3.46, jk1)
    with pytest.raises(SettingError):
        validate_change("cell_rfv", 3.45, jk1)  # not below rcv 3.45
    with pytest.raises(SettingError):
        validate_change("cell_rcv", 3.30, jk1)  # below rfv 3.35


def test_validate_missing_other_value_passes():
    validate_change("charge_utp", 6.0, {})
    validate_change("charge_utp", 6.0, {"charge_otp": 50.0})


def test_validate_unknown_name():
    with pytest.raises(SettingError):
        validate_change("nope", 1, {})


def test_labels_cover_all_settings():
    assert set(LABELS) == set(SETTINGS)
    for name, label in LABELS.items():
        assert isinstance(label, str) and label.strip(), name
    assert LABELS["charge_utp"] == "Charge UTP"
    assert LABELS["cell_ovp"] == "Cell OVP"
    assert LABELS["max_charge_current"] == "Max charge current"


def test_current_calibration_range_allows_negative_and_stays_read_only():
    # the JK app shows e.g. -0.493 A
    s = SETTINGS['current_calibration']
    assert (s.min, s.max) == (-1000.0, 1000.0)
    with pytest.raises(SettingError, match='not writable'):
        encode_setting('current_calibration', -0.493)
    with pytest.raises(SettingError):
        parse_write_option('current_calibration')


def test_nmc_voltages_in_range():
    # syssi's 3.65 V cap is LiFePO4-only; an NMC pack needs RCV/SOC100 around 4.1 V
    assert encode_setting("cell_rcv", 4.1)[0] == 0x09
    assert encode_setting("cell_soc100_voltage", 4.1)[0] == 0x07
