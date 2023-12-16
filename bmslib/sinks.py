import base64
import datetime
import hashlib
import math
import os
import queue
import random
import statistics
import sys
import time
import zlib
from typing import List, Dict

from bmslib.bms import BmsSample
from bmslib.bt import BtBms
from bmslib.sampling import BmsSampleSink
from bmslib.util import get_logger, sid_generator
from mqtt_util import remove_none_values, remove_equal_values

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
    def __init__(self, flush_interval=2, **kwargs):
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

        self.Q = queue.Queue(200_000)
        self.db = kwargs.get('database')
        self.time_last_flush = 0
        self._last_volt: Dict[str, List[int]] = {}
        self.flush_interval = flush_interval
        self.silent = False

        self._prev_fields = {}

        if not kwargs.get('verify_ssl', False):
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def publish_voltages(self, bms_name, voltages: List[int], short=False):
        if not voltages:
            return

        if bms_name not in self._last_volt or len(voltages) != len(self._last_volt[bms_name]):
            self._last_volt[bms_name] = [-1] * len(voltages)

        last_volt = self._last_volt[bms_name]

        pub_anyway = random.random() < (1/100)

        fields = {(f"voltage_cell%03i" % i): int(voltages[i]) for i in range(len(voltages)) if
                  voltages[i] != last_volt[i] or pub_anyway}

        if not short:
            fields["voltage_cell_max"] = int(max(voltages))
            fields["voltage_cell_min"] = int(min(voltages))
            fields["voltage_cell_mean"] = float(statistics.mean(voltages))
            fields["voltage_cell_median"] = float(statistics.median(voltages))

        if fields:
            point = {
                "measurement": 'batmon',
                "time": datetime.datetime.utcnow(),
                "fields": fields,
                "tags": dict(device=bms_name)
            }
            self.Q.put(point)

        for i in range(len(voltages)):
            if voltages[i] == last_volt[i] and not pub_anyway:
                continue
            last_volt[i] = voltages[i]

            if not short:
                point = {
                    "measurement": 'cells',
                    "time": datetime.datetime.utcnow(),
                    "fields": dict(voltage=int(round(voltages[i]))),
                    "tags": dict(device=bms_name, cell_index=i),
                }
                self.Q.put(point)

        self._maybe_flush()

    def publish_sample(self, bms_name, sample: BmsSample, tags=None):
        fields = flatten({**sample.values(), "timestamp": None})
        remove_none_values(fields)
        for k, v in fields.items():
            if isinstance(v, int):
                fields[k] = float(v)
            elif isinstance(v, float):
                fields[k] = round(v, 3)
        fields1 = dict(fields)
        if random.random() > (1/200):
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
        self.Q.put(point)
        self._maybe_flush()

    def publish_meters(self, bms_name, readings: Dict[str, float]):
        now = datetime.datetime.utcnow()
        point = {
            "measurement": 'batmon',
            "time": now,
            "fields": {(f"meter_%s" % name): round(value, 5) for name, value in readings.items()},
            "tags": dict(device=bms_name)
        }
        self.Q.put(point)

    def flush(self):
        batch = []
        while not self.Q.empty() and len(batch) < 20_000:
            batch.append(self.Q.get())
        # self.influxdb_client.write_points(batch, time_precision='ms')
        if batch:
            try:
                res = self.influxdb_client.write_points(batch, time_precision='ms')
            except:
                res = False
                not self.silent and logger.error(sys.exc_info(), exc_info=True)
            if not res and not self.silent:
                logger.error('Failed to write points to influxdb')
            self.time_last_flush = time.time()

    def _maybe_flush(self):
        now = time.time()
        if now - self.time_last_flush > self.flush_interval:
            self.flush()


def hash_urlsafe(s: str):
    if not s:
        return None
    sh = hashlib.sha1(s.encode("utf-8"))
    return base64.urlsafe_b64encode(sh.digest()[1::2]).replace(b'=', b"")


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
    return r['data']['data_disk'] or None


class TelemetrySink(InfluxDBSink):

    def __init__(self, bms_by_name: Dict[str, BtBms]):
        super().__init__(
            flush_interval=30,
            host="tm.fabi.me",
            username="batmon_wo",
            password="no" + "secret",
            database="batmon_tele",
            ssl=False
        )
        self.uid = get_user_id()
        try:
            self.did = hash_urlsafe(get_disk_id())
        except:
            self.did = None

        self.addrh_by_name = {n: hash_urlsafe(bms.address) for n, bms in bms_by_name.items()}

        logger.info("tele started, uid='%s' did='%s' addr=%s", self.uid, self.did, self.addrh_by_name)
        self.silent = True

    def publish_sample(self, bms_name, sample: BmsSample, tags=None):
        tags_ = dict(uid=self.uid, did=self.did)
        tags and tags_.update(tags)
        try:
            super().publish_sample(self.addrh_by_name[bms_name], sample, tags=tags_)
        except:
            pass

    def publish_voltages(self, bms_name, voltages: List[int], short=True):
        # tags_ = dict(uid=self.uid, did=self.did)
        super().publish_voltages(self.addrh_by_name[bms_name], voltages, short=short)

    def publish_meters(self, bms_name, readings: Dict[str, float]):
        raise NotImplementedError()


"""
# Telemtry

v1 api https://community.influxdata.com/t/unable-to-connect-v1-clients-to-influxdbv2/21630/5


influx config create \
  -n open_pe \
  -u http://tm.fabi.me:8086 \
  -p fab:pw \
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
