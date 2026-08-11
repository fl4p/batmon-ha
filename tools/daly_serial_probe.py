#!/usr/bin/env python3
"""Standalone Daly UART / RS485 link prober (#398).

When `daly_uart` reports "timeout awaiting result for cmd=0x93, got 0/1
responses", the add-on log cannot tell you *why*: no bytes on the wire at all
(wiring / adapter / port), bytes but wrong baud (garbage), or clean frames from
a board number we never addressed. This script sweeps the parameters that
actually differ between working and non-working setups and dumps every raw byte
it sees, so one run answers the question.

It deliberately depends on nothing but pyserial, so it can be curl'ed into the
add-on container and run there:

    python3 daly_serial_probe.py /dev/serial/by-id/usb-FTDI_FT232R_...-port0

Useful flags:
    --boards 1-8        board numbers to address (Daly board 1 = wire 0x40)
    --baud 9600,115200  baud rates to try
    --listen 5          just listen this many seconds, send nothing
    -v                  hexdump every read, not just the ones that decode

What is swept, and why each matters:

  * board number   Daly puts the *board* number in byte 1 of a request:
                   board 1 = 0x40, board 2 = 0x41, ... A BMS whose board
                   number is not 1 stays completely silent when addressed as
                   0x40 -- which is all batmon <= 2.14 ever sent.
  * fill bytes     The 8 don't-care payload bytes of a read request. Daly's
                   firmware UART is bit-banged and resyncs on edges, so 0x00
                   fill (no edges) is answered much less reliably than 0xAA.
                   dbus-serialbattery ships 0xAA for exactly this reason.
  * RTS / DTR      A USB-RS485 adapter without auto-direction uses RTS (or
                   DTR) as the transceiver's driver-enable. pyserial asserts
                   both on open, which can pin the adapter in transmit so the
                   reply is never heard. Deasserting costs nothing to try.
  * baud rate      Daly UART/RS485 is 9600 8N1, but a wrong rate is the other
                   classic cause of "bytes arrive, none of them parse".

Exit status is 0 if any frame decoded, 1 otherwise.
"""
import argparse
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    sys.exit("pyserial missing: pip3 install pyserial")

HEADER = 0xA5
FRAME_LEN = 13

# Wire value of byte 1 for board number 1. Board N -> BOARD1_ADDR + (N - 1),
# and a reply carries the board number itself in byte 1 (so board 1 answers
# with 0x01). Cross-checked against mr-manuel/venus-os_dbus-serialbattery
# bms/daly.py: `buffer[1] = self.address[0]  # Board No 1 = 0x40; No 2 = 0x41`
# and its reply check `(63 + id) != self.address[0]`.
BOARD1_ADDR = 0x40

# Commands every Daly answers with exactly one frame. 0x90 (SOC/voltage/current)
# is the cheapest liveness probe; 0x93 is the one batmon polls first, so it is
# worth confirming separately.
PROBE_COMMANDS = (0x90, 0x93, 0x94)


def build_request(command: int, addr_byte: int, fill: int) -> bytes:
    """13-byte Daly request: A5 <addr> <cmd> 08 <8x fill> <checksum>."""
    frame = bytearray([HEADER, addr_byte, command, 0x08])
    frame.extend([fill] * 8)
    frame.append(sum(frame) & 0xFF)
    return bytes(frame)


def split_frames(buf: bytes):
    """Yield (frame, ok) for every 13-byte candidate, resyncing on 0xA5.

    Bytes before the first header and any trailing partial frame are reported
    separately by the caller via the leftover count, never silently dropped --
    "we skipped 40 bytes of garbage" is the diagnosis, so it must be visible.
    """
    i = 0
    skipped = 0
    while i < len(buf):
        if buf[i] != HEADER:
            i += 1
            skipped += 1
            continue
        if i + FRAME_LEN > len(buf):
            break
        frame = buf[i:i + FRAME_LEN]
        ok = (sum(frame[:12]) & 0xFF) == frame[12]
        yield frame, ok, skipped
        skipped = 0
        i += FRAME_LEN
    if i < len(buf):
        yield buf[i:], None, skipped


def is_echo(frame: bytes, request: bytes) -> bool:
    """True if `frame` is our own request looped back rather than a reply.

    A half-duplex RS485 adapter whose driver-enable is stuck asserted (no
    auto-direction, RTS wired to DE) puts the transmitted bytes straight back on
    RX. Such a frame passes the checksum test trivially -- it was checksummed by
    build_request -- so without this it would be counted as a valid reply and the
    tool would announce "BMS answers" on a link that has no BMS on it at all.

    Two independent signals, either is conclusive:
      * byte-identical to what we just sent;
      * byte 1 in the host-address range 0x40..0x4F. A genuine reply carries the
        board NUMBER there (1..16), never the host address, so the two ranges
        cannot collide.
    """
    if frame == request:
        return True
    return BOARD1_ADDR <= frame[1] <= BOARD1_ADDR + 15


def hexs(b: bytes) -> str:
    return ' '.join('%02x' % x for x in b)


def read_for(ser, seconds: float) -> bytes:
    """Drain the port for `seconds`, returning everything that arrived."""
    out = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        n = ser.in_waiting
        if n:
            out.extend(ser.read(n))
            # a frame may still be in flight; keep a short tail window
            deadline = max(deadline, time.monotonic() + 0.15)
        else:
            time.sleep(0.01)
    return bytes(out)


def describe(frame: bytes) -> str:
    board = frame[1]
    cmd = frame[2]
    return 'board=%d cmd=0x%02x data=%s' % (board, cmd, hexs(frame[4:12]))


def parse_range(spec: str):
    out = []
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part[1:]:
            a, b = part.split('-', 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('port', help='serial device, e.g. /dev/ttyUSB0')
    ap.add_argument('--baud', default='9600',
                    help='comma-separated baud rates to try (default 9600)')
    ap.add_argument('--boards', default='1-8',
                    help='board numbers to address (default 1-8)')
    ap.add_argument('--fill', default='aa,00',
                    help='comma-separated payload fill bytes, hex (default aa,00)')
    ap.add_argument('--wait', type=float, default=0.35,
                    help='seconds to wait for a reply per request (default 0.35)')
    ap.add_argument('--listen', type=float, default=2.0,
                    help='seconds of passive listening before probing (default 2)')
    ap.add_argument('-v', '--verbose', action='store_true',
                    help='hexdump every non-empty read')
    args = ap.parse_args()

    bauds = parse_range(args.baud)
    boards = parse_range(args.boards)
    fills = [int(f, 16) for f in args.fill.split(',') if f.strip()]

    stat = dict(rx=0, frames=0, bad_crc=0, skipped=0, echo=0)
    hits = []          # (label, board, cmd, fill, frame)
    incomplete = []    # sweep configurations that did not fully run
    opened = 0

    print('probing %s' % args.port)
    print('bauds=%s boards=%s fill=%s' % (bauds, boards, ['%02x' % f for f in fills]))

    def sweep(ser, label):
        """One (baud, rts/dtr) configuration. Raises only on a dead port."""
        if args.listen > 0:
            ser.reset_input_buffer()
            quiet = read_for(ser, args.listen)
            stat['rx'] += len(quiet)
            if quiet:
                print('  %s: %d unsolicited bytes: %s'
                      % (label, len(quiet), hexs(quiet[:39])))

        for board in boards:
            addr = BOARD1_ADDR + board - 1
            for fill in fills:
                for cmd in PROBE_COMMANDS:
                    req = build_request(cmd, addr, fill)
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                    ser.write(req)
                    ser.flush()
                    rx = read_for(ser, args.wait)
                    stat['rx'] += len(rx)
                    if not rx:
                        if args.verbose:
                            print('  %s board=%d fill=%02x cmd=0x%02x -> silence'
                                  % (label, board, fill, cmd))
                        continue
                    if args.verbose:
                        print('  %s board=%d fill=%02x cmd=0x%02x -> %d B: %s'
                              % (label, board, fill, cmd, len(rx), hexs(rx[:39])))
                    for frame, ok, skipped in split_frames(rx):
                        stat['skipped'] += skipped
                        if ok is None:
                            continue  # trailing partial frame
                        if not ok:
                            stat['bad_crc'] += 1
                            print('  CRC %s fill=%02x -> %s'
                                  % (label, fill, hexs(frame)))
                        elif is_echo(frame, req):
                            # Our own request looped back. It is checksummed by
                            # construction, so counting it as a reply would
                            # report "BMS answers" on a bus where the RS485
                            # transceiver is stuck transmitting -- precisely the
                            # fault this tool exists to find.
                            stat['echo'] += 1
                            print('  ECHO %s fill=%02x -> %s (our own request came '
                                  'back)' % (label, fill, hexs(frame)))
                        else:
                            stat['frames'] += 1
                            hits.append((label, board, cmd, fill, frame))
                            print('  OK  %s fill=%02x -> %s'
                                  % (label, fill, describe(frame)))

    for baud in bauds:
        # rts/dtr: None keeps pyserial's default (both asserted). False
        # deasserts, which is what a non-auto-direction RS485 adapter needs to
        # stay in receive.
        for rts, dtr in ((None, None), (False, False)):
            label = 'baud=%d rts=%s dtr=%s' % (baud, rts, dtr)
            try:
                ser = serial.Serial(args.port, baudrate=baud, timeout=0.1)
            except Exception as e:
                print('  %s: cannot open port: %s' % (label, e))
                # A port we cannot open is not a port we proved silent.
                return 2
            opened += 1
            try:
                # Not every device has modem-control lines (ioctl ENOTTY on a
                # pty, and on some USB bridges). That must downgrade this one
                # configuration, never abort the run -- and it must not be
                # reported as "tried rts=False" when it wasn't.
                try:
                    if rts is not None:
                        ser.rts = rts
                    if dtr is not None:
                        ser.dtr = dtr
                except Exception as e:
                    print('  %s: cannot set modem lines (%s) -- skipping this '
                          'configuration' % (label, e))
                    incomplete.append('%s (modem lines unsupported)' % label)
                    continue
                try:
                    sweep(ser, label)
                except Exception as e:
                    # Keep whatever we already learned; a partial sweep is still
                    # evidence, as long as we say it was partial.
                    print('  %s: aborted mid-sweep: %s' % (label, e))
                    incomplete.append('%s (%s)' % (label, e))
            finally:
                ser.close()

    print('')
    print('--- summary ---')
    print('raw bytes received: %d' % stat['rx'])
    print('valid frames: %d, echoes of our own request: %d, bad checksum: %d, '
          'resync-skipped bytes: %d'
          % (stat['frames'], stat['echo'], stat['bad_crc'], stat['skipped']))
    if incomplete:
        print('NOT fully tested: %s' % '; '.join(incomplete))

    total_rx, total_frames = stat['rx'], stat['frames']

    if total_frames:
        boards_seen = sorted({f[1] for f in hits})
        fills_ok = sorted({f[3] for f in hits})
        print('')
        print('BMS answers. board number(s): %s' % ', '.join(str(b) for b in boards_seen))
        print('working fill byte(s): %s' % ', '.join('0x%02x' % f for f in fills_ok))
        print('configure batmon with:  type: daly_uart:%d' % boards_seen[0])
        return 0

    print('')
    if len(incomplete) == opened:
        # Every configuration failed before sending anything. We proved nothing.
        print('No configuration completed, so this run says NOTHING about the BMS.')
        print('Fix the errors above (is %s really a serial port?) and re-run.' % args.port)
        return 2

    if stat['echo']:
        print('Everything that came back was our OWN request looped back (%d frames),'
              % stat['echo'])
        print('so no BMS was heard. That is the RS485 direction-control fault: the')
        print('transceiver stays in transmit and never lets the reply through. Check:')
        print('  1. Is the adapter auto-direction? If it drives DE from RTS or DTR,')
        print('     try --baud with the rts=False pass (see whether it appeared above).')
        print('  2. Some adapters echo unconditionally in hardware. Then this is')
        print('     expected and tells you nothing about the BMS -- test with the')
        print('     vendor PC tool on the same adapter to confirm it works at all.')
        return 1

    if total_rx == 0:
        print('NO bytes received at all with boards %s at %s baud -- the link is dead'
              % ('/'.join(str(b) for b in boards), '/'.join(str(b) for b in bauds)))
        print('in the receive direction, and nothing in batmon can fix that.')
        print('(Boards outside that range: --boards 1-16.) Check, in order:')
        print('  1. Right port on the BMS. Daly UART (TTL, 3.3V) and RS485 are')
        print('     different RJ45 sockets with different pinouts. An FT232R')
        print('     "USB UART" cable belongs on the UART port; an RS485 port needs')
        print('     a transceiver (MAX485 etc).')
        print('  2. RS485 A/B swapped -- the single most common cause. Swap them.')
        print('  3. GND connected between adapter and BMS.')
        print('  4. On the UART port: is a Bluetooth module occupying it? The BT')
        print('     dongle and the UART header are the same UART on many Daly')
        print('     boards, so only one of the two can talk at a time.')
        print('  5. BMS awake -- some Daly need charger presence or the activation')
        print('     button before the comms port responds.')
    else:
        print('Bytes arrived but nothing decoded as a Daly frame. That means the')
        print('wiring is fine and the framing is wrong: try other baud rates')
        print('(--baud 9600,19200,115200) and check for a half-duplex echo of our')
        print('own request (A5 40 ...) which would indicate the adapter loops back.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
