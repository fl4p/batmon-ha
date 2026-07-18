import threading
import time

from bmslib.wired.transport import SerialTransport, StdioTransport


class SerialBleakClientWrapper(object):

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
        self._rx_thread = threading.Thread(target=self._on_receive)
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
        while True:
            data = self.t.is_open and self.t.read()
            if data:
                for callback in self.callback.values():
                    callback(self, data)
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