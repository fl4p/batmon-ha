"""Daly UART/RS485 decoder + framing regression tests."""
import struct

import pytest

from bmslib.models import daly_uart as du
from bmslib.models.daly import daly_command_message
from bmslib.test.data import daly_uart_fixtures as fx


# === Request builder ========================================================

def test_baudrate_is_9600():
    """maland16/daly-bms-uart and the Daly v1.2 protocol PDF both specify
    9600 8N1 for the UART link. JK UART uses 115200 — these must not get
    confused by sharing the same transport class."""
    assert du.DalyUart.BAUDRATE == 9600


def test_build_command_uses_address_4():
    """UART path must set the host-address byte to 4 (USB/RS485)."""
    msg = du.build_command(0x90)
    assert msg[0] == 0xA5
    assert msg[1] == 0x40  # "%i0" with i=4 → "40"
    assert msg[2] == 0x90
    assert msg[3] == 0x08
    assert len(msg) == 13


def test_build_command_matches_known_request_frames():
    """All four read commands match the byte sequences we'd expect from the
    Daly UART v1.2 PDF (cross-derived via daly_command_message(addr=4))."""
    for cmd, expected in fx.REQUEST_FRAMES.items():
        assert du.build_command(cmd) == expected, (
            f"cmd 0x{cmd:02x}: got {du.build_command(cmd).hex()}, "
            f"expected {expected.hex()}"
        )


def test_uart_and_ble_request_frames_differ_only_in_address_byte():
    """The on-wire difference between UART and BLE is exactly one byte:
    position 1 (the host-address nibble). Everything else — header, CRC
    layout, payload zero-padding — is identical."""
    for cmd in (0x90, 0x93, 0x94, 0x95):
        uart = bytes(daly_command_message(cmd, address=4))
        ble = bytes(daly_command_message(cmd, address=8))
        # Address byte differs
        assert uart[1] == 0x40
        assert ble[1] == 0x80
        # Everything else except CRC matches
        assert uart[:1] == ble[:1] == b"\xA5"
        assert uart[2:12] == ble[2:12]
        # CRC differs because input bytes differ
        assert uart[12] != ble[12]


# === Frame validation =======================================================

def test_validate_frame_accepts_good_fixture():
    assert du.validate_response_frame(fx.STATUS_CHARGING["frame"]) is True


def test_validate_frame_rejects_bad_crc():
    bad = bytearray(fx.STATUS_CHARGING["frame"])
    bad[-1] ^= 0xFF
    assert du.validate_response_frame(bytes(bad)) is False


def test_validate_frame_rejects_bad_header():
    bad = b"\xFF" + fx.STATUS_CHARGING["frame"][1:]
    assert du.validate_response_frame(bad) is False


def test_validate_frame_rejects_wrong_length():
    assert du.validate_response_frame(fx.STATUS_CHARGING["frame"][:10]) is False
    assert du.validate_response_frame(fx.STATUS_CHARGING["frame"] + b"\x00") is False


# === Buffer reassembly ======================================================

def test_feed_buffer_assembles_single_frame_from_chunks():
    """A 13-byte frame delivered as arbitrary-sized chunks must reassemble."""
    full = fx.STATUS_CHARGING["frame"]
    buf = bytearray()
    out = bytearray()
    # Worst case: deliver one byte at a time
    for byte in full:
        out.extend(du.feed_buffer(buf, bytes([byte])))
    assert bytes(out) == full
    assert len(buf) == 0


def test_feed_buffer_discards_junk_before_header():
    buf = bytearray()
    junk = b"\xDE\xAD\xBE\xEF\x00"
    assert du.feed_buffer(buf, junk) == b""
    out = du.feed_buffer(buf, fx.STATUS_CHARGING["frame"])
    assert out == fx.STATUS_CHARGING["frame"]


def test_feed_buffer_returns_multiple_frames():
    """Cell-voltage reads expect multiple back-to-back responses (one per
    3 cells). feed_buffer must return all complete frames in one call."""
    full = fx.STATUS_CHARGING["frame"] + fx.STATES_8S["frame"] + fx.SOC_26V4["frame"]
    buf = bytearray()
    out = du.feed_buffer(buf, full)
    assert out == full
    assert len(buf) == 0


def test_feed_buffer_drops_bad_crc_frame_and_resyncs():
    """If a frame has bad CRC, drop it and continue scanning. A subsequent
    valid frame must still be returned."""
    bad = bytearray(fx.STATUS_CHARGING["frame"])
    bad[-1] ^= 0xFF
    good = fx.STATES_8S["frame"]

    buf = bytearray()
    out = du.feed_buffer(buf, bytes(bad) + good)
    assert out == good  # bad one silently dropped


# === Payload decoders (re-use DalyBt decode logic on UART payloads) ========

def _payload_of(frame):
    """Strip the 4-byte header and 1-byte CRC, return the 8-byte payload —
    same slice DalyBt._notification_callback hands to _fetch_futures."""
    return frame[4:-1]


@pytest.mark.parametrize("fixture", fx.ALL_RESPONSES, ids=lambda f: f["name"])
def test_payload_matches_ble_decoder_expectations(fixture):
    """The 8-byte payload extracted from a UART frame must decode identically
    to a BLE payload — same struct format, same scaling factors."""
    payload = _payload_of(fixture["frame"])
    cmd = fixture["cmd"]
    exp = fixture["expected"]

    if cmd == 0x90:
        v, _x, i, soc = struct.unpack(">h h h h", payload)
        assert v / 10 == pytest.approx(exp["voltage"], abs=0.05)
        assert (i - 30000) / 10 == pytest.approx(exp["current"], abs=0.05)
        assert soc / 10 == pytest.approx(exp["soc"], abs=0.05)
    elif cmd == 0x93:
        mode, charging_mos, discharging_mos, _b, mah = struct.unpack(">b ? ? B l", payload)
        assert mode == {"stationary": 0, "charging": 1, "discharging": 2}[exp["mode"]]
        assert charging_mos == exp["charging_mosfet"]
        assert discharging_mos == exp["discharging_mosfet"]
        assert mah / 1000 == pytest.approx(exp["capacity_ah"], abs=0.005)
    elif cmd == 0x94:
        n_cells, n_temps, charging, discharging, _bits, cycles = struct.unpack(
            ">b b ? ? b h x", payload
        )
        assert n_cells == exp["num_cells"]
        assert n_temps == exp["num_temps"]
        assert charging == exp["charging"]
        assert discharging == exp["discharging"]
        assert cycles == exp["num_cycles"]
