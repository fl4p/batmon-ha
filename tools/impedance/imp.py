import datetime
import math

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

df = pd.read_csv('mppt_scan_I.csv')
I = pd.Series(df.A.values, index=pd.DatetimeIndex(df.time.values))

df = pd.read_csv('mppt_scan_V.csv')
U = df.pivot_table(values='V', index=pd.DatetimeIndex(df.time.values), columns='entity_id')
U = U['bat_caravan_cell_voltages_1'].ffill(limit=20)

matplotlib.use('MacOSX')



# noise filter
U = U.rolling(20).median()
U = U.rolling('8s').mean()
U = U.rolling('20s').mean()
I = I.rolling('8s').mean()


def normalize_std(s):
    return (s - s.mean()) / s.std()


fig, ax = plt.subplots(3, 1)

ax[0].plot(normalize_std(I), label='I')
ax[0].plot(normalize_std(U), label='U')

di = I - I.mean()
ax[0].plot(normalize_std(I)[abs(di.rolling('10s').max() - di.rolling('10s').min()) < 4])

# ax[0].plot(normalize_std(I), label='U')

ax[0].legend()
# ax[0].title('normalized')

ax[1].plot(I, label='I')
ax[1].plot(I.rolling(20).mean(), label='sma20')
ax[1].plot(abs(di.rolling('10s').max() - di.rolling('10s').min()))

# ax[2].plot(U, label='U')

df = pd.concat(dict(u=U - U.mean(), i=I - I.mean()), axis=1).ffill(limit=20).dropna(how='any')
# relaxation: exclude areas where recent current range is above threshold
df = df[abs(df.i.rolling('10s').max() - df.i.rolling('10s').min()) < 4]

# relaxation: exclude areas where recent current is near total average
# df = df[abs(df.i) > 2]
"""
the previous line is a fix for the std/std pseudo-regression
if U has still noise after filtering, the resistance estimate is too high
need to do some sort of clustering
"""


x=df.i.values
y=df.u.values

ax[2].scatter(x=x, y=y, marker='x')
ax[2].scatter(x=[x.mean()], y=[y.mean()], marker='x')
# plt.scatter(x=, y=)


A = np.vstack([x, np.ones(len(x))]).T
m, c = np.linalg.lstsq(A, y, rcond=None)[0]
plt.plot(x, m*x + c, 'r', label='ols %.2f mOhm (std %.2f minmax %.2f)' % (m*1000, np.std(y)/np.std(x)*1000, (U.max() - U.min())/(I.max() - I.min())*1e3))
plt.plot(x, np.std(y)/np.std(x)*x + c, 'b', label='std %.2f' % (np.std(y)/np.std(x)*1000))
plt.legend()

plt.show()

#plt.figure()
#df = df[abs(df.i) > 2]
#x=df.i
#y=df.u
#plt.plot(df.u)
#plt.plot(y/x)
# (y/x).plot()
#plt.show()


# m, c = np.linalg.lstsq(A, y, rcond=None)[0]


class BatteryResistanceTrackerParams():
    def __init__(self):
        self.transient_time = 10  # 500
        self.chg_current_threshold = 5


class EWMA:
    def __init__(self, span: int):
        self.alpha = math.nan
        self.y = math.nan
        self.update_span(span)

    def update_span(self, span):
        self.alpha = (2 / (span + 1))

    def add(self, x):
        if not math.isfinite(x):
            return
        if not math.isfinite(self.y):
            self.y = x
        self.y = (1 - self.alpha) * self.y + self.alpha * x


class EWM:
    def __init__(self, span: int, std_regularisation: float):
        self.avg = EWMA(span)
        self.std = EWMA(span)
        self._last_x = math.nan
        self.std_regularisation = std_regularisation

    def add(self, x):
        self.avg.add(x)
        if math.isfinite(self._last_x):
            if self.std_regularisation != 0:
                x = abs(x) * self.std_regularisation
            pct = (x - self._last_x) / self._last_x
            self.std.add(pct)
        self._last_x = x


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
