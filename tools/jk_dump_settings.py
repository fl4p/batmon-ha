"""
JK settings dump
----------------

Read-only: connects to a JK BMS (JK02_32S BLE protocol) with batmon's JKBt driver,
captures the raw 300-byte settings frame (type 0x01), checks it and saves it to a file.
It only sends the same read requests the driver sends on every connect; it never
sends a command that changes a BMS setting or switch.

Stop the batmon service first (the BMS accepts only one BLE connection), then run
from the batmon directory:

    venv/bin/python tools/jk_dump_settings.py C8:47:80:51:FB:7F jk1_settings.bin

Optional: BATMON_ADAPTER=hci1 selects the Bluetooth adapter.
"""

import argparse
import asyncio
import os
import pathlib
import sys

if __name__ == '__main__':
    # allow running as a script (from the batmon dir or a copy in /tmp); not on import
    sys.path[:0] = [os.getcwd(), str(pathlib.Path(__file__).resolve().parents[1])]

from bmslib.models.jikong import JKBt, calc_crc  # noqa: E402

FRAME_LEN = 300
SETTINGS_HEADER = bytes([0x55, 0xAA, 0xEB, 0x90, 0x01])


def check_settings_frame(frame: bytes) -> None:
    """Raise ValueError if `frame` is not a complete, intact 0x01 settings frame."""
    if len(frame) != FRAME_LEN:
        raise ValueError('bad length %d, expected %d' % (len(frame), FRAME_LEN))
    if frame[:5] != SETTINGS_HEADER:
        raise ValueError('bad header %s, expected %s' % (frame[:5].hex(' '), SETTINGS_HEADER.hex(' ')))
    crc = calc_crc(frame[:-1])
    if crc != frame[-1]:
        raise ValueError('bad CRC: computed %02x, frame has %02x' % (crc, frame[-1]))


async def dump(address: str, adapter=None) -> bytes:
    # keep_alive=False and an explicit teardown: JKBt defaults to keep_alive, which makes
    # `async with` skip the disconnect, and a JK takes only one BLE link, so a leftover
    # link (also after a half-done connect) would lock batmon out when it restarts.
    bms = JKBt(address, name='jk', keep_alive=False, verbose_log=False, adapter=adapter)
    try:
        await bms.connect()
        if 0x01 not in bms._resp_table:
            await bms._q(cmd=0x96, resp=0x01)  # same read request the driver uses on connect
        return bytes(bms._resp_table[0x01][0])
    finally:
        await bms.force_disconnect()  # bounded; closes the client even if disconnect() raises


def main():
    ap = argparse.ArgumentParser(description='Read-only capture of the JK BMS 0x01 settings frame')
    ap.add_argument('address', help='BMS MAC address')
    ap.add_argument('out', help='output file for the raw 300-byte frame')
    args = ap.parse_args()

    try:
        frame = asyncio.run(dump(args.address, adapter=os.environ.get('BATMON_ADAPTER') or None))
    except Exception as e:
        sys.exit('could not read the settings frame from %s: %s' % (args.address, str(e) or type(e).__name__))
    try:
        check_settings_frame(frame)
    except ValueError as e:
        sys.exit('settings frame check failed (%s), nothing written: %s' % (e, frame.hex(' ')))

    pathlib.Path(args.out).write_bytes(frame)
    print(frame.hex(' '))
    print('OK %s: %d bytes, header %s, crc %02x -> %s'
          % (args.address, len(frame), frame[:5].hex(' '), frame[-1], args.out))


if __name__ == '__main__':
    main()
