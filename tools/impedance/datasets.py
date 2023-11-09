import json

import influxdb
import pandas as pd

from tools.impedance.data import ql_time_range

_influxdb_client = None


def influxdb_client():
    global _influxdb_client
    if not _influxdb_client:
        with open('influxdb_server.json') as fp:
            _influxdb_client = influxdb.InfluxDBClient(**{k[9:]: v for k, v in json.load(fp).items()})
    return _influxdb_client


def ant24_2023_07(cell_index=2):
    r = influxdb_client().query("""
     SELECT mean(voltage_cell%03i) as u, mean(current) as i FROM "autogen"."batmon"      
    WHERE time >= 1694527270s and time <= 1694551907s
     GROUP BY time(1s), "device"::tag, "cell_index"::tag fill(null)
     """ % cell_index)

    points = r.get_points(tags=dict(device='ant24'))
    points = pd.DataFrame(points)
    points.u.ffill(limit=20, inplace=True)
    points.i.ffill(limit=200, inplace=True)
    points.set_index(pd.DatetimeIndex(points.time), inplace=True)
    points.drop(columns='time', inplace=True)
    return points


def batmon(tr, device="bat_caravan", cell_index=0, freq="1s"):
    r = influxdb_client().query("""
     SELECT 
        mean(voltage_cell%03i) as u, 
        mean(current) as i,
        mean(soc) as soc,
        mean(temperatures_0) as temp0,
        mean(temperatures_1) as temp1 
    FROM "autogen"."batmon"      
    WHERE %s and device = '%s'
     GROUP BY time(%s), "device"::tag, "cell_index"::tag fill(null)
     """ % (cell_index, ql_time_range(tr), device, freq))

    points = r.get_points(tags=dict(device=device))
    points = pd.DataFrame(points)
    points.u.ffill(limit=20, inplace=True)
    points.i.ffill(limit=200, inplace=True)
    points.soc.ffill(limit=200, inplace=True)
    points.temp0.ffill(limit=200, inplace=True)
    points.temp1.ffill(limit=200, inplace=True)
    points.set_index(pd.DatetimeIndex(points.time), inplace=True)
    points.drop(columns='time', inplace=True)
    return points
