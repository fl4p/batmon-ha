"""jk_pb_uart_passive: two packs on one listen-only port, master (0) and slave (1)."""
import asyncio

import pytest

import bmslib.uart
from bmslib.test.test_jk_pb_bus import cycle, with_cell_mv, STATUS, SETTINGS


class _NullWrapper:
    def __init__(self, address, **kwargs):
        self.address = address
        self.services = []
        self.rx_thread_error = None
        self.written = []

    async def connect(self, timeout=None):
        pass

    async def disconnect(self):
        pass

    async def start_notify(self, char, cb):
        pass

    async def stop_notify(self, char):
        pass

    async def write_gatt_char(self, char, data):
        self.written.append(bytes(data))


@pytest.fixture
def serial_stub(monkeypatch):
    monkeypatch.setattr(bmslib.uart, 'SerialBleakClientWrapper', _NullWrapper)


def _pack_voltage(frame: bytes) -> bytes:
    """Pack voltage (32S layout, 118 + 32) = sum of the four cells, so the frame stays plausible."""
    b = bytearray(frame)
    b[150:154] = sum(int.from_bytes(b[6 + 2 * i:8 + 2 * i], 'little') for i in range(4)).to_bytes(4, 'little')
    b[-1] = sum(b[:-1]) & 0xFF
    return bytes(b)


def _feed(bms_list, stream, size=97):
    for i in range(0, len(stream), size):
        for bms in bms_list:
            bms._notification_handler(None, stream[i:i + size])


def test_each_pack_gets_its_own_frames(serial_stub, monkeypatch):
    import bmslib.test.test_jk_pb_bus as fx
    from bmslib.models.jk_pb_uart_passive import JkPbUartPassive

    # the two packs differ in their first cell (3335 / 3287 mV, as on the real pair), hence in pack voltage
    monkeypatch.setattr(fx, 'MASTER_STATUS', _pack_voltage(fx.MASTER_STATUS))
    monkeypatch.setattr(fx, 'SLAVE_STATUS', _pack_voltage(fx.SLAVE_STATUS))

    async def run():
        master = JkPbUartPassive('serial', name='JK_0', adapter='/dev/ttyUSB0', type_spec='0')
        slave = JkPbUartPassive('serial', name='JK_1', adapter='/dev/ttyUSB0', type_spec='1')
        for b in (master, slave):
            await b.connect()
        stream = fx.cycle() * 3
        # a live bus: frames keep coming while the sampler waits
        feeder = asyncio.get_running_loop().create_task(_slow_feed([master, slave], stream))
        out = []
        for _ in range(2):
            out.append((await master.fetch(), await slave.fetch()))
        await feeder
        return master, slave, out, await master.fetch_voltages(), await slave.fetch_voltages()

    async def _slow_feed(bms_list, stream):
        for i in range(0, len(stream), 300):
            _feed(bms_list, stream[i:i + 300], 300)
            await asyncio.sleep(0.001)

    master, slave, out, mv_master, mv_slave = asyncio.run(run())
    for m, s in out:
        assert m.voltage == pytest.approx(13.382)
        assert s.voltage == pytest.approx(13.334)
    assert mv_master[0] == 3335 and mv_slave[0] == 3287
    assert master.client.written == [] and slave.client.written == []  # listen-only


def test_never_writes_and_needs_no_device_info(serial_stub):
    from bmslib.models.jk_pb_uart_passive import JkPbUartPassive

    async def run():
        bms = JkPbUartPassive('serial', name='JK_1', adapter='/dev/ttyUSB0', type_spec='1')
        await bms.connect()
        di = await bms.fetch_device_info()
        with pytest.raises(NotImplementedError):
            await bms._request(0x1620)
        return bms, di

    bms, di = asyncio.run(run())
    assert di.mnf == 'JK' and di.name == 'JK_1'
    assert bms._has_float_charger is False
    assert bms.client.written == []


def test_timeout_names_the_polled_addresses(serial_stub, caplog):
    from bmslib.models.jk_pb_uart_passive import JkPbUartPassive
    from bmslib.test.test_jk_pb_bus import build_request

    async def run():
        bms = JkPbUartPassive('serial', name='JK_1', adapter='/dev/ttyUSB0', type_spec='1')
        bms.PASSIVE_TIMEOUT = 0.2
        await bms.connect()
        bms._notification_handler(None, build_request(5, 0x1620) + build_request(6, 0x1620))  # a master, no replies
        with pytest.raises(asyncio.TimeoutError):
            await bms.fetch()

    with caplog.at_level('WARNING'):
        asyncio.run(run())
    assert 'address(es) [5, 6]' in caplog.text and 'frames for none' in caplog.text
