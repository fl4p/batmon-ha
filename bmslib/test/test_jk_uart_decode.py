"""JK / Jikong UART (RS485) decoder regression tests.

Exercise the pure-decode path in ``bmslib.models.jikong_uart`` without any
BLE / serial transport — feed the captured bytes directly to the parser.
"""
import pytest

from bmslib.models import jikong_uart as ju
from bmslib.test.data import jk_uart_fixtures as fx


def test_build_status_request_matches_known_good():
    """The 21-byte read-all-registers request must match the bytes used by
    Louisvdw/dbus-serialbattery (``Jkbms.command_status``) and documented in
    syssi/esphome-jk-bms ``components/jk_bms/jk_bms.cpp:82`` as
    ``-> 0x4E 0x57 0x00 0x13 0x00 0x00 0x00 0x00 0x06 0x03 0x00 0x00 0x00
    0x00 0x00 0x00 0x68 0x00 0x00 0x01 0x29``."""
    expected = b"\x4E\x57\x00\x13\x00\x00\x00\x00\x06\x03\x00\x00\x00\x00\x00\x00\x68\x00\x00\x01\x29"
    assert ju.build_status_request() == expected
    assert len(expected) == 21


def test_crc_roundtrip():
    req = ju.build_status_request()
    data_len = int.from_bytes(req[2:4], "big")
    assert ju.crc16_jk(req[:data_len]) == int.from_bytes(req[data_len:], "big")


def test_validate_frame_accepts_mpp_solar_fixture():
    assert ju.validate_frame(fx.MPP_SOLAR_14S_RESPONSE) == 283


def test_validate_frame_rejects_bad_header():
    with pytest.raises(ju.JKUartFrameError, match="bad header"):
        ju.validate_frame(b"\xFF\xFF" + fx.MPP_SOLAR_14S_RESPONSE[2:])


def test_validate_frame_rejects_bad_crc():
    bad = bytearray(fx.MPP_SOLAR_14S_RESPONSE)
    bad[-1] ^= 0xFF
    with pytest.raises(ju.JKUartFrameError, match="crc mismatch"):
        ju.validate_frame(bytes(bad))


def test_validate_frame_rejects_truncated():
    with pytest.raises(ju.JKUartFrameError, match="truncated"):
        ju.validate_frame(fx.MPP_SOLAR_14S_RESPONSE[:50])


def test_decode_temp_signed_convention():
    """Match syssi's ``get_temperature_`` formula (``99 - raw`` when raw > 99).
    Field comment in syssi's source claims "100 = 100°C" but the code returns
    ``-1`` for raw 100 — we follow the code."""
    assert ju._decode_temp(99) == 99.0
    assert ju._decode_temp(100) == -1.0
    assert ju._decode_temp(101) == -2.0
    assert ju._decode_temp(140) == -41.0


def test_decode_current_sign_convention():
    """Per dbus-serialbattery: high bit clear = discharge (negative).
    Cross-checked with mpp-solar's BigHex2Short formula."""
    # high bit set (charging) — magnitude in lower 15 bits
    assert ju._decode_current_0x84(0x81C5) == pytest.approx(4.53)
    # high bit clear (discharging) — magnitude is the raw value
    assert ju._decode_current_0x84(0x01C5) == pytest.approx(-4.53)
    # zero
    assert ju._decode_current_0x84(0x8000) == 0.0
    assert ju._decode_current_0x84(0x0000) == 0.0


def test_parse_status_frame_mpp_solar_14s():
    """Decode the full mpp-solar test response and validate every field."""
    sample = ju.parse_status_frame(fx.MPP_SOLAR_14S["raw"])
    exp = fx.MPP_SOLAR_14S["expected"]

    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.005)
    assert sample.current == pytest.approx(exp["current"], abs=0.005)
    assert sample.soc == pytest.approx(exp["soc"], abs=0.05)
    assert sample.num_cycles == exp["num_cycles"]
    assert sample.total_charge_throughput == pytest.approx(
        exp["total_charge_throughput"], abs=0.5
    )
    assert sample.capacity == pytest.approx(exp["capacity"], abs=0.005)
    assert list(sample.temperatures) == pytest.approx(exp["temperatures"], abs=0.05)
    assert sample.mos_temperature == pytest.approx(exp["mos_temperature"], abs=0.05)
    assert sample.switches == exp["switches"]


def test_parse_tlv_body_cell_array():
    """Walk the cell-array tag directly and check all 14 cell voltages."""
    data_len = ju.validate_frame(fx.MPP_SOLAR_14S_RESPONSE)
    body = fx.MPP_SOLAR_14S_RESPONSE[11:data_len - 3]
    fields = ju.parse_tlv_body(body)

    cells = fields[0x79]
    assert len(cells) == 14
    # Indices should be 1..14 (1-based per syssi annotation)
    assert [idx for idx, _ in cells] == list(range(1, 15))

    voltages = [mv for _, mv in cells]
    assert voltages == fx.MPP_SOLAR_14S["expected"]["cell_voltages_mv"]


def test_parse_tlv_body_rejects_unknown_tag():
    # Inject an unknown tag 0xC1 into a tiny body
    body = b"\xC1\x00\x68"
    with pytest.raises(ju.JKUartFrameError, match="unknown register tag"):
        ju.parse_tlv_body(body)


def test_parse_device_info_strings():
    info = ju.parse_device_info(fx.MPP_SOLAR_14S_RESPONSE)
    exp = fx.MPP_SOLAR_14S["device_info"]
    assert info["software_version"] == exp["software_version"]
    assert info["production_date"] == exp["production_date"]
    assert info["device_id"] == exp["device_id"]
    assert info["manufacturer"] == exp["manufacturer"]
    assert info["protocol_version"] == exp["protocol_version"]


def test_feed_buffer_assembles_frame_from_chunks():
    """The wrapper delivers data in arbitrary chunks (``readline`` splits at
    every 0x0A byte). ``feed_buffer`` must reassemble across chunk boundaries."""
    full = fx.MPP_SOLAR_14S_RESPONSE
    # Split on every \n to simulate readline's chopping behaviour
    chunks = []
    last = 0
    for i, b in enumerate(full):
        if b == 0x0A:
            chunks.append(full[last:i + 1])
            last = i + 1
    chunks.append(full[last:])
    # Sanity: the chopping really does happen — there's at least one chunk break
    assert len(chunks) > 1

    buf = bytearray()
    frame = None
    for chunk in chunks:
        frame = ju.feed_buffer(buf, chunk)
        if frame is not None:
            break
    assert frame == full
    assert len(buf) == 0  # consumed


def test_feed_buffer_discards_junk_before_header():
    """Random bytes ahead of 4E 57 are dropped (e.g. line noise after open)."""
    full = fx.MPP_SOLAR_14S_RESPONSE
    buf = bytearray()
    junk = b"\xDE\xAD\xBE\xEF\x00\x00"
    assert ju.feed_buffer(buf, junk) is None
    assert ju.feed_buffer(buf, full) == full


def test_feed_buffer_returns_only_first_frame_then_holds_rest():
    """If two frames arrive back-to-back, only the first is returned; the
    second stays buffered for the next ``feed_buffer`` call."""
    full = fx.MPP_SOLAR_14S_RESPONSE
    buf = bytearray()
    out = ju.feed_buffer(buf, full + full)
    assert out == full
    # second frame still pending
    assert len(buf) == len(full)
    out2 = ju.feed_buffer(buf, b"")
    assert out2 == full


def test_parse_tlv_body_walks_individual_registers():
    """Independently verify a few non-cell registers can be read out of the
    full body. Cross-checks the per-tag widths in REGISTER_WIDTHS against the
    real frame layout."""
    data_len = ju.validate_frame(fx.MPP_SOLAR_14S_RESPONSE)
    body = fx.MPP_SOLAR_14S_RESPONSE[11:data_len - 3]
    fields = ju.parse_tlv_body(body)

    # 0x83 — total voltage raw u16 BE = 5578 (×0.01 → 55.78V)
    assert int.from_bytes(fields[0x83], "big") == 5578
    # 0x85 — SOC byte = 100%
    assert fields[0x85][0] == 100
    # 0x87 — cycles raw = 25
    assert int.from_bytes(fields[0x87], "big") == 25
    # 0xAA — capacity u32 BE = 234 Ah
    assert int.from_bytes(fields[0xAA], "big") == 234
    # 0x8C — modes bitmask = 0x0003 (charge + discharge)
    assert int.from_bytes(fields[0x8C], "big") == 0x0003
    # 0xAB / 0xAC — charging / discharging MOS bytes
    assert fields[0xAB][0] == 1 and fields[0xAC][0] == 1
