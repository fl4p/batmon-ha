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
- syssi/esphome-daly-bms (RS485 framing + addressing)
- dreadnought/python-daly-bms (cmd 0x90/0x93/0x94/0x95 layouts)
- Daly UART v1.2 PDF (forums.ni.com mirror)
"""
import asyncio

from bmslib.models.daly import DalyBt, daly_command_message, calc_crc


RESP_LEN = 13
HEADER_BYTE = 0xA5


def build_command(command: int) -> bytes:
    """13-byte request frame for the UART path (host address = 4)."""
    return bytes(daly_command_message(command, address=4))


def validate_response_frame(frame: bytes) -> bool:
    """Header + length + CRC check for a single 13-byte response."""
    if len(frame) != RESP_LEN:
        return False
    if frame[0] != HEADER_BYTE:
        return False
    return calc_crc(frame[0:12]) == frame[12]


def feed_buffer(buf: bytearray, chunk: bytes) -> bytes:
    """Accumulate ``chunk`` into ``buf`` and return every complete 13-byte
    frame currently available (popping them off ``buf``).

    Bytes ahead of the first ``0xA5`` header are skipped silently so line
    noise after open / partial frames don't poison subsequent reads.
    """
    buf.extend(chunk)
    out = bytearray()
    while True:
        # Drop garbage until we hit the header
        while buf and buf[0] != HEADER_BYTE:
            del buf[0]
        if len(buf) < RESP_LEN:
            break
        frame = bytes(buf[:RESP_LEN])
        del buf[:RESP_LEN]
        if validate_response_frame(frame):
            out.extend(frame)
        # else: bad CRC — drop frame and resync
    return bytes(out)


class DalyUart(DalyBt):
    """Daly BMS over an RS485 / USB-UART adapter.

    Re-uses every ``DalyBt`` decoder (``_fetch_status``, ``fetch_states``,
    ``fetch_voltages``, ``fetch_temperatures``, ``fetch_soc``); only the
    request-builder address byte and the BLE connect/notify glue change.

    Configure with:
        - address: serial
        - adapter: /dev/ttyUSB0  (the serial port path)
        - type:    daly_uart
        - alias:   any human-readable name
    """

    WIRE_ADDRESS = 4  # USB / RS485

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._uart_buf = bytearray()

    def _wrap_notify(self, sender, data):
        """Re-buffer chunked serial data into 13-byte frames before handing
        off to ``DalyBt._notification_callback`` (which assumes aligned
        frames)."""
        frames = feed_buffer(self._uart_buf, bytes(data))
        if frames:
            self._notification_callback(sender, frames)

    async def connect(self, timeout=10, **kwargs):
        # Open the serial wrapper (BtBms.__init__ already created the
        # SerialBleakClientWrapper when address == 'serial').
        await self.client.connect(timeout=timeout)
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("daly-uart", "notify")
        await self.client.start_notify(char, self._wrap_notify)
        # Mark a sentinel UUID so DalyBt.disconnect() can stop_notify.
        self.UUID_RX = char
        self.UUID_TX = char  # _q writes to this; the wrapper ignores the char arg
