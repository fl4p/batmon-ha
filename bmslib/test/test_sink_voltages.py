"""Cell voltages for sinks: a sink with wants_voltages_every_sample=False (the GUI)
must not cause a per-sample fetch; it gets the voltages the sampler fetched anyway."""
import asyncio
import time

import pytest

import bmslib.mqtt_util as mqtt_util
from bmslib.bms import BmsSample
from bmslib.gui.state import GuiState, GuiStateSink
from bmslib.sampling import BmsSampler, BmsSampleSink


@pytest.fixture(autouse=True)
def _clean_state_callbacks(monkeypatch):
    monkeypatch.setattr(mqtt_util, '_state_callbacks', {})


class _Bms:
    name = 'sv_fake'
    address = 'serial'
    is_virtual = False
    is_connected = True
    connect_time = 0
    verbose_log = False

    def __init__(self):
        self.k = 0
        self.n_voltage_fetches = 0
        self.t0 = time.time()

    def __str__(self):
        return 'FakeBms(sv)'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        self.k += 1
        return BmsSample(voltage=53.0, current=10.0, soc=60.0, timestamp=self.t0 + self.k)

    async def fetch_voltages(self):
        self.n_voltage_fetches += 1
        return [3300] * 16

    def debug_data(self):
        return None


class _Sink(BmsSampleSink):
    def __init__(self, every):
        self.wants_voltages_every_sample = every
        self.voltages = []

    def publish_sample(self, bms_name, sample, tags=None):
        pass

    def publish_voltages(self, bms_name, voltages):
        self.voltages.append(voltages)

    def publish_meters(self, bms_name, readings):
        pass


def _run(sinks, n=3):
    bms = _Bms()
    # publish_period long: only the first cycle publishes to MQTT (and fetches voltages for it)
    s = BmsSampler(bms, mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60,
                   publish_period=3600, sinks=sinks)
    s.num_samples = 1
    s._last_power = 530
    for _ in range(n):
        asyncio.run(s())
    return bms


def test_lazy_sink_adds_no_voltage_fetches():
    sink = _Sink(every=False)
    bms = _run([sink])
    assert bms.n_voltage_fetches == 1  # the MQTT publish cycle only
    assert sink.voltages == [[3300] * 16]  # and the sink got that fetch


def test_gui_sink_adds_no_voltage_fetches():
    state = GuiState(app_version='test', runtime={})
    bms = _run([GuiStateSink(state)])
    assert bms.n_voltage_fetches == 1
    assert state._voltages.get('sv_fake') == [3300] * 16


def test_eager_sink_fetches_every_sample_and_lazy_one_shares_it():
    eager, lazy = _Sink(every=True), _Sink(every=False)
    bms = _run([eager, lazy])
    assert bms.n_voltage_fetches == 3
    assert len(eager.voltages) == 3 and len(lazy.voltages) == 3
