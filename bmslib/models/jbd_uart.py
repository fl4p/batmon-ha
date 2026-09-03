"""JBD / Xiaoxiang / LLT BMS over UART or RS485 (wired).

The wire protocol is byte-identical to the BLE one in ``bmslib.models.jbd``:
``DD A5 <cmd> 00 FF <chk> 77`` requests, ``DD <cmd> <status> <len> ... <chk> 77``
replies. The BLE module is nothing but a UART bridge, so the only things that
change here are the transport (a serial port instead of a GATT characteristic)
and the link settings, 9600 8N1 per the JBD protocol PDF and every UART client
(dbus-serialbattery ``lltjbd.py``, syssi/esphome-jbd-bms ``jbd_bms`` UART
component, bms-tools).

Configure with:
    address: serial
    adapter: /dev/ttyUSB0      # TTL UART header or the RS485 port via an adapter
    type:    jbd_uart
    alias:   any human-readable name

Untested on hardware: the decoder is the BLE one (covered by unit tests with
captured frames) and the framing is the same resync-on-0xDD loop, only the port
handling is new. Feedback welcome.
"""
from bmslib.models.jbd import JbdBt


class JbdUart(JbdBt):
    BAUDRATE = 9600
    # eol=None -> raw binary reads: a JBD frame is length-prefixed binary and may
    # contain 0x0A anywhere, so the default readline() framing would split it.
    # The inherited _notification_handler reassembles on the 0xDD header.
    SERIAL_KWARGS = dict(eol=None, timeout=1)

    def __init__(self, address, **kwargs):
        kwargs.setdefault('_uses_pin', False)  # no pairing on a wire
        super().__init__(address, **kwargs)

    async def connect(self, timeout=10, **kwargs):
        # BtBms.__init__ already created the SerialBleakClientWrapper for
        # address == 'serial'; just open it and register the frame handler.
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("jbd-uart", "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char  # the serial wrapper ignores the char on write

    async def _q(self, cmd):
        # Half duplex: hold the bus for the whole exchange when the port is shared.
        lock = getattr(self.client, 'bus_lock', None)
        if lock is None:
            return await super()._q(cmd)
        async with lock():
            return await super()._q(cmd)
