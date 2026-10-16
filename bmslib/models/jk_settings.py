"""
JK BMS (JK02_32S protocol) settings: register table and settings-frame decoder.

Registers, write lengths, factors, min/max defaults and settings-frame offsets
are taken from syssi/esphome-jk-bms (https://github.com/syssi/esphome-jk-bms):
`components/jk_bms_ble/number/__init__.py` (JK02_32S column + CONFIG_SCHEMA),
`switch/__init__.py` and `jk_bms_ble.cpp` (`decode_jk02_settings_`).
Offsets were cross-checked against real frames from a JK-B2A8S30P on firmware
15.26 and 17.06 (bmslib/test/data/jk_settings_b2a8s30p_fw*.bin).

Deliberately not in this table:
- charging / discharging / balancer switches: already handled by `JKBt.set_switch`.
- soc_calibration (0x6E): already handled by `JKBt.set_soc`.
- soh_calibration (0x6F) and the emergency switch (0x6B): their state is not in
  the 0x01 settings frame, so a write could not be read back and verified.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Union

HEADER = bytes([0x55, 0xAA, 0xEB, 0x90])
FRAME_TYPE_SETTINGS = 0x01
FRAME_LEN = 300


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Setting:
    name: str
    kind: str  # 'number' | 'switch'
    # number: (offset, width, signed), width 1 or 4, little-endian
    # switch: (offset, bitmask)
    # None: no known offset in the settings frame (cannot be read back)
    read: Optional[Tuple]
    register: int
    length: int  # write payload length in bytes
    factor: float  # raw = value * factor
    signed: bool
    unit: Optional[str]
    min: float
    max: float
    step: float
    danger: str  # 'low' | 'med' | 'high' | 'critical'


def _num(name, register, read, factor, unit, danger, vmin, vmax, length=4):
    signed = bool(read[2]) if read is not None else False
    return Setting(name=name, kind='number', read=read, register=register, length=length,
                   factor=float(factor), signed=signed, unit=unit, min=vmin, max=vmax,
                   step=1 / factor, danger=danger)


def _sw(name, register, read, danger):
    return Setting(name=name, kind='switch', read=read, register=register, length=4,
                   factor=1.0, signed=False, unit=None, min=0, max=1, step=1, danger=danger)


F, T = False, True

_TABLE = [
    # name, register, read (offset, width, signed), factor, unit, danger, min, max[, write length]
    _num('smart_sleep_voltage', 0x01, (6, 4, F), 1000, 'V', 'low', 0.003, 3.65, length=1),
    _num('cell_uvp', 0x02, (10, 4, F), 1000, 'V', 'high', 1.2, 4.35),
    _num('cell_uvpr', 0x03, (14, 4, F), 1000, 'V', 'high', 1.2, 4.35),
    _num('cell_ovp', 0x04, (18, 4, F), 1000, 'V', 'high', 1.2, 4.35),
    _num('cell_ovpr', 0x05, (22, 4, F), 1000, 'V', 'high', 1.2, 4.35),
    _num('balance_trigger_voltage', 0x06, (26, 4, F), 1000, 'V', 'med', 0.003, 1.0),
    _num('cell_soc100_voltage', 0x07, (30, 4, F), 1000, 'V', 'med', 0.003, 3.65),
    _num('cell_soc0_voltage', 0x08, (34, 4, F), 1000, 'V', 'med', 0.003, 3.65),
    _num('cell_rcv', 0x09, (38, 4, F), 1000, 'V', 'med', 0.003, 3.65),
    _num('cell_rfv', 0x0A, (42, 4, F), 1000, 'V', 'med', 0.003, 3.65),
    _num('power_off_voltage', 0x0B, (46, 4, F), 1000, 'V', 'high', 1.2, 4.35),
    _num('max_charge_current', 0x0C, (50, 4, F), 1000, 'A', 'high', 1.0, 600.1),
    _num('charge_ocp_delay', 0x0D, (54, 4, F), 1, 's', 'med', 2, 600),
    _num('charge_ocpr_time', 0x0E, (58, 4, F), 1, 's', 'med', 2, 600),
    _num('max_discharge_current', 0x0F, (62, 4, F), 1000, 'A', 'high', 1.0, 1200.1),
    _num('discharge_ocp_delay', 0x10, (66, 4, F), 1, 's', 'med', 2, 600),
    _num('discharge_ocpr_time', 0x11, (70, 4, F), 1, 's', 'med', 2, 600),
    _num('scpr_time', 0x12, (74, 4, F), 1, 's', 'med', 2, 600),
    _num('max_balance_current', 0x13, (78, 4, F), 1000, 'A', 'med', 0.3, 15.0),
    _num('charge_otp', 0x14, (82, 4, T), 10, '°C', 'high', 30, 80),
    _num('charge_otpr', 0x15, (86, 4, T), 10, '°C', 'high', 30, 80),
    _num('discharge_otp', 0x16, (90, 4, T), 10, '°C', 'high', 30, 80),
    _num('discharge_otpr', 0x17, (94, 4, T), 10, '°C', 'high', 30, 80),
    _num('charge_utp', 0x18, (98, 4, T), 10, '°C', 'high', -45, 20),
    _num('charge_utpr', 0x19, (102, 4, T), 10, '°C', 'high', -45, 20),
    _num('mosfet_otp', 0x1A, (106, 4, T), 10, '°C', 'high', 50, 110),
    _num('mosfet_otpr', 0x1B, (110, 4, T), 10, '°C', 'high', 50, 110),
    _num('cell_count', 0x1C, (114, 1, F), 1, None, 'critical', 2, 32),
    _num('total_capacity', 0x20, (130, 4, F), 1000, 'Ah', 'high', 2, 20000),
    _num('scp_delay', 0x21, (134, 4, F), 1, 'µs', 'high', 0, 1000000),
    _num('balance_start_voltage', 0x22, (138, 4, F), 1000, 'V', 'med', 1.2, 4.25),
    _num('precharge_time', 0x25, (274, 1, F), 1, 's', 'med', 0, 255),
    _num('heating_start_temp', 0x37, (284, 1, T), 1, '°C', 'med', -40, 100, length=1),
    _num('heating_stop_temp', 0x38, (285, 1, T), 1, '°C', 'med', -40, 100, length=1),
    _num('smart_sleep_delay', 0x39, (286, 1, F), 1, 'h', 'low', 1, 100, length=1),
    _num('discharge_utp', 0x3A, (296, 1, T), 1, '°C', 'high', -40, 100, length=1),
    _num('discharge_utpr', 0x3B, (297, 1, T), 1, '°C', 'high', -40, 100, length=1),
    # rcv_time, rfv_time, emergency_duration, re_bulk_soc: syssi reads 266/267/269/278 from the
    # device-info frame (0x03), not the 0x01 settings frame; no unique offset found in our fixtures.
    _num('rcv_time', 0xB3, None, 10, 'h', 'low', 0, 18.2, length=1),
    _num('rfv_time', 0xB4, None, 10, 'h', 'low', 0, 18.2, length=1),
    _num('emergency_duration', 0xB5, None, 1, 'min', 'med', 1, 255, length=1),
    _num('re_bulk_soc', 0xB7, None, 1, '%', 'low', 0, 50, length=1),
    # calibration values (app: 13.257 V / 13.272 V, -0.493 A) are not in the settings frame
    _num('voltage_calibration', 0x64, None, 1000, 'V', 'critical', 1.0, 200.0),
    _num('current_calibration', 0x67, None, 1000, 'A', 'critical', -1000.0, 1000.0),

    _sw('heating', 0x27, (282, 0x01), 'med'),
    _sw('disable_temperature_sensors', 0x28, (282, 0x02), 'critical'),
    _sw('display_always_on', 0x2B, (282, 0x10), 'low'),
    _sw('smart_sleep', 0x2D, (282, 0x40), 'low'),
    _sw('disable_pcl_module', 0x2E, (282, 0x80), 'med'),
    _sw('timed_stored_data', 0x2F, (283, 0x01), 'low'),
    _sw('charging_float_mode', 0x30, (283, 0x02), 'med'),
    _sw('emergency_button_trigger', 0x31, (283, 0x04), 'low'),
    _sw('dry_contact_alarm_intermittent', 0x32, (283, 0x08), 'low'),
    _sw('discharge_ocp2', 0x33, (283, 0x10), 'med'),
    _sw('discharge_ocp3', 0x34, (283, 0x20), 'med'),
    _sw('gps_locked_charging', 0x35, (283, 0x40), 'critical'),
    _sw('gps_locked_discharging', 0x36, (283, 0x80), 'critical'),
]

SETTINGS: Dict[str, Setting] = {s.name: s for s in _TABLE}

LABELS: Dict[str, str] = {
    'smart_sleep_voltage': 'Smart sleep voltage',
    'cell_uvp': 'Cell UVP',
    'cell_uvpr': 'Cell UVPR',
    'cell_ovp': 'Cell OVP',
    'cell_ovpr': 'Cell OVPR',
    'balance_trigger_voltage': 'Balance trigger voltage',
    'cell_soc100_voltage': 'SOC 100% voltage',
    'cell_soc0_voltage': 'SOC 0% voltage',
    'cell_rcv': 'Cell RCV',
    'cell_rfv': 'Cell RFV',
    'power_off_voltage': 'Power off voltage',
    'max_charge_current': 'Max charge current',
    'charge_ocp_delay': 'Charge OCP delay',
    'charge_ocpr_time': 'Charge OCPR time',
    'max_discharge_current': 'Max discharge current',
    'discharge_ocp_delay': 'Discharge OCP delay',
    'discharge_ocpr_time': 'Discharge OCPR time',
    'scpr_time': 'SCPR time',
    'max_balance_current': 'Max balance current',
    'charge_otp': 'Charge OTP',
    'charge_otpr': 'Charge OTPR',
    'discharge_otp': 'Discharge OTP',
    'discharge_otpr': 'Discharge OTPR',
    'charge_utp': 'Charge UTP',
    'charge_utpr': 'Charge UTPR',
    'mosfet_otp': 'MOSFET OTP',
    'mosfet_otpr': 'MOSFET OTPR',
    'cell_count': 'Cell count',
    'total_capacity': 'Total capacity',
    'scp_delay': 'SCP delay',
    'balance_start_voltage': 'Balance start voltage',
    'precharge_time': 'Precharge time',
    'heating_start_temp': 'Heating start temp',
    'heating_stop_temp': 'Heating stop temp',
    'smart_sleep_delay': 'Smart sleep delay',
    'discharge_utp': 'Discharge UTP',
    'discharge_utpr': 'Discharge UTPR',
    'rcv_time': 'RCV time',
    'rfv_time': 'RFV time',
    'emergency_duration': 'Emergency duration',
    're_bulk_soc': 'Re-bulk SOC',
    'voltage_calibration': 'Voltage calibration',
    'current_calibration': 'Current calibration',
    'heating': 'Heating',
    'disable_temperature_sensors': 'Disable temperature sensors',
    'display_always_on': 'Display always on',
    'smart_sleep': 'Smart sleep',
    'disable_pcl_module': 'Disable PCL module',
    'timed_stored_data': 'Timed stored data',
    'charging_float_mode': 'Charging float mode',
    'emergency_button_trigger': 'Emergency button trigger',
    'dry_contact_alarm_intermittent': 'Dry contact alarm intermittent',
    'discharge_ocp2': 'Discharge OCP2',
    'discharge_ocp3': 'Discharge OCP3',
    'gps_locked_charging': 'GPS locked charging',
    'gps_locked_discharging': 'GPS locked discharging',
}


def decode_settings(frame: bytes) -> Dict[str, Union[float, bool]]:
    """Decode a JK02_32S settings frame (type 0x01) into {name: value}.

    Only settings with a known read offset are returned.
    """
    if len(frame) < FRAME_LEN:
        raise SettingError('settings frame too short: %d bytes' % len(frame))
    if bytes(frame[:4]) != HEADER:
        raise SettingError('settings frame has wrong header %s' % bytes(frame[:4]).hex())
    if frame[4] != FRAME_TYPE_SETTINGS:
        raise SettingError('not a settings frame (type 0x%02x)' % frame[4])

    out = {}
    for s in SETTINGS.values():
        if s.read is None:
            continue
        if s.kind == 'switch':
            offset, mask = s.read
            out[s.name] = bool(frame[offset] & mask)
        else:
            offset, width, signed = s.read
            raw = int.from_bytes(frame[offset:offset + width], 'little', signed=signed)
            value = raw / s.factor
            out[s.name] = int(value) if s.factor == 1 else value
    return out


def _fmt(v) -> str:
    v = round(float(v), 6)
    if v == int(v):
        return str(int(v))
    return repr(v)


def format_value(name: str, value) -> str:
    """User-facing value without float noise: switches ON/OFF, numbers like 2.65 / 3."""
    if value is None:
        return '?'
    s = SETTINGS.get(name)
    if (s is not None and s.kind == 'switch') or isinstance(value, bool):
        return 'ON' if value else 'OFF'
    try:
        return _fmt(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)
