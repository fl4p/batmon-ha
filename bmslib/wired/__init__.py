import threading
import time

from bmslib.util import get_logger
from bmslib.wired.transport import SerialTransport, StdioTransport

logger = get_logger()


class SerialBleakClientWrapper(object):
    # Consecutive read errors tolerated before the reader thread gives up, and
    # the pause between retries. Class attributes so tests can shrink them.
    RX_MAX_ERRORS = 20
    RX_ERROR_SLEEP = 0.5

    def __init__(self, address, baudrate: int = 115200, **kwargs):
        self.address = address
        # Forward optional framing knobs (eol / timeout) a BMS class exposes via
        # its SERIAL_KWARGS; unknown kwargs are ignored so existing callers keep
        # the readline() default.
        serial_kwargs = {k: kwargs[k] for k in ('eol', 'timeout') if k in kwargs}
        self.t = SerialTransport(address.split(':')[-1], baudrate=baudrate, **serial_kwargs)
        # self.t = StdioTransport()
        self.callback = {}
        self.services = []
        # Set when the reader thread has stopped for good. Anything waiting on a
        # response must be able to tell "nothing arrived yet" from "nothing will
        # ever arrive again", instead of timing out identically forever.
        self.rx_thread_error = None
        self._rx_thread = threading.Thread(target=self._on_receive, daemon=True,
                                           name='serial-rx')
        self._rx_thread.start()

    async def get_services(self):
        return self.services

    async def connect(self, timeout=None):
        self.t.open()

    async def disconnect(self):
        self.t.close()

    @property
    def is_connected(self):
        return self.t.is_open

    def _on_receive(self):
        # Any exception escaping this loop used to kill the reader thread
        # silently: the port stayed "connected", writes kept succeeding, and
        # every command timed out forever with no hint as to why. Log it and
        # record it so the failure is attributable.
        errors = 0
        while True:
            try:
                data = self.t.is_open and self.t.read()
                if data:
                    errors = 0
                    for callback in list(self.callback.values()):
                        try:
                            callback(self, data)
                        except Exception:
                            logger.exception('serial rx callback failed on %d bytes', len(data))
            except Exception as e:
                errors += 1
                # Bounded retry: a transient read error (USB re-enumeration) is
                # worth riding out, a permanent one must not spin the CPU or
                # pretend the link is alive.
                if errors >= self.RX_MAX_ERRORS:
                    self.rx_thread_error = e
                    logger.error('serial reader thread giving up on %s after %d '
                                 'consecutive errors: %s', self.t.port, errors, e)
                    return
                logger.warning('serial read error on %s (%d): %s', self.t.port, errors, e)
                time.sleep(self.RX_ERROR_SLEEP)
            time.sleep(0.1) # todo block

    async def start_notify(self, char, callback):
        self.callback[char] = callback
        pass

    async def stop_notify(self, char):
        self.callback.pop(char, None)

    async def write_gatt_char(self, _char, data):
        self.t.write(data)



class SerialServiceStub():
    def __init__(self, uuid):
        self.uuid = uuid

class SerialCharStub():
    def __init__(self, uuid_or_handle, property_name):
        self.uuid_or_handle = uuid_or_handle
        self.property_name = property_name

    # Value-based identity so a stub is a stable dict key. Serial BMS models
    # build a fresh SerialCharStub on every connect() and register it as the
    # notify-callback key. Without this, each reconnect adds a *new* key
    # (SerialBleakClientWrapper.stop_notify never removes the old one, because
    # BtBms.stop_notify is gated on `client.services`, which is always empty for
    # serial transports), so the same _notification_handler accumulates and
    # every received chunk is delivered N times, corrupting frame reassembly
    # from the second poll onward. Equal stubs collapse to one key instead.
    def __eq__(self, other):
        return (isinstance(other, SerialCharStub)
                and self.uuid_or_handle == other.uuid_or_handle
                and self.property_name == other.property_name)

    def __hash__(self):
        return hash((self.uuid_or_handle, self.property_name))