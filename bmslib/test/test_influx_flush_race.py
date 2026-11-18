"""A shutdown flush may overlap the periodic flusher after its join timeout."""
from concurrent.futures import ThreadPoolExecutor
import queue
import threading

import pytest

pytest.importorskip('influxdb')
from bmslib.sinks import InfluxDBSink


class RacingQueue(queue.Queue):
    """Force both consumers to see the last point before either removes it."""
    def __init__(self):
        super().__init__()
        self.barrier = threading.Barrier(2)
        self.first_checks = set()
        self.check_lock = threading.Lock()

    def empty(self):
        result = super().empty()
        ident = threading.get_ident()
        with self.check_lock:
            first = ident not in self.first_checks
            self.first_checks.add(ident)
        if first:
            self.barrier.wait(timeout=1)
        return result

    def get(self, block=True, timeout=None):
        # Bound the old blocking read so the regression cannot hang pytest.
        return super().get(block=block, timeout=.1 if block else None)


@pytest.fixture
def sink():
    result = InfluxDBSink(host='localhost', database='test', flush_interval=3600)
    result._stop_event.set()
    result._flush_thread.join(timeout=1)
    yield result
    result.influxdb_client.close()


def test_overlapping_flushes_do_not_block_on_an_already_drained_queue(sink):
    sink.Q = RacingQueue()
    point = {'measurement': 'test', 'fields': {'voltage': 12}, 'tags': {}}
    sink.Q.put(point)
    sent = []
    def write(batch, **kwargs):
        sent.extend(batch)
        return True
    sink.influxdb_client.write_points = write
    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = [workers.submit(sink.flush) for _ in range(2)]
        errors = [job.exception(timeout=2) for job in jobs]
    assert errors == [None, None]
    assert sent == [point]


def test_flush_retains_batch_limit_and_remainder(sink):
    for i in range(20001):
        sink.Q.put({'measurement': 'test', 'fields': {'v': i}, 'tags': {}})
    batches = []
    def write(batch, **kwargs):
        batches.append(batch)
        return True
    sink.influxdb_client.write_points = write
    sink.flush()
    assert len(batches[0]) == 20000
    assert sink.Q.qsize() == 1
    sink.flush()
    assert len(batches[1]) == 1
    assert sink.Q.qsize() == 0


def test_empty_flush_does_not_write(sink):
    def unexpected_write(*args, **kwargs):
        pytest.fail('empty queue was sent')
    sink.influxdb_client.write_points = unexpected_write
    sink.flush()
