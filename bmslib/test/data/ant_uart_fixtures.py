"""ANT BMS wired-UART fixtures. Provenance: bmslib/test/data/SOURCES.md.

Expected values are what an independent decoder (syssi/esphome-ant-bms, in its
tests or in the reporter's ESPHome log) printed for the same bytes, not what
batmon's decoder produces.
"""


def _hex(s):
    return bytes.fromhex(''.join(s.split()))


# --- 2021+ "7E A1" protocol ---------------------------------------------------

# syssi/esphome-ant-bms tests/components/ant_bms/frames_16s_status.h, the
# STATUS_FRAME_16S recording from esp32-ble-example-faker.yaml (Apache-2.0).
NEW_16S = dict(
    raw=_hex("""
        7E A1 11 00 00 8E 05 01 02 10 00 00 00 00 00 00 00 00 80 00 80 01 00 00 00 00 00 00 00 00 00 00 00 00
        E4 0C E4 0C E5 0C E5 0C E8 0C E7 0C E7 0C E6 0C E8 0C E7 0C E7 0C E7 0C E7 0C E7 0C E6 0C E9 0C
        01 00 02 00 02 00 07 00 A4 14 03 00 5B 00 64 00 01 01 00 00 00 76 B0 10 D5 67 0E 0F BA 32 4A 00
        0F 00 00 00 10 58 2E 02 00 00 00 00 E9 0C 10 00 E4 0C 01 00 05 00 E6 0C 00 00 80 00 7A 00 0F 02
        F2 FA B9 8C 3B 00 BB D8 58 00 DA 2D 43 00 E8 B6 49 00 05 43 AA 55"""),
    # syssi's frames_16s_status.h header / ant_bms_test.cpp assertions
    expected=dict(
        voltage=52.84, current=0.3, soc=91, soh=100,
        capacity=280.0, charge=252.602, total_charge_throughput=4862.65,
        temperatures=[1, 2], mos_temperature=2,
        cells=[3300, 3300, 3301, 3301, 3304, 3303, 3303, 3302, 3304, 3303, 3303, 3303, 3303, 3303, 3302, 3305],
        switches=dict(charge=True, discharge=True),
    ),
)

# Same source, DEVICE_INFO_FRAME: model "16ZM", software "16ZMUB00-211026A".
NEW_DEVICE_INFO = _hex("""
    7E A1 12 6C 02 20 31 36 5A 4D 00 00 00 00 00 00 00 00 00 00 00 00 31 36 5A 4D 55 42 30 30 2D 32
    31 31 30 32 36 41 72 08 FF 0B 00 00 41 F2 AA 55""")

# syssi/esphome-ant-bms issue #18, comment by bchiu 2022-10-26: the vendor PC
# tool's serial exchange with a 24BHUB board, reply to 7E A1 01 00 00 BE 18 55 AA 55
# at 20:25:25. syssi's field annotation of this frame (issue #18, 2023-03-07):
# 14 cells 0x1011, 4 NTCs 1C 00 1C 00 D8 FF 1C 00, MOS 0x1C, total voltage 0x167E,
# current 0, SOC 0x60, SOH 0x64, capacity 0x01C9C380, remaining 0x01B3554F,
# cycle capacity 0x5308.
NEW_14S_PC_TOOL = dict(
    raw=_hex("""
        7e a1 11 00 00 8e 05 01 04 0e 02 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00 00 00 00 00
        00 00 11 10 11 10 11 10 11 10 11 10 11 10 11 10 11 10 11 10 11 10 11 10 11 10 11 10 11 10 1c 00
        1c 00 d8 ff 1c 00 1c 00 1c 00 7e 16 00 00 60 00 64 00 01 02 00 00 80 c3 c9 01 4f 55 b3 01 08 53
        00 00 00 00 00 00 cb 26 12 00 00 00 00 00 11 10 01 00 11 10 01 00 00 00 11 10 01 00 70 00 03 00
        ac 02 f1 fa 7d 2e 00 00 94 77 00 00 de 07 00 00 77 76 00 00 8f 5f aa 55"""),
    expected=dict(
        voltage=57.58, current=0.0, soc=96, soh=100,
        capacity=30.0, charge=28.53, total_charge_throughput=21.256,
        mos_temperature=28, cells=[4113] * 14,
    ),
)

# --- pre-2021 "AA 55 AA FF" protocol ------------------------------------------

# syssi/esphome-ant-bms issue #28, lucasimons' log 8304995 (2020 board, 8S,
# 19200 baud, request 5A 5A 00 00 01 01), frame logged 14:55:41. The adapter
# appended 10 FF bytes after the 140-byte frame; kept as captured.
# Expected: the same log's ESPHome sensor lines at 14:55:40 (fix-negative-power branch).
OLD_2020_8S_CHARGING = dict(
    raw=_hex("""
        AA55AAFF010D0D2B0D2A0D2A0D290D2A0D2A0D2A0D2A00000000000000000000000000000000000000000000000000
        0000000000000000000000000000000000000000000000FFFFFFB64F0000000005A2CA9401276D3101B30A9000110012
        00100011000000000101000000000000FFFFFF3C010D2B040D290D2A080000007E007A00000000000064050F73
        FFFFFFFFFFFFFFFFFFFF"""),
    expected=dict(
        voltage=26.9, current=-7.4, soc=79, charge=94.55477, total_charge_throughput=19361.07227,
        uptime=28510864, cells=[3371, 3370, 3370, 3369, 3370, 3370, 3370, 3370],
        # ESPHome's "temperature 1..4" = offsets 91, 93, 95, 97
        raw_temperatures=[17, 18, 16, 17], power=-196,
        charge_status=1, discharge_status=1, balancer_status=0,
    ),
)

# Same issue, log 8283047, frame logged 09:38:14. This is also syssi's
# STATUS_FRAME_8S (tests/components/ant_bms_old/frames_8s.h); expected values
# from that file's header / ant_bms_old_test.cpp.
OLD_2020_8S = dict(
    raw=_hex("""
        AA55AAFF010B0D0C0D0C0D0B0D0A0D0C0D0B0D0C0D0C00000000000000000000000000000000000000000000000000
        0000000000000000000000000000000000000000000000FFFFFF95290000000002E3DB950126226301B16EBA000F000F
        000D000D000000000101000000000000FFFFFEE3010D0C040D0A0D0B080000007E007A000000000000040F0EDF"""),
    expected=dict(
        voltage=26.7, current=-10.7, soc=41, capacity=0.0, charge=48.487, total_charge_throughput=19276.387,
        uptime=28405434, cells=[3340, 3340, 3339, 3338, 3340, 3339, 3340, 3340],
        raw_temperatures=[15, 15, 13, 13, 0, 0], power=-285,
        charge_status=1, discharge_status=1, balancer_status=0,
    ),
)

# syssi/esphome-ant-bms issue #37, lucasimons' log 8599527: a 2021 board (300 A)
# answering the old request at 19200 baud, 20:51:02. The log only has the text
# sensors at DEBUG: charge and discharge MOSFET "Manually turned off" (0x0F),
# balancer "Off". Power reads 9999, the placeholder the issue is about.
OLD_2021_8S = dict(
    raw=_hex("""
        AA55AAFF010A0D060D060D0B0CF10CF30D0E0D100CE8000000000000000000000000000000000000000000000000
        000000000000000000000000000000000000000000000000000000005B127A398010A22CA5000011F8000F44D0001200110011
        000EFFD8FFD80F0F0000000000000000270F070D10100CE80D0008010E0000000002120000000000000E64"""),
    expected=dict(charge_status=0x0F, discharge_status=0x0F, balancer_status=0, power=9999),
)
