"""Live GUI state, fed by a sink, read by the server.

Deliberately free of any HTTP dependency, so the server implementation stays
swappable and this module can also run on a phone alongside the same serializer.

No locks: the sink writes and the server reads on the same asyncio loop.
GuiServer.start() asserts that, so the invariant is checked rather than assumed.
"""
import time
from typing import Optional

from bmslib.sampling import BmsSampleSink
from bmslib.util import get_logger
from bmslib.wire import model

logger = get_logger()


def link_status(status: Optional[dict], sample_ts, now, expire_after) -> dict:
    """Closed enum, evaluated in precedence order.

    Precedence matters: a disconnected node that is also erroring reports
    'error', because the error text is the actionable part. A node with no
    sampler status at all is 'never', not 'connected'.
    """
    s = status or {}
    errors = s.get('num_errors') or 0
    out = {'errors': errors}

    if not s or (not s.get('num_samples') and sample_ts is None):
        out['status'] = 'never'
        return out

    if s.get('is_virtual') and s.get('last_error_type') == 'GroupNotReady':
        out['status'] = 'waiting'
        out['detail'] = s.get('debug_data') or s.get('last_error')
        return out

    if errors:
        out['status'] = 'error'
        out['detail'] = s.get('last_error')
        return out

    if not s.get('connected'):
        t_retry = s.get('t_next_retry') or 0
        if t_retry and t_retry > now:
            out['status'] = 'connecting'
            out['detail'] = 'retry in %.0f s' % (t_retry - now)
        else:
            out['status'] = 'disconnected'
        return out

    if sample_ts is not None and expire_after and (now - sample_ts) > expire_after:
        out['status'] = 'stale'
        out['detail'] = '%.0f s old' % (now - sample_ts)
        return out

    out['status'] = 'connected'
    return out


class GuiState:
    def __init__(self, app_version, runtime, producer='batmon-server'):
        self.app_version = app_version
        self.runtime = runtime
        self.producer = producer
        self._nodes = {}          # id -> {kind, group_kind, members, meta}
        self._samples = {}
        self._voltages = {}
        self._meters = {}
        self._device_info = {}
        self._status_source = None
        self._rev = 0
        self._node_rev = {}       # id -> revision of its last change

    # ---- registration ----

    def register_node(self, node_id, kind, group_kind=None, members=None, meta=None):
        self._nodes[node_id] = {
            'id': node_id, 'kind': kind,
            'group_kind': group_kind,
            'members': list(members or []),
            **{k: v for k, v in (meta or {}).items() if v is not None},
        }
        self._bump(node_id)

    def set_status_source(self, fn):
        self._status_source = fn

    def node_ids(self):
        return list(self._nodes)

    # ---- updates (must never raise: a sink cannot be allowed to break sampling) ----

    def _bump(self, node_id):
        self._rev += 1
        self._node_rev[node_id] = self._rev

    def update_sample(self, node_id, sample):
        try:
            self._samples[node_id] = sample
            self._bump(node_id)
        except Exception as e:
            logger.debug('gui update_sample %s: %s', node_id, e)

    def update_voltages(self, node_id, voltages):
        try:
            self._voltages[node_id] = list(voltages or [])
            self._bump(node_id)
        except Exception as e:
            logger.debug('gui update_voltages %s: %s', node_id, e)

    def update_meters(self, node_id, meters):
        try:
            self._meters[node_id] = dict(meters or {})
            self._bump(node_id)
        except Exception as e:
            logger.debug('gui update_meters %s: %s', node_id, e)

    def update_device_info(self, node_id, info):
        try:
            self._device_info[node_id] = info
            self._bump(node_id)
        except Exception as e:
            logger.debug('gui update_device_info %s: %s', node_id, e)

    @property
    def revision(self):
        return self._rev

    def changed_since(self, rev):
        return {nid for nid, r in self._node_rev.items() if r > rev}

    # ---- documents ----

    def _node_doc(self, node_id, now):
        sample = self._samples.get(node_id)
        status = None
        if self._status_source:
            try:
                status = self._status_source(node_id)
            except Exception:
                status = None
        ts = getattr(sample, 'timestamp', None) if sample is not None else None
        expire = (status or {}).get('expire_after')
        link = link_status(status, ts, now, expire)
        return model.node_state(
            node_id, sample=sample,
            voltages_mv=self._voltages.get(node_id),
            meters=self._meters.get(node_id),
            link=link, now=now)

    def system_doc(self):
        nodes = []
        for nid, n in self._nodes.items():
            d = dict(n)
            info = self._device_info.get(nid)
            if info is not None:
                d['device_info'] = {k: v for k, v in dict(
                    mnf=getattr(info, 'mnf', None), model=getattr(info, 'model', None),
                    hw_version=getattr(info, 'hw_version', None),
                    sw_version=getattr(info, 'sw_version', None),
                    name=getattr(info, 'name', None), sn=getattr(info, 'sn', None),
                ).items() if v is not None}
            cells = self._voltages.get(nid)
            if cells:
                d['num_cells'] = len(cells)
            nodes.append(d)
        return model.system_document(nodes, producer=self.producer,
                                     app_version=self.app_version, runtime=self.runtime)

    def state_doc(self, node_ids=None, partial=False):
        now = time.time()
        ids = node_ids if node_ids is not None else list(self._nodes)
        states = {nid: self._node_doc(nid, now) for nid in ids if nid in self._nodes}
        return model.state_document(states, partial=partial)


class GuiStateSink(BmsSampleSink):
    # The GUI must not add BLE traffic: cell voltages are delivered when the
    # sampler already fetched them, never fetched on the GUI's behalf.
    wants_voltages_every_sample = False

    def __init__(self, state: GuiState):
        self.state = state

    def publish_sample(self, bms_name, sample, tags=None):
        self.state.update_sample(bms_name, sample)

    def publish_voltages(self, bms_name, voltages):
        self.state.update_voltages(bms_name, voltages)

    def publish_meters(self, bms_name, readings):
        self.state.update_meters(bms_name, readings)

    def close(self):
        pass
