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


# === Request frames ========================================================
# Byte 1 is 0x40 for board number 1 (0x41 for board 2, ...); the 8 payload bytes
# of a read command are don't-care, and the checksum is a plain sum of the first
# 12 bytes, so the fill byte is free to choose.
#
# The wired path fills with 0xAA (see daly_uart.UART_FILL): Daly's firmware UART
# resyncs on edges and an all-zero payload gives it none, so 0x00 requests go
# unanswered far more often on a wire link. dbus-serialbattery made the same
# choice.
#
# Checksums below are derived independently of the builder, so a regression in
# the builder cannot silently redefine "expected":
#   sum = (0xA5 + addr + cmd + 0x08 + 8*0xAA) & 0xFF
#       = (0x5FD + addr + cmd) & 0xFF
#   addr 0x40: 0x90 -> 0xCD, 0x93 -> 0xD0, 0x94 -> 0xD1, 0x95 -> 0xD2
#   addr 0x41: 0x90 -> 0xCE      addr 0x42: 0x93 -> 0xD2
REQUEST_FRAMES = {
    0x90: bytes.fromhex("a5409008aaaaaaaaaaaaaaaacd"),  # SOC
    0x93: bytes.fromhex("a5409308aaaaaaaaaaaaaaaad0"),  # Status
    0x94: bytes.fromhex("a5409408aaaaaaaaaaaaaaaad1"),  # States
    0x95: bytes.fromhex("a5409508aaaaaaaaaaaaaaaad2"),  # Cell voltages
}

# Same commands with the 0x00 fill batmon <= 2.14 sent, kept so the two encodings
# stay distinguishable in tests (and as the reference for the Daly v1.2 PDF's
# own example frames, which are zero-filled).
REQUEST_FRAMES_ZERO_FILL = {
    0x90: bytes.fromhex("a540900800000000000000007d"),  # SOC
    0x93: bytes.fromhex("a5409308000000000000000080"),  # Status
    0x94: bytes.fromhex("a5409408000000000000000081"),  # States
    0x95: bytes.fromhex("a5409508000000000000000082"),  # Cell voltages
}

# Board addressing: request byte 1 = 0x3F + board number.
REQUEST_FRAMES_BOARD = {
    (0x90, 2): bytes.fromhex("a5419008aaaaaaaaaaaaaaaace"),
    (0x93, 3): bytes.fromhex("a5429308aaaaaaaaaaaaaaaad2"),
}


ALL_RESPONSES = [SOC_26V4, STATUS_CHARGING, STATES_8S]
