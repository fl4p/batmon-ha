#!/usr/bin/env python3
"""Sort a raw capture of a JK-PB RS485-1 bus into the packs it belongs to.

Capture on the host with the batmon add-on stopped (listen only, nothing is sent):

    stty -F /dev/serial/by-id/usb-... 115200 cs8 -parenb -cstopb raw -echo
    timeout 60 cat /dev/serial/by-id/usb-... > rx.bin

then:

    python3 tools/jk_pb_capture.py rx.bin

Prints, per RS485 address, how many frames of each type were attributed to it and the first cell
voltages, pack voltage and SoC of its latest status frame. Needs no packages beyond batmon itself.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from bmslib.uart.jk_pb_bus import PbBusParser  # noqa: E402

TYPES = {1: 'settings', 2: 'status', 3: 'device info'}


def main(path):
    data = open(path, 'rb').read()
    p = PbBusParser()
    frames = p.feed(data)
    print('%d bytes, %d request(s), %d frame(s), %d corrupt, %d junk byte(s)' % (
        len(data), p.requests, len(frames), p.corrupt, p.junk))
    print('polled addresses:', sorted(p.polled))

    count = defaultdict(lambda: defaultdict(int))
    last = {}
    for f in frames:
        count[f.owner][f.ftype] += 1
        if f.ftype == 0x02:
            last[f.owner] = f.frame
    for owner in sorted(count):
        kinds = ', '.join('%d x %s' % (n, TYPES[t]) for t, n in sorted(count[owner].items()))
        print('address %d: %s' % (owner, kinds))
        fr = last.get(owner)
        if fr:
            cells = [int.from_bytes(fr[6 + 2 * i:8 + 2 * i], 'little') for i in range(32)]
            cells = [c for c in cells if c]
            volt = int.from_bytes(fr[150:154], 'little') / 1000  # 32S layout
            print('   pack %.3f V, %d cell(s): %s ...' % (volt, len(cells), cells[:4]))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
