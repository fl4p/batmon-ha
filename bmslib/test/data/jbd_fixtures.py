"""JBD / Xiaoxiang Smart BMS test fixtures (BLE function 0x03 — basic info).

Each fixture is a raw bytes literal (as captured / documented) plus the expected
``BmsSample`` field values that batmon-ha's ``JbdBt.fetch()`` should produce.

See bmslib/test/data/SOURCES.md for provenance.
"""

# syssi/esphome-jbd-bms — components/jbd_bms/jbd_bms.cpp:104-129
# Annotated example with per-byte ground-truth values in the C++ source comments.
SYSSI_3CELL = dict(
    name="jbd_3cell_syssi_example",
    raw=bytes.fromhex(
        "dd03001d"           # SOF, fn=0x03, status=ok, len=0x1d
        "0617"               # voltage = 1559 * 0.01 = 15.59 V
        "0000"               # current = 0.00 A
        "01f3"               # residual capacity = 4.99 Ah
        "01f4"               # nominal capacity = 5.00 Ah
        "0000"               # cycle life = 0
        "2c7c"               # production date packed
        "00000000"           # balancer u32 = 0
        "0000"               # protection bitmask = 0
        "80"                 # version = 0x80
        "64"                 # SOC = 100
        "03"                 # MOS = charge+discharge ON
        "04"                 # cell count = 4 (decoder reads but doesn't expose)
        "03"                 # NTC count = 3
        "0b8d0b8c0b88"       # temps: 22.6, 22.5, 22.1 C
        "fa85"               # checksum
        "77"                 # EOF
    ),
    expected=dict(
        voltage=15.59,
        current=0.0,
        charge=4.99,
        capacity=5.00,
        soc=99.8,            # BmsSample re-derives SOC from charge/capacity
        num_cycles=0,
        temperatures=[22.6, 22.5, 22.1],
        switches=dict(charge=True, discharge=True),
        problem_code=0,
        problem=False,
    ),
)

# bmslib/models/dummy.py JBDDummy — captured by batmon-ha author from a 7s pack
DUMMY_7CELL = dict(
    name="jbd_7cell_dummy",
    raw=bytes.fromhex(
        "dd03001b0a50fda4b717dac000002cf300000000000016540308020b7d0b77f8e277"
    ),
    expected=dict(
        voltage=26.40,
        current=6.04,
        charge=468.71,
        capacity=560.00,
        num_cycles=0,
        temperatures=[21.0, 20.4],
        switches=dict(charge=True, discharge=True),
        problem_code=0,
        problem=False,
    ),
)


ALL = [SYSSI_3CELL, DUMMY_7CELL]
