"""tools/jk_dump_settings.py: settings-frame sanity check and read-only guarantee."""

import pathlib
import sys

import pytest

from bmslib.models.dummy import JKDummy
from bmslib.models.jikong import JKBt, calc_crc
from tools import jk_dump_settings
from tools.jk_dump_settings import check_settings_frame

TOOL_SRC = pathlib.Path(__file__).resolve().parents[2] / 'tools' / 'jk_dump_settings.py'


def _valid_frame() -> bytes:
    # JKDummy's 0x01 message is the 300-byte frame followed by a 20-byte command echo
    frame = bytes(JKDummy(is_new_11x=True).MSGS[0][:300])
    assert len(frame) == 300 and calc_crc(frame[:-1]) == frame[-1]
    return frame


def _with_crc(body: bytes) -> bytes:
    return body + bytes([calc_crc(body)])


def test_valid_frame_passes():
    check_settings_frame(_valid_frame())


def test_wrong_header_rejected():
    f = _valid_frame()
    bad = _with_crc(bytes([0x55, 0xAA, 0xEB, 0x90, 0x02]) + f[5:299])  # 0x02 = cell data, not settings
    with pytest.raises(ValueError, match='header'):
        check_settings_frame(bad)


@pytest.mark.parametrize('n', [0, 299, 301, 320])
def test_wrong_length_rejected(n):
    f = _valid_frame()
    body = (f[:299] + bytes(n))[:max(n - 1, 0)]
    bad = _with_crc(body) if n else b''
    assert len(bad) == n
    with pytest.raises(ValueError, match='length'):
        check_settings_frame(bad)


def test_bad_crc_rejected():
    f = bytearray(_valid_frame())
    f[-1] = (f[-1] + 1) & 0xFF
    with pytest.raises(ValueError, match='CRC'):
        check_settings_frame(bytes(f))


class _FakeLink:
    """Stand-in for JKBt.connect/disconnect: no Bluetooth, records the calls."""

    def __init__(self, frame=None, connect_error=None):
        self.frame, self.connect_error = frame, connect_error
        self.connects = self.disconnects = 0

    def install(self, monkeypatch):
        link = self

        async def connect(bms, timeout=20):
            link.connects += 1
            if link.connect_error:
                raise link.connect_error
            bms._resp_table[0x01] = bytearray(link.frame), 0.0

        async def disconnect(bms):
            link.disconnects += 1

        monkeypatch.setattr(JKBt, 'connect', connect)
        monkeypatch.setattr(JKBt, 'disconnect', disconnect)
        return self


def _run_main(monkeypatch, out):
    monkeypatch.setattr(sys, 'argv', ['jk_dump_settings.py', '00:11:22:33:44:55', str(out)])
    jk_dump_settings.main()


def test_main_writes_frame_and_disconnects(monkeypatch, tmp_path, capsys):
    link = _FakeLink(frame=_valid_frame()).install(monkeypatch)
    out = tmp_path / 'jk.bin'
    _run_main(monkeypatch, out)
    assert out.read_bytes() == _valid_frame()
    assert (link.connects, link.disconnects) == (1, 1)
    assert 'OK 00:11:22:33:44:55: 300 bytes' in capsys.readouterr().out


def test_main_disconnects_and_writes_nothing_on_bad_frame(monkeypatch, tmp_path):
    bad = bytearray(_valid_frame())
    bad[-1] ^= 0xFF
    link = _FakeLink(frame=bad).install(monkeypatch)
    out = tmp_path / 'jk.bin'
    with pytest.raises(SystemExit, match='CRC'):
        _run_main(monkeypatch, out)
    assert not out.exists()
    assert link.disconnects == 1


def test_main_disconnects_when_connect_fails(monkeypatch, tmp_path):
    link = _FakeLink(connect_error=RuntimeError('settings frame timeout')).install(monkeypatch)
    out = tmp_path / 'jk.bin'
    with pytest.raises(SystemExit, match='settings frame timeout'):
        _run_main(monkeypatch, out)
    assert not out.exists()
    assert link.disconnects == 1  # a half-open link must not block batmon


def test_import_does_not_touch_sys_path():
    import importlib
    before = list(sys.path)
    importlib.reload(jk_dump_settings)
    assert sys.path == before


def test_tool_never_writes_to_bms():
    src = TOOL_SRC.read_text()
    for forbidden in ('_write', 'set_switch', 'set_soc'):
        assert forbidden not in src, forbidden
