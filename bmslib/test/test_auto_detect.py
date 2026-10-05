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
        bms._connect_with_scanner = device.connect  # never start a real scanner in tests
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
    async def fake_detect(addr, res=None, **kw):
        if addr == 'AA:00:00:00:00:01':
            res.type, res.probe, res.connected = 'daly', 'daly (A5) on fff1/fff2', True
        else:
            res.error = 'cannot connect'
        return res

    monkeypatch.setattr(ad, 'detect', fake_detect)
    devices = [dict(address='AA:00:00:00:00:01', type='auto', alias='a'),
               dict(address='aa:00:00:00:00:02', type='Auto', alias='b'),
               dict(address='serial', type='auto', alias='c', adapter='/dev/ttyUSB0'),
               dict(address='AA:03', type='jbd', alias='d')]
    out, unresolved = asyncio.run(resolve_auto_devices(devices, {}))
    assert out[0]['type'] == 'daly'
    assert out[1]['address'] == '#aa:00:00:00:00:02'
    assert out[2]['address'] == '#serial'
    assert out[3] is devices[3]
    # a group naming the failed device by alias or by MAC (any case) finds it here
    from bmslib.group import resolve_member_ref
    assert resolve_member_ref(unresolved, 'b') == 'b'
    assert resolve_member_ref(unresolved, ' AA:00:00:00:00:02') == 'b'
    assert 'serial' not in unresolved  # a wired device is only known by its alias
    assert resolve_member_ref(unresolved, 'a') is None


def test_pair_only_marks_auto_unresolved_without_probing(monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError('must not probe in pair-only')

    monkeypatch.setattr(ad, 'detect', boom)
    out, unresolved = asyncio.run(resolve_auto_devices([dict(address='AA:01', type='auto', alias='a')], {},
                                                       detect_now=False))
    assert out[0]['address'] == '#AA:01' and 'a' in unresolved


def test_name_address_is_resolved_to_the_mac(monkeypatch):
    seen = {}

    async def fake_detect(addr, adv=None, res=None, **kw):
        seen['addr'], seen['adv'] = addr, adv
        res.type, res.connected = 'daly', True
        return res

    monkeypatch.setattr(ad, 'detect', fake_detect)
    discovered = [SimpleNamespace(address='D6:C1:4E:10:00:D1', name='DL-D6C14E1000D1')]
    adverts = {'D6:C1:4E:10:00:D1': 'ADV'}
    out, _ = asyncio.run(resolve_auto_devices([dict(address='DL-D6C14E1000D1', type='auto')], adverts, discovered))
    assert seen == dict(addr='D6:C1:4E:10:00:D1', adv='ADV')
    assert out[0] == dict(address='DL-D6C14E1000D1', type='daly')  # construct_bms resolves the name itself


def test_timeout_keeps_what_was_learned(monkeypatch, caplog):
    async def slow_detect(addr, res=None, **kw):
        res.connected = True
        res.tried.append('daly (A5) on fff1/fff2')
        await asyncio.Event().wait()  # hangs (asyncio.sleep is shortened by _fast)

    monkeypatch.setattr(ad, 'detect', slow_detect)
    monkeypatch.setattr(ad, 'DETECT_TIMEOUT', 0.05)
    caplog.set_level('INFO')
    out, unresolved = asyncio.run(resolve_auto_devices([dict(address='AA:01', type='auto', alias='a')], {}))
    assert out[0]['address'] == '#AA:01' and 'a' in unresolved
    msg = caplog.text
    assert 'never reached' not in msg and 'daly (A5) on fff1/fff2' in msg and 'timed out' in msg


def test_stuck_unsubscribe_does_not_keep_the_link(monkeypatch):
    # the deadline fires while waiting for a reply; teardown then meets an
    # unsubscribe that never returns. It must be bounded on its own, since nothing
    # cancels it a second time.
    monkeypatch.setattr(ad, 'TEARDOWN_TIMEOUT', 0.05)
    monkeypatch.setattr(ad, 'REPLY_TIMEOUT', 30)

    class Stuck(_FakeDevice):
        async def stop_notify(self, char):
            await asyncio.Event().wait()  # never returns

    dev = Stuck(GATT_416, lambda f: None)

    async def run():
        t0 = asyncio.get_running_loop().time()
        try:
            # outer watchdog: only reached if the inner deadline can't finish
            await asyncio.wait_for(asyncio.wait_for(detect('aa', 'x', bms_factory=_factory(dev)), 0.3), 3)
        except asyncio.TimeoutError:
            pass
        return asyncio.get_running_loop().time() - t0

    assert asyncio.run(run()) < 1.5
    assert not dev.is_connected


def test_falls_back_to_connecting_with_scanner():
    dev = _FakeDevice(GATT_416, _daly_a5, connect_ok=False)

    def make(address, **kw):
        bms = _factory(dev)(address, **kw)

        async def with_scanner(timeout=20):
            dev.is_connected = True

        bms._connect_with_scanner = with_scanner
        return bms

    res = asyncio.run(detect('aa', 'x', bms_factory=make))
    assert res.type == 'daly'


def test_waits_for_late_service_discovery():
    class Late(_FakeDevice):
        calls = 0

        @property
        def services(self):
            Late.calls += 1
            return GATT_416 if Late.calls > 2 else []

        @services.setter
        def services(self, v):
            pass

    dev = Late(GATT_416, _daly_a5)
    res = asyncio.run(detect('aa', 'x', bms_factory=_factory(dev)))
    assert res.type == 'daly'


def test_structurally_empty_replies_are_rejected():
    probes = {p.type: p for p in _build_probes()}
    # JBD: checksum-valid, but no basic info / no cells to decode
    assert not _accepts(probes['jbd'], _jbd(0x03, b'') + _jbd(0x04, b''))
    # ANT: CRC-valid status frame without a status payload
    assert not _accepts(probes['ant'], bytes.fromhex('7ea1110000009ce5aa55'))
    # JK: a checksum-colliding 0x03 header in front of a real status frame
    status = bytearray(_jk(0x02))
    fake = bytearray(b'\x55\xaa\xeb\x90\x03') + status
    fake = fake[:300]
    fake[-1] = sum(fake[:-1]) & 0xFF
    assert not _accepts(probes['jk'], bytes(fake) + bytes(status)[len(fake) - 5:])


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


def test_module_answering_only_host_0x40_is_daly():
    dev = _FakeDevice(GATT_416, lambda f: _daly_a5(f) if f[1] == 0x40 else None)
    res = asyncio.run(detect('aa', 'x', bms_factory=_factory(dev)))
    assert res.type == 'daly' and res.probe == 'daly (A5, host 0x40) on fff1/fff2'
