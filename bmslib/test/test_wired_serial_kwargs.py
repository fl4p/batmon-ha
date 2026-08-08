"""Regression tests for the serial-transport wiring (#398).

Every wired BMS model declares its framing via ``SERIAL_KWARGS`` (``eol`` /
``timeout``). Until #398 ``BtBms.__init__`` passed only ``BAUDRATE`` to
``SerialBleakClientWrapper``, so those declarations were dead code and every
binary-framed model silently ran with the defaults ``eol=b'\\n', timeout=None``
— which parks the reader thread in ``read_until(b'\\n')`` forever whenever a
response frame contains no 0x0A. Symptom: ``timeout awaiting result for
cmd=0x93, got 0/1 responses`` on every single poll.
"""
import pytest

import bmslib.wired
from bmslib.models.basen_uart import BasenUart
from bmslib.models.daly_uart import DalyUart
from bmslib.models.pace import PaceUart


class _CapturingWrapper:
    """Stand-in for SerialBleakClientWrapper (which would spawn a reader
    thread and touch pyserial); records what BtBms handed it."""
    last = None

    def __init__(self, address, **kwargs):
        _CapturingWrapper.last = dict(address=address, **kwargs)


@pytest.fixture
def captured(monkeypatch):
    monkeypatch.setattr(bmslib.wired, 'SerialBleakClientWrapper', _CapturingWrapper)
    _CapturingWrapper.last = None
    return _CapturingWrapper


@pytest.mark.parametrize("cls", [DalyUart, BasenUart, PaceUart])
def test_serial_kwargs_reach_the_transport(captured, cls):
    cls('serial', name='t', adapter='/dev/ttyUSB0')
    got = captured.last
    assert got is not None, "BtBms did not construct the serial wrapper"
    assert got['baudrate'] == cls.BAUDRATE
    for k, v in cls.SERIAL_KWARGS.items():
        assert got.get(k, '<missing>') == v, (
            f"{cls.__name__}.SERIAL_KWARGS[{k!r}] never reached the transport"
        )


def test_daly_uart_does_not_frame_on_newline():
    """Daly frames are fixed-length binary; splitting on 0x0A is wrong and,
    with a blocking timeout, fatal. Raw mode + a finite timeout is the only
    combination that works — feed_buffer() does the framing."""
    assert DalyUart.SERIAL_KWARGS['eol'] is None
    assert DalyUart.SERIAL_KWARGS['timeout'] is not None
    assert DalyUart.SERIAL_KWARGS['timeout'] > 0


def test_model_without_serial_kwargs_still_works(captured):
    """Models that don't declare SERIAL_KWARGS (e.g. JK UART) must keep the
    historical readline defaults, not crash on the new forwarding."""
    from bmslib.models.jikong_uart import JKUart
    assert not hasattr(JKUart, 'SERIAL_KWARGS')
    JKUart('serial', name='t', adapter='/dev/ttyUSB0')
    assert 'eol' not in captured.last
    assert 'timeout' not in captured.last
