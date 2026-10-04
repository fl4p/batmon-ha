"""`type: auto`: strict reply validators, probe safety, and the detection run (#416)."""

import asyncio
from types import SimpleNamespace

import pytest

import bmslib.auto_detect as ad
from bmslib.auto_detect import AutoDetectBt, _build_probes, detect, resolve_auto_devices
from bmslib.test.data import ant_fixtures, daly2_fixtures, jbd_fixtures


def _uuid(short: int) -> str:
    return '0000%04x-0000-1000-8000-00805f9b34fb' % short


def _jbd(cmd: int, payload: bytes) -> bytes:
    body = bytes([0x00, len(payload)]) + payload
    ck = (0x10000 - sum(body)) & 0xFFFF
    return bytes([0xDD, cmd]) + body + ck.to_bytes(2, 'big') + b'\x77'


def _jk(frame_type: int) -> bytes:
    f = bytearray(300)
    f[0:4] = b'\x55\xaa\xeb\x90'
    f[4] = frame_type
    f[6:16] = b'JK_B2A8S20'
    f[-1] = sum(f[:-1]) & 0xFF
    return bytes(f)


# Real replies where we have them: #416's snoop log for Daly A5 (4S LiFePO4 at rest)
DALY_90 = bytes.fromhex('a501900800850000753001e44d')
DALY_94 = bytes.fromhex('a501940804020000000000438b')
REPLIES = {
    'daly': DALY_90 + DALY_94,
    'daly2': daly2_fixtures.AIOBMSBLE_4S['raw'],
    'jbd': jbd_fixtures.SYSSI_3CELL['raw'] + _jbd(0x04, bytes.fromhex('0cfe0cff0cfe0cff')),
    'jk': _jk(0x03),
    'ant': bytes(ant_fixtures.INLINE_8S['raw']),
}


def _accepts(probe, buf: bytes) -> bool:
    return all(valid(buf) for _frame, valid in probe.steps)


def test_each_reply_confirms_exactly_its_own_type():
    for typ, reply in REPLIES.items():
        hits = {p.type for p in _build_probes() if _accepts(p, reply)}
        assert hits == {typ}, (typ, hits)


def test_split_and_prefixed_replies_still_validate():
    for typ, reply in REPLIES.items():
        probe = next(p for p in _build_probes() if p.type == typ)
        buf = b'\x00junk' + reply
        assert _accepts(probe, buf), typ
        assert not _accepts(probe, buf[:-1]), typ  # truncated: not yet


def test_requests_are_never_accepted_as_replies():
    # an echo of what we sent must not confirm anything (Daly's own callback accepts its 0x90 echo)
    probes = _build_probes()
    sent = b''.join(frame for p in probes for frame, _ in p.steps)
    assert not [p.type for p in probes if _accepts(p, sent)]


def test_daly2_rejects_foreign_address_and_wrong_length():
    probe = next(p for p in _build_probes() if p.type == 'daly2')
    assert not _accepts(probe, bytes.fromhex('300304000100020af1'))  # CRC-valid, address 0x30
    short = bytearray(daly2_fixtures.AIOBMSBLE_4S['raw'])
    assert not _accepts(probe, bytes(short[:20]))


def test_checksum_matters():
    for typ, reply in REPLIES.items():
        probe = next(p for p in _build_probes() if p.type == typ)
        bad = bytearray(reply)
        bad[-3 if typ in ('jbd', 'ant') else -1] ^= 0x01
        assert not _accepts(probe, bytes(bad)), typ


def test_no_probe_targets_a_sig_characteristic():
    from bmslib.models.snoop import _is_sig_characteristic
    for p in _build_probes():
        assert not _is_sig_characteristic(p.tx) and not _is_sig_characteristic(p.rx), p


# ---------------------------------------------------------------- detection run

def _char(short, *props):
    return SimpleNamespace(uuid=_uuid(short), properties=list(props), handle=short)


# GATT of the #416 module as an ESPHome proxy shows it
GATT_416 = [
    SimpleNamespace(characteristics=[_char(0x2A00, 'read', 'write'), _char(0x2A01, 'read', 'write')]),
    SimpleNamespace(characteristics=[_char(0xFFF1, 'read', 'notify'),
                                     _char(0xFFF2, 'read', 'write-without-response', 'write'),
                                     _char(0xFFF3, 'read', 'write-without-response', 'write')]),
]


class _FakeDevice:
    """A peripheral answering one protocol: `respond(frame) -> reply bytes or None`."""

    def __init__(self, services, respond, connect_ok=True):
        self.services = services
        self._respond = respond
        self._cb = None
        self.connect_ok = connect_ok
        self.writes = []
        self.is_connected = False

    async def connect(self, timeout=20):
        if not self.connect_ok:
            raise TimeoutError('no device')
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, char, cb, **kw):
        self._cb = cb

    async def stop_notify(self, char):
        self._cb = None

    async def write_gatt_char(self, char, data, response=None):
        self.writes.append((char.uuid, bytes(data)))
        reply = self._respond(bytes(data))
        if reply and self._cb:
            for i in range(0, len(reply), 20):  # MTU-sized notifications
                self._cb(None, bytearray(reply[i:i + 20]))


def _factory(device):
    def make(address, **kw):
        bms = AutoDetectBt('test_jbd', **kw)  # test_ address: no real BLE client is created
        bms.client = device
        return bms

    return make


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(ad, 'REPLY_TIMEOUT', 0.2)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(ad.asyncio, 'sleep', lambda s: real_sleep(min(s, 0.01)))


def _daly_a5(frame):
    if frame[0] == 0xA5:
        return {0x90: DALY_90, 0x94: DALY_94}.get(frame[2])


def test_416_module_detected_as_daly_without_touching_gap():
    dev = _FakeDevice(GATT_416, _daly_a5)
    res = asyncio.run(detect('D6:C1:4E:10:00:D1', 'DL-D6C14E1000D1', bms_factory=_factory(dev)))
    assert res.type == 'daly' and res.connected
    assert {uuid for uuid, _ in dev.writes} == {_uuid(0xFFF2)}
    assert not dev.is_connected  # probe connection released


def test_dl_advert_tries_daly2_first_then_daly(monkeypatch):
    monkeypatch.setattr(ad, 'advert_hints', lambda adv, addr: (['daly2', 'daly'], []))
    dev = _FakeDevice(GATT_416, _daly_a5)
    res = asyncio.run(detect('D6:C1:4E:10:00:D1', 'x', adv=object(), bms_factory=_factory(dev)))
    assert res.type == 'daly'
    assert dev.writes[0][1][:2] == b'\xd2\x03'  # Modbus first, ignored, then A5
    assert res.tried == ['daly2 (D2 Modbus) on fff1/fff2', 'daly (A5) on fff1/fff2']


def test_daly2_device_detected():
    reply = daly2_fixtures.AIOBMSBLE_4S['raw']
    dev = _FakeDevice(GATT_416, lambda f: reply if f[:2] == b'\xd2\x03' else None)
    res = asyncio.run(detect('aa', 'x', bms_factory=_factory(dev)))
    assert res.type == 'daly2'


def test_jbd_layout_tries_jbd_before_daly():
    gatt = [SimpleNamespace(characteristics=[_char(0xFF01, 'notify'), _char(0xFF02, 'write')])]
    dev = _FakeDevice(gatt, lambda f: REPLIES['jbd'] if f[0] == 0xDD else None)
    res = asyncio.run(detect('aa', 'x', bms_factory=_factory(dev)))
    assert res.type == 'jbd'
    assert all(data[0] == 0xDD for _, data in dev.writes)  # no foreign frame sent


def test_silent_device_is_not_guessed():
    dev = _FakeDevice(GATT_416, lambda f: None)
    res = asyncio.run(detect('aa', 'x', bms_factory=_factory(dev)))
    assert res.type is None and res.connected
    assert 'jbd' in res.skipped and res.tried


def test_unreachable_device_is_unverified_not_negative():
    dev = _FakeDevice(GATT_416, _daly_a5, connect_ok=False)
    res = asyncio.run(detect('aa', 'x', bms_factory=_factory(dev)))
    assert res.type is None and not res.connected and res.error and not res.tried


def test_resolve_rewrites_type_and_skips_failures(monkeypatch):
    async def fake_detect(addr, **kw):
        return ad.Result(type='daly', probe='daly (A5) on fff1/fff2', connected=True) if addr == 'AA:01' \
            else ad.Result(connected=False, error='cannot connect')

    monkeypatch.setattr(ad, 'detect', fake_detect)
    devices = [dict(address='AA:01', type='auto', alias='a'),
               dict(address='AA:02', type='Auto', alias='b'),
               dict(address='serial', type='auto', alias='c', adapter='/dev/ttyUSB0'),
               dict(address='AA:03', type='jbd', alias='d')]
    out = asyncio.run(resolve_auto_devices(devices, {}))
    assert out[0]['type'] == 'daly'
    assert out[1]['address'] == '#AA:02'
    assert out[2]['address'] == '#serial'
    assert out[3] is devices[3]


def test_dl_advertisement_hints_daly_protocols():
    pytest.importorskip('aiobmsble')
    from bleak.backends.scanner import AdvertisementData
    adv = AdvertisementData(local_name='DL-D6C14E1000D1', manufacturer_data={}, service_data={},
                            service_uuids=[_uuid(0xFFF0)], tx_power=None, rssi=-55, platform_data=())
    first, _others = ad.advert_hints(adv, 'D6:C1:4E:10:00:D1')
    assert first[:2] == ['daly2', 'daly']
    assert ad.advert_hints(None, 'aa') == ([], [])


def test_daly2_rejects_full_length_reply_from_another_address():
    from bmslib.models.daly2 import _modbus_crc16
    body = bytes([0x30]) + daly2_fixtures.AIOBMSBLE_4S['raw'][1:-2]
    crc = _modbus_crc16(body)
    frame = body + bytes([crc & 0xFF, crc >> 8])
    probe = next(p for p in _build_probes() if p.type == 'daly2')
    assert not _accepts(probe, frame)


def test_reply_that_arrived_before_the_request_does_not_count():
    # answers 0x90 with both replies, never answers 0x94 itself
    dev = _FakeDevice(GATT_416, lambda f: DALY_90 + DALY_94 if f[0] == 0xA5 and f[2] == 0x90 else None)
    res = asyncio.run(detect('aa', 'x', bms_factory=_factory(dev)))
    assert res.type is None
