import glob
import socket
from typing import Optional

import serial  # pyserial

from bmslib.util import get_logger

logger = get_logger()

# Network serial servers (#422): ser2net, ESP USB/RS485 gateways, ... `tcp://`
# is what Zigbee2MQTT and those gateways document; pyserial calls it `socket://`.
_URL_SCHEMES = {'tcp': 'socket', 'socket': 'socket', 'rfc2217': 'rfc2217'}

# A raw-mode read drains at most this much before handing it over.
_RAW_READ_MAX = 4096

# Without one, a write to a gateway that stopped reading blocks forever, on the
# event loop thread (write_gatt_char).
_URL_WRITE_TIMEOUT = 2


def serial_url(port: str) -> Optional[str]:
    """The pyserial URL for a network port (`tcp://host:port`), None for a device path."""
    scheme, sep, rest = port.partition('://')
    if not sep:
        return None
    if scheme.lower() not in _URL_SCHEMES:
        raise ValueError('unsupported serial URL %r, use tcp://host:port or rfc2217://host:port'
                         % port)
    return _URL_SCHEMES[scheme.lower()] + '://' + rest


class Transport(object):

    def open(self):
        raise NotImplementedError()

    def read(self) -> bytes:
        raise NotImplementedError()

    def write(self, data: bytes):
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()


class SerialTransport(Transport):

    def __init__(self, port, baudrate: int = 115200, eol: bytes = b'\n', timeout=None):
        self.port = port
        self.baudrate = baudrate
        # `eol` is the frame delimiter read() stops at. Default b'\n' keeps the
        # historical readline() behaviour; paceic frames end in b'\r' instead.
        self.eol = eol
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None
        # Byte counters, so a model that never completes a response can report
        # whether anything arrived at all. "0 bytes received" (dead link) and
        # "N bytes received, none parseable" (wrong baud / framing / address)
        # need completely different fixes but produced the same add-on log
        # message before #398.
        self.rx_bytes = 0
        self.tx_bytes = 0

    def open(self):
        port = self.port
        url = serial_url(port)
        if url:
            # Raw TCP has no line settings: the baud rate is set on the server.
            logger.info('opening network serial port %s @ %s baud (eol=%s, timeout=%s)',
                        url, self.baudrate, self.eol, self.timeout)
            self.ser = serial.serial_for_url(url, baudrate=self.baudrate, timeout=self.timeout,
                                             write_timeout=_URL_WRITE_TIMEOUT)
            return
        if '*' in port:
            files = glob.glob(port)
            if not files:
                raise FileNotFoundError('Serial port device not found: {}'.format(port))
            port =files[0]
        # Log the framing config too: it is per-model (SERIAL_KWARGS) and was
        # silently dropped before #398, so "did my build get the fix?" must be
        # answerable from a normal INFO log without a debug rebuild.
        logger.info('opening serial port %s @ %s baud (eol=%s, timeout=%s)',
                    port, self.baudrate, self.eol, self.timeout)
        self.ser = serial.Serial(port, baudrate=self.baudrate, timeout=self.timeout)

    def close(self):
        if self.ser is not None:
            self.ser.close()

    def write(self, data: bytes):
        self.ser.write(data)
        self.tx_bytes += len(data)

    @property
    def is_open(self):
        return self.ser and self.ser.is_open

    def read(self) -> Optional[bytes]:
        if self.ser.is_open and self.ser.readable():
            # eol=None: raw mode for length-framed binary protocols (e.g. basen),
            # where the terminator byte (0x0D) also occurs inside the payload, so
            # read_until would fragment a single frame into many pieces. Wait
            # for one byte, then drain whatever is buffered.
            if self.eol is None:
                data = self.ser.read(1)
                # pyserial's socket:// in_waiting is only ever 0 or 1 (it asks
                # select(), not the buffer), so drain until it says empty rather
                # than reading in_waiting once: that delivered one byte per reader
                # pass, ~10 B/s, and every reply timed out over tcp:// (#422).
                while data and len(data) < _RAW_READ_MAX:
                    n = self.ser.in_waiting
                    if not n:
                        break
                    data += self.ser.read(min(n, _RAW_READ_MAX - len(data)))
            else:
                # read_until(b'\n') is equivalent to readline(); a different `eol`
                # (e.g. b'\r' for paceic) splits frames on that byte instead.
                data = self.ser.read_until(self.eol)
            self.rx_bytes += len(data or b'')
            return data
        return None

class StdioTransport(Transport):

    def __init__(self):
        self.is_open = False
        pass
        #self.port = port
        #self.ser: Optional[serial.Serial] = None

    def open(self):
        self.is_open = True
        pass

    def close(self):
        pass

    def write(self, data: bytes):
        print(data)


    def read(self) -> Optional[bytes]:
        return b''


class SocketTransport(Transport):
    def __init__(self, ip, port):
        self.addr = (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(4)

    def open(self):
        logger.info('connecting to %s:%u', *self.addr)
        self.sock.connect(self.addr)

    def close(self):
        self.sock.close()

    def read(self):
        return self.sock.recv(1024)

    def write(self, data):
        return self.sock.send(data)
