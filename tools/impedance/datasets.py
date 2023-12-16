import json

import influxdb
import pandas as pd

from bmslib.cache.disk import disk_cache_deco
from tools.impedance.data import ql_time_range, fetch_batmon_ha_sensors

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


def batmon(tr, device="bat_caravan", cell_index=0, num_cells=1, freq="1s"):
    select_cells = ", ".join('mean(voltage_cell%03i) as "%i"' % (ci, ci) for ci in range(cell_index, cell_index+num_cells))
    q = """
     SELECT 
        %s,
        mean(current) as i,
        mean(soc) as soc,
        mean(temperatures_0) as temp0,
        mean(temperatures_1) as temp1 
    FROM "autogen"."batmon"      
    WHERE %s and device = '%s'
     GROUP BY time(%s), "device"::tag, "cell_index"::tag fill(null)
     """ % (select_cells, ql_time_range(tr), device, freq)
    r = influxdb_client().query(q)

    points = r.get_points(tags=dict(device=device))
    points = pd.DataFrame(points)
    assert not points.empty
    for ci in range(cell_index, num_cells):
        points.loc[:, str(ci)] = points.loc[:, str(ci)].ffill(limit=200, inplace=False)
    points.i.ffill(limit=200, inplace=True)
    points.soc.ffill(limit=200, inplace=True)
    points.temp0.ffill(limit=200, inplace=True)
    points.temp1.ffill(limit=200, inplace=True)
    points.set_index(pd.DatetimeIndex(points.time), inplace=True)
    points.drop(columns='time', inplace=True)
    dn = points.dropna(axis=1, how='all').dropna(how="any")
    points = points.loc[dn.first_valid_index():dn.last_valid_index(), :]
    return points[points.i.first_valid_index():]


@disk_cache_deco()
def daly22(num_cells, freq):
    """
    this is the first data i collected with 280ah lifepo4 from aliexpress

    note#1: there is more data later the year (after a gap)
    note#2: jbd22 is connected to the same battery (after daly bms)

    :param num_cells:
    :param freq:
    :return:
    """
    df = fetch_batmon_ha_sensors(("2022-01-05", "2022-05-05"), 'daly_bms', num_cells=num_cells, freq=freq)
    df.loc[:, 'i'] *= -1
    return df


@disk_cache_deco()
def daly22_1(num_cells, freq):
    """
    this is the first data i collected with 280ah lifepo4 from aliexpress

    note#1: there is more data later the year (after a gap)
    note#2: jbd22 is connected to the same battery (after daly bms)

    :param num_cells:
    :param freq:
    :return:
    """
    df = fetch_batmon_ha_sensors(("2022-01-05", "2022-05-05"), 'daly_bms', num_cells=num_cells, freq=freq)
    df.loc[:, 'i'] *= -1
    return df


@disk_cache_deco()
def daly22_full(num_cells, freq):
    df = fetch_batmon_ha_sensors(("2022-01-05", "2022-12-01"), 'daly_bms', num_cells=num_cells, freq=freq)
    df.loc[:, 'i'] *= -1
    return df


@disk_cache_deco()
def daly22_idle(num_cells, freq):
    df = fetch_batmon_ha_sensors(("2022-06-05", "2022-11-01"), 'daly_bms', num_cells=num_cells, freq=freq)
    df.loc[:, 'i'] *= -1
    return df


@disk_cache_deco()
def jbd22(num_cells, freq):
    df = fetch_batmon_ha_sensors(("2022-01-05", "2022-05-05"), 'jbd_bms', num_cells=num_cells, freq=freq)
    df.loc[:, 'i'] *= -1
    return df


@disk_cache_deco()
def jbd_full(num_cells, freq):
    df = fetch_batmon_ha_sensors(("2022-01-05", "2023-04-17"), 'jbd_bms', num_cells=num_cells, freq=freq)
    df.loc[:, 'i'] *= -1
    return df


@disk_cache_deco()
def jbd22_full(num_cells, freq):
    df = fetch_batmon_ha_sensors(("2022-01-05", "2022-12-01"), 'jbd_bms', num_cells=num_cells, freq=freq)
    df.loc[:, 'i'] *= -1
    return df


@disk_cache_deco()
def jk_full(num_cells, freq):
    df = fetch_batmon_ha_sensors(("2023-04-12", "2023-10-26"), 'jk_bms', num_cells=num_cells, freq=freq)
    df.loc[:"2023-06-29T14:00:00Z", 'i'] *= -1  # here we changed the settings (invert_current)
    return df


# @disk_cache_deco()
def ant24_23_11_12_fry(num_cells, freq='1s', **kwargs):
    """ AC load (pulsed induction hob) various periods 5 - 10s """
    df = batmon(tr=('2023-11-12 14:56:42', '2023-11-12 15:26:37'), device="ant24", num_cells=num_cells, freq=freq, **kwargs)
    df = df.ffill(limit=1800)
    dn = df.dropna(axis=1, how='all').dropna(how="any")
    df = df.loc[dn.first_valid_index():dn.last_valid_index(), :]
    return df


def ant24_23_11_12_dc(num_cells, freq='1s', **kwargs):
    """ Some DC load changes +-25A """
    # https://h.fabi.me/grafana/d/f3466d95-2c89-43ee-b9dd-3e722d26fcbd/batmon?orgId=1&var-device_name=daly_bms&from=1699803467980&to=1699803759112
    df = batmon(tr=('2023-11-12 15:37:47', '2023-11-12 15:42:39'), device="ant24", num_cells=num_cells, freq=freq, **kwargs)
    return df


def ant24_23_11_11_fridge(freq='1s', **kwargs):
    # https://h.fabi.me/grafana/d/f3466d95-2c89-43ee-b9dd-3e722d26fcbd/batmon?orgId=1&var-device_name=daly_bms&from=1699732460000&to=1699772063942
    df = batmon(tr=('2023-11-11 19:54:20', '2023-11-12 06:54:23'), device="ant24",  freq=freq,  **kwargs)
    df = df.ffill(limit=1800)
    dn = df.dropna(axis=1, how='all').dropna(how="any")
    df = df.loc[dn.first_valid_index():dn.last_valid_index(), :]
    return df


def ant24_23_11_21_pulse_coffee():
    pass
    # 2023-11-21 08:00:12
    # 2023-11-21 08:15:12
#


# def ant24_and daly()
# tesla charging using edecoa 3500w 8A-> 5A
    # 2023-11-23 17:00:00
    # 2023-11-23 18:00:00