"""The JK02_32S write registers, pinned, so a change to the table in jk_settings.py is deliberate.

Copied by hand from syssi/esphome-jk-bms components/jk_bms_ble/:
- number/__init__.py NUMBERS: (JK02_32S register = 3rd value, length, factor)
- switch/__init__.py SWITCHES: register = 3rd value; length 4 and factor 1 per the JK02_32S
  frame list in number/__init__.py (e.g. `27 04 01000000  Heating on`).
Do not regenerate this from SETTINGS: a wrong register writes another setting.
"""

from bmslib.models.jk_settings import SETTINGS

PINNED = {
    # name: (register, length, factor)
    'smart_sleep_voltage': (0x01, 1, 1000),
    'cell_uvp': (0x02, 4, 1000),
    'cell_uvpr': (0x03, 4, 1000),
    'cell_ovp': (0x04, 4, 1000),
    'cell_ovpr': (0x05, 4, 1000),
    'balance_trigger_voltage': (0x06, 4, 1000),
    'cell_soc100_voltage': (0x07, 4, 1000),
    'cell_soc0_voltage': (0x08, 4, 1000),
    'cell_rcv': (0x09, 4, 1000),  # cell_request_charge_voltage
    'cell_rfv': (0x0A, 4, 1000),  # cell_request_float_voltage
    'power_off_voltage': (0x0B, 4, 1000),
    'max_charge_current': (0x0C, 4, 1000),
    'charge_ocp_delay': (0x0D, 4, 1),
    'charge_ocpr_time': (0x0E, 4, 1),
    'max_discharge_current': (0x0F, 4, 1000),
    'discharge_ocp_delay': (0x10, 4, 1),
    'discharge_ocpr_time': (0x11, 4, 1),
    'scpr_time': (0x12, 4, 1),  # short_circuit_protection_recovery_time
    'max_balance_current': (0x13, 4, 1000),
    'charge_otp': (0x14, 4, 10),
    'charge_otpr': (0x15, 4, 10),
    'discharge_otp': (0x16, 4, 10),
    'discharge_otpr': (0x17, 4, 10),
    'charge_utp': (0x18, 4, 10),
    'charge_utpr': (0x19, 4, 10),
    'mosfet_otp': (0x1A, 4, 10),
    'mosfet_otpr': (0x1B, 4, 10),
    'cell_count': (0x1C, 4, 1),
    'total_capacity': (0x20, 4, 1000),
    'scp_delay': (0x21, 4, 1),  # short_circuit_protection_delay
    'balance_start_voltage': (0x22, 4, 1000),
    'precharge_time': (0x25, 4, 1),  # discharge_precharge_time
    'heating_start_temp': (0x37, 1, 1),
    'heating_stop_temp': (0x38, 1, 1),
    'smart_sleep_delay': (0x39, 1, 1),
    'discharge_utp': (0x3A, 1, 1),
    'discharge_utpr': (0x3B, 1, 1),
    'rcv_time': (0xB3, 1, 10),  # cell_request_charge_voltage_time
    'rfv_time': (0xB4, 1, 10),  # cell_request_float_voltage_time
    'emergency_duration': (0xB5, 1, 1),
    're_bulk_soc': (0xB7, 1, 1),
    'voltage_calibration': (0x64, 4, 1000),
    'current_calibration': (0x67, 4, 1000),

    'heating': (0x27, 4, 1),
    'disable_temperature_sensors': (0x28, 4, 1),
    'display_always_on': (0x2B, 4, 1),
    'smart_sleep': (0x2D, 4, 1),
    'disable_pcl_module': (0x2E, 4, 1),
    'timed_stored_data': (0x2F, 4, 1),
    'charging_float_mode': (0x30, 4, 1),
    'emergency_button_trigger': (0x31, 4, 1),
    'dry_contact_alarm_intermittent': (0x32, 4, 1),
    'discharge_ocp2': (0x33, 4, 1),
    'discharge_ocp3': (0x34, 4, 1),
    'gps_locked_charging': (0x35, 4, 1),
    'gps_locked_discharging': (0x36, 4, 1),
}


def test_register_map_pinned():
    assert set(SETTINGS) == set(PINNED)
    for name, s in SETTINGS.items():
        assert (s.register, s.length, s.factor) == PINNED[name], name


def test_registers_unique():
    regs = [r for r, _, _ in PINNED.values()]
    assert len(regs) == len(set(regs))
