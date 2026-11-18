import asyncio
import json
import math

import pytest

from bmslib.bms import BmsSample
from bmslib.estimators import cell_capacity
from bmslib.gui.state import GuiState, GuiStateSink, link_status
from bmslib.gui import server as gsrv
from bmslib.test._sampler_helpers import make_sampler
from bmslib.wire import model, topology


# ---------------- topology ----------------

NODES = [
    dict(id='solo', kind='pack'),
    dict(id='p1', kind='pack'),
    dict(id='p2', kind='pack'),
    dict(id='p3', kind='pack'),
    dict(id='bank', kind='group', group_kind='parallel', members=['p1', 'p2']),
    dict(id='str', kind='group', group_kind='series', members=['p3']),
]


def test_topology_roots_and_edges():
    t = topology.build(NODES)
    assert t['roots'] == ['solo', 'bank', 'str']
    assert [(e['parent'], e['child'], e['kind'], e['index']) for e in t['edges']] == [
        ('bank', 'p1', 'parallel', 0),
        ('bank', 'p2', 'parallel', 1),
        ('str', 'p3', 'series', 0),
    ]
    # members are not duplicated into the node objects
    assert all('members' not in n for n in t['nodes'])


def test_topology_rejects_nesting():
    nodes = [dict(id='a', kind='pack'),
             dict(id='inner', kind='group', group_kind='parallel', members=['a']),
             dict(id='outer', kind='group', group_kind='parallel', members=['inner'])]
    with pytest.raises(ValueError, match='one level deep'):
        topology.build(nodes)


def test_topology_rejects_two_parents():
    nodes = [dict(id='a', kind='pack'),
             dict(id='g1', kind='group', group_kind='parallel', members=['a']),
             dict(id='g2', kind='group', group_kind='parallel', members=['a'])]
    with pytest.raises(ValueError, match='two groups'):
        topology.build(nodes)


def test_topology_rejects_unknown_member():
    with pytest.raises(ValueError, match='unknown member'):
        topology.build([dict(id='g', kind='group', members=['nope'])])


# ---------------- field catalog ----------------

def test_catalog_covers_sample_desc_and_meters():
    from bmslib.wire.fields import sample_desc, meter_desc
    cat = model.field_catalog()
    for d in sample_desc.values():
        assert d['field'] in cat, d['field']
    for name in meter_desc:
        assert 'meter:' + name in cat
    for k, v in cat.items():
        assert 'label' in v and v['label'], k
        assert 'unit' in v, k


def test_catalog_defaults_significant_digits():
    """11 of 16 sample_desc entries have no significant_digits; indexing would
    raise KeyError on ordinary fields such as capacity."""
    cat = model.field_catalog()
    assert cat['capacity']['significant_digits'] == 5
    assert cat['voltage']['significant_digits'] == 4


# ---------------- values / nan encoding ----------------

def test_values_omit_unknown_scalars_and_are_numbers():
    s = BmsSample(voltage=12.345678, current=1.5, capacity=100, charge=50)
    v = model.sample_to_values(s)
    assert isinstance(v['voltage'], float) and isinstance(v['capacity'], int)
    # unknown scalars must be ABSENT, never null
    assert 'balance_current' not in v
    assert 'mos_temperature' not in v
    raw = json.dumps(v)
    assert 'null' not in raw and 'NaN' not in raw


def test_encode_rejects_nan():
    with pytest.raises(ValueError):
        model.encode({'x': float('nan')})
    with pytest.raises(ValueError):
        model.encode({'x': float('inf')})


def test_arrays_carry_null_for_unknown():
    st = model.node_state('n', voltages_mv=[3000, None, 3020])
    assert st['cells_mv'] == [3000, None, 3020]
    # cell_stats computed over known values only
    assert st['cell_stats']['min_mv'] == 3000


def test_config_hash_excludes_itself_and_is_stable():
    d1 = model.system_document(NODES, 'p', '1', 'standalone')['data']
    d2 = model.system_document(NODES, 'p', '1', 'standalone')['data']
    assert d1['config_hash'] == d2['config_hash']
    # recomputing over the data WITHOUT config_hash reproduces it -> it did not hash itself
    import hashlib
    bare = {k: v for k, v in d1.items() if k != 'config_hash'}
    assert hashlib.sha1(model._canonical(bare).encode()).hexdigest()[:12] == d1['config_hash']
    # and it changes when topology changes
    d3 = model.system_document(NODES + [dict(id='x', kind='pack')], 'p', '1', 'standalone')['data']
    assert d3['config_hash'] != d1['config_hash']


def test_state_document_round_trips():
    s = BmsSample(voltage=12.0, current=1.0, capacity=100, charge=50)
    doc = model.state_document({'n': model.node_state('n', sample=s)}, partial=True)
    back = json.loads(model.encode(doc))
    assert back['type'] == 'state' and back['data']['partial'] is True


# ---------------- link status ----------------

NOW = 1000.0


def _st(**kw):
    base = dict(connected=True, num_samples=5, num_errors=0, t_next_retry=0,
                last_error=None, last_error_type=None, is_virtual=False,
                debug_data=None, expire_after=20)
    base.update(kw)
    return base


@pytest.mark.parametrize('status,ts,expect', [
    (None, None, 'never'),
    (_st(num_samples=0), None, 'never'),
    (_st(is_virtual=True, last_error_type='GroupNotReady', num_errors=1), NOW, 'waiting'),
    (_st(num_errors=3, last_error='boom'), NOW, 'error'),
    (_st(connected=False, t_next_retry=NOW + 38), NOW, 'connecting'),
    (_st(connected=False), NOW, 'disconnected'),
    (_st(), NOW - 100, 'stale'),
    (_st(), NOW, 'connected'),
])
def test_link_status_table(status, ts, expect):
    assert link_status(status, ts, NOW, 20)['status'] == expect


def test_link_status_precedence_error_beats_disconnected():
    """A disconnected node that is also erroring reports the error: the text is
    the actionable part."""
    s = _st(connected=False, num_errors=2, last_error='no route')
    assert link_status(s, NOW, NOW, 20)['status'] == 'error'


def test_link_status_omits_detail_it_does_not_have():
    """A node can error, or wait on its group, before there is any text to show. The key
    is then absent rather than null: an unknown scalar is an absent key (wire rule 1).
    The GUI reads it as `if (link.detail)`, so both render the same."""
    waiting = link_status(_st(is_virtual=True, last_error_type='GroupNotReady', num_errors=1),
                          NOW, NOW, 20)
    assert waiting['status'] == 'waiting' and 'detail' not in waiting

    erroring = link_status(_st(num_errors=1), NOW, NOW, 20)
    assert erroring['status'] == 'error' and 'detail' not in erroring

    # and it is still there when there is something to say
    assert link_status(_st(num_errors=1, last_error='boom'), NOW, NOW, 20)['detail'] == 'boom'


# ---------------- GuiState / sink ----------------

def test_sink_updates_and_change_tracking():
    st = GuiState(app_version='t', runtime='standalone')
    st.register_node('p1', kind='pack')
    st.register_node('p2', kind='pack')
    sink = GuiStateSink(st)
    assert sink.wants_voltages_every_sample is False   # GUI adds no BLE traffic

    rev = st.revision
    sink.publish_sample('p1', BmsSample(voltage=12.0, current=1.0))
    sink.publish_voltages('p1', [3000, 3010])
    assert st.changed_since(rev) == {'p1'}

    doc = st.state_doc(partial=False)['data']
    assert doc['nodes']['p1']['values']['voltage'] == 12.0
    assert doc['nodes']['p1']['cells_mv'] == [3000, 3010]
    # untouched node still renders, with a link status
    assert doc['nodes']['p2']['link']['status'] == 'never'


def test_cell_capacity_reaches_the_node_document():
    st = GuiState(app_version='t', runtime='standalone')
    st.register_node('p1', kind='pack')
    sink = GuiStateSink(st)
    rev = st.revision
    res = dict(n_top=2, n_bottom=1, n_cycle=1, pack_ah=240.0, pack_sigma=1.0,
               cells=[dict(ah=241.5, sigma=3.0, bound=False), None, dict(ah=263.0, sigma=0.0, bound=True)])
    sink.publish_cell_capacity('p1', res)
    assert st.changed_since(rev) == {'p1'}
    assert st.state_doc()['data']['nodes']['p1']['cell_capacity'] == res
    sink.publish_cell_capacity('p1', None)  # estimator disabled: nothing shown
    assert 'cell_capacity' not in st.state_doc()['data']['nodes']['p1']


def test_estimates_reach_the_node_document():
    st = GuiState(app_version='t', runtime='standalone')
    st.register_node('p1', kind='pack')
    sink = GuiStateSink(st)
    est = dict(soh=dict(qmax=95.2, soh=95.2, capacity=100.0, segments=4, newest_t=1.7e9, limiting_cell=3),
               cell_resistance=dict(mohm=0.42, windows=12), pack_temp=dict(c=18.3),
               soc_gauge_error=dict(pp=-4.1, anchor='top', t=1.7e9, bms_soc=95.9, soc=100.0))
    rev = st.revision
    sink.publish_estimates('p1', est)
    assert st.changed_since(rev) == {'p1'}
    assert st.state_doc()['data']['nodes']['p1']['estimates'] == est
    sink.publish_estimates('p1', {})  # all withdrawn
    assert 'estimates' not in st.state_doc()['data']['nodes']['p1']


def test_state_never_raises_from_a_bad_update():
    """A sink must not be able to break sampling."""
    st = GuiState(app_version='t', runtime='standalone')
    st.register_node('p1', kind='pack')
    st.update_voltages('p1', object())      # not iterable
    st.update_meters('p1', object())
    assert 'p1' in st.node_ids()


def test_system_doc_has_topology_and_fields():
    st = GuiState(app_version='2.21', runtime='standalone')
    st.register_node('p1', kind='pack')
    st.register_node('g', kind='group', group_kind='series', members=['p1'])
    d = st.system_doc()['data']
    assert d['roots'] == ['g']
    assert d['edges'][0]['kind'] == 'series'
    assert 'voltage' in d['fields']


# ---------------- websocket framing ----------------

def test_frame_roundtrip_small_and_large():
    import asyncio

    async def rt(payload):
        frame = gsrv.encode_frame(payload)
        # re-mask it as a client would, then parse
        import os as _os
        mask = _os.urandom(4)
        masked = bytearray(frame[2:] if frame[1] < 126 else
                           (frame[4:] if frame[1] == 126 else frame[10:]))
        for i in range(len(masked)):
            masked[i] ^= mask[i & 3]
        n = len(payload)
        if n < 126:
            hdr = bytes([0x81, 0x80 | n])
        elif n < (1 << 16):
            import struct
            hdr = bytes([0x81, 0x80 | 126]) + struct.pack('!H', n)
        else:
            import struct
            hdr = bytes([0x81, 0x80 | 127]) + struct.pack('!Q', n)
        r = asyncio.StreamReader()
        r.feed_data(hdr + mask + bytes(masked))
        r.feed_eof()
        fin, op, data = await gsrv.read_frame(r)
        assert fin and op == gsrv.OP_TEXT
        return data

    assert asyncio.run(rt(b'hello')) == b'hello'
    big = b'x' * 70000
    assert asyncio.run(rt(big)) == big


def test_unmasked_client_frame_is_rejected():
    """RFC 6455 5.1: a client frame MUST be masked."""
    import asyncio

    async def go():
        r = asyncio.StreamReader()          # needs a running loop
        r.feed_data(bytes([0x81, 0x05]) + b'hello')   # mask bit clear
        r.feed_eof()
        return await gsrv.read_frame(r)

    with pytest.raises(gsrv.WsProtocolError, match='not masked'):
        asyncio.run(go())


def test_oversized_control_frame_is_rejected():
    import asyncio, os as _os

    async def go():
        r = asyncio.StreamReader()
        r.feed_data(bytes([0x89, 0x80 | 126]) + b'\x00\xff' + _os.urandom(4) + b'x' * 255)
        r.feed_eof()
        return await gsrv.read_frame(r)

    with pytest.raises(gsrv.WsProtocolError, match='control frame too long'):
        asyncio.run(go())


def test_accept_key_matches_rfc_example():
    """RFC 6455 1.3 worked example."""
    assert gsrv._accept_key('dGhlIHNhbXBsZSBub25jZQ==') == 's3pPLMBiTxaQ9kYGzzhZRbK+xOo='


# ---------------- websocket origin (CSWSH) ----------------

def test_origin_same_host_allowed():
    assert gsrv.GuiServer._origin_ok(
        {'origin': 'http://192.168.1.5:8099', 'host': '192.168.1.5:8099'})


def test_origin_cross_site_refused():
    """Browsers do not apply same-origin policy to WebSocket handshakes, so
    without this an attacker page could read the whole state stream."""
    assert not gsrv.GuiServer._origin_ok(
        {'origin': 'https://evil.example', 'host': '192.168.1.5:8099'})
    # a look-alike prefix must not pass either
    assert not gsrv.GuiServer._origin_ok(
        {'origin': 'http://192.168.1.5:8099.evil.example', 'host': '192.168.1.5:8099'})


def test_origin_absent_allowed_for_non_browser_clients():
    """curl / scripts / the mobile app send no Origin and are not subject to
    CSWSH -- an attacker who can set arbitrary headers needs no victim browser."""
    assert gsrv.GuiServer._origin_ok({'host': '192.168.1.5:8099'})


def test_origin_ingress_allowed_despite_mismatch():
    """Under HA ingress the browser sends the frontend origin while Host is the
    add-on's, so they legitimately differ. A page cannot set custom headers on a
    WebSocket handshake, so X-Ingress-Path cannot be forged by an attacking page."""
    assert gsrv.GuiServer._origin_ok({
        'origin': 'https://ha.local:8123', 'host': '172.30.32.1:8099',
        'x-ingress-path': '/api/hassio_ingress/abc'})


# ---------------- UI escaping ----------------

def test_ui_escapes_every_interpolation():
    """Device names and error text originate off-box (any BLE device in range can
    advertise any name) and reach innerHTML."""
    import pathlib, re
    html = pathlib.Path(__file__).resolve().parents[1] / 'gui' / 'web' / 'index.html'
    src = html.read_text()
    assert 'const esc =' in src
    bad = [l.strip() for l in src.splitlines()
           if '${' in l and 'esc(' not in l and '${cls}' not in l]
    assert not bad, "unescaped interpolation reaching innerHTML: %s" % bad


def test_a_sample_in_flight_is_not_an_error():
    """__call__ counts the cycle as failed until it succeeds; the GUI read that
    mid-cycle as an error with no detail."""
    import asyncio
    from bmslib.sampling import BmsSampler
    from bmslib.test.test_qmax import _Bms
    s = BmsSampler(_Bms(), mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60)
    seen = []

    async def inner():
        seen.append(link_status(s.status(), None, 0, 0))
        return object()

    s._sample_inner = inner
    asyncio.run(s())
    assert seen[0].get('errors') == 0 and seen[0]['status'] != 'error'

    async def failing():
        raise RuntimeError('boom')

    s._sample_inner = failing
    s.num_samples = 1  # else it is 'never'
    with pytest.raises(RuntimeError):  # the caller handles it
        asyncio.run(s())
    st = link_status(s.status(), None, 0, 0)
    assert st['status'] == 'error' and st['errors'] == 1 and 'boom' in st['detail']


# The wire rule (wire/model.py, doc/GUI.md): an unknown SCALAR is an absent key, never
# null. Only a positional ARRAY may hold a null, because the index is the cell or sensor
# number and an array cannot have holes. The arrays are declared, so this reads them from
# the model rather than listing names: extra_desc entries marked array, plus the arrays
# CellCapacityEstimator.result() emits.
POSITIONAL_ARRAYS = ({name for name, desc in model.extra_desc.items() if desc.get('array')}
                     | set(cell_capacity.RESULT_ARRAYS))


def _null_violations(node, path=(), positional=False):
    """Paths where the document carries a null it must not.

    A dict value that is None is always a violation -- that is the scalar case the rule
    forbids. A list element may be None only when the array it sits in is declared
    positional, which is checked by the key holding the list, not by the path.
    """
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if v is None:
                out.append(path + (k,))
            else:
                out.extend(_null_violations(v, path + (k,), k in POSITIONAL_ARRAYS))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if v is None:
                if not positional:
                    out.append(path + (i,))
            else:
                out.extend(_null_violations(v, path + (i,), positional))
    return out


def test_the_document_carries_no_null_scalars():
    """Driven through a real sampler with every estimator on and no `capacity:` option,
    which is where the optional fields (capacity, soh) stay unknown. The cell capacity
    estimator first emitted `capacity: null` here, and `_estimates()` passed `soh: null`
    through the same way."""
    st = GuiState(app_version='t', runtime='standalone')
    st.register_node('fake', kind='pack')
    sampler = make_sampler(GuiStateSink(st), soh_estimator=True, impedance_estimator=True,
                           cell_capacity_estimator=True)  # no design_capacity on purpose
    asyncio.run(sampler())

    doc = st.state_doc()['data']
    assert doc['nodes']['fake']['values']['voltage'] == 13.0  # the sample did reach the doc
    offenders = _null_violations(doc)
    assert offenders == [], 'an unknown scalar must be an absent key, not null: %s' % (offenders,)


def test_a_rich_document_may_only_hold_nulls_in_positional_arrays():
    """The fixture the rule is really about: every array that legitimately has holes, plus
    the per-cell and per-term values a real pack eventually produces. A NaN probe is a
    null inside temperatures_c, and top_ah/bottom_ah hold a null per term that is not
    known yet -- all three are positional. Anything else is a violation."""
    st = GuiState(app_version='t', runtime='standalone')
    st.register_node('p1', kind='pack')
    sink = GuiStateSink(st)
    sink.publish_sample('p1', BmsSample(13.0, 1.0, temperatures=[20.0, math.nan, 21.0]))
    sink.publish_voltages('p1', [3300, None, 3302])
    sink.publish_cell_capacity('p1', dict(
        n_top=2, n_bottom=1, n_cycle=1, capacity=280.0,
        cells=[dict(ah=240.0, sigma=1.0, bound=False, soh=85.7, soh_sigma=0.4), None],
        top_ah=[dict(v=0.5, s=0.1, bound=False), None],
        bottom_ah=[None, dict(v=-0.4, s=0.2, bound=True)]))
    # a node that is erroring before it has any error text: link.detail must be absent,
    # not null (link_status used to set it to None)
    st.set_status_source(lambda node_id: dict(num_errors=1, num_samples=3))

    doc = st.state_doc()['data']
    node = doc['nodes']['p1']
    # the fixture really does carry the nulls the rule has to tolerate
    assert None in node['temperatures_c'] and None in node['cells_mv']
    assert None in node['cell_capacity']['top_ah']
    assert _null_violations(doc) == []


def test_estimates_omit_unknown_scalars(monkeypatch):
    """_estimates() is the other producer of these dicts: it copies fields out of
    qmax.result(), and on a device without a `capacity:` option soh and capacity are
    None there. They must arrive as absent keys, not nulls."""
    sampler = make_sampler(soh_estimator=True)  # no design_capacity: soh/capacity unknown
    monkeypatch.setattr(type(sampler.qmax), 'result',
                        lambda self: dict(qmax=271.3, soh=None, capacity=None, segments=3,
                                          newest_t=None, limiting_cell=None))

    assert sampler._estimates()['soh'] == dict(qmax=271.3, segments=3)
