import json

import influxdb
import pandas as pd

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


def batmon(device="bat_caravan", cell_index=0):
    r = influxdb_client().query("""
     SELECT mean(voltage_cell%03i) as u, mean(current) as i FROM "autogen"."batmon"      
    WHERE time >= now() - 2d and time <= now() and device = '%s'
     GROUP BY time(1s), "device"::tag, "cell_index"::tag fill(null)
     """ % (cell_index, device))

    points = r.get_points(tags=dict(device=device))
    points = pd.DataFrame(points)
    points.u.ffill(limit=20, inplace=True)
    points.i.ffill(limit=200, inplace=True)
    points.set_index(pd.DatetimeIndex(points.time), inplace=True)
    points.drop(columns='time', inplace=True)
    return points
