"""Daly BMS over UART / RS485.

Same wire protocol as the BLE one (``bmslib.models.daly``): 13-byte fixed
request/response frames, ``A5 <addr> <cmd> 08 <8 bytes data> <crc>``.

The only on-wire difference is the *host* address byte in REQUESTS:
  - 8 = Bluetooth (used by ``DalyBt``)
  - 4 = USB / RS485 (used by ``DalyUart``)

Responses carry an address byte too (typically ``01`` = BMS) but the
decoder ignores it — bytes [0:4] are stripped before parsing the 8-byte
payload, same as the BLE path.

Two practical UART quirks the BLE class doesn't need to handle:

  1. ``SerialBleakClientWrapper`` uses ``serial.readline()``, which splits
     payload bytes at every ``0x0A``. We accumulate raw bytes in a buffer
     and only invoke the inherited ``_notification_callback`` once we have
     an integer multiple of 13 bytes.

  2. There's no GATT service discovery — we install one notify callback on
     a serial char stub and call it a day.

References cross-checked against:
- maland16/daly-bms-uart — Arduino library; UART-only, so the closest
  authoritative source for the wire details. Init sets ``my_txBuffer[1] =
  0x40`` (host=4); link runs at ``9600 8N1``; checksum = sum-mod-256 over
  the first 12 bytes; cmd 0x90 reads voltage at payload bytes 0-1,
  current at 4-5, SOC at 6-7 — identical to the BLE path in ``daly.py``.
- syssi/esphome-daly-bms (RS485 framing + addressing)
- dreadnought/python-daly-bms (cmd 0x90/0x93/0x94/0x95 layouts)
- Daly UART v1.2 PDF (forums.ni.com mirror)
"""
import asyncio
from typing import Optional

from bmslib.models.daly import (DalyBt, daly_board_addr_byte, daly_command_message,
                                calc_crc)


RESP_LEN = 13
HEADER_BYTE = 0xA5

# Payload fill byte for wired requests. See daly_command_message(fill=...): the
# 8 don't-care bytes of a read request are 0x00 on the BLE path, but Daly's
# firmware UART resyncs on edges and an all-zero payload provides none, so 0x00
# requests go unanswered far more often over a wire link. dbus-serialbattery
# ships 0xAA here and reports it "reduces read errors dramatically" (#398).
UART_FILL = 0xAA


def build_command(command: int, board: int = 1) -> bytes:
    """13-byte request frame for the UART path, addressed to `board` (1-based)."""
    return bytes(daly_command_message(command, addr_byte=daly_board_addr_byte(board),
                                     fill=UART_FILL))


def validate_response_frame(frame: bytes) -> bool:
    """Header + length + CRC check for a single 13-byte response."""
    if len(frame) != RESP_LEN:
        return False
    if frame[0] != HEADER_BYTE:
        return False
    return calc_crc(frame[0:12]) == frame[12]


def feed_buffer(buf: bytearray, chunk: bytes, stats: Optional[dict] = None) -> bytes:
    """Accumulate ``chunk`` into ``buf`` and return every complete 13-byte
    frame currently available (popping them off ``buf``).

    Bytes ahead of the first ``0xA5`` header are skipped so line noise after
    open / partial frames don't poison subsequent reads. Every such drop is
    counted into ``stats`` when given (keys ``rx``, ``skipped``, ``bad_crc``,
    ``frames``): dropping silently made "nothing on the wire" and "plenty on the
    wire, none of it parseable" produce the identical add-on log line, which is
    the whole reason #398 took two rounds to diagnose.
    """
    def bump(key, n=1):
        if stats is not None and n:
            stats[key] = stats.get(key, 0) + n

    bump('rx', len(chunk))
    buf.extend(chunk)
    out = bytearray()
    while True:
        # Drop garbage until we hit the header
        skipped = 0
        while buf and buf[0] != HEADER_BYTE:
            del buf[0]
            skipped += 1
        bump('skipped', skipped)
        if len(buf) < RESP_LEN:
            break
        frame = bytes(buf[:RESP_LEN])
        del buf[:RESP_LEN]
        if validate_response_frame(frame):
            out.extend(frame)
            bump('frames')
        else:
            # bad CRC — drop frame and resync
            bump('bad_crc')
    return bytes(out)


class DalyUart(DalyBt):
    """Daly BMS over an RS485 / USB-UART adapter.

    Re-uses every ``DalyBt`` decoder (``_fetch_status``, ``fetch_states``,
    ``fetch_voltages``, ``fetch_temperatures``, ``fetch_soc``); only the
    request-builder address byte and the BLE connect/notify glue change.

    Configure with:
        - address: serial
        - adapter: /dev/ttyUSB0  (the serial port path)
        - type:    daly_uart      (or `daly_uart:2` for RS485 board number 2)
        - alias:   any human-readable name

    The board number defaults to 1, which is the factory setting. If yours was
    changed (Daly's PC tool calls it "Board No"), a request addressed to board 1
    gets no answer at all -- run ``tools/daly_serial_probe.py <port>`` to find
    out which board number replies.
    """

    WIRE_ADDRESS = 4  # USB / RS485
    # Daly UART is 9600 8N1 per the protocol PDF + maland16/daly-bms-uart.
    # The JK UART path uses 115200 (BtBms default).
    BAUDRATE = 9600
    # eol=None -> raw binary reads. Daly response frames are fixed-length (13 B)
    # binary and usually contain no 0x0A at all, so the default read_until(b'\n')
    # with timeout=None parks the reader thread forever and every command times
    # out with "got 0/N responses" (#398). feed_buffer() below does the framing.
    SERIAL_KWARGS = dict(eol=None, timeout=1)
    # Gap inserted before every request, taken while holding the bus (see _q).
    # Without a pause Daly's firmware UART is still busy with the previous reply
    # and simply drops the next request. dbus-serialbattery uses the same 20 ms
    # for the same reason ("else the Daly is not ready and throws a lot of no
    # reply errors"). A poll sends 2 commands in steady state (0x90 soc, 0x95
    # voltages) and 4 on the cycles where the 30 s status/temperature caches
    # refresh, so this costs 40-80 ms per BMS per poll.
    # We deliberately do NOT flush the input buffer as it does: our reader thread
    # owns the port, so flushing from the event loop would race with it, and
    # feed_buffer() already resyncs on the 0xA5 header.
    REQUEST_SETTLE = 0.02

    def __init__(self, address, **kwargs):
        # `type: daly_uart:<n>` -> RS485 board number (1-based, factory default 1)
        spec = kwargs.pop('type_spec', None)
        self.board = int(spec) if spec else 1
        daly_board_addr_byte(self.board)  # validate now, not on first poll
        super().__init__(address, **kwargs)
        self._uart_buf = bytearray()
        # Cumulative wire stats, reported on timeout so a dead link and a
        # mis-framed one are distinguishable from the add-on log alone.
        self._uart_stats: dict = {}
        self._board_mismatch_warned = False
        if self.board != 1:
            self.logger.info('%s addressing Daly board %d (wire byte 0x%02x)',
                             self.name, self.board, daly_board_addr_byte(self.board))

    def _build_request(self, command: int, extra="") -> bytearray:
        return daly_command_message(command, extra=extra,
                                    addr_byte=daly_board_addr_byte(self.board),
                                    fill=UART_FILL)

    async def _settle(self):
        if self.REQUEST_SETTLE:
            await asyncio.sleep(self.REQUEST_SETTLE)

    async def _q(self, command: int, num_responses: int = 1):
        # RS485 is half duplex and the bus may carry several BMS. Hold it for the
        # whole request/response exchange, or with concurrent_sampling two units
        # transmit at once and both replies are lost.
        lock = getattr(self.client, 'bus_lock', None)
        if lock is None:
            await self._settle()
            return await super()._q(command, num_responses)
        async with lock():
            # The settle has to happen INSIDE the critical section. Sleeping
            # before taking the lock lets a queued BMS burn its gap while another
            # unit is still mid-exchange, so it transmits the instant the lock
            # frees -- no gap at all after the previous reply, which is exactly
            # what the settle exists to prevent. Invisible with one BMS; it only
            # bites with several units on one bus, which is the case that needs
            # it most (#398).
            await self._settle()
            return await super()._q(command, num_responses)

    def _q_timeout_context(self) -> str:
        # A dead reader thread has to be reported FIRST. Its death freezes
        # _uart_stats, so the byte counts below would keep asserting a confident
        # (and by then wrong) diagnosis forever. Absence of new bytes here means
        # "nobody was listening", not "nothing arrived".
        rx_err = getattr(self.client, 'rx_thread_error', None)
        if rx_err is not None:
            return ('board=%d, the serial reader thread died (%s) — no reply can be '
                    'delivered until it restarts, which happens on the next reconnect. '
                    'The byte counts below are stale' % (self.board, rx_err))

        s = self._uart_stats
        decoded = s.get('rx', 0)
        # Port-level count is authoritative when the transport exposes it: it
        # also covers bytes that arrived before the notify callback was
        # registered, which the decoder's own counter cannot see.
        port_rx = getattr(self.client, 'rx_bytes', None)
        rx = decoded if port_rx is None else max(port_rx, decoded)

        if not rx:
            return ('board=%d, 0 bytes received on %s since connect — nothing reached us, '
                    'so this is wiring / adapter / port, not framing. Run '
                    'tools/daly_serial_probe.py to localize it' % (self.board, self._adapter))
        return ('board=%d, %d bytes received on %s (%d reached the decoder: %d valid '
                'frames, %d bad checksum, %d resync-skipped) — bytes are arriving, so '
                'check baud rate and board number with tools/daly_serial_probe.py'
                % (self.board, rx, self._adapter, decoded, s.get('frames', 0),
                   s.get('bad_crc', 0), s.get('skipped', 0)))

    def _wrap_notify(self, sender, data):
        """Re-buffer chunked serial data into 13-byte frames before handing
        off to ``DalyBt._notification_callback`` (which assumes aligned frames).

        Byte 1 of a reply is the board number that answered. What to do with a
        frame from another board depends on who else is on the wire:

        * shared bus -- it belongs to a sibling BMS, which is reading the same
          byte stream and will decode it itself. Drop it silently; decoding it
          here would attribute another battery's voltages to this one.
        * port to ourselves -- keep it, and say so once. This is the "wrong
          board number configured" case, and accepting the frame is what makes
          the add-on usable while the user fixes the config.
        """
        frames = feed_buffer(self._uart_buf, bytes(data), self._uart_stats)
        if not frames:
            return

        shared = getattr(self.client, 'shared', False)
        mine = bytearray()
        for i in range(0, len(frames), RESP_LEN):
            frame = frames[i:i + RESP_LEN]
            replier = frame[1]
            if replier == self.board:
                mine.extend(frame)
                continue
            if shared:
                self._uart_stats['other_board'] = self._uart_stats.get('other_board', 0) + 1
                continue
            if not self._board_mismatch_warned:
                self._board_mismatch_warned = True
                self.logger.warning(
                    '%s addressed Daly board %d but board %d answered — set '
                    '`type: daly_uart:%d` to address it explicitly',
                    self.name, self.board, replier, replier)
            mine.extend(frame)

        if mine:
            self._notification_callback(sender, bytes(mine))

    async def connect(self, timeout=10, **kwargs):
        # Open the serial wrapper (BtBms.__init__ already created the
        # SerialBleakClientWrapper when address == 'serial').
        await self.client.connect(timeout=timeout)
        # Scope the wire stats and the reassembly buffer to this session: a
        # partial frame left over from a previous connect would corrupt the first
        # read, and stale counts would make _q_timeout_context() lie about
        # whether anything arrived "since connect".
        self._uart_buf.clear()
        self._uart_stats.clear()
        from bmslib.wired import SerialCharStub
        # The stub uuid is the bus-wide identity of this BMS (SerialCharStub has
        # value-based equality). Include the board number so several Daly can
        # share one RS485 port without overwriting each other's callback -- and
        # so two units configured with the SAME board number are rejected as the
        # duplicate they are, rather than silently stealing each other's replies.
        char = SerialCharStub("daly-uart-%d" % self.board, "notify")
        await self.client.start_notify(char, self._wrap_notify)
        # Mark a sentinel UUID so DalyBt.disconnect() can stop_notify.
        self.UUID_RX = char
        self.UUID_TX = char  # _q writes to this; the wrapper ignores the char arg
