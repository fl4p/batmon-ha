import base64
import datetime
import hashlib
import math
import os
import queue
import random
import statistics
import threading
import time
import zlib
from typing import List, Dict, Union

from bmslib.bms import BmsSample
from bmslib.bt import BtBms
from bmslib.circuit_breaker import CircuitBreaker
from bmslib.mqtt_util import remove_none_values, remove_equal_values
from bmslib.sampling import BmsSampleSink
from bmslib.util import get_logger, sid_generator

logger = get_logger()

from collections.abc import MutableMapping


def flatten(dictionary, parent_key='', separator='_'):
    items = []
    for key, value in dictionary.items():
        new_key = parent_key + separator + key if parent_key else key
        if isinstance(value, MutableMapping):
            items.extend(flatten(value, new_key, separator=separator).items())
        elif isinstance(value, list):
            items.extend(flatten({str(i): value[i] for i in range(len(value))}, new_key, separator=separator).items())
        else:
            items.append((new_key, value))
    return dict(items)


class InfluxDBSink(BmsSampleSink):
    def __init__(self, flush_interval=2, backoff_interval=0, **kwargs):
        import influxdb
        self.influxdb_client = influxdb.InfluxDBClient(**kwargs)

        def _request_gzip(data, headers, **kwargs):
            if headers is None:
                headers = {}
            if data:
                headers['content-encoding'] = 'gzip'
                compress = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
                # n = len(data)
                data = compress.compress(data) + compress.flush()
                headers['Content-Length'] = str(len(data))
                # logger.debug("comp ratio %.2f %s %s", len(data) / n, len(data), self.influxdb_client._database)

            return self.influxdb_client._session.request_(data=data, headers=headers, **kwargs)

        self.influxdb_client._session.request_ = self.influxdb_client._session.request
        self.influxdb_client._session.request = _request_gzip

        self.Q = queue.Queue(50_000)
        self.db = kwargs.get('database')
        self._last_volt: Dict[str, List[int]] = {}
        self.flush_interval = flush_interval
        self.silent = False
        self.cb = CircuitBreaker(backoff_interval)

        self._prev_fields = {}

        if not kwargs.get('verify_ssl', False):
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._stop_event = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name='InfluxDBSinkFlush', daemon=True)
        self._flush_thread.start()

    def publish_voltages(self, bms_name, voltages: List[Union[int,float]], short=False, tags=None):
        if not voltages:
            return
        tags = tags or {}

        if bms_name not in self._last_volt or len(voltages) != len(self._last_volt[bms_name]):
            self._last_volt[bms_name] = [-1] * len(voltages)

        last_volt = self._last_volt[bms_name]

        pub_anyway = random.random() < (1 / 100)

        def _valid(v):
            return v is not None and not (isinstance(v, float) and not math.isfinite(v))

        fields = {(f"voltage_cell%03i" % i): int(voltages[i]) for i in range(len(voltages)) if
                  _valid(voltages[i]) and (voltages[i] != last_volt[i] or pub_anyway)}

        if not short:
            valid_voltages = [v for v in voltages if _valid(v)]
            if valid_voltages:
                fields["voltage_cell_max"] = int(max(valid_voltages))
                fields["voltage_cell_min"] = int(min(valid_voltages))
                fields["voltage_cell_mean"] = float(statistics.mean(valid_voltages))
                fields["voltage_cell_median"] = float(statistics.median(valid_voltages))

        if fields:
            point = {
                "measurement": 'batmon',
                "time": datetime.datetime.utcnow(),
                "fields": fields,
                "tags": dict(device=bms_name, **tags)
            }
            self._enqueue(point)

        for i in range(len(voltages)):
            if not _valid(voltages[i]):
                continue
            if voltages[i] == last_volt[i] and not pub_anyway:
                continue
            last_volt[i] = voltages[i]

            if not short:
                point = {
                    "measurement": 'cells',
                    "time": datetime.datetime.utcnow(),
                    "fields": dict(voltage=int(round(voltages[i]))),
                    "tags": dict(device=bms_name, cell_index=i, **tags),
                }
                self._enqueue(point)

    def publish_sample(self, bms_name, sample: BmsSample, tags=None):
        fields = flatten({**sample.values(), "timestamp": None})
        remove_none_values(fields)
        for k, v in fields.items():
            if isinstance(v, int):
                fields[k] = float(v)
            elif isinstance(v, float):
                fields[k] = round(v, 3)
        fields1 = dict(fields)
        if random.random() > (1 / 200):
            remove_equal_values(fields, self._prev_fields.get(bms_name))
        self._prev_fields[bms_name] = fields1

        if not fields:
            return
        point = {
            "measurement": 'batmon',
            "time": int(math.ceil(sample.timestamp * 1e3)),
            "fields": fields,
            "tags": dict(device=bms_name)
        }
        if tags:
            point['tags'].update(tags)
        self._enqueue(point)

    def publish_meters(self, bms_name, readings: Dict[str, float]):
        now = datetime.datetime.utcnow()
        point = {
            "measurement": 'batmon',
            "time": now,
            "fields": {(f"meter_%s" % name): round(value, 5) for name, value in readings.items()},
            "tags": dict(device=bms_name)
        }
        self._enqueue(point)

    def _enqueue(self, point):
        # During an outage backoff with no prior success, drop new points so
        # memory stays flat (the server may be unreachable for this user).
        remove_none_values(point['tags'])
        if self.cb.enabled and not self.cb.keep_batch_on_failure \
                and not self.cb.should_attempt():
            return
        try:
            self.Q.put_nowait(point)
        except queue.Full:
            pass  # bounded memory: drop the newest point

    def _drain_queue(self):
        try:
            while True:
                self.Q.get_nowait()
        except queue.Empty:
            pass

    def flush(self):
        """Drain the queue and attempt one write. Always attempts regardless of
        the circuit breaker (callers like shutdown want a final flush);
        _flush_loop is the backoff-gated periodic entry point."""
        now = time.time()
        batch = []
        while not self.Q.empty() and len(batch) < 20_000:
            batch.append(self.Q.get())
        if batch:
            try:
                res = self.influxdb_client.write_points(batch, time_precision='ms')
            except Exception as e:
                res = False
                if not self.silent:
                    from bmslib.util import summarize_exc
                    logger.error('influxdb write failed: %s', summarize_exc(e))
            if res:
                self.cb.on_success(now)
            else:
                if not self.silent:
                    logger.error('Failed to write points to influxdb')
                self.cb.on_failure(now)
                if self.cb.keep_batch_on_failure:
                    for point in batch:
                        self._enqueue(point)  # re-queue for retry after backoff window
                elif self.cb.enabled:
                    self._drain_queue()  # never succeeded: drop, stay flat

    def _flush_loop(self):
        while not self._stop_event.wait(self.flush_interval):
            try:
                if self.cb.should_attempt():
                    self.flush()
            except Exception as e:
                if not self.silent:
                    logger.error('flush loop error: %s', e)

    def close(self):
        self._stop_event.set()
        self._flush_thread.join(timeout=5)
        try:
            self.flush()
        except Exception:
            pass


# --- QuestDB native writer ------------------------------------------------
#
# batmon_tele_batmon columns stored as SCALED INTEGERS: field -> scale. The sink
# writes round(raw * scale) as an integer (decode on read: stored / scale). The
# scale fixes the unit, so the column type stays stable across samples. Scales
# come from measured pco gains (doc/QuestDB-compression.md). `current` is scaled
# at the user's request despite a slight measured regression (it is signed and
# oscillates around zero, which pco's delta/int-mult dislikes) -- revisit if its
# Parquet size matters more than uniformity.
#
# IMPORTANT: the matching QuestDB column must be INT/LONG, NOT FLOAT. ILP into a
# FLOAT column silently coerces the integer back to a float (e.g. 3300 -> 3300.0)
# and the pco win is lost; worse, the unit changes (mV vs V), so this cannot be
# pointed at a table that already holds FLOAT volt/amp history -- it needs a
# fresh table whose columns are integer from row one.
QUESTDB_INT_SCALE = {
    "voltage": 1000,          # V  -> mV
    "current": 1000,          # A  -> mA
    "balance_current": 1000,  # A  -> mA
    "soc": 100,               # %  -> centi-%
    "soh": 100,               # %  -> centi-%
    "capacity": 100,          # Ah -> centi-Ah
    "aged_capacity": 100,     # Ah -> centi-Ah
    "cycle_capacity": 100,    # Ah -> centi-Ah
    "mos_temperature": 100,   # degC -> centi-degC
    **{("temperatures_%d" % i): 100 for i in range(8)},  # degC -> centi-degC
}

# Stored as a plain integer (LONG) at native scale -- a bitmask / count, no unit.
QUESTDB_LONG_FIELDS = frozenset({"problem_code"})

# Stored as BOOLEAN. Every value across the export is 0/1/null; emit Python bool
# so the line protocol carries a boolean (ILP will not coerce 0/1 floats into a
# BOOLEAN column).
QUESTDB_BOOL_FIELDS = frozenset({
    "switches_charge", "switches_discharge", "switches_balance", "switches_float_charge",
    "switches_status_normal", "switches_status_charging", "switches_status_discharging",
    "switches_status_protection", "switches_status_overvolt_protection",
    "switches_status_undervolt_protection", "switches_status_overtemp",
    "switches_status_undertemp", "switches_status_short",
})

# Kept as FLOAT: wide dynamic range or already-derived values where a linear int
# grid would overflow or earn nothing (see doc/QuestDB-compression.md).
QUESTDB_FLOAT_FIELDS = frozenset({
    "power", "_power", "charge", "total_charge_throughput", "num_cycles",
    "num_samples", "battery_charging", "problem", "uptime", "runtime",
})

# batmon_tele_batmon has exactly 32 per-cell columns (voltage_cell000..031).
QUESTDB_MAX_CELLS = 32


def _qdb_finite_number(v):
    """True if v is a finite int/float (and not a bool, which we route by name)."""
    if isinstance(v, bool):
        return False
    return isinstance(v, int) or (isinstance(v, float) and math.isfinite(v))


class QuestDBSink(InfluxDBSink):
    """Telemetry sink tuned for QuestDB's pco Parquet codec.

    Reuses InfluxDBSink's transport (QuestDB ingests InfluxDB v1 line protocol on
    its /write endpoint, and the fork maps measurement `m` written with
    `?db=batmon_tele` to the table `batmon_tele_m`). Only the field encoding
    differs from the InfluxDB sink:

      - scaled-integer columns (QUESTDB_INT_SCALE) -> `field=Ni` -> INT/LONG;
      - problem_code -> LONG; switches_* -> BOOLEAN; the rest -> FLOAT;
      - fields absent from the batmon_tele_* schema are DROPPED, because ILP
        auto-create is on and any stray field would re-create a dropped column.

    The destination columns must be integer (see QUESTDB_INT_SCALE note). For a
    truly native transport (QuestDB ILP on port 9000/9009) the official `questdb`
    client could replace the base class, at the cost of a C-extension dependency
    this addon currently avoids on constrained platforms.
    """

    def _encode_sample_fields(self, fields):
        """Map flattened sample fields to QuestDB-schema-typed values, dropping
        anything not in the schema."""
        out = {}
        for k, v in fields.items():
            scale = QUESTDB_INT_SCALE.get(k)
            if scale is not None:
                if _qdb_finite_number(v):
                    out[k] = int(round(v * scale))      # INT (scaled)
            elif k in QUESTDB_BOOL_FIELDS:
                out[k] = bool(v)                         # BOOLEAN
            elif k in QUESTDB_LONG_FIELDS:
                if _qdb_finite_number(v):
                    out[k] = int(v)                      # LONG
            elif k in QUESTDB_FLOAT_FIELDS:
                if isinstance(v, bool):
                    out[k] = float(v)                    # FLOAT (0.0/1.0)
                elif _qdb_finite_number(v):
                    out[k] = round(float(v), 3)          # FLOAT
            # else: not a schema column -> drop (avoid ILP auto-create).
        return out

    def publish_sample(self, bms_name, sample: BmsSample, tags=None):
        fields = self._encode_sample_fields(flatten({**sample.values(), "timestamp": None}))
        remove_none_values(fields)
        fields1 = dict(fields)
        if random.random() > (1 / 200):
            remove_equal_values(fields, self._prev_fields.get(bms_name))
        self._prev_fields[bms_name] = fields1

        if not fields:
            return
        point = {
            "measurement": 'batmon',
            "time": int(math.ceil(sample.timestamp * 1e3)),
            "fields": fields,
            "tags": dict(device=bms_name),
        }
        if tags:
            point['tags'].update(tags)
        self._enqueue(point)

    def publish_voltages(self, bms_name, voltages: List[Union[int, float]], short=False, tags=None):
        if not voltages:
            return
        tags = tags or {}

        if bms_name not in self._last_volt or len(self._last_volt[bms_name]) != len(voltages):
            self._last_volt[bms_name] = [-1] * len(voltages)
        last_volt = self._last_volt[bms_name]
        pub_anyway = random.random() < (1 / 100)
        n = min(len(voltages), QUESTDB_MAX_CELLS)

        def _valid(v):
            return v is not None and not (isinstance(v, float) and not math.isfinite(v))

        # Per-cell columns on batmon_tele_batmon (INT mV). No aggregates: the
        # schema dropped voltage_cell_min/max/mean/median and ILP auto-create
        # would re-add them.
        fields = {}
        for i in range(n):
            v = voltages[i]
            if _valid(v) and (v != last_volt[i] or pub_anyway):
                fields["voltage_cell%03i" % i] = int(round(v))
        if fields:
            self._enqueue({
                "measurement": 'batmon',
                "time": datetime.datetime.utcnow(),
                "fields": fields,
                "tags": dict(device=bms_name, **tags),
            })

        # One row per changed cell on batmon_tele_cells (INT mV).
        for i in range(n):
            v = voltages[i]
            if not _valid(v):
                continue
            if v == last_volt[i] and not pub_anyway:
                continue
            last_volt[i] = v
            self._enqueue({
                "measurement": 'cells',
                "time": datetime.datetime.utcnow(),
                "fields": dict(voltage=int(round(v))),
                "tags": dict(device=bms_name, cell_index=i, **tags),
            })


def hash_urlsafe(s: str):
    if not s:
        return None
    sh = hashlib.sha1(s.encode("utf-8"))
    return base64.urlsafe_b64encode(sh.digest()[1::2]).rstrip(b'=').decode("ascii")


def get_user_id():
    from bmslib.store import root_dir
    user_id_fn = root_dir + 'user_id'

    if not os.path.exists(user_id_fn) or os.stat(user_id_fn).st_size < 5:
        with open(user_id_fn, 'w') as fh:
            sid = sid_generator(6)
            fh.writelines([sid])

    with open(user_id_fn, 'r') as fh:
        return fh.readline()


def get_disk_id():
    import requests
    bearer = os.environ.get('SUPERVISOR_TOKEN')
    r = requests.get('http://supervisor/os/info', timeout=3, headers=dict(Authorization=f"Bearer {bearer}")).json()
    return str(r['data']['data_disk']) or None


class TelemetrySink(QuestDBSink):

    def __init__(self, bms_by_name: Dict[str, BtBms]):
        super().__init__(
            flush_interval=120,
            backoff_interval=3600,
            host="tm.fabi.me",
            username="batmon_wo",
            password="no" + "secret",
            database="batmon_tele",
            ssl=False
        )
        bms_by_name = {n: bms for n, bms in bms_by_name.items() if not bms.is_virtual}
        self.uid = get_user_id()
        try:
            self.did = hash_urlsafe(get_disk_id())
        except:
            self.did = None

        self.addrh_by_name = {n: hash_urlsafe(bms.address) for n, bms in bms_by_name.items()}
        self.slug_by_name = {n: bms.slug for n, bms in bms_by_name.items()}

        self.sample_interval = 15
        self._last_pub: Dict[str, float] = {}

        # logger.info("tele started, uid='%s' did='%s' addr=%s", self.uid, self.did, self.slug_by_name)
        self.silent = True

    def _should_sample(self, bms_name) -> bool:
        now = time.time()
        if now - self._last_pub.get(bms_name, 0) < self.sample_interval:
            return False
        self._last_pub[bms_name] = now
        return True

    def publish_sample(self, bms_name, sample: BmsSample, tags=None):
        if bms_name not in self.slug_by_name:
            return
        if 'dummy' in self.slug_by_name[bms_name]:
            return
        if not self._should_sample(bms_name):
            return
        tags_ = dict(uid=self.uid, did=self.did, addrh=self.addrh_by_name[bms_name], slug=self.slug_by_name[bms_name])
        tags and tags_.update(tags)
        try:
            QuestDBSink.publish_sample(self,
                                       self.slug_by_name[bms_name] + '_' + self.addrh_by_name[bms_name],
                                       sample, tags=tags_)
        except Exception as e:
            pass

    def publish_voltages(self, bms_name, voltages: List[int], short=True, tags=None):
        if bms_name not in self.slug_by_name:
            return
        if 'dummy' in self.slug_by_name[bms_name]:
            return
        if not self._should_sample(bms_name):
            return
        tags_ = dict(uid=self.uid, did=self.did, addrh=self.addrh_by_name[bms_name], slug=self.slug_by_name[bms_name])
        tags and tags_.update(tags)
        try:
            # Telemetry only collects raw per-cell voltages, never the computed
            # aggregates (voltage_cell_min/max/mean/median). QuestDBSink already
            # drops the aggregates; short=True is kept for belt-and-suspenders.
            QuestDBSink.publish_voltages(self, self.slug_by_name[bms_name] + '_' + self.addrh_by_name[bms_name], voltages, short=True, tags=tags_)
        except Exception:
            pass

    def publish_meters(self, bms_name, readings: Dict[str, float]):
        raise NotImplementedError()


"""
# Telemtry

v1 api https://community.influxdata.com/t/unable-to-connect-v1-clients-to-influxdbv2/21630/5


influx config create \
  -n open_pe \
  -u http://tm.fabi.me:8086 \
  -p usr:pw \
  -o openpe
  
influx bucket create --org openpe --name batmon_tele -r 0

influx user list
influx user create --org openpe --name batmon_wo --password nosecret 

bucket=$(influx bucket find --name batmon_tele|tail -n1| awk '{print $1}')
influx auth create --org openpe --user batmon_wo --write-bucket $bucket
influx v1 auth create --org openpe --username batmon_wo --password nosecret --write-bucket $bucket

influx auth list | grep batmon_wo

influx org create --name open_pe


"""
