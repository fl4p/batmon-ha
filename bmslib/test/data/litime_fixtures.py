"""LiTime BLE shunt-style fixtures.

LiTime, Redodo, and a handful of other 12V/24V LFP brands share the same BLE
protocol (reverse-engineered by calledit/LiTime_BMS_bluetooth). The 101-byte
status response below is borrowed from aiobmsble's Redodo test (Apache-2.0)
and routed through batmon-ha's ``LitimeBt.fetch``.
"""


# patman15/aiobmsble tests/bms/test_redodo_bms.py
REDODO_8S = dict(
    name="litime_8s_redodo_aiobmsble",
    raw=bytearray(
        b"\x00\x00\x65\x01\x93\x55\xaa\x00\x46\x66\x00\x00\xbc\x67\x00\x00\xf5\x0c\xf7\x0c\xfc\x0c"
        b"\xfb\x0c\xf8\x0c\xf2\x0c\xfa\x0c\xf5\x0c\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x65\xfa\xff\xff\x17\x00\x16\x00\xfe\xff\x00\x00\x00\x00\xe9\x1a\x04\x29"
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x41\x00\x64\x00\x00\x00\x03\x00\x00\x00\x5f\x01\x00\x00\xa2"
    ),
    expected=dict(
        voltage=26.556,
        current=1.435,        # batmon-ha negates the signed reading
        charge=68.89,
        capacity=105.0,
        num_cycles=3,
        total_charge_throughput=351,
        temperatures=[23],
        mos_temperature=22,
        cell_voltages_mv=[3317, 3319, 3324, 3323, 3320, 3314, 3322, 3317],
    ),
)


ALL = [REDODO_8S]
