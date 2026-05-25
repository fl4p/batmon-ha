"""JK / Jikong BMS UART (RS485) status-frame fixtures.

Distinct from the BLE protocol — UART uses ``4E 57 …`` TLV framing.

Two complementary sources:

  - ``MPP_SOLAR_14S``: full 285-byte real capture from
    ``jblance/mpp-solar/mppsolar/protocols/jkserial.py``'s
    ``COMMANDS['getBalancerData']['test_responses'][1]``. A real "JK_B1A20S15P"
    pack, 14S, fw 11.XW_S11.261, fully charged (SOC 100%).
    Apache-2.0 licensed. CRC byte 0x51D6 matches ``sum-mod-65536`` of the
    preceding 283 bytes.

  - ``SYSSI_14S_DESCRIPTION``: the per-register annotation block at
    ``syssi/esphome-jk-bms`` ``components/jk_bms/jk_bms.cpp:88-360`` —
    documents each tag's wire bytes and decoded value. Used for unit-level
    coverage of each register without synthesising a full frame (the syssi
    file only annotates per-register, no concatenated frame).
"""

# The full response from mpp-solar's jkserial.py test fixture.
# Stored as raw bytes (Apache-2.0) — verbatim copy.
MPP_SOLAR_14S_RESPONSE: bytes = (
    b'NW\x01\x1b\x00\x00\x00\x00\x03\x00\x01y*\x01\x0f\x90\x02\x0f\x91\x03\x0f\x94\x04\x0f\x8e'
    b'\x05\x0f\x92\x06\x0f\x91\x07\x0f\x91\x08\x0f\x91\t\x0f\x93\n\x0f\x8e\x0b\x0f\x91\x0c\x0f'
    b'\x90\r\x0f\x90\x0e\x0f\x8d\x80\x00!\x81\x00\x1c\x82\x00\x1e\x83\x15\xca\x84\x81\xc5\x85d'
    b'\x86\x02\x87\x00\x19\x89\x00\x00\x16\xda\x8a\x00\x0e\x8b\x00\x00\x8c\x00\x03\x8e\x16\xb2'
    b'\x8f\x10\xf4\x90\x106\x91\x10\x04\x92\x00\x05\x93\x0c\x1c\x94\x0c\x80\x95\x00\x05\x96\x01,'
    b'\x97\x00n\x98\x01,\x99\x00U\x9a\x00\x1e\x9b\x0b\xb8\x9c\x002\x9d\x01\x9e\x00Z\x9f\x00F\xa0'
    b'\x00d\xa1\x00d\xa2\x00\x14\xa3\x00<\xa4\x00<\xa5\x00\x01\xa6\x00\x03\xa7\xff\xec\xa8\xff'
    b'\xf6\xa9\x0e\xaa\x00\x00\x00\xea\xab\x01\xac\x01\xad\x047\xae\x01\xaf\x01\xb0\x00\n\xb1'
    b'\x14\xb2123456\x00\x00\x00\x00\xb3\x00\xb4Input Us\xb52306\xb6\x00\x01\x82\xe3\xb7'
    b'11.XW_S11.261__\xb8\x00\xb9\x00\x00\x00\xea\xbaInput UserdaJK_B1A20S15P\xc0\x01\x00\x00'
    b'\x00\x00h\x00\x00Q\xd6'
)


MPP_SOLAR_14S = dict(
    name="jk_uart_mpp_solar_b1a20s15p_14s_fw_11_26",
    raw=MPP_SOLAR_14S_RESPONSE,
    expected=dict(
        # All 14 cells balanced at ~3.985 V (max-min = 7 mV)
        cell_voltages_mv=[
            3984, 3985, 3988, 3982, 3986, 3985, 3985,
            3985, 3987, 3982, 3985, 3984, 3984, 3981,
        ],
        # 0x83 0x15CA = 5578 × 0.01
        voltage=55.78,
        # 0x84 0x81C5 — high bit set → charging at (0x01C5)/100 = 4.53 A.
        # batmon-ha convention: positive = charging.
        current=4.53,
        soc=100.0,
        num_cycles=25,
        total_charge_throughput=5850.0,
        capacity=234.0,  # 0xAA 0x000000EA
        temperatures=[28.0, 30.0],  # 0x81 = T1, 0x82 = T2
        mos_temperature=33.0,  # 0x80
        switches=dict(charge=True, discharge=True, balance=False),
    ),
    device_info=dict(
        device_id="Input Us",
        production_date="2306",
        software_version="11.XW_S11.261__",
        manufacturer="Input UserdaJK_B1A20S15P",
        protocol_version=1,
    ),
)


ALL = [MPP_SOLAR_14S]
