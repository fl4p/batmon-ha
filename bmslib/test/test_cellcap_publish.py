"""The cell capacity estimator's result reaches a sink, including the reason it was
switched off: a restored-disabled estimator must say why it is off rather than stay
silent, which is what the GUI shows (qmax has _withdraw_qmax() for the same job).
"""
import asyncio

from bmslib.bms import BmsSample
from bmslib.sampling import BmsSampler


class _Bms:
    name = 'fake'
    address = 'AA:BB:CC:DD:EE:FF'
    is_virtual = False
    is_connected = True
    connect_time = 0
    verbose_log = False

    def __str__(self):
        return 'Bms(fake)'

    def debug_data(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        return BmsSample(13.0, 1.0)

    async def fetch_voltages(self):
        return [3300, 3301]

    async def fetch_device_info(self):
        return None

    async def disconnect(self):
        self.is_connected = False


class _Sink:
    wants_voltages_every_sample = False

    def __init__(self):
        self.capacity = []

    def publish_sample(self, bms_name, sample, tags=None):
        pass

    def publish_voltages(self, bms_name, voltages, short=False, tags=None):
        pass

    def publish_meters(self, bms_name, readings):
        pass

    def publish_cell_capacity(self, bms_name, result):
        self.capacity.append(result)


def _sampler(sink):
    return BmsSampler(_Bms(), mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60,
                      sinks=[sink], cell_capacity_estimator=True, design_capacity=280)


def test_a_disabled_estimator_publishes_why_it_is_off():
    sink = _Sink()
    sampler = _sampler(sink)
    sampler.cellcap.disable('chemistry verdict', persistent=True)

    asyncio.run(sampler())
    assert sink.capacity == [dict(disabled='chemistry verdict')]

    asyncio.run(sampler())  # once, not on every sample
    assert sink.capacity == [dict(disabled='chemistry verdict')]
