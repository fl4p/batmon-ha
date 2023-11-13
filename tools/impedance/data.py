import datetime
import json

import influxdb
import pandas as pd
import pytz
from dateutil.tz import tzutc

from bmslib.cache.disk import disk_cache_deco


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
    assert time_range[0] <= time_range[1]
    return " (time >= '%s' and time < '%s') " % (time_range[0].isoformat(), time_range[1].isoformat())

    # noinspection SqlDialectInspection


@disk_cache_deco()
def fetch_influxdb_ha(measurement, time_range, entity_id, freq=None):
    with open('influxdb_ha.json') as fp:
        influxdb_client = influxdb.InfluxDBClient(**{k[9:]: v for k, v in json.load(fp).items()})

    # print(influxdb_client.get_list_measurements())

    q = """
         SELECT %(agg)s(value) as v FROM "home_assistant"."autogen"."%(measurement)s"
         WHERE %(tr)s and "entity_id" = '%(entity_id)s' 
         %(group_by)s
         """ % (dict(
        agg='mean' if freq else '',
        group_by=f"GROUP BY time({freq})" if freq else "",
        measurement=measurement,
        tr=ql_time_range(time_range),
        entity_id=entity_id,

    ))
    print(q.replace('\n', ' ').strip(), '...')
    r = influxdb_client.query(q)

    points = r.get_points()  # tags=dict(entity_id=entity_id))
    points = pd.DataFrame(points)
    if points.empty:
        return pd.DataFrame(dict(v=[]), index=pd.DatetimeIndex([], tz=datetime.timezone.utc))
    points.set_index(pd.DatetimeIndex(points.time), inplace=True)
    points.drop(columns='time', inplace=True)

    return points


def fetch_batmon_ha_sensors(tr, alias, num_cells, freq='1s'):
    # f = None
    f = freq
    i = fetch_influxdb_ha("A", tr, alias + "_soc_current", freq=f ).v.rename('i')
    soc = fetch_influxdb_ha("%", tr, alias + "_soc_soc_percent", freq=f).v.rename('soc')
    temp1 = fetch_influxdb_ha("°C", tr, alias + "_temperatures_1", freq=f).v.rename('temp0')
    temp2 = fetch_influxdb_ha("°C", tr, alias + "_temperatures_2", freq=f).v.rename('temp1')
    u = [
        fetch_influxdb_ha("V", tr, alias + "_cell_voltages_%i" % (1 + ci), freq=f).v.rename(str(ci)) * 1e3  # rename(dict(v=ci))
        for ci in range(num_cells)
    ]
    print("joining..")


    um = pd.concat([i, soc, temp1, temp2] + u, axis=1).resample(freq).mean()
    um.loc[:, "temp0"].ffill(limit=1000, inplace=True)
    um.loc[:, "temp1"].ffill(limit=1000, inplace=True)
    um.ffill(limit=200, inplace=True)
    um = um[~um.i.isna()].dropna(how="all")
    return um


# fetch_influxdb_ha(['2022-02-01','2022-03-01'])


if __name__ == "__main__":
    fetch_batmon_ha_sensors(("2022-01-05", "2022-02-05"), 'daly_bms')
