"""ANT BMS BLE status-frame fixtures (modern 7E A1 protocol, func 0x11)."""


# Inline example in batmon-ha's bmslib/models/ant.py:151 (commented-out at
# the head of fetch()). 8s, 2 temp sensors, 100 Ah pack. Decoded values are
# what AntBt.fetch() actually produces today — pinned as regression baseline.
INLINE_8S = dict(
    name="ant_8s_2temp_inline",
    raw=bytearray(
        b"~\xa1\x11\x00\x00~\x05\x01\x02\x08\x02\x00\x00\x00\x00\x00"
        b"\x00\x00\x01\x00B\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\xd4\r\xd5\r\xd5\r\xd5\r\xd5\r\xd4\r\xd5\r\xd5\r"
        b"\xd8\xff\xd8\xff\x1c\x00\x1d\x00\x11\x0b\x00\x00d\x00d\x00"
        b"\x01\x02\x00\x00\x00\xe1\xf5\x05\x00\xe1\xf5\x05\xa52\x00\x00"
        b"\x00\x00\x00\x00\xff\x97\x01\x00\x00\x00\x00\x00\xd5\r\x02\x00"
        b"\xd4\r\x01\x00\x01\x00\xd4\r\xf8\xff\x82\x00\x00\x00\xab\x02"
        b"\xf2\xfa\x10\x00\x00\x00:e\x00\x00\x1f\x00\x00\x00\xfab\x00\x00"
        b"\x11\xc3\xaaU"
    ),
    expected=dict(
        voltage=28.33,
        current=0.0,
        charge=100.0,
        capacity=100.0,
        total_charge_throughput=12.965,
        soc=100.0,
        soh=100.0,  # fresh battery in this capture; raw u16 at SOC+2 = 100
        mos_temperature=28,
        switches=dict(charge=False, discharge=True),
        cell_voltages_mv=[3540, 3541, 3541, 3541, 3541, 3540, 3541, 3541],
    ),
)


# ant24 (sw 20PHUB00-211026A, 8s LFP, 245 Ah), captured 2026-11-23 while charging at 6.2 A (raw current is
# negative while charging on this pack; batmon's invert_current flips it). A 126-byte payload: ends after the
# charge/discharge time totals, so there is no remaining-time field.
ANT24_8S = dict(
    name="ant24_8s_charging",
    raw=bytearray.fromhex(
        "7ea11100007e0502020800000000000000000000c411000000000000000000000000880d420d5a0d3f0d450d560da50d4c0d"
        "d8ffd8ff14001700b00ac2ff5e0064000101020040679a0ee5c0930dd80c8c0157ffffff4efd8b0341000000a50d07003f0d"
        "040066005d0dffff81007c00aa02f2faf170a101bfa8760122b97c010fb69d0014daaa55"),
    expected=dict(
        voltage=27.36,
        current=-6.2,
        charge=227.787,
        capacity=245.0,
        total_charge_throughput=25955.544,
        soc=92.97,  # BmsSample's charge / capacity; the BMS itself says 94
        soh=100.0,
        mos_temperature=20,
        switches=dict(charge=True, discharge=True),
        cell_voltages_mv=[3464, 3394, 3418, 3391, 3397, 3414, 3493, 3404],
    ),
)


ALL = [INLINE_8S, ANT24_8S]
