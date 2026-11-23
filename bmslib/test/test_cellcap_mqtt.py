"""The cell capacity estimator in Home Assistant: per-cell capacity, top imbalance and what limits the
pack (bmslib/mqtt_util.py publish_cell_capacity), discovered with the other entities."""
import asyncio
import json

import paho.mqtt.client as paho
import pytest

from bmslib import mqtt_util
from bmslib.bms import BmsSample
from bmslib.estimators.cell_capacity import CellCapacityEstimator
from bmslib.mqtt_util import capacity_limit_text, publish_cell_capacity, publish_hass_discovery
from bmslib.test._sampler_helpers import RecordingSink, make_sampler
from bmslib.test.test_cell_capacity_progress import CAPS, Q0, R, run
from bmslib.test.test_cell_capacity import Pack


@pytest.fixture(autouse=True)
def _fresh_mqtt_state(monkeypatch):
    monkeypatch.setattr(mqtt_util, '_last_values', {})


class _Recorder:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload

        class R:
            rc = paho.MQTT_ERR_SUCCESS
        return R()


DEV = 'test/ant'


def test_discovery():
    c = _Recorder()
    publish_hass_discovery(c, DEV, 30, BmsSample(13.2, 1.0), 4, [], cell_capacity=True)
    node = 'homeassistant/sensor/test_ant'
    for k in range(1, 5):
        d = json.loads(c.published[f'{node}/_cell_capacity_{k}/config'])
        assert d['state_topic'] == f'{DEV}/cell_capacity/{k}'
        assert d['json_attributes_topic'] == f'{DEV}/cell_capacity/{k}/attributes'
        assert d['unit_of_measurement'] == 'Ah' and d['name'] == 'Cell Capacity %d' % k
        assert d['expire_after'] == mqtt_util.CELL_CAPACITY_EXPIRE_S
    imb = json.loads(c.published[f'{node}/_cell_imbalance/config'])
    assert imb['unit_of_measurement'] == 'Ah' and imb['state_topic'] == f'{DEV}/cell_imbalance'
    lim = json.loads(c.published[f'{node}/_capacity_limit/config'])
    assert 'unit_of_measurement' not in lim and 'state_class' not in lim  # a text sensor
    assert lim['json_attributes_topic'] == f'{DEV}/capacity_limit/attributes'

    off = _Recorder()
    publish_hass_discovery(off, DEV, 30, BmsSample(13.2, 1.0), 4, [])
    assert not any('cell_capacity' in t or 'capacity_limit' in t or 'cell_imbalance' in t for t in off.published)


def test_states_of_a_known_pack():
    res = run(CellCapacityEstimator('t', capacity=100), Pack(CAPS, Q0, R)).result()
    c = _Recorder()
    publish_cell_capacity(c, DEV, res, 4)
    p = c.published
    for k in range(4):
        assert p[f'{DEV}/cell_capacity/{k + 1}'] == res['cells'][k]['ah']
        a = json.loads(p[f'{DEV}/cell_capacity/{k + 1}/attributes'])
        assert a['sigma'] == res['cells'][k]['sigma'] and a['bound'] == res['cells'][k]['bound']
        assert a['soh'] == res['cells'][k]['soh']
        assert a['top_lead'] == res['top_ah'][k] and a['bottom_lag'] == res['bottom_ah'][k]
    assert p[f'{DEV}/cell_imbalance'] == res['imbalance']['ah'] == 0.9
    a = json.loads(p[f'{DEV}/cell_imbalance/attributes'])
    assert a['cell'] == 4 and a['top_leads'] == [x['v'] for x in res['top_ah']] and a['n_top'] == 3
    assert p[f'{DEV}/capacity_limit'] == 'weakest cell 1'
    a = json.loads(p[f'{DEV}/capacity_limit/attributes'])
    assert a['by'] == 'weakest_cell' and a['recoverable_ah'] == 0.0 and a['pack_ah'] == res['pack_ah']


def test_unknown_goes_out_as_none():
    """Collecting (no cycle yet), disabled, or no result: every value unknown, the per-cell ones too."""
    for res in (dict(n_top=0, n_bottom=0, n_cycle=0), dict(disabled='chemistry verdict'), None):
        mqtt_util._last_values.clear()  # each case on its own, past the dedup
        c = _Recorder()
        publish_cell_capacity(c, DEV, res, 2)
        assert all(c.published[f'{DEV}/{k}'] == 'None'
                   for k in ('cell_capacity/1', 'cell_capacity/2', 'cell_imbalance', 'capacity_limit'))


def test_limit_text():
    assert capacity_limit_text(dict(by='weakest_cell', cell=3, recoverable_ah=0.2)) == 'weakest cell 3'
    assert capacity_limit_text(dict(by='imbalance', recoverable_ah=3.04, recoverable_sigma=0.5)) == \
        'imbalance, 3.0 Ah recoverable'
    assert capacity_limit_text(dict(by='imbalance', recoverable_ah=5.0, bound=True)) == \
        'imbalance, ≥5.0 Ah recoverable'
    assert capacity_limit_text(dict(by='imbalance', recoverable_ah=4.0, recoverable_min_ah=1.6)) == \
        'imbalance, 1.6–4.0 Ah recoverable'
    assert capacity_limit_text(dict(by='undecided', recoverable_ah=1.4)) == 'undecided'


def test_the_sampler_discovers_and_publishes_them():
    sink = RecordingSink()
    sampler = make_sampler(sink, cell_capacity_estimator=True, design_capacity=280)
    c = _Recorder()
    sampler.mqtt_client = c
    asyncio.run(sampler())  # the first sample also sends the discovery
    assert sampler.cellcap.n_cells == 2  # the fake BMS's two cells
    assert 'homeassistant/sensor/fake/_capacity_limit/config' in c.published
    assert 'homeassistant/sensor/fake/_cell_capacity_2/config' in c.published
    assert c.published['fake/capacity_limit'] == 'None' and c.published['fake/cell_capacity/2'] == 'None'

    # a changed result goes out at once, without waiting for the next discovery
    est = sampler.cellcap
    est.tops.append(dict(t=1.0, epoch=est.epoch, ref=0, q_cut=0.0, cells=[dict(v=0.0), dict(v=2.5)]))
    est.rev += 1
    asyncio.run(sampler())
    assert c.published['fake/cell_imbalance'] == 2.5
    assert json.loads(c.published['fake/cell_imbalance/attributes'])['cell'] == 2

    # unchanged, it goes out again after every discovery: entities HA just created need a state
    c.published.clear()
    sampler.period_discov.state = True
    asyncio.run(sampler())
    assert 'fake/cell_imbalance' not in c.published  # not with the configs: HA may not have subscribed yet
    asyncio.run(sampler())
    assert 'fake/meter/sample_count' in c.published  # the discovery cycle ran
    assert c.published['fake/cell_imbalance'] == 2.5


def test_a_disabled_estimator_lets_its_entities_expire():
    """Off: "None" once, then nothing, so expire_after takes the entities to unavailable."""
    sampler = make_sampler(RecordingSink(), cell_capacity_estimator=True, design_capacity=280)
    c = _Recorder()
    sampler.mqtt_client = c
    asyncio.run(sampler())
    sampler.cellcap.disable('chemistry verdict')
    asyncio.run(sampler())
    assert c.published['fake/capacity_limit'] == 'None'
    c.published.clear()
    for _ in range(2):
        sampler.period_discov.state = True
        asyncio.run(sampler())
    assert not [t for t in c.published if 'capacity' in t or 'imbalance' in t]


def test_no_expiry_with_expire_values_after_0():
    c = _Recorder()
    publish_hass_discovery(c, DEV, 0, BmsSample(13.2, 1.0), 2, [], cell_capacity=True)
    for k in ('_cell_capacity_1', '_cell_imbalance', '_capacity_limit'):
        assert not json.loads(c.published[f'homeassistant/sensor/test_ant/{k}/config']).get('expire_after')
