import glob
import socket
from typing import Optional

import serial  # pyserial

from bmslib.util import get_logger

logger = get_logger()


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

    def __init__(self, port):
        self.port = port
        self.ser: Optional[serial.Serial] = None

    def open(self):
        port = self.port
        if '*' in port:
            files = glob.glob(port)
            if not files:
                raise FileNotFoundError('Serial port device not found: {}'.format(port))
            port =files[0]
        logger.info(f'opening serial port {port}')
        self.ser = serial.Serial(port, baudrate=115200)

    def close(self):
        if self.ser is not None:
            self.ser.close()

    def write(self, data: bytes):
        self.ser.write(data)

    @property
    def is_open(self):
        return self.ser and self.ser.is_open

    def read(self) -> Optional[bytes]:
        if self.ser.is_open and self.ser.readable():
            return self.ser.readline()
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
