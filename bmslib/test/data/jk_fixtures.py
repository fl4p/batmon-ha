"""JK / Jikong BMS BLE status-frame fixtures (300-byte ``55 AA EB 90`` frames).

Two real captures from ``bmslib/models/dummy.py``'s ``JKDummy.MSGS``:
  - 8s firmware < 11.x (legacy 24-cell offset layout)
  - 16s firmware 11.x (32-cell offset layout, the JK11_32S protocol variant)

The dummy bytes are baked into batmon-ha to simulate a real device; the
expected values here are what ``JKBt._decode_sample`` actually produces, pinning
the current behavior of the decoder against accidental regressions.
"""

from bmslib.models.dummy import JKDummy

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
        soc=65.83,
        charge=181.036,
        capacity=275.0,
        cycle_capacity=22.173,
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


ALL = [LEGACY_8S]
