"""The JK web app decodes the same bytes as the Python driver, or CI fails.

bmslib/gui/web/jk.html carries a hand port of bmslib/models/jikong.py so an
Android phone can talk to a JK over Web Bluetooth with no add-on in the loop.
Two implementations of one wire format drift silently -- a firmware offset fixed
on one side and not the other produces plausible wrong numbers, not an error.

So both are run against the SAME captured frames (the #365 fixtures) and
compared field by field. This is the shared-golden-fixture contract from
plans/gui-and-mobile-app.md, made executable.

Skipped when node is unavailable; it is a JS parity check, not a Python test.
"""
import json
import math
import pathlib
import shutil
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
HTML = REPO / 'bmslib' / 'gui' / 'web' / 'jk.html'
DATA = REPO / 'bmslib' / 'test' / 'data'

pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')


def _python_expected(status: bytes, settings: bytes) -> dict:
    from bmslib.models.jikong import JKBt
    bms = JKBt('test_jk', name='parity')
    bms._resp_table[0x01] = (bytearray(settings), time.time())
    bms._resp_table[0x02] = (bytearray(status), time.time())
    bms.is_new_11fw_32s = True
    s = bms._decode_sample(bytearray(status), t_buf=time.time(), has_float_charger=False)
    return dict(
        voltage=s.voltage, current=s.current, soc=s.soc, charge=s.charge,
        capacity=s.capacity, soh=s.soh, aged_capacity=s.aged_capacity,
        num_cycles=s.num_cycles, throughput=s.total_charge_throughput,
        mos=s.mos_temperature, balance=s.balance_current,
        temps=[t for t in (s.temperatures or []) if not math.isnan(t)],
        switches=s.switches, num_cells=settings[114],
    )


JS_HARNESS = r"""
import fs from 'fs';
const html = fs.readFileSync(process.argv[2], 'utf8');
const src = html.slice(html.indexOf('const HEADER'), html.indexOf('// ---------- BLE'));
fs.writeFileSync(process.argv[3], src +
  '\nexport {decodeSample, cellVoltages, feedFrames, jkCommand};\n');
const P = await import('file://' + process.argv[3]);
const status = new Uint8Array(fs.readFileSync(process.argv[4]));
const settings = new Uint8Array(fs.readFileSync(process.argv[5]));
const n = Number(process.argv[6]);
const s = P.decodeSample(status, settings);

// reassemble the frame from 20-byte notify packets, as a real BLE link delivers it
const st = {buf: new Uint8Array(0)};
let frames = [];
for (let i = 0; i < status.length; i += 20)
  frames = frames.concat(P.feedFrames(st, status.slice(i, i + 20)));

console.log(JSON.stringify({
  voltage: s.voltage, current: s.current, soc: s.soc, charge: s.charge,
  capacity: s.capacity, soh: s.soh, aged_capacity: s.agedCapacity,
  num_cycles: s.cycles, throughput: s.throughput, mos: s.mosTemp,
  balance: s.balanceCurrent, temps: s.temps, switches: s.switches,
  cells: P.cellVoltages(status, n),
  frames_reassembled: frames.length,
  cmd96: Buffer.from(P.jkCommand(0x96)).toString('hex'),
  cmd97: Buffer.from(P.jkCommand(0x97)).toString('hex'),
}));
"""


@pytest.fixture(scope='module')
def js(tmp_path_factory):
    status = (DATA / 'jk_issue365_status.bin').read_bytes()
    settings = (DATA / 'jk_issue365_settings.bin').read_bytes()
    d = tmp_path_factory.mktemp('jkparity')
    harness = d / 'h.mjs'
    harness.write_text(JS_HARNESS)
    r = subprocess.run(
        ['node', str(harness), str(HTML), str(d / 'proto.mjs'),
         str(DATA / 'jk_issue365_status.bin'), str(DATA / 'jk_issue365_settings.bin'),
         str(settings[114])],
        capture_output=True, text=True)
    assert r.returncode == 0, "node harness failed:\n%s" % r.stderr
    return json.loads(r.stdout), _python_expected(status, settings)


@pytest.mark.parametrize('field', [
    'voltage', 'current', 'soc', 'charge', 'capacity', 'soh', 'aged_capacity',
    'num_cycles', 'throughput', 'mos', 'balance',
])
def test_scalar_fields_match_python(js, field):
    got, exp = js
    assert got[field] == pytest.approx(exp[field], rel=1e-9), field


def test_temps_and_switches_match_python(js):
    got, exp = js
    assert got['temps'] == pytest.approx(exp['temps'])
    assert got['switches'] == exp['switches']


def test_cell_voltages_match_python(js):
    got, exp = js
    status = (DATA / 'jk_issue365_status.bin').read_bytes()
    n = exp['num_cells']
    py = [int.from_bytes(status[6 + i * 2:8 + i * 2], 'little') for i in range(n)]
    assert got['cells'] == py


def test_framing_reassembles_from_mtu_chunks(js):
    """A JK notify packet does not respect frame boundaries."""
    got, _ = js
    assert got['frames_reassembled'] == 1


def test_command_frames_match_python(js):
    from bmslib.models.jikong import _jk_command
    got, _ = js
    assert got['cmd96'] == _jk_command(0x96, []).hex()
    assert got['cmd97'] == _jk_command(0x97, []).hex()
