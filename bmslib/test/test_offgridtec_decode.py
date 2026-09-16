"""Offgridtec OGT-12200 trace-based decoder regression tests.

The fixtures are complete FFE4 records captured from an OGT-12200-19H006.
On the wire each binary record is transported as::

    B6 + 112 ASCII-hex characters + 8 * 03

The 112 characters encode 56 bytes.  Bytes 54..55 contain the big-endian
16-bit sum of bytes 0..53.  Current occupies bytes 4..7 as a signed
little-endian 32-bit value in mA; this matters because charging frames have
``0000`` in bytes 6..7 while discharging frames have ``FFFF`` there.
"""

import asyncio
import math

import pytest

from bmslib.models import get_bms_model_class
from bmslib.models.offgridtec import OffgridtecBt


TRACE_FIXTURES = (
    {
        "id": "positive-3927",
        "raw": "6E340000570F0000C820030051005A00600B00808590150D1A0D1D0D1F0D"
        "000000000000000000000000000000000000000000000000053D",
        "voltage_mv": 13422,
        "current_ma": 3927,
        "cycles": 81,
        "soc": 90,
        "temperature": 18.05,
        "cells": [3349, 3354, 3357, 3359],
    },
    {
        "id": "positive-3964",
        "raw": "6B3400007C0F0000C820030051005A00600B00808590150D1A0D1D0D1F0D"
        "000000000000000000000000000000000000000000000000055F",
        "voltage_mv": 13419,
        "current_ma": 3964,
        "cycles": 81,
        "soc": 90,
        "temperature": 18.05,
        "cells": [3349, 3354, 3357, 3359],
    },
    {
        "id": "positive-3949",
        "raw": "6B3400006D0F0000C820030051005A00600B00808590150D1A0D1D0D1F0D"
        "0000000000000000000000000000000000000000000000000550",
        "voltage_mv": 13419,
        "current_ma": 3949,
        "cycles": 81,
        "soc": 90,
        "temperature": 18.05,
        "cells": [3349, 3354, 3357, 3359],
    },
    {
        "id": "positive-3956-soc91",
        "raw": "79340000740F0000C820030051005B00600B00808590180D1E0D200D230D"
        "0000000000000000000000000000000000000000000000000574",
        "voltage_mv": 13433,
        "current_ma": 3956,
        "cycles": 81,
        "soc": 91,
        "temperature": 18.05,
        "cells": [3352, 3358, 3360, 3363],
    },
    {
        "id": "positive-3912-soc91",
        "raw": "79340000480F0000C820030051005B00600B00808590180D1E0D200D230D"
        "0000000000000000000000000000000000000000000000000548",
        "voltage_mv": 13433,
        "current_ma": 3912,
        "cycles": 81,
        "soc": 91,
        "temperature": 18.05,
        "cells": [3352, 3358, 3360, 3363],
    },
    {
        "id": "positive-3964-cells-b",
        "raw": "873400007C0F0000C820030051005B00600B008085901A0D230D240D260D"
        "0000000000000000000000000000000000000000000000000598",
        "voltage_mv": 13447,
        "current_ma": 3964,
        "cycles": 81,
        "soc": 91,
        "temperature": 18.05,
        "cells": [3354, 3363, 3364, 3366],
    },
    {
        "id": "positive-3949-cells-b",
        "raw": "873400006D0F0000C820030051005B00600B008085901A0D230D240D260D"
        "0000000000000000000000000000000000000000000000000589",
        "voltage_mv": 13447,
        "current_ma": 3949,
        "cycles": 81,
        "soc": 91,
        "temperature": 18.05,
        "cells": [3354, 3363, 3364, 3366],
    },
    {
        "id": "negative-927",
        "raw": "DB33000061FCFFFFC820030050005E00720B008096B4F20CF60CF80CFB0C"
        "0000000000000000000000000000000000000000000000000C54",
        "voltage_mv": 13275,
        "current_ma": -927,
        "cycles": 80,
        "soc": 94,
        "temperature": 19.85,
        "cells": [3314, 3318, 3320, 3323],
    },
)


def _raw(fixture: dict) -> bytes:
    return bytes.fromhex(fixture["raw"])


def _wire(raw: bytes) -> bytes:
    return b"\xB6" + raw.hex().upper().encode("ascii") + b"\x03" * 8


def _with_cells(fixture: dict, cells: list[int]) -> bytes:
    """Create a synthetic topology variant while preserving traced framing."""
    assert 1 <= len(cells) <= 16
    raw = bytearray(_raw(fixture))
    raw[0:4] = sum(cells).to_bytes(4, "little")
    raw[22:54] = b"".join(cell.to_bytes(2, "little") for cell in cells)
    raw[22 + 2 * len(cells):54] = b"\x00" * (32 - 2 * len(cells))
    raw[54:56] = (sum(raw[:54]) & 0xFFFF).to_bytes(2, "big")
    return bytes(raw)


def _bms() -> OffgridtecBt:
    return OffgridtecBt("00:11:22:33:44:55", name="offgridtec-test")


@pytest.mark.parametrize("fixture", TRACE_FIXTURES, ids=lambda f: f["id"])
def test_captured_record_layout_and_checksum(fixture):
    """The copied fixtures must remain byte-for-byte valid trace evidence."""
    raw = _raw(fixture)

    assert len(raw) == 56
    assert int.from_bytes(raw[54:56], "big") == sum(raw[:54]) & 0xFFFF
    assert int.from_bytes(raw[0:4], "little") == fixture["voltage_mv"]
    assert int.from_bytes(raw[4:8], "little", signed=True) == fixture["current_ma"]
    assert int.from_bytes(raw[8:12], "little") == 205_000
    assert raw[18] == 0

    all_cell_slots = [
        int.from_bytes(raw[offset:offset + 2], "little")
        for offset in range(22, 54, 2)
    ]
    assert all_cell_slots[:4] == fixture["cells"]
    assert all_cell_slots[4:] == [0] * 12
    assert abs(sum(fixture["cells"]) - fixture["voltage_mv"]) <= 20


@pytest.mark.parametrize("fixture", TRACE_FIXTURES, ids=lambda f: f["id"])
def test_decode_captured_record(fixture):
    record = _bms()._decode_record(_raw(fixture))

    assert record is not None
    assert record["voltage"] == pytest.approx(fixture["voltage_mv"] / 1000)
    assert record["raw_current"] == pytest.approx(fixture["current_ma"] / 1000)
    assert record["current"] == pytest.approx(-fixture["current_ma"] / 1000)
    assert record["capacity"] == pytest.approx(205.0)
    assert record["cycles"] == fixture["cycles"]
    assert record["soc"] == fixture["soc"]
    assert record["temperature"] == pytest.approx(fixture["temperature"])
    assert record["problem_code"] == 0
    assert record["alarms"] == {
        "hv": False,
        "lv": False,
        "occ": False,
        "ocd": False,
        "ltd": False,
        "ltc": False,
        "htd": False,
        "htc": False,
    }
    assert record["pack_status"] == 0x80
    assert record["afe_status"] in (0x85, 0x96)
    assert record["cells"] == fixture["cells"]


def test_decode_rejects_bad_checksum():
    raw = bytearray(_raw(TRACE_FIXTURES[-1]))
    raw[-1] ^= 0x01

    assert _bms()._decode_record(bytes(raw)) is None


@pytest.mark.parametrize(
    ("cells", "expected_voltage"),
    (
        ([3300] * 8, 26.4),   # nominal 24 V LiFePO4 pack
        ([4200] * 16, 67.2),  # all slots and voltage above the u16 range
    ),
)
def test_decode_supports_up_to_16_cells_without_12v_limit(cells, expected_voltage):
    raw = _with_cells(TRACE_FIXTURES[0], cells)
    record = _bms()._decode_record(raw)

    assert record is not None
    assert record["voltage"] == pytest.approx(expected_voltage)
    assert record["cells"] == cells
    assert record["cell_slots"] == cells + [0] * (16 - len(cells))


@pytest.mark.parametrize("length", (0, 30, 55, 57))
def test_decode_rejects_wrong_length(length):
    raw = _raw(TRACE_FIXTURES[0])
    candidate = (raw + b"\x00")[:length]

    assert _bms()._decode_record(candidate) is None


def test_notification_reassembles_fragmented_wire_record():
    async def run():
        bms = _bms()
        wire = _wire(_raw(TRACE_FIXTURES[0]))
        chunks = (wire[:1], wire[1:18], wire[18:39], wire[39:87], wire[87:])

        with bms._fetch_futures.acquire("realtime"):
            for chunk in chunks:
                bms._notification_handler(None, chunk)
            return await bms._fetch_futures.wait_for("realtime", 0.1)

    record = asyncio.run(run())
    assert record["voltage"] == pytest.approx(13.422)
    assert record["raw_current"] == pytest.approx(3.927)


def test_notification_drains_every_record_in_a_burst():
    """A 244-byte MTU carries two 121-byte records per notify. Stopping after the
    first one leaked the rest into the buffer, one record per burst forever, and
    published an ever-staler reading."""

    async def run():
        bms = _bms()
        first, last = _raw(TRACE_FIXTURES[0]), _raw(TRACE_FIXTURES[-1])

        with bms._fetch_futures.acquire("realtime"):
            for _ in range(20):
                bms._notification_handler(None, _wire(first) * 9 + _wire(last))
            record = await bms._fetch_futures.wait_for("realtime", 0.1)
        return record, len(bms._buffer)

    record, buffered = asyncio.run(run())
    assert buffered == 0
    # the newest record of the burst, not the first one
    assert record["voltage"] == pytest.approx(13.275)


def test_decode_keeps_a_record_with_an_out_of_range_cell():
    """An over-voltage cell or a dead sense line must still be published: dropping
    the record makes the device, and its alarm sensors, go unavailable exactly when
    they matter."""
    bms = _bms()
    for cells in ([3349, 3354, 4650, 3359], [3349, 0, 3357, 3359], [1200, 3354, 3357, 3359]):
        record = bms._decode_record(_with_cells(TRACE_FIXTURES[0], cells))
        assert record is not None, cells
        assert record["cells"] == cells


def test_decode_keeps_a_record_measured_under_load():
    """Pack voltage is measured after the shunt and the MOSFETs, so at high current
    it drifts from the sum of the cells by far more than the captures' 3-14 mV."""
    bms = _bms()
    raw = bytearray(_with_cells(TRACE_FIXTURES[0], [3349, 3354, 3357, 3359]))
    cell_sum = 3349 + 3354 + 3357 + 3359
    raw[0:4] = (cell_sum - 400).to_bytes(4, "little")  # ~0.4 V of sag
    raw[54:56] = (sum(raw[:54]) & 0xFFFF).to_bytes(2, "big")
    assert bms._decode_record(bytes(raw)) is not None


def test_decode_rejects_a_misaligned_record():
    """The pack-voltage cross-check still has to catch a decode that is off by
    volts, which is all it was ever able to catch."""
    bms = _bms()
    raw = bytearray(_with_cells(TRACE_FIXTURES[0], [3349, 3354, 3357, 3359]))
    raw[0:4] = (6000).to_bytes(4, "little")
    raw[54:56] = (sum(raw[:54]) & 0xFFFF).to_bytes(2, "big")
    assert bms._decode_record(bytes(raw)) is None


def test_notification_skips_bad_checksum_and_resynchronizes():
    async def run():
        bms = _bms()
        valid = _raw(TRACE_FIXTURES[-1])
        corrupt = bytearray(valid)
        corrupt[0] ^= 0x01

        with bms._fetch_futures.acquire("realtime"):
            bms._notification_handler(None, _wire(bytes(corrupt)) + _wire(valid))
            return await bms._fetch_futures.wait_for("realtime", 0.1)

    record = asyncio.run(run())
    assert record["voltage"] == pytest.approx(13.275)
    assert record["raw_current"] == pytest.approx(-0.927)


def test_fetch_maps_confirmed_topband_fields_to_batmon():
    async def run():
        bms = _bms()

        async def start_notify(uuid, callback):
            assert uuid == bms.UUID_RX
            callback(None, _wire(_raw(TRACE_FIXTURES[-1])))

        bms.start_notify = start_notify
        return await bms.fetch()

    sample = asyncio.run(run())
    assert sample.voltage == pytest.approx(13.275)
    assert sample.current == pytest.approx(0.927)  # BatMon: discharge is positive
    assert sample.soc == 94
    assert math.isnan(sample.charge)
    assert sample.capacity == pytest.approx(205.0)
    # The vendor app calls this design capacity. Its qualitative health label
    # is not a transmitted SOH/effective-capacity measurement.
    assert math.isnan(sample.soh)
    assert math.isnan(sample.aged_capacity)
    assert sample.num_cycles == 80
    assert sample.temperatures == pytest.approx([19.85])
    assert sample.problem is False
    assert sample.problem_code == 0
    assert sample.alarms == {
        "hv": False,
        "lv": False,
        "occ": False,
        "ocd": False,
        "ltd": False,
        "ltc": False,
        "htd": False,
        "htc": False,
    }


@pytest.mark.parametrize("bit, alarm", list(enumerate(OffgridtecBt.ALARM_BITS)))
def test_fetch_maps_problem_code_bits_to_named_alarms(bit, alarm):
    async def run():
        bms = _bms()
        raw = bytearray(_raw(TRACE_FIXTURES[0]))
        raw[18] = 1 << bit
        raw[54:56] = (sum(raw[:54]) & 0xFFFF).to_bytes(2, "big")

        async def start_notify(_uuid, callback):
            callback(None, _wire(bytes(raw)))

        bms.start_notify = start_notify
        return await bms.fetch()

    sample = asyncio.run(run())
    assert sample.problem is True
    assert sample.problem_code == 1 << bit
    assert sample.alarms[alarm] is True
    assert sum(sample.alarms.values()) == 1


def test_fetch_refreshes_subscription_for_each_keep_alive_sample():
    async def run():
        bms = _bms()
        fixtures = iter((TRACE_FIXTURES[0], TRACE_FIXTURES[-1]))
        subscribe_calls = []

        async def start_notify(uuid, callback):
            subscribe_calls.append(uuid)
            callback(None, _wire(_raw(next(fixtures))))

        bms.start_notify = start_notify
        first = await bms.fetch()
        second = await bms.fetch()
        return bms, subscribe_calls, first, second

    bms, subscribe_calls, first, second = asyncio.run(run())

    assert subscribe_calls == [bms.UUID_RX, bms.UUID_RX]
    assert first.voltage == pytest.approx(13.422)
    assert second.voltage == pytest.approx(13.275)


def test_offgridtec_registry_entry_resolves_native_decoder():
    assert get_bms_model_class("offgridtec") is OffgridtecBt
