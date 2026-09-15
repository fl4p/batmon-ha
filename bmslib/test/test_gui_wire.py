import json

import pytest

from bmslib.bms import BmsSample
from bmslib.gui.state import GuiState, GuiStateSink, link_status
from bmslib.gui import server as gsrv
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
