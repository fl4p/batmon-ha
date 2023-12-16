import json
import math

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

if False:  # read from csv files
    df = pd.read_csv('mppt_scan_I.csv')
    I = pd.Series(df.A.values, index=pd.DatetimeIndex(df.time.values))

    df = pd.read_csv('mppt_scan_V.csv')
    U = df.pivot_table(values='V', index=pd.DatetimeIndex(df.time.values), columns='entity_id')
    U = U['bat_caravan_cell_voltages_1'].ffill(limit=20)

else:  # query influxdb (configuration influxdb_server.json)
    import influxdb

    with open('influxdb_server.json') as fp:
        influxdb_client = influxdb.InfluxDBClient(**{k[9:]: v for k, v in json.load(fp).items()})

        # r = influxdb_client.query("""
        # SELECT mean("voltage") as u, mean(current) as i FROM "autogen"."cells"
        # WHERE cell_index = '2' and time >= 1694703222983ms and time <= 1694708766302ms
        # GROUP BY time(3s), "device"::tag, "cell_index"::tag fill(null)
        # """)
        #   WHERE time >= 1694703222983ms and time <= 1694708766302ms
        # WHERE time >= 1694527270071ms and time <= 1694551907278ms
        # WHERE time >= 1694703222983ms and time <= 1694708766302ms

        r = influxdb_client.query("""
        SELECT mean(voltage_cell002) as u, mean(current) as i FROM "autogen"."batmon"      
       WHERE time >= 1694527270071ms and time <= 1694551907278ms
        GROUP BY time(1s), "device"::tag, "cell_index"::tag fill(null)
        """)

        #   r = influxdb_client.query("""
        #    SELECT mean(voltage_cell002) as u, mean(current) as i FROM "autogen"."batmon"
        #   WHERE time >= '2023-11-07T20' and time <= 1694551907278ms
        #    GROUP BY time(1s), "device"::tag, "cell_index"::tag fill(null)
        #    """)

        # &from=1693810297203&to=1693852625487
        # https://h.fabi.me/grafana/d/f3466d95-2c89-43ee-b9dd-3e722d26fcbd/batmon?orgId=1&from=1693810297203&to=1693852625487

        points = r.get_points(tags=dict(device='ant24'))
        points = pd.DataFrame(points)
        U = pd.Series(points.u.values, index=pd.DatetimeIndex(points.time)).ffill(limit=20) * 1e-3
        I = pd.Series(points.i.values, index=pd.DatetimeIndex(points.time)).ffill(limit=200)
        print(U)
        # from=1694703222983 & to = 1694708766302

matplotlib.use('MacOSX')

# noise filter
U = U.rolling(20).median()
U = U.rolling('8s').mean()
U = U.rolling('20s').mean()
I = I.rolling('8s').mean()

u_mask = (((U.ewm(span=60 * 5).mean().pct_change() * 1e4) ** 2).ewm(span=40).mean() < 0.2) \
         & (((U.pct_change() * 1e4) ** 2).ewm(span=40).mean() < 0.2)

i_mask = ((((I + 0.01).ewm(span=60 * 5).mean().pct_change() * 1e2) ** 2).ewm(span=40).mean() < 0.02) \
         & ((((I + 0.01).pct_change() * 1e2) ** 2).ewm(span=40).mean() < 0.02)


def normalize_std(s):
    return (s - s.mean()) / s.std()


fig, ax = plt.subplots(2, 1)

ax[0].plot(normalize_std(I), label='I')
ax[0].plot(normalize_std(U), label='U')
ax[0].plot(normalize_std(U)[u_mask & i_mask], label='U_masked', linewidth=0, marker='.')

di = I - I.mean()
# ax[0].plot(normalize_std(I)[abs(di.rolling('10s').max() - di.rolling('10s').min()) < 4])

# ax[0].plot(normalize_std(I), label='U')

ax[0].legend()
# ax[0].title('normalized')

# ax[1].plot(I, label='I')
# ax[1].plot(I.rolling(20).mean(), label='sma20')
# ax[1].plot(abs(di.rolling('5min').max() - di.rolling('5min').min()), label='mask')
# ax[1].legend()

# ax[2].plot(U, label='U')

df = pd.concat(dict(u=U - U.mean(), i=I - I.mean()), axis=1).ffill(limit=20).dropna(how='any')
# relaxation: exclude areas where recent current range is above threshold
# df = df[abs(df.i.rolling('10s').max() - df.i.rolling('10s').min()) < 4]
df = df[i_mask & u_mask]

# relaxation: exclude areas where recent current is near total average
# df = df[abs(df.i) > 2]
"""
the previous line is a fix for the std/std pseudo-regression
if U has still noise after filtering, the resistance estimate is too high
need to do some sort of clustering
"""

x = df.i.values
y = df.u.values * 1000

try:
    ax[1].scatter(x=x, y=y, marker='x')
    ax[1].scatter(x=[x.mean()], y=[y.mean()], marker='x')
except Exception as e:
    print(e)
# plt.scatter(x=, y=)


A = np.vstack([x, np.ones(len(x))]).T
m, c = np.linalg.lstsq(A, y, rcond=None)[0]
r2 = 1 - c / (y.size * y.var())
plt.plot(x, m * x + c, 'r', label='ols %.2f mOhm (r2 %.5f minmax %.2f)' % (
    m, r2, (U.max() - U.min()) / (I.max() - I.min()) * 1e3))

plt.plot(x, np.std(y) / np.std(x) * x + c, 'b', label='std %.2f' % (np.std(y) / np.std(x)))

df = pd.concat(dict(u=U, i=1 / -I), axis=1).ffill(limit=20).dropna(how='any')
# relaxation: exclude areas where recent current range is above threshold
# df = df[abs(df.i.rolling('10s').max() - df.i.rolling('10s').min()) < 4]
df = df[i_mask & u_mask]
corr = df[I.abs() > 2].corr().iloc[0, 1]
cov = df[I.abs() > 2].cov().iloc[0, 1] * 1000
plt.plot(x, cov * x + c, 'b', label='cov %.2f (corr %.3f)' % (cov, corr,))

plt.legend()

plt.show()


# plt.figure()
# df = df[abs(df.i) > 2]
# x=df.i
# y=df.u
# plt.plot(df.u)
# plt.plot(y/x)
# (y/x).plot()
# plt.show()


# m, c = np.linalg.lstsq(A, y, rcond=None)[0]


class BatteryResistanceTrackerParams():
    def __init__(self):
        self.transient_time = 10  # 500
        self.chg_current_threshold = 5




class BatteryResistanceTracker():
    params: BatteryResistanceTrackerParams

    def __init__(self, params: BatteryResistanceTrackerParams):
        self.params = params

        self.stats_short = EWM(20, std_regularisation=1e-6)
        self.stats_long = EWM(400, std_regularisation=1e-6)

        self.charging_for_sec = 0

    def update(self, dt: float, u: float, i: float):

        self.stats_short.add(i)
        self.stats_long.add(i)

        if i > self.params.chg_current_threshold:
            self.charging_for_sec += dt
        else:
            pass  # self.c

        pass
