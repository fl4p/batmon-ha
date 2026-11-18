"""A fake BMS and a recording sink for sampler-level tests.

Enough of BtBms and BmsSampleSink for BmsSampler to run one cycle with no radio and
no broker, so a test can drive a real sampler instead of calling a private method.
Same pattern as _decode_helpers.py.
"""
from bmslib.bms import BmsSample
from bmslib.sampling import BmsSampler


class FakeBms:
    name = 'fake'
    address = 'AA:BB:CC:DD:EE:FF'
    is_virtual = False
    is_connected = True
    connect_time = 0
    verbose_log = False

    def __init__(self, sample=None):
        self.sample = sample or (lambda: BmsSample(13.0, 1.0))
        self.disconnects = 0

    def __str__(self):
        return 'Bms(fake)'

    def debug_data(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        return self.sample()

    async def fetch_voltages(self):
        return [3300, 3301]

    async def fetch_temperatures(self):
        return []

    async def fetch_device_info(self):
        return None

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


class RecordingSink:
    """Records every publish_* the sampler makes, for assertions."""
    wants_voltages_every_sample = False

    def __init__(self):
        self.samples = []
        self.voltages = []
        self.meters = []
        self.capacity = []
        self.estimates = []

    def publish_sample(self, bms_name, sample, tags=None):
        self.samples.append(sample)

    def publish_voltages(self, bms_name, voltages, short=False, tags=None):
        self.voltages.append(voltages)

    def publish_meters(self, bms_name, readings):
        self.meters.append(readings)

    def publish_cell_capacity(self, bms_name, result):
        self.capacity.append(result)

    def publish_estimates(self, bms_name, estimates):
        self.estimates.append(estimates)


def make_sampler(sink=None, bms=None, **kwargs):
    return BmsSampler(bms or FakeBms(), mqtt_client=None, dt_max_seconds=120,
                      expire_after_seconds=60,
                      sinks=[sink] if sink is not None else None, **kwargs)
