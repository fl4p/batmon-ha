"""Daly legacy BLE fixtures (the ``A5 80 cmd 08 ...`` 13-byte command/response
protocol used by ``bmslib.models.daly.DalyBt``).

Daly responses arrive without the leading ``A5 01 cmd 08`` header that the
write-side builds; the ``_notification_callback`` strips bytes ``[0:4]`` and
the trailing CRC and hands the 8-byte payload to ``_fetch_futures``. The
fixtures below capture that 8-byte payload, which is what ``_q`` returns
directly.
"""

# Status (cmd 0x93) — from in-source comments in bmslib/models/daly.py:237-245.
# The C-style format is ">b ? ? B l": mode, charging_mosfet, discharging_mosfet,
# unused_byte, capacity_in_mAh (signed 32-bit BE).
STATUS_DSG_ON = dict(
    name="daly_status_dsg_on_1",
    cmd=0x93,
    raw=b"\x01\x01\x01\xca\x00\x03\xdd\x38",
    expected=dict(
        mode="charging",
        charging_mosfet=True,
        discharging_mosfet=True,
        capacity_ah=253.24,   # 0x0003DD38 mAh → 253.24 Ah
    ),
)

STATUS_DSG_OFF = dict(
    name="daly_status_dsg_off_1",
    cmd=0x93,
    raw=b"\x01\x01\x01\x5d\x00\x03\xda\x2c",
    expected=dict(
        mode="charging",
        charging_mosfet=True,
        discharging_mosfet=True,   # raw bit doesn't track real switch state
        capacity_ah=252.460,
    ),
)

ALL_STATUS = [STATUS_DSG_ON, STATUS_DSG_OFF]


# States (cmd 0x94) — from in-source comments at daly.py:285.
# Format ">b b ? ? b h x": num_cells, num_temps, charging, discharging,
# state_bits, num_cycles, pad.
STATES_8CELL = dict(
    name="daly_states_8cell_2temp",
    cmd=0x94,
    raw=b"\x08\x01\x00\x00\x02\x00\x35\xdc",
    expected=dict(
        num_cells=8,
        num_temps=1,
        charging=False,
        discharging=False,
        num_cycles=0x35,   # 53 from raw u16 BE 0x0035
        states={"DI2": True},   # bit 1 of 0x02
    ),
)

ALL_STATES = [STATES_8CELL]


# SOC (cmd 0x90) — synthesized per protocol doc (4× signed BE 16-bit fields:
# voltage*10, x_voltage*10, current+30000 (centred *10), soc*10).
# Source: agent web-search round-trip-verified against dreadnought/python-daly-bms.
SOC_SYNTHETIC_265V_5A = dict(
    name="daly_soc_synth_26v5_neg5a_78p5",
    cmd=0x90,
    # 264 → 26.4V; 0 unused; 30050 → +5.0A; 785 → 78.5% SOC
    raw=b"\x01\x08\x00\x00\x75\x62\x03\x11",
    expected=dict(
        voltage=26.4,
        current=5.0,   # decoder formula: (raw - 30000) / 10 (raw 30050 → 5.0)
        soc=78.5,
    ),
)

ALL_SOC = [SOC_SYNTHETIC_265V_5A]
