"""snoop's active probe must not write SIG characteristics such as GAP Device Name (#416)."""

import asyncio
from types import SimpleNamespace

from bmslib.models.snoop import SnoopBt, _is_sig_characteristic


def _uuid(short: int) -> str:
    return '0000%04x-0000-1000-8000-00805f9b34fb' % short


def test_is_sig_characteristic():
    assert _is_sig_characteristic(_uuid(0x2A00))  # Device Name
    assert _is_sig_characteristic(_uuid(0x2A01).upper())  # Appearance
    assert not _is_sig_characteristic(_uuid(0xFFF2))
    assert not _is_sig_characteristic(_uuid(0xFF02))
    assert not _is_sig_characteristic('02f00000-0000-0000-0000-00000000ff01')


def test_probe_skips_gap_device_name():
    # GATT layout of the Daly module in #416, as seen through an ESPHome proxy.
    def char(short, *props):
        return SimpleNamespace(uuid=_uuid(short), properties=list(props))

    services = [
        SimpleNamespace(characteristics=[char(0x2A00, 'read', 'write'), char(0x2A01, 'read', 'write')]),
        SimpleNamespace(characteristics=[char(0xFFF1, 'read', 'notify'),
                                         char(0xFFF2, 'read', 'write-without-response', 'write'),
                                         char(0xFFF3, 'read', 'write-without-response', 'write')]),
    ]
    written = []

    async def write_gatt_char(c, data, response=False):
        written.append(c.uuid)

    bms = SnoopBt("00:11:22:33:44:55", name="snoop", type_spec="daly")
    bms.client = SimpleNamespace(services=services, write_gatt_char=write_gatt_char)

    async def no_sleep(_):
        pass

    orig_sleep = asyncio.sleep
    asyncio.sleep = no_sleep
    try:
        asyncio.run(bms._probe("daly"))
    finally:
        asyncio.sleep = orig_sleep

    assert set(written) == {_uuid(0xFFF2), _uuid(0xFFF3)}
