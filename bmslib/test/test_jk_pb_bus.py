"""Attribution of JK-PB frames on a bus with a master (bmslib/uart/jk_pb_bus.py).

The stream is built like the capture of a PB2A16S20P master (address 0) with one slave (address 1):
the master polls 1..15 with two requests each and puts its own status + settings frames on the bus
after the request for address 15; only address 1 answers.
"""
import os
import random

from bmslib.uart.jk_pb_bus import PbBusParser, MASTER_ADDR
from bmslib.uart.modbus_rtu import append_crc

_DATA = os.path.join(os.path.dirname(__file__), 'data')


def _read(name):
    return open(os.path.join(_DATA, name), 'rb').read()


def with_cell_mv(frame: bytes, cell0_mv: int) -> bytes:
    """Same frame with another first cell voltage (and a valid checksum), to tell packs apart."""
    b = bytearray(frame)
    b[6:8] = cell0_mv.to_bytes(2, 'little')
    b[-1] = sum(b[:-1]) & 0xFF
    return bytes(b)


STATUS = _read('jk_issue365_status.bin')
SETTINGS = _read('jk_issue365_settings.bin')
MASTER_STATUS = with_cell_mv(STATUS, 3335)
MASTER_SETTINGS = SETTINGS
SLAVE_STATUS = with_cell_mv(STATUS, 3287)
SLAVE_SETTINGS = SETTINGS


REG_STATUS, REG_SETTINGS = 0x1620, 0x161E


def build_request(addr, reg):
    # written out here instead of imported from the driver, so the parser is checked against the wire bytes
    return append_crc(bytes([addr, 0x10, reg >> 8, reg & 0xFF, 0x00, 0x01, 0x02, 0x00, 0x00]))


def ack(addr, reg):
    return append_crc(bytes([addr, 0x10, reg >> 8, reg & 0xFF, 0x00, 0x01]))


def cycle(acks=False, junk=b''):
    out = b''
    for addr in range(1, 16):
        for reg, reply in ((REG_STATUS, SLAVE_STATUS), (REG_SETTINGS, SLAVE_SETTINGS)):
            out += build_request(addr, reg)
            if addr == 1:
                out += reply + (ack(addr, reg) if acks else b'') + junk
    out += MASTER_STATUS + MASTER_SETTINGS
    return out


def feed_all(stream, chunks=None):
    p = PbBusParser()
    out = []
    if chunks is None:
        out += p.feed(stream)
    else:
        i = 0
        while i < len(stream):
            n = chunks()
            out += p.feed(stream[i:i + n])
            i += n
    return p, out


EXPECTED_ONE_CYCLE = [(1, 0x02, SLAVE_STATUS), (1, 0x01, SLAVE_SETTINGS),
                      (0, 0x02, MASTER_STATUS), (0, 0x01, MASTER_SETTINGS)]


def as_tuples(frames):
    return [(f.owner, f.ftype, f.frame) for f in frames]


def test_master_and_slave_frames_are_told_apart():
    assert MASTER_ADDR == 0
    p, out = feed_all(cycle() * 3)
    assert as_tuples(out) == EXPECTED_ONE_CYCLE * 3
    assert [f.solicited for f in out[:4]] == [True, True, False, False]
    assert p.polled == set(range(1, 16)) and p.owners == {0, 1}
    assert p.requests == 90 and p.corrupt == 0 and p.junk == 0


def test_same_result_for_any_chunking():
    stream = cycle(acks=True) * 3
    _, whole = feed_all(stream)
    for size in (1, 2, 7, 11, 299, 300, 301):
        _, out = feed_all(stream, lambda: size)
        assert as_tuples(out) == as_tuples(whole), size
    rnd = random.Random(1)
    _, out = feed_all(stream, lambda: rnd.randint(1, 400))
    assert as_tuples(out) == as_tuples(whole)


def test_acks_and_junk_between_frames_are_skipped():
    p, out = feed_all(cycle(acks=True, junk=b'AT\r\n') * 2)
    assert as_tuples(out) == EXPECTED_ONE_CYCLE * 2
    assert p.junk == 2 * 2 * 4  # the 'AT\r\n' after each of the 2 frames of a cycle


def test_joining_mid_loop_waits_for_a_request():
    stream = cycle() * 2
    # starts inside the slave's status frame: its tail, then the next request, ...
    first_req = len(build_request(1, REG_STATUS))
    for cut in (first_req + 150, first_req + 299):
        _, out = feed_all(stream[cut:])
        # the cut frame is lost, nothing is attributed before a request has been seen
        assert as_tuples(out) == EXPECTED_ONE_CYCLE[1:] + EXPECTED_ONE_CYCLE, cut


def test_corrupt_frame_is_dropped_and_the_loop_stays_in_step():
    stream = bytearray(cycle() * 2)
    first = stream.index(SLAVE_STATUS[:6])
    stream[first + 100] ^= 0xFF  # corrupts the first slave status frame
    p, out = feed_all(bytes(stream))
    assert as_tuples(out) == EXPECTED_ONE_CYCLE[1:] + EXPECTED_ONE_CYCLE
    assert p.corrupt == 1


def test_master_frame_of_the_settings_type_first_does_not_break_the_next_pair():
    # unlikely order (settings before status), but the status frame must still go to the master
    stream = cycle().replace(MASTER_STATUS + MASTER_SETTINGS, MASTER_SETTINGS + MASTER_STATUS)
    _, out = feed_all(stream * 2)
    owners = [f.owner for f in out]
    # the first frame matches the request for address 15 (settings): attributed to it, which nobody
    # uses; the status frame is the master's. Only the master's settings are lost for that cycle.
    assert owners == [1, 1, 15, 0] * 2
