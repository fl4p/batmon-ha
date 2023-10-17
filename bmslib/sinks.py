import datetime
import math
import queue
import time
from typing import List

from bmslib.bms import BmsSample
from bmslib.sampling import BmsSampleSink
from bmslib.util import get_logger
from mqtt_util import remove_none_values

logger = get_logger()

from collections.abc import MutableMapping

def flatten(dictionary, parent_key='', separator='_'):
    items = []
    for key, value in dictionary.items():
        new_key = parent_key + separator + key if parent_key else key
        if isinstance(value, MutableMapping):
            items.extend(flatten(value, new_key, separator=separator).items())
        elif isinstance(value, list):
            items.extend(flatten({str(i):value[i] for i in range(len(value))}, new_key, separator=separator).items())
        else:
            items.append((new_key, value))
    return dict(items)

class InfluxDBSink(BmsSampleSink):
    def __init__(self, **kwargs):
        import influxdb
        self.influxdb_client = influxdb.InfluxDBClient(**kwargs)
        self.Q = queue.Queue(200_000)
        self.db = kwargs.get('database')
        self.time_last_flush = 0

    def publish_voltages(self, bms_name, voltages: List[int]):
        if len(voltages) == 0:
            return

        point = {
            "measurement": 'batmon',
            "time": datetime.datetime.utcnow(),
            "fields": {(f"voltage_cell%03i" % i): int(voltages[i]) for i in range(len(voltages))},
            "tags": dict(device=bms_name)
        }
        self.Q.put(point)

        for i in range(len(voltages)):
            point = {
                "measurement": 'cells',
                "time": datetime.datetime.utcnow(),
                "fields": dict(voltage=int(round(voltages[i]))),
                "tags": dict(device=bms_name, cell_index=i),
            }
            self.Q.put(point)

        self._maybe_flush()

    def publish_sample(self, bms_name, sample: BmsSample):
        fields =  flatten({**sample.values(), "timestamp": None})
        remove_none_values(fields)
        for k, v in fields.items():
            if isinstance(v , int):
                fields[k] = float(v)
        if not fields:
            return
        point = {
            "measurement": 'batmon',
            "time": int(math.ceil(sample.timestamp * 1e3)),
            "fields": fields,
            "tags": dict(device=bms_name)
        }
        self.Q.put(point)
        self._maybe_flush()

    def _maybe_flush(self):
        now = time.time()
        if now - self.time_last_flush > 2:
            batch = []
            while not self.Q.empty() and len(batch) < 20_000:
                batch.append(self.Q.get())
            # self.influxdb_client.write_points(batch, time_precision='ms')
            if batch:
                res = self.influxdb_client.write_points(batch, time_precision='ms')
                if not res:
                    logger.error('Failed to write points to influxdb')
                self.time_last_flush = now
