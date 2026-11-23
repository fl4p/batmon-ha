"""
Seplos protocol reference
-------------------------

Renders doc/dev/SEPLOS_PROTOCOL.md from the Seplos V3 vendor app "Octopus BMS" (the Android
app Seplos links from its BMS 3.0 Bluetooth install guide). The app is a DCloud uni-app; its
whole protocol lives in assets/apps/*/www/app-service.js as data the code builds frames from:

  * one read object per register block: `readCommand` (Modbus request as hex text),
    optional `writeCommand` / `writeAllStruct`, and the reply layout as a C-like `struct {...}`
    template that the app decodes big endian;
  * label tables `[[key, english, chinese, fmt-or-group], ...]` per block, which give every
    register its name and scaling (`fmt`, see FMT below) and every coil bit its alarm group.

This script reads those statically (regular expressions over the bundle, nothing is executed)
and adds what the app does in code, copied by hand into NOTES / FMT below.

    python3 -I tools/seplos_protocol.py --apk Octopus_BMS_1.0.0.apk
    python3 -I tools/seplos_protocol.py --js app-service.js

The APK: https://www.pgyer.com/be91fs ("Octopus BMS" 1.0.0 build 6, the link on
https://seplos.com/how-to-install-seplos-3-0-bms-bluetooth-app-on-android-and-ios-operating-systems.html).
Treat it as untrusted data: this script only reads the zip.
"""

import argparse
import hashlib
import json
import pathlib
import re
import zipfile

OUT = pathlib.Path(__file__).resolve().parents[1] / 'doc' / 'dev' / 'SEPLOS_PROTOCOL.md'

# The app's value formatter (function Yn in app-service.js): fmt -> (divisor, unit). The app's
# own mistakes are kept (and flagged in the doc): '1℃', 's' and 'mΩ' divide by 10, 'A', 'mv'
# and 'H' are plain integers.
FMT = {
    '': (1, ''), None: (1, ''),
    'A': (1, 'A'), 'H': (1, 'h'), 'Min': (1, 'min'), 'us': (1, 'µs'), 'ms': (1, 'ms'),
    'mv': (1, 'mV'), 'times': (1, ''), 'Cell': (1, 'cell #'),
    '0.1K': ('K', '°C'),
    '10AH': (0.1, 'Ah'),
    '10mA': (100, 'A'), '10mV': (100, 'V'), '10mAH': (100, 'Ah'),
    '1mV': (1000, 'V'),
    '100mA': (10, 'A'), '0.10%': (10, '%'),
    'mΩ': (10, 'mΩ'), '0.1%': (10, '%'), '1℃': (10, '°C'),
    's': (10, 's'), '0.1s': (10, 's'),
}

# Which protocol object is which product, from the app's own selection code (function So:
# advertised service UUID + local name) and its loader (jo.load: "bms" | "bmsx04" | "abu" | "bmswifi").
VARIANT_DOC = {
    'bms': 'Seplos BMS 3.0 (10E/16E-style V3 boards) through the Seplos BLE module; selected when the '
           'first advertised service is FFF0 and the local name does not start with `ABU`.',
    'bmsx04': 'Same BMS behind a second BLE module type (service 00002760-08C2-11E1-9073-0E8AC72E1001); '
              'pack data only, no EMS / inverter blocks.',
    'abu': 'Seplos "ABU" product (local name starts with `ABU`, service FFF0), Modbus device address 0xE0, '
           'its own smaller register map.',
    'bmswifi': 'Seplos BMS 3.0 through the WiFi/BLE combo module (advertised service 1804, data on 1900); '
               'same register map as `bms`.',
}

BLOCK_DOC = {
    'pack_info_a': 'pack summary (PIA)', 'pack_info_b': 'cells and temperatures (PIB)',
    'pack_info_c': 'pack alarm / state bits (PIC)', 'pcs_control_info': 'inverter protocol selection',
    'system_function': 'function switches (coils)', 'system_parameter': 'settings (protection thresholds, capacity, inverter requests)',
    'system_parameter_a': 'settings, first half', 'system_parameter_b': 'settings, second half',
    'ems_info_a': 'system (all packs) summary (EIA)', 'ems_info_b': 'system extremes (EIB)',
    'ems_info_c': 'system alarm / state bits (EIC)', 'version_info': 'names, firmware, serial numbers',
}

# label table to use for a block (the halves share the full parameter table)
LABELS_FOR = {'system_parameter_a': 'system_parameter', 'system_parameter_b': 'system_parameter'}

TYPES = {'u8': 1, 'i8': 1, 'u16': 2, 'i16': 2, 'u32': 4, 'i32': 4, 'char': 1, 'uchar': 1}


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(hexs: str) -> str:
    """The app's $n(): hex text -> bytes, CRC16-Modbus (LE) appended unless already there."""
    b = bytes.fromhex(hexs)
    c = crc16_modbus(b[:-2]) if len(b) > 2 else None
    if c is not None and b[-2:] == bytes([c & 0xFF, c >> 8]):
        return b.hex(' ').upper()
    c = crc16_modbus(b)
    return (b + bytes([c & 0xFF, c >> 8])).hex(' ').upper()


def load_js(apk=None, js=None):
    if js:
        return pathlib.Path(js).read_text('utf-8'), dict(file=js)
    raw = pathlib.Path(apk).read_bytes()
    with zipfile.ZipFile(apk) as z:
        name = next(n for n in z.namelist() if re.match(r'assets/apps/[^/]+/www/app-service\.js$', n))
        manifest = json.loads(z.read(name.replace('app-service.js', 'manifest.json')))
        text = z.read(name).decode('utf-8')
    return text, dict(file=pathlib.Path(apk).name, sha256=hashlib.sha256(raw).hexdigest(),
                      app=manifest.get('name'), version=manifest.get('version', {}).get('name'),
                      code=manifest.get('version', {}).get('code'), uni_id=manifest.get('id'))


def _def(src, name, before):
    """Position just after `name=` of the last definition of `name` before `before`."""
    pat = re.compile(r'(?<![\w$.])' + re.escape(name) + r'=(?!=)')
    pos = None
    for m in pat.finditer(src, 0, before):
        pos = m.end()
    if pos is None:
        raise ValueError('definition of %s not found' % name)
    return pos


def _bracket(src, start):
    """src[start] is '[' or '{': return the matching literal."""
    open_, close = src[start], {'[': ']', '{': '}'}[src[start]]
    depth, i, q = 0, start, None
    while True:
        ch = src[i]
        if q:
            if ch == '\\':
                i += 1
            elif ch == q:
                q = None
        elif ch in '"\'`':
            q = ch
        elif ch == open_:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def parse_struct(tmpl):
    fields = []
    for t, name, n in re.findall(r'\b(u8|i8|u16|i16|u32|i32|char|uchar)\s+(\w+)(?:\[(\d+)\])?\s*;', tmpl):
        fields.append((t, name, int(n) if n else None))
    return fields


def parse(src):
    variants = {}
    loader = re.search(r'"bms"==\(l=l\.toLowerCase\(\)\)\?this\._instance=([\w$]+):"bmsx04"==l\?this\._instance=([\w$]+)'
                       r':"abu"==l\?this\._instance=([\w$]+):"bmswifi"==l&&\(this\._instance=([\w$]+)\)', src)
    objs = dict(zip(loader.groups(), ('bms', 'bmsx04', 'abu', 'bmswifi')))
    for m in re.finditer(r'([\w$]+)=\{serviceId:"([^"]+)",serviceIdReadAndNotify:"([^"]+)",serviceIdWrite:"([^"]+)",'
                         r'writeChunkSize:(\d+),read:([\w$]+),write:([\w$]+),parse:([\w$]+)', src):
        var, svc, rx, tx, chunk, readfn = m.group(1, 2, 3, 4, 5, 6)
        name = objs[var]
        lang = re.search(r'getLang:\(\)=>([\w$]+)', src[m.end():]).group(1)
        fm = None
        for fm in re.finditer(r'function ' + re.escape(readfn) + r'\(l,n=null\)\{(.*?)\}', src[:m.start()]):
            pass
        dispatch = re.findall(r'"(\w+)"==l(?:\?|&&)([\w$]+)\.read\(n\)', fm.group(1))
        blocks = {}
        for bname, ovar in dispatch:
            p = _def(src, ovar, fm.start())
            obj = src[p:p + 20000]
            b = dict(read=re.match(r'\{readCommand:"([^"]*)"', obj).group(1))
            end = obj.find('parse(l){')
            wc = re.search(r'writeCommand:"([^"]*)"', obj[:end])
            if wc:
                b['write'] = wc.group(1)
            wa = re.search(r'writeAllStruct:(\{[^}]*\})', obj[:end])
            if wa:
                b['write_all'] = dict(re.findall(r'(\w+):(\d+)', wa.group(1)))
                b['write_all_struct'] = parse_struct(re.search(r'an\.qs`(.*?)`', obj[:end], re.S).group(1))
            b['reply'] = parse_struct(re.search(r'parse\(l\)\{const n=an\.qs`(.*?)`', obj, re.S).group(1))
            b['word_swap'] = '(65535&t)<<16|(4294901760&t)>>>16' in obj[:obj.find('update(')] if 'update(' in obj else False
            blocks[bname] = b
        # label tables
        lp = _def(src, lang, m.end())
        langobj = _bracket(src, src.index('{', lp))
        labels = {}
        for key, mvar in re.findall(r'(\w+):([\w$]+)', langobj):
            if key in ('__proto__', 'en', 'zh_cn'):
                continue
            mp = _def(src, mvar, lp)
            tvar = re.match(r'Object\.freeze\(Object\.defineProperty\(\{__proto__:null,default:([\w$]+)', src[mp:]).group(1)
            tp = _def(src, tvar, mp)
            labels[key] = json.loads(_bracket(src, tp))
        variants[name] = dict(service=svc, rx=rx, tx=tx, blocks=blocks, labels=labels)
    return variants


def _scale(fmt):
    d, unit = FMT.get(fmt, (1, '?' + str(fmt)))
    if d == 'K':
        return '(x-2731)/10', unit
    if d == 1:
        return '1', unit
    return ('%g' % d), unit


def req_info(block, addr_default='00'):
    r = block['read'].strip()
    dev_prefix = block['read'].startswith(' ')
    h = bytes.fromhex(('01 ' + r) if dev_prefix else r)
    fc, start, count = h[1], int.from_bytes(h[2:4], 'big'), int.from_bytes(h[4:6], 'big')
    ex = with_crc((addr_default + ' ' + r) if dev_prefix else r)
    return fc, start, count, dev_prefix, ex


def register_rows(block, labels):
    lab = {row[0]: row for row in labels}
    rows, off = [], 0
    for t, name, n in block['reply'][3:]:
        if name == 'crc':
            break
        size = TYPES[t] * (n or 1)
        row = lab.get(name)
        if name.startswith('ADDR_'):
            reg = '0x' + name[5:].split('_')[0]
            en = row[1] if row and len(row) > 1 and row[1] else ''
            zh = row[2] if row and len(row) > 2 and row[2] else ''
            fmt = row[3] if row and len(row) > 3 else None
            div, unit = _scale(fmt)
            typ = ('%s[%d]' % (t, n)) if n else t
            if n and t == 'u8':
                typ, div, unit = 'ascii[%d]' % n, '', ''
            if name.endswith('_fill'):
                reg, en = reg + ' (low byte)', 'padding'
            elif name + '_fill' in {f[1] for f in block['reply']}:
                reg += ' (high byte)'
            rows.append((off, reg, typ, div, unit, en, zh, fmt))
        off += size
    return rows


def coil_rows(block, labels, base):
    lab = {row[0]: row for row in labels}
    nbytes = sum(TYPES[t] * (n or 1) for t, name, n in block['reply'][3:] if name != 'crc')
    rows = []
    for i in range(nbytes * 8):
        key = 'ADDR_%X' % (base + i)
        row = lab.get(key)
        if not row or len(row) < 2 or not row[1]:
            continue
        group = row[3] if len(row) > 3 and row[3] in ('alarm', 'protect') else ''
        rows.append((i // 8, i % 8, '0x%X' % (base + i), row[1], row[2] if len(row) > 2 else '', group))
    return rows


def md_table(head, rows):
    out = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    for r in rows:
        out.append('| ' + ' | '.join(str(c).replace('|', '\\|') for c in r) + ' |')
    return '\n'.join(out)


def block_signature(b, labels):
    return json.dumps([b, labels], sort_keys=True, ensure_ascii=False)


NOTES_FRAMING = """\
## Framing

Every variant speaks **Modbus RTU** inside BLE writes and notifications; no extra envelope, no
login. Request: `<dev> <fc> <start-hi> <start-lo> <count-hi> <count-lo> <crc-lo> <crc-hi>`,
CRC16-Modbus (poly 0xA001, init 0xFFFF) over everything before it, little endian. Registers and
reply values are big endian.

| fc | meaning | count is | reply |
|---|---|---|---|
| `0x04` | read input registers | registers | `<dev> 04 <n bytes> <data> <crc>` |
| `0x01` | read coils | coils (bits) | `<dev> 01 <n bytes> <data> <crc>`; byte k bit i (LSB first) = coil start+8k+i |
| `0x10` | write registers | registers | `<dev> 10 <start> <count> <crc>` |
| `0x0F` | write coils | coils | `<dev> 0F <start> <count> <crc>` |
| fc + 0x80 | error | - | `<dev> <fc + 0x80> <code> <crc>` (5 bytes), codes below |

The app keeps one request in flight and reassembles notifications until it has `byte[2] + 5`
bytes (read replies). It matches a reply to the request by the function code and start address
it sent, and tells the two settings halves apart by the register count (53 vs 106).

**Device address.** `00` on the `bms` / `bmswifi` variants is the master pack and serves the
system (EMS) blocks; pack blocks are read from address `n` = 1..pack count (the pack page reads
`pack index + 1`). Settings, function switches and the inverter-protocol block are always read
from and written to address `00`. The `abu` variant uses `E0` for everything.

**Password.** The BMS has none. The app asks for `000000` before it sends a settings or
function-switch write and stores it in memory; nothing is sent to the BMS.
"""

NOTES_QUIRKS = """\
## What the app does in code

- **EIA 32-bit words are swapped.** The app reads `u32`/`i32` big endian and then swaps the
  16-bit halves (`(t & 0xFFFF) << 16 | t >>> 16`), so on the wire each 32-bit value is
  **low word first**, each word big endian. Its swap test is `parseInt("2000".."2012") <= 2012`
  on the hex register name read as decimal, so `0x200A`, `0x200C`, `0x200E` parse as 200 and are
  dropped entirely (no label either). `0x2014`.. are plain `u16`.
- **Pack current scale depends on firmware.** In PIA the app shows `0x1001` in 0.01 A, but in
  0.1 A (`100mA`) when the firmware version starts with `4`. The version is `0x1714`, two bytes
  the app decodes as text and shows as `<byte0>.<byte1>`; the test is `4 == version[0]`, so it
  fires when byte 0 is ASCII `4` (0x34).
- **Derived values.** The PIB page shows cell voltage spread / max / min and temperature
  spread / max / min under keys `ADDR_1000`..`ADDR_1005`; the app computes them from cells 1-16
  and temperatures 1-4 (`0x1110`-`0x1113`). They are not registers of that block.
- **Run state.** Coils `0x1240`-`0x1247` (pack) and `0x2200`-`0x2207` (system) are one-hot: the
  app shows the last set bit as the state (discharge, charge, floating charge, full charge,
  standby, off).
- **Coil labels within a byte.** In the PIC table the app names coil `0x1207` "cell 1" and
  `0x1200` "cell 8" (and `0x120F` cell 9 .. `0x1208` cell 16), i.e. cell n of a group of 8 is
  bit 7-(n-1) of its byte. The tables below follow the app.
- **Misleading fmt names.** The app's formatter divides `1℃`, `mΩ` and `s` by 10, so those
  registers are in 0.1 °C, 0.1 mΩ and 0.1 s; `A`, `mv` and `H` are shown unscaled. The ÷ column
  is what the app does.
- **Inverter protocol byte order.** The app reads the protocol number from the high byte of
  `0x1800` (struct `u8 ADDR_1800; u8 ADDR_1800_fill`) but writes it as a 16-bit value
  `00 <n>`, i.e. into the low byte. One of the two is wrong, or the BMS mirrors it; not checked on
  hardware.
- **Single-register settings writes are not sent.** `system_parameter.write(key, value)` only
  logs the frame (`00 10 <reg> 00 01 02 <val>`); the app always writes all 106 registers of
  `0x1300` in one `0x10` request, and all 80 function coils of `0x1400` in one `0x0F` request.

## Destructive requests

None of the blocks contains a factory reset, calibration or shutdown command; the app sends
exactly three kinds of write, all to device `00` (`abu`: only the settings write, device `E0`,
34 registers):

- `0x10` to `0x1800` (1 register): switches the inverter protocol (CAN or RS485) the BMS speaks
  to the inverter. A wrong value stops the inverter link.
- `0x0F` to `0x1400`, 80 coils: every function switch at once (alarms and trips on/off, heating,
  balancing, LCD, aerosol detection). Turning a trip off disables that protection.
- `0x10` to `0x1300`, 106 registers: every protection threshold, delay, capacity and the
  inverter charge request in one frame. A frame built from stale or misscaled values rewrites the
  whole protection set.

There is no MOSFET switch command in the app; `0x1278`/`0x2238` (discharge FET on) and
`0x1279`/`0x2239` (charge FET on) are read-only state bits.
"""

NOTES_V2 = """\
## Seplos V2 (BMS 1.0 / 2.0 hardware)

Not covered by this app. The V2 Bluetooth app ("BMS(2.0)_1.1.0", 2023-09-25) is only on
seplos.com behind a login. What is known about V2 comes from other sources:

- RS485/RS232: ASCII-hex `~ VER ADR CID1 CID2 LENGTH INFO CHKSUM \\r` frames, VER `0x20`, CID1
  `0x46`, CID2 `0x42` telemetry and `0x44` alarms (`bmslib/models/seplos_uart.py`).
- BLE (service FF00, FF01 notify, FF02 write, names `BP0?`..`BP2?`): **binary** frames
  `7E 10 <adr> 46 <cmd> <len16> <data> <crc16-xmodem BE> 0D`, reply version `0x14`, commands
  `0x51` (device info), `0x61` (single pack data), `0x62` (parallel data)
  (aiobmsble `seplos_v2_bms.py`).
"""


def render(variants, meta) -> str:
    out = ['# Seplos BMS protocol (V3, Modbus RTU over BLE)', '']
    src = '`%s`' % meta['file']
    if meta.get('version'):
        src = 'vendor app "%s" %s (versionCode %s, uni-app `%s`), APK sha256 `%s`' % (
            meta['app'], meta['version'], meta['code'], meta['uni_id'], meta['sha256'])
    out += ['Generated by `tools/seplos_protocol.py` from the request strings, reply structs and label tables in '
            'the `app-service.js` of the %s, from https://www.pgyer.com/be91fs (the Android link in Seplos\'s '
            'BMS 3.0 Bluetooth guide). The app downloads no protocol definition. Do not edit by hand: run the '
            'tool again.' % src, '']
    out += ['## Variants', '',
            md_table(['app protocol', 'GATT service', 'notify', 'write', 'what it is'],
                     [(n, '`%s`' % v['service'], '`%s`' % v['rx'], '`%s`' % v['tx'], VARIANT_DOC.get(n, ''))
                      for n, v in variants.items()]), '']
    out += [NOTES_FRAMING]
    err = variants['bms']['labels'].get('response_error') or []
    out += ['**Error codes** (fc+0x80 reply, byte 2): ' +
            ', '.join('%s %s' % (code, en) for code, en, *_ in err if code != '_') + '.', '']
    out += [NOTES_QUIRKS]

    seen = {}
    for vname, v in variants.items():
        out += ['## Requests: `%s`' % vname, '']
        rows = []
        for bname, b in v['blocks'].items():
            fc, start, count, dev, ex = req_info(b, 'E0' if vname == 'abu' else '00')
            dev_s = 'pack (1..n)' if dev and bname.startswith('pack') else ex[:2]
            w = ''
            if 'write_all' in b:
                wa = b['write_all']
                w = 'fc 0x%02X, %s x %s, %s bytes' % (int(wa['cmd']), '0x%04X' % int(wa['addr']), wa['nums'], wa['len'])
            elif 'write' in b:
                w = '`%s <crc>`' % b['write'].replace('#ADDR#', '%02X %02X' % (start >> 8, start & 0xFF)).replace('#VAL#', '<val16>')
            rows.append(('`%s`' % bname, BLOCK_DOC.get(bname, ''), '0x%02X' % fc, '0x%04X' % start, count,
                         dev_s, '`%s`' % ex, w))
        out += [md_table(['block', 'content', 'fc', 'start', 'count', 'device', 'request (device 00)', 'write'], rows), '']

        for bname, b in v['blocks'].items():
            labels = v['labels'].get(LABELS_FOR.get(bname, bname), [])
            if bname in ('system_parameter_a', 'system_parameter_b'):
                continue
            sig = block_signature(b, labels)
            title = '### `%s` %s: %s' % (vname, bname, BLOCK_DOC.get(bname, ''))
            if sig in seen:
                out += [title, '', 'Same layout and labels as `%s`.' % seen[sig], '']
                continue
            seen[sig] = '%s %s' % (vname, bname)
            fc, start, count, dev, ex = req_info(b)
            out += [title, '']
            if fc == 0x01:
                rows = coil_rows(b, labels, start)
                out += ['Coils from 0x%04X; offset = data byte, bit (LSB = 0).' % start, '',
                        md_table(['byte', 'bit', 'coil', 'meaning', 'app label (zh)', 'group'], rows), '']
            else:
                rows = register_rows(b, labels)
                out += ['Offset = byte in the reply data (after `<dev> <fc> <len>`).' +
                        (' 32-bit values low word first (see above).' if b.get('word_swap') else ''), '',
                        md_table(['offset', 'register', 'type', '÷', 'unit', 'meaning', 'app label (zh)', 'app fmt'],
                                 [(o, r, t, d, u, e, z, '`%s`' % f if f else '') for o, r, t, d, u, e, z, f in rows]), '']
            extra = [row for row in labels if row[0].startswith('ADDR_') and len(row) > 1 and row[1]
                     and fc != 0x01 and row[0] not in {n for _, n, _ in b['reply']}]
            if extra:
                out += ['Shown on the same page but read from another block or computed by the app: ' +
                        ', '.join('`%s` %s' % (r[0], r[1]) for r in extra) + '.', '']
            if bname == 'pcs_control_info':
                opts = [r for r in labels if not r[0].startswith(('ADDR_', 'floor', 'protocol'))]
                out += ['Values of `0x1800` (inverter protocol): ' +
                        ', '.join('%s %s' % (r[3], r[1]) for r in sorted(opts, key=lambda r: r[3])) + '.', '']
            if bname == 'system_function':
                out += ['Write: all 10 bytes in one `0x0F` frame `00 0F 14 00 00 50 0A <10 bytes> <crc>`, same bit '
                        'layout as the read.', '']
            if bname == 'system_parameter' and 'system_parameter_a' in v['blocks']:
                out += ['Read either as one request (106 registers) or as two halves of 53 (`0x1300`, `0x1335`). '
                        'Write: one `0x10` frame with all 106 registers in the order above.', '']
            elif bname == 'system_parameter' and 'write_all' in b:
                out += ['Write: one `0x10` frame with all %s registers in the order above.' % b['write_all']['nums'], '']
    out += [NOTES_V2]
    return '\n'.join(out).rstrip() + '\n'


def main():
    ap = argparse.ArgumentParser(description='Render doc/dev/SEPLOS_PROTOCOL.md from the Seplos app bundle')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--apk', help='the Octopus BMS APK')
    g.add_argument('--js', help='an extracted app-service.js')
    ap.add_argument('--out', default=str(OUT))
    a = ap.parse_args()
    src, meta = load_js(a.apk, a.js)
    pathlib.Path(a.out).write_text(render(parse(src), meta), 'utf-8')
    print('wrote', a.out)


if __name__ == '__main__':
    main()
