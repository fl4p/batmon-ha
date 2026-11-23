"""
ANT protocol reference
----------------------

Renders doc/dev/ANT_PROTOCOL.md from ANT's own protocol definition: the JSON the vendor
app "ANT BMS V2" downloads at start (the app builds every frame from it), plus the
layout of the 0x11 status frame and its flag bits, which the app decodes in code
(sdk/api/custom_analyze/bms_0x11_1.0.0.ts in its app-service.js) and which are copied
into STATUS_0x11 / PROTECT_BITS / WARNING_BITS below.

    python3 tools/ant_protocol.py                 # fetch the current definition, write the doc
    python3 tools/ant_protocol.py --json FILE     # from a saved copy
    python3 tools/ant_protocol.py --save FILE     # also keep the fetched JSON

The list comes from POST https://api.antbms.com/admin-api/business/protocol/getFormatProtocol
(empty body, no login); the BMS protocol is the entry with code "mqtt_new".
"""

import argparse
import json
import pathlib
import re
import urllib.request

LIST_URL = 'https://api.antbms.com/admin-api/business/protocol/getFormatProtocol'
UA = 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Mobile Safari/537.36'
OUT = pathlib.Path(__file__).resolve().parents[1] / 'doc' / 'dev' / 'ANT_PROTOCOL.md'

# 0x11 status payload (after 7E A1 11 <addr> <len>), in order. (field, bytes, type, divisor, unit, meaning);
# bytes None: repeated, count from an earlier field. From the app's bms_0x11 parser.
STATUS_0x11 = [
    ('currentAuth', 1, 'u8', 1, '', 'permission level of this link'),
    ('batteryState', 1, 'u8', 1, '', 'battery state'),
    ('batteryTemperatureNum', 1, 'u8', 1, '', 'number of cell temperature probes (M)'),
    ('batteryStrandNum', 1, 'u8', 1, '', 'number of cells (N)'),
    ('protectFlagBit', 8, 'u64', 1, '', 'protection flags, PROTECT_BITS'),
    ('warningFlagBit', 8, 'u64', 1, '', 'warning / state flags, WARNING_BITS'),
    ('pushFlagBit', 8, 'u64', 1, '', 'push flags'),
    ('batteryItems', None, 'u16 x N', 1000, 'V', 'cell voltages; bits 13-15 of each: active balancer error state '
                                                 '(bsErrSt), masked off before scaling (& 0x1FFF)'),
    ('temperatureItems', None, 'i16 x M', 1, '°C', 'cell temperatures'),
    ('mosTemperature', 2, 'i16', 1, '°C', 'MOSFET temperature'),
    ('equilibriumTemperature', 2, 'i16', 1, '°C', 'balancer temperature'),
    ('totalVoltage', 2, 'u16', 100, 'V', 'pack voltage'),
    ('electricCurrent', 2, 'i16', 10, 'A', 'current'),
    ('soc', 2, 'u16', 1, '%', 'SoC'),
    ('soh', 2, 'u16', 1, '%', 'SoH'),
    ('dischargeMosState', 1, 'u8', 1, '', 'discharge MOSFET state'),
    ('chargeMosState', 1, 'u8', 1, '', 'charge MOSFET state'),
    ('equilibriumState', 1, 'u8', 1, '', 'balancer state'),
    ('bmsType', 1, 'u8', 1, '', 'BMS type (1: active balancer current in 0.01 A, else 0.1 A)'),
    ('batteryPhysicsCapacity', 4, 'u32', 1e6, 'Ah', 'physical capacity (register 0x00A2)'),
    ('batterySurplusCapacity', 4, 'u32', 1e6, 'Ah', 'remaining capacity'),
    ('totalLopCapacity', 4, 'u32', 1e3, 'Ah', 'total cycled capacity'),
    ('batteryPower', 4, 'i32', 1, 'W', 'power'),
    ('runningTime', 4, 'u32', 1, 'ms', 'running time (unit as the app labels it)'),
    ('equilibriumFlagBit', 4, 'u32', 1, '', 'active balancer: bits 24-31 cell index, 16-23 line resistance '
                                            '(/100), 8-15 current (i8, /100 or /10 by bmsType), 0-7 fault'),
    ('highestMonomerVoltage', 2, 'u16', 1000, 'V', 'highest cell voltage'),
    ('highestMonomerFlag', 2, 'u16', 1, '', 'highest cell number'),
    ('minimumMonomerVoltage', 2, 'u16', 1000, 'V', 'lowest cell voltage'),
    ('minimumMonomerFlag', 2, 'u16', 1, '', 'lowest cell number'),
    ('monomerVoltageDiff', 2, 'u16', 1000, 'V', 'cell voltage spread'),
    ('averageMonomerVoltage', 2, 'u16', 1000, 'V', 'mean cell voltage'),
    ('dsv', 2, 'i16', 10, 'V', 'DSV'),
    ('dv', 2, 'u16', 10, 'V', 'DV'),
    ('cv', 2, 'u16', 10, 'V', 'CV'),
    ('com', 2, 'u16', 1, '', 'COM'),
    ('batteryType', 2, 'u16', 1, '', 'battery (cell) type'),
    ('cumulativeDischargeCapacity', 4, 'u32', 1e3, 'Ah', 'total discharged'),
    ('cumulativeChargeCapacity', 4, 'u32', 1e3, 'Ah', 'total charged'),
    ('cumulativeDischargeTime', 4, 'u32', 1, 's', 'total discharge time'),
    ('cumulativeChargeTime', 4, 'u32', 1, 's', 'total charge time'),
    ('lastChargeTime', 4, 'u32', 1, 's', 'duration of this charge'),
    ('lastChargeInterval', 4, 'u32', 1, 's', 'time since the last charge'),
    ('chargeSurplusTime', 2, 'u16', 1, 'min', 'charge time remaining'),
    ('dischargeSurplusTime', 2, 'u16', 1, 'min', 'discharge time remaining'),
    ('highSpeedElectricCurrent', 2, 'i16', 10, 'A', 'fast-sampled current'),
    ('expirationDate', 2, 'u16', 1, 'h', 'usage period remaining'),
    ('collectionFault', 2, 'u16', 1, '', 'acquisition (AFE) fault'),
    ('chargerOutVoltage', 2, 'u16', 10, 'V', 'charger output voltage'),
    ('chargerOutCurrent', 2, 'u16', 10, 'A', 'charger output current'),
    ('chargerState', 2, 'u16', 1, '', 'charger state'),
]
# The parser stops at the end of the frame: firmware with a shorter frame lacks the later fields.

PROTECT_BITS = [  # protectFlagBit, bit n
    'cell type not set', 'cell overvoltage', 'cell overvoltage L2', 'pack overvoltage', 'cell undervoltage',
    'cell undervoltage L2', 'pack undervoltage', 'cell voltage spread', 'cell over-temperature charging',
    'cell over-temperature discharging', 'MOSFET over-temperature', 'cell under-temperature charging',
    'cell under-temperature discharging', 'charge overcurrent', 'discharge overcurrent', 'discharge overcurrent L2',
    'short circuit', 'discharge switch control 1', 'discharge switch control 2', 'discharge switch control 3',
    'discharge switch control 4', 'charge switch control 1', 'charge switch control 2', 'charge switch control 3',
    'charge switch control 4', 'balance wire disconnected', 'current abnormal', 'discharge MOSFET fault',
    'charge MOSFET fault', 'internal communication fault', 'precharge failed', 'BMS initialising', 'self-test 1',
    'self-test 2', 'charge communication check', 'GPS lost', 'anti-spark active', 'self-test 3',
    'firmware update required', 'relay precharge off', 'relay welded', 'discharge fuse fault', 'charge fuse fault',
    'module lost', 'usage period expired', 'pre-discharge MOSFET fault', 'precharge MOSFET fault', 'load lock on',
    'acquisition receive mode', '49', 'acquisition boot mode']

WARNING_BITS = [  # warningFlagBit, bit n
    'cell overvoltage', 'pack overvoltage', 'cell undervoltage', 'pack undervoltage', 'cell voltage spread',
    'cell hot charging', 'cell hot discharging', 'cell cold charging', 'cell cold discharging', 'MOSFET hot',
    'charge overcurrent', 'discharge overcurrent', 'SoC level 1', 'SoC level 2', 'cell check abnormal',
    'pack voltage check abnormal', 'precharge failed', 'battery full', 'charging', 'discharging',
    'CAN charger connected', 'RS485 charger connected', 'charger connected (current)', 'charge MOSFET on',
    'discharge MOSFET on', 'balancer on', 'standby', 'balance limit', 'balance spread', 'balance auto',
    'balancer over-temperature', 'voltage protection', 'temperature protection', 'system error', 'GPS lost',
    'balance check', 'heater on', 'forced output', 'Bluetooth off', 'relay precharging', 'MOSFET precharging',
    'charge relay on', 'discharge relay on', 'clock fault', 'charge forced on', 'debug mode', 'usage period expired',
    'usage period ending', 'anti-spark', 'low-voltage shutdown imminent', 'ANT charger connected',
    'balance waiting', 'test mode', 'internal communication unstable']

FUNC_EN = {  # the vendor's function names, translated
    '01': 'read status', '02': 'read BMS settings', '03': 'read system log', '04': 'read chip ID',
    '05': 'read protection counters', '07': 'read DTU settings', '0D': 'debug values', '22': 'write BMS settings',
    '23': 'password check', '26': 'write DTU settings', '51': 'BMS control', '53': 'DTU control',
    '56': 'display commands', '57': 'Bluetooth module / boot upgrade', '59': 'read active balancer settings',
    '70': 'extended functions', '11': 'status', '12': 'BMS settings', '13': 'system log', '14': 'chip ID',
    '15': 'protection counters', '16': 'DTU location', '17': 'DTU settings', '1D': 'debug values',
    '42': 'write BMS settings, reply', '43': 'password check, reply', '46': 'write DTU settings, reply',
    '61': 'BMS control, reply', '62': 'boot upgrade, reply', '63': 'DTU control, reply', '65': 'boot upgrade, reply',
    '66': 'display command, reply', '67': 'Bluetooth module, reply', '69': 'active balancer report',
    '80': 'extended functions, reply', '87': 'hardware update info', 'E0': 'traffic report', 'FE': 'traffic statistics',
    'FF': 'result',
}

# Reply code of a 0x51 command (0x61 frame, payload byte 0) and of a settings write
# (bleComApi.handleSetParamsResult / setParamsApi in the app)
COMMAND_RESULT = {1: 'done', 2: 'failed', 3: 'permission denied', 4: 'address out of range', 5: 'battery type error',
                  6: 'battery info error', 7: 'firmware mismatch'}


def fetch(save=None):
    req = urllib.request.Request(LIST_URL, data=b'{}', method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': UA})
    lst = json.load(urllib.request.urlopen(req, timeout=30))['data']
    entry = next(e for e in lst if e.get('code') == 'mqtt_new')
    req = urllib.request.Request(entry['downloadUrl'], headers={'User-Agent': UA})
    raw = urllib.request.urlopen(req, timeout=60).read()
    if save:
        pathlib.Path(save).write_bytes(raw)
    return json.loads(raw), entry['downloadUrl']


def _addr(hexs):
    """'22A20004{4}' -> (func, addr, len); None for another shape (0x56/0x57/0x59 carry a sub-command byte
    after the address: the template column has the frame)."""
    if not re.fullmatch(r'[0-9A-Fa-f]{8}(\{\d+\})?', hexs):
        return None
    return hexs[0:2], int(hexs[4:6] + hexs[2:4], 16), int(hexs[6:8], 16)


def _cell(x):
    return str(x).replace('|', '\\|').replace('\n', ' ') if x not in (None, '') else ''


def render(proto, source) -> str:
    ver = sorted(proto['protocols'])[-1]
    p = proto['protocols'][ver]
    out = [
        '# ANT BMS protocol (7E A1)',
        '',
        'Generated by `tools/ant_protocol.py` from ANT\'s own protocol definition, %s %s (protocol %s), '
        'the table the vendor app "ANT BMS V2" builds its frames from, and the app\'s decoder for the 0x11 '
        'status frame. Do not edit by hand: run the tool again.' % (proto.get('protocolCode'), source, ver),
        '',
        '## Framing',
        '',
        '| transport | head | tail | CRC |',
        '|---|---|---|---|',
    ]
    for c in proto.get('packetConfigs', []):
        out.append('| %s | `%s` | `%s` | %s |' % (c['type'], c['prefix'], c['suffix'] or '-',
                                                  'CRC16-Modbus, LE, over func..data' if c['needCrc'] else '-'))
    out += [
        '',
        'Frame: `7E A1 <func> <addr-lo> <addr-hi> <len> [<data>] <crc-lo> <crc-hi> AA 55`. The tables below give each',
        'request as func, address (16 bit) and length; `{n}` in the template is an n-byte value, little endian. A reply',
        'has func = request func + 0x10 (`02`->`12`, `22`->`42`, `23`->`43`, `51`->`61`). A value is the raw integer;',
        'divide by `range` for the unit.',
        '',
        '**Permission levels.** Every request has a `level`. A link starts low; func `0x23` (password check) raises',
        'it: `0x0162` level 4, `0x016A` level 5 (default password `123456789abc`), `0x0176` level 9 (root; the app',
        'obtains it from ANT\'s server). The status frame reports the level (`currentAuth`).',
        '',
        '**Settings** (func `0x22`) change RAM only: `0x51` command `saveAppParamters` (`0x0007`) stores them in flash,',
        '`resetBms` (`0x0009`) reboots and reloads flash.',
        '',
        '**Not reversible from batmon.** Among the `0x51` commands: `closedBms` (0x000B) switches the BMS off,',
        '`restoreFactorySettings` (0x000C) and `restoreFactory` (0x002A) reset every setting, `titaniumLithium` /',
        '`ternary` / `ironLithium` / `sodiumElectricity` / `nsmtParams` load a chemistry\'s voltage set,',
        '`batteryDisable` (0x0011) locks the pack. Send none of them to a pack that powers anything.',
        '',
        '**Result codes** of a `0x51` command (`0x61` reply, data byte 0): ' +
        ', '.join('%d %s' % kv for kv in COMMAND_RESULT.items()) + '.',
        '',
        '## Readings: status frame (func 0x01 -> 0x11)',
        '',
        'Request `7E A1 01 00 00 BE <crc> AA 55`. Reply payload, in order; offsets follow from the cell and probe',
        'counts. Short frames (older firmware) end early.',
        '',
        '| # | field | bytes | type | ÷ | unit | meaning |',
        '|---|---|---|---|---|---|---|',
    ]
    off = 0
    for f, n, t, div, unit, what in STATUS_0x11:
        pos = str(off) if off is not None else 'var'
        out.append('| %s | `%s` | %s | %s | %s | %s | %s |' % (pos, f, n or 'var', t, '%g' % div, unit, what))
        off = None if (off is None or n is None) else off + n
    out += ['', 'Protection flags (`protectFlagBit`):', '']
    out += ['%d: %s' % (i, b) + ('  ' if i < len(PROTECT_BITS) - 1 else '') for i, b in enumerate(PROTECT_BITS)]
    out += ['', 'Warning and state flags (`warningFlagBit`):', '']
    out += ['%d: %s' % (i, b) + ('  ' if i < len(WARNING_BITS) - 1 else '') for i, b in enumerate(WARNING_BITS)]

    names = {f: c.get('funcName', '') for f, c in p['sendCommands'].items()}
    out += ['', '## Requests by function', '']
    for f in sorted(p['sendCommands'], key=lambda x: int(x, 16)):
        c = p['sendCommands'][f]
        regs = sorted(c.get('regDetails', {}).values(), key=lambda r: (r.get('hex', ''), r.get('key', '')))
        out += ['### 0x%s %s (%s, %d)' % (f, FUNC_EN.get(f, ''), names[f], len(regs)), '',
                '| addr | len | template | key | 定义 (vendor name) | type | ÷ range | unit | level | reply |',
                '|---|---|---|---|---|---|---|---|---|---|']
        for r in regs:
            x = _addr(r['hex'])
            out.append('| %s | %s | `%s` | `%s` | %s | %s | %s | %s | %s | %s |' % (
                '0x%04X' % x[1] if x else '', x[2] if x else '', r['hex'], r.get('key', ''), _cell(r.get('define')), _cell(r.get('dataType')), _cell(r.get('range')),
                _cell(r.get('unit')), _cell(r.get('level')), _cell(r.get('ack'))))
        out.append('')

    bulk = p.get('bulkReadCommands') or {}
    if bulk:
        out += ['## Bulk reads', '', '| key | 定义 | template | addr | len | reply |', '|---|---|---|---|---|---|']
        for b in sorted(bulk.values(), key=lambda b: b['hex']):
            x = _addr(b['hex'])
            out.append('| `%s` | %s | `%s` | %s | %s | 0x%s |' % (b['key'], _cell(b.get('define')), b['hex'],
                                                               '0x%04X' % x[1] if x else '', x[2] if x else '',
                                                               b.get('ackFunc', '')))
        out.append('')

    out += ['## Replies by function', '',
            'Registers in a reply (table-driven ones; `custom` ones are decoded in the app\'s code).', '']
    for f in sorted(p['reportDefine'], key=lambda x: int(x, 16)):
        r = p['reportDefine'][f]
        det = r.get('details') or {}
        out.append('### 0x%s %s (%s)%s' % (f, FUNC_EN.get(f, ''), r.get('funcName', ''),
                                      ' (decoded in the app\'s code%s)' % (': ' + r['customCode'] if r.get('customCode') else '')
                                      if r.get('analysisType') == 'custom'
                                      else ''))
        out.append('')
        if not det:
            continue
        out += ['| reg | len | field | 定义 | type | ÷ range | unit |', '|---|---|---|---|---|---|---|']
        for d in sorted(det.values(), key=lambda d: int(d['reg'][2:4] + d['reg'][0:2], 16)):
            out.append('| 0x%s | %s | `%s` | %s | %s | %s | %s |' % (
                d['reg'][2:4] + d['reg'][0:2], _cell(d.get('len')), d.get('fieldName', ''), _cell(d.get('define')),
                _cell(d.get('dataType')), _cell(d.get('range')), _cell(d.get('unit'))))
        out.append('')
    return '\n'.join(out).rstrip() + '\n'


def main():
    ap = argparse.ArgumentParser(description='Render doc/dev/ANT_PROTOCOL.md from ANT\'s protocol definition')
    ap.add_argument('--json', help='a saved protocol JSON instead of fetching it')
    ap.add_argument('--save', help='write the fetched JSON here')
    ap.add_argument('--out', default=str(OUT))
    args = ap.parse_args()
    if args.json:
        proto, source = json.load(open(args.json)), pathlib.Path(args.json).name
    else:
        proto, source = fetch(args.save)
    pathlib.Path(args.out).write_text(render(proto, source))
    print('wrote', args.out)


if __name__ == '__main__':
    main()
