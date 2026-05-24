"""Daly BMS UART/RS485 fixtures.

Reuses the 8-byte payloads from ``daly_fixtures.py`` but wraps them in the
full 13-byte ``A5 01 <cmd> 08 <payload> <crc>`` envelope the BMS sends on
the wire. The wire format is identical between BLE and UART — only the
*request* address byte differs (4 = USB/RS485, 8 = BLE), which is why
``DalyUart`` can reuse every ``DalyBt`` response decoder.

Reference for the address-byte convention (4 = USB/RS485, 8 = BLE):
- dreadnought/python-daly-bms ``dalybms/daly_bms.py`` —
  ``"4 for RS485, 8 for UART/Bluetooth"``
- syssi/esphome-daly-bms README — ``4 = USB, 8 = Bluetooth``
- bmslib/models/daly.py inline comment (was already there).
"""
from bmslib.models.daly import calc_crc


def _wrap_response(cmd: int, payload: bytes) -> bytes:
    """Build a complete 13-byte BMS→host response: ``A5 01 cmd 08 <payload> crc``."""
    assert len(payload) == 8
    frame = bytes([0xA5, 0x01, cmd, 0x08]) + payload
    return frame + bytes([calc_crc(frame)])


# === SOC (cmd 0x90) =========================================================
# 4× signed 16-bit BE fields: voltage*10, x_voltage*10, current+30000 (centred,
# scaled ×10), soc*10. Same payload as daly_fixtures.SOC_SYNTHETIC_265V_5A but
# wrapped for the wire format.
SOC_26V4 = dict(
    name="daly_uart_soc_26v4_pos5a_78p5",
    cmd=0x90,
    frame=_wrap_response(0x90, b"\x01\x08\x00\x00\x75\x62\x03\x11"),
    expected=dict(
        voltage=26.4,
        current=5.0,
        soc=78.5,
    ),
)


# === Status (cmd 0x93) ======================================================
# Format ">b ? ? B l": mode, charging_mosfet, discharging_mosfet, byte, mAh_BE
STATUS_CHARGING = dict(
    name="daly_uart_status_charging_253ah",
    cmd=0x93,
    frame=_wrap_response(0x93, b"\x01\x01\x01\xca\x00\x03\xdd\x38"),
    expected=dict(
        mode="charging",
        charging_mosfet=True,
        discharging_mosfet=True,
        capacity_ah=253.24,
    ),
)


# === States (cmd 0x94) ======================================================
# Format ">b b ? ? b h x": num_cells, num_temps, charging, discharging,
# state_bits, num_cycles_BE, pad
STATES_8S = dict(
    name="daly_uart_states_8cell_1temp",
    cmd=0x94,
    frame=_wrap_response(0x94, b"\x08\x01\x00\x00\x02\x00\x35\xdc"),
    expected=dict(
        num_cells=8,
        num_temps=1,
        charging=False,
        discharging=False,
        num_cycles=0x35,
        states={"DI2": True},
    ),
)


# === Request frames (address=4) ============================================
# Cross-checked by feeding ``daly_command_message(cmd, address=4)`` through
# the same builder the live ``_q`` uses.
REQUEST_FRAMES = {
    0x90: bytes.fromhex("a540900800000000000000007d"),  # SOC
    0x93: bytes.fromhex("a5409308000000000000000080"),  # Status
    0x94: bytes.fromhex("a5409408000000000000000081"),  # States
    0x95: bytes.fromhex("a5409508000000000000000082"),  # Cell voltages
}


ALL_RESPONSES = [SOC_26V4, STATUS_CHARGING, STATES_8S]
