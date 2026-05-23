"""Daly v2 (Modbus over BLE, ``D2 03`` protocol) test fixtures.

batmon-ha's ``bmslib.models.daly2.Daly2Bt`` speaks the Modbus dialect that
ESPHome's ``daly_bms_ble`` / aiobmsble's ``daly_bms`` plugin also target.
"""


def _hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").replace("\n", ""))


# Source: aiobmsble tests/bms/test_daly_bms.py (CMD_INFO / 124-byte payload).
# 4-cell, 4-temp pack. Decoded ground truth lifted from aiobmsble's
# ``_RESULT_DEFS`` (Apache-2.0, patman15/aiobmsble).
AIOBMSBLE_4S = dict(
    name="daly2_4s_aiobmsble",
    raw=_hex(
        """
        d2 03 7c 10 1f 10 29 10 33 10 3d 00 00 00 00 00 00 00 00 00
        00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
        00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
        00 00 00 00 00 00 00 00 3c 00 3d 00 3e 00 3f 00 00 00 00 00
        00 00 00 00 8c 75 4e 03 84 10 3d 10 1f 00 00 00 00 00 00 0d
        80 00 04 00 04 00 39 00 01 00 00 00 01 10 2e 01 41 00 2a 00
        00 00 00 00 00 00 00 a0 df
        """
    ),
    expected=dict(
        voltage=14.0,
        current=3.0,
        soc=90.0,
        charge=345.6,
        num_cycles=57,
        temperatures=[20, 21, 22, 23],
        switches=dict(charge=False, discharge=False),
    ),
)


ALL = [AIOBMSBLE_4S]
