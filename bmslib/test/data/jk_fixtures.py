"""JK / Jikong BMS BLE status-frame fixtures (300-byte ``55 AA EB 90`` frames).

Two real captures from ``bmslib/models/dummy.py``'s ``JKDummy.MSGS``:
  - 8s firmware < 11.x (legacy 24-cell offset layout)
  - 16s firmware 11.x (32-cell offset layout, the JK11_32S protocol variant)

Plus one real BLE capture from a JK_B2A8S20P (fw 11.50) posted in issue #365,
used to lock in the "capacity = settings-frame value, not cell-info value" fix.

The dummy bytes are baked into batmon-ha to simulate a real device; the
expected values here are what ``JKBt._decode_sample`` actually produces, pinning
the current behavior of the decoder against accidental regressions.
"""

from pathlib import Path

from bmslib.models.dummy import JKDummy

_DATA_DIR = Path(__file__).parent

_LEGACY = JKDummy(is_new_11x=False).MSGS
_NEW11 = JKDummy(is_new_11x=True).MSGS

# Both dummies share the same settings (0x01) frame.
JK_24S_SETTINGS, JK_24S_STATUS = _LEGACY
_, JK_32S_STATUS = _NEW11

LEGACY_8S = dict(
    name="jk_legacy_8s_fw_pre11",
    settings_frame=JK_24S_SETTINGS,
    status_frame=JK_24S_STATUS,
    is_new_11fw_32s=False,
    expected=dict(
        voltage=26.911,
        current=-12.219,
        # BMS-authoritative integer SOC; previously this fixture pinned 65.83
        # (the value BmsSample re-derived from charge/capacity for higher
        # precision), but the JK BMS reports SOC against its internal aged
        # capacity, so we now trust the raw byte (#365).
        soc=65.0,
        charge=181.036,
        capacity=275.0,
        total_charge_throughput=22.173,
        num_cycles=0,
        temperatures=[26.8, 24.8],
        mos_temperature=28.3,
        balance_current=0.0,
        switches=dict(charge=True, discharge=True, balance=True),
        uptime=3144960.0,
        voltages_mv=[3362, 3364, 3365, 3370, 3364, 3362, 3365, 3362],
    ),
)

NEW11_16S = dict(
    name="jk_new11_16s_cells",
    status_frame=JK_32S_STATUS,
    # 16 LiFePO4 cells at u16 LE offset 6: bytes "13 0d 12 0d ..."
    expected_voltages_mv=[
        3347, 3346, 3346, 3343, 3346, 3348, 3347, 3346,
        3346, 3346, 3349, 3347, 3349, 3348, 3347, 3347,
    ],
)

# Frame 1 of six annotated example responses in syssi's ESPHome JK BLE
# component (lines 430-432 of components/jk_bms_ble/jk_bms_ble.cpp). A real
# 16-cell pack captured by syssi, with every byte position labelled and a
# worked example value beside each field. The frame uses the JK02_24S layout
# (cells 1..16 in bytes 6..37, then 16 bytes of cell-17..24 padding, then the
# enabled-cells bitmask `FF FF 00 00` at byte 54). Stored without the dot
# separators that syssi uses for readability.
_SYSSI_FRAME_8C_HEX = (
    "55AAEB90028CFF0C010D010DFF0C010D010DFF0C010D010D010D010DFF0C010D010D010D010D00000000000000000000000000000000FFFF0000000D000000009D0196018C0187018401840183018401850181018301860182018201830185010000000000000000000000000000000000000000000003D000000000000000000000BE00BF00D2000000000000548E0B0100683C0100000000003D04000064007904CA0310000101AA06000000000000000000000000070001000000D50200000000AED63B400000000058AAFDFF0000000100020000ECE64F00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000CD"
)


def _synth_jk_settings_for_syssi(num_cells: int, capacity_ah: float) -> bytes:
    """Build a minimal 300-byte settings (0x01) frame for use with the syssi
    cell-info fixture. Only the bytes ``JKBt._decode_sample`` actually reads
    are populated; everything else is zero.

    The dummy ``JKDummy`` settings frame is 8-cell and would lie about
    ``num_cells`` (offset 114) and ``capacity`` (offset 130) for a 16-cell pack,
    so we synthesise a fitting one here rather than reuse it.
    """
    buf = bytearray(300)
    buf[114] = num_cells
    buf[118] = 1  # charge MOS enabled
    buf[122] = 1  # discharge MOS enabled
    buf[126] = 1  # balancer enabled
    # capacity in mAh, u32 little-endian
    buf[130:134] = int(round(capacity_ah * 1000)).to_bytes(4, "little", signed=False)
    return bytes(buf)


SYSSI_BLE_16S = dict(
    name="jk_syssi_ble_16s_jk02_24s_frame",
    # 16 cells enabled, 81 Ah nominal (matches syssi's annotated byte map at
    # byte 146 → 0x00 0x01 0x3C 0x68 = 81000 mAh).
    settings_frame=_synth_jk_settings_for_syssi(num_cells=16, capacity_ah=81.0),
    status_frame=bytes.fromhex(_SYSSI_FRAME_8C_HEX),
    # JK02_24S frame layout (up to 24 cells) — batmon-ha applies offset=0 here.
    # A "new firmware" pack with ≤24 cells still uses this frame variant in
    # syssi's parser; batmon-ha's ``is_new_11fw_32s`` flag controls only the
    # offset (0 vs 32), not which frame variant is being parsed.
    is_new_11fw_32s=False,
    expected=dict(
        voltage=53.251,
        current=0.0,
        soc=84.0,
        charge=68.494,
        capacity=81.0,
        total_charge_throughput=1.085,
        num_cycles=0,
        # T1, T2 sensors from syssi's annotation
        temperatures=[19.0, 19.1],
        mos_temperature=21.0,
        balance_current=0.0,
        switches=dict(charge=True, discharge=True, balance=True),
        uptime=1049546.0,
        voltages_mv=[
            3327, 3329, 3329, 3327, 3329, 3329, 3327, 3329,
            3329, 3329, 3329, 3327, 3329, 3329, 3329, 3329,
        ],
    ),
)


ISSUE_365_B2A8S20P = dict(
    name="jk_issue365_b2a8s20p_fw_11_50",
    settings_frame=bytes((_DATA_DIR / "jk_issue365_settings.bin").read_bytes()),
    status_frame=bytes((_DATA_DIR / "jk_issue365_status.bin").read_bytes()),
    is_new_11fw_32s=True,
    # User-set 320 Ah, but cell-info offset 178 holds a BMS-aged 251.235 Ah
    # (~ SOH 79% × 320). The decoder must read capacity from the settings frame.
    expected=dict(
        voltage=13.405,
        current=-33.292,
        soc=66,
        charge=165.5,
        capacity=320.0,  # the fix: was 251.235 before
        total_charge_throughput=107478.918,  # lifetime ∫|I|dt, 427 cycles × ~250-320 Ah
        num_cycles=427,
        soh=79.0,
        aged_capacity=251.235,  # BMS-reported (offset 178); ≈ capacity × soh/100 = 252.8
    ),
)


ALL = [LEGACY_8S, ISSUE_365_B2A8S20P, SYSSI_BLE_16S]
