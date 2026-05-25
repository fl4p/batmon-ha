"""Victron SmartShunt characteristic-value decode tests.

The Victron decoder reads one BLE characteristic per metric (voltage, current,
power, SOC, consumed Ah) and applies a per-characteristic ``func`` and
``na_bytes`` sentinel. This module exercises that decode table directly.
"""

import math

import pytest

from bmslib.models.victron import VICTRON_CHARACTERISTICS, parse_value


def _bytes_le(value: int, length: int, signed: bool) -> bytes:
    return value.to_bytes(length, byteorder="little", signed=signed)


def test_voltage_decode():
    char = VICTRON_CHARACTERISTICS["voltage"]
    # 12.34V → 1234 * 0.01, signed i16 LE
    raw = _bytes_le(1234, 2, signed=True)
    assert parse_value(raw, char) == pytest.approx(12.34, abs=0.001)


def test_current_decode_charging():
    char = VICTRON_CHARACTERISTICS["current"]
    # Victron reports charge as positive, batmon negates → expect -2.500 A
    raw = _bytes_le(2500, 4, signed=True)   # +2.5 A charge in Victron's sign
    assert parse_value(raw, char) == pytest.approx(-2.5, abs=0.001)


def test_current_decode_discharging():
    char = VICTRON_CHARACTERISTICS["current"]
    raw = _bytes_le(-5000, 4, signed=True)  # -5 A discharge in Victron's sign
    assert parse_value(raw, char) == pytest.approx(5.0, abs=0.001)


def test_power_decode():
    char = VICTRON_CHARACTERISTICS["power"]
    raw = _bytes_le(75, 2, signed=True)
    # batmon negates so a Victron-positive (charge) becomes negative in our sign
    assert parse_value(raw, char) == -75


def test_soc_decode():
    char = VICTRON_CHARACTERISTICS["soc"]
    raw = _bytes_le(9550, 2, signed=False)  # 95.50%
    assert parse_value(raw, char) == pytest.approx(95.50, abs=0.01)


def test_soc_unavailable_returns_nan():
    char = VICTRON_CHARACTERISTICS["soc"]
    assert math.isnan(parse_value(char["na_bytes"], char))


def test_charge_decode():
    char = VICTRON_CHARACTERISTICS["charge"]
    raw = _bytes_le(-450, 4, signed=True)   # consumed 45.0 Ah
    assert parse_value(raw, char) == pytest.approx(-45.0, abs=0.01)


def test_charge_unavailable_returns_nan():
    char = VICTRON_CHARACTERISTICS["charge"]
    assert math.isnan(parse_value(char["na_bytes"], char))
