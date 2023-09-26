import json

import influxdb
import pandas as pd
import pytz
from dateutil.tz import tzutc


def to_utc(t, **kwargs) -> pd.Timestamp:
    if isinstance(t, pd.Timestamp) and len(kwargs) == 0 and (t.tzinfo == pytz.utc or t.tzinfo == tzutc()):
        if t.tzinfo != pytz.utc:
            return t.replace(tzinfo=pytz.utc)
        else:
            return t
    t = pd.to_datetime(t, **kwargs)
    return t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')


def ql_time_range(time_range, freq=None):
    time_range = list(map(to_utc, time_range))
    if freq is not None:
        time_range = list(map(lambda t: t - pd.to_timedelta(freq), time_range))
    return " (time >= '%s' and time < '%s') " % (time_range[0].isoformat(), time_range[1].isoformat())


def fetch_influxdb_ha(time_range):
    with open('influxdb_local.json') as fp:
        influxdb_client = influxdb.InfluxDBClient(**{k[9:]: v for k, v in json.load(fp).items()})

    print(influxdb_client.get_list_measurements())

    r = influxdb_client.query("""
        SELECT value as A FROM "home_assistant"."autogen"."A"
        WHERE %s and "entity_id" =~ /.+_soc_current/ group by entity_id
    """ % ql_time_range(time_range))

    print(r)


fetch_influxdb_ha(['2022-02-01','2022-03-01'])
