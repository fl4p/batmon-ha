"""
ANT settings
------------

Reads (and, only with --write, changes) settings registers of an ANT BMS (7E A1 protocol,
BLE) with batmon's AntBt driver: nominal capacity, cell/pack undervoltage protection,
shutdown voltage, low-SoC warnings (bmslib/models/ant.py ANT_SETTINGS). Values are raw
register values as the BMS stores them; a write is read back and printed.

The BMS takes one BLE link: stop batmon first. With `ble_stack: esphome` in
/data/options.json (or ./options.json) it connects through the configured ESPHome
Bluetooth proxies, as batmon does; run it with the esphome venv in that case:

    /app/venv_esphome/bin/python3 tools/ant_settings.py AA:BB:CC:DD:EE:FF
    /app/venv_esphome/bin/python3 tools/ant_settings.py AA:BB:CC:DD:EE:FF --write nominal_capacity=245000000 \
        --command save

A write lives in RAM until `--command save`; `--command reset` reboots the BMS and reloads the saved settings.

Optional: BATMON_ADAPTER=hci1 selects the Bluetooth adapter (bleak stack).
"""

import argparse
import asyncio
import json
import os
import pathlib
import sys

if __name__ == '__main__':
    sys.path[:0] = [os.getcwd(), str(pathlib.Path(__file__).resolve().parents[1])]


def _proxies():
    """The configured ESPHome proxies with the bleak shim installed (before bleak is imported), else None."""
    for path in ('/data/options.json', 'options.json'):
        try:
            opt = json.load(open(path))
            break
        except Exception:
            opt = None
    if not opt or opt.get('ble_stack') != 'esphome':
        return None
    from bmslib.ble.esphome_proxy import install_bleak_shim
    return (opt.get('bluetooth_proxies') or []) if install_bleak_shim() else None


def _parse_writes(items):
    from bmslib.models.ant import ANT_SETTINGS
    out = []
    for it in items or ():
        k, _, v = it.partition('=')
        addr = ANT_SETTINGS.get(k)
        if addr is None:
            addr = int(k, 0)
        out.append((k, addr, int(v, 0)))
    return out


async def run(address, writes, proxies, adapter=None, trace=False, commands=()):
    if proxies is not None:
        from bmslib.ble.esphome_proxy import start_proxies
        await start_proxies(proxies)
        await asyncio.sleep(5)  # let the proxies report advertisements
    from bmslib.models.ant import AntBt, ANT_SETTINGS
    bms = AntBt(address, name='ant', keep_alive=False, verbose_log=False, adapter=adapter)
    if trace:
        handler = bms._notification_handler

        def traced(sender, data):
            print('RX', bytes(data).hex(' '))
            handler(sender, data)
        bms._notification_handler = traced
    try:
        await bms.connect()
        print(await bms.fetch_device_info())
        before = {}
        for k, a in ANT_SETTINGS.items():
            try:
                before[k] = await bms.read_setting(a)
            except Exception as e:
                before[k] = 'read failed: %s' % (str(e) or type(e).__name__)
            print('%-22s 0x%04X  %s' % (k, a, before[k]))
        for k, a, v in writes:
            echo = await bms.write_setting(a, v)
            back = await bms.read_setting(a)
            print('WRITE %s 0x%04X: %s -> %d, echoed %d, reads back %d %s'
                  % (k, a, before.get(k, '?'), v, echo, back, 'OK' if back == v else 'NOT TAKEN'))
            if back != v:
                return 2
        if commands:
            print('status before: capacity %.1f Ah, charge %.1f Ah, SoC %.1f %%' % _status(await bms.fetch()))
        from bmslib.models.ant import ANT_COMMAND_RESULT
        for a in commands:
            reply = await bms.command(a)
            code = int(reply.split()[6], 16) if reply and len(reply.split()) > 6 else None  # unpadded hex
            print('COMMAND 0x%04X: reply %s (%s)' % (a, reply, ANT_COMMAND_RESULT.get(code, code)))
            await asyncio.sleep(3)
            print('status after: capacity %.1f Ah, charge %.1f Ah, SoC %.1f %%' % _status(await bms.fetch()))
        return 0
    finally:
        await bms.force_disconnect()
        if proxies is not None:
            from bmslib.ble.esphome_proxy import stop_proxies
            await stop_proxies()


def _command(x):
    from bmslib.models.ant import ANT_COMMANDS
    return ANT_COMMANDS[x] if x in ANT_COMMANDS else int(x, 0)


def _status(s):
    return s.capacity, s.charge, s.soc


def main():
    ap = argparse.ArgumentParser(description='Read (and with --write change) ANT BMS settings registers')
    ap.add_argument('address', help='BMS MAC address')
    ap.add_argument('--write', action='append', metavar='NAME=RAW',
                    help='register name (or address) = raw value; repeatable')
    ap.add_argument('--command', action='append', metavar='NAME|ADDR', type=_command, default=[],
                    help='after the writes, send a 0x51 command: save (0x0007, settings to flash), reset '
                         '(0x0009, reboot), or a register address (doc/dev/ANT_PROTOCOL.md; some cannot be undone)')
    ap.add_argument('--trace', action='store_true', help='print every frame the BMS sends')
    args = ap.parse_args()
    proxies = _proxies()  # before anything imports bleak (bmslib.models.ant does)
    sys.exit(asyncio.run(run(args.address, _parse_writes(args.write), proxies,
                             adapter=os.environ.get('BATMON_ADAPTER') or None, trace=args.trace, commands=args.command)))


if __name__ == '__main__':
    main()
