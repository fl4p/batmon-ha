import datetime
import math

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd

df = pd.read_csv('mppt_scan_I.csv')
I = pd.Series(df.A.values, index=pd.DatetimeIndex(df.time.values))

df = pd.read_csv('mppt_scan_V.csv')
U = df.pivot_table(values='V', index=pd.DatetimeIndex(df.time.values), columns='entity_id')
U = U['bat_caravan_cell_voltages_1'].dropna()

matplotlib.use('MacOSX')

plt.figure()


def normalize_std(s):
    return (s - s.mean()) / s.std()


fig, ax = plt.subplots(3, 1)

ax[0].plot(normalize_std(I), label='I')
ax[0].plot(normalize_std(U), label='U')
ax[0].legend()
# ax[0].title('normalized')

ax[1].plot(I, label='I')
ax[1].plot(I.rolling(20).mean(), label='sma20')
ax[2].plot(U, label='U')

plt.show()


class BatteryResistanceTrackerParams():
    def __init__(self):
        self.transient_time = 10  # 500
        self.chg_current_threshold = 5

class EWMA:
    def __init__(self, span):
        self.alpha = math.nan
        self.y = math.nan
        self.update_span(span)

    def update_span(self, span):
        self.alpha = (2 / (span + 1))

    def add(self, x):
        if not math.isfinite(x):
            return
        if not math.isfinite( self.y):
            self.y = x
        self.y = (1 - self.alpha) * self.y + self.alpha * x

class EWM:
    def __init__(self, span):
        self.avg = EWMA(span)
        self.std = EWMA(span)
        self.last_x = math.nan

    def add(self,x):
        self.avg.add(x)
        if math.isfinite(self.last_x):
            pct = (x - self.last_x) / self.last_x
            self.std.add(pct)
        self.last_x = x


class BatteryResistanceTracker():
    params: BatteryResistanceTrackerParams

    def __init__(self, params: BatteryResistanceTrackerParams):
        self.params = params

        self.stats_short = EWM(20)
        self.stats_long = EWM(400)

        self.charging_for_sec = 0

    def update(self, dt:float, u:float, i:float):

        self.stats_short.add(i)
        self.stats_long.add(i)

        if i > self.params.chg_current_threshold:
            self.charging_for_sec += dt
        else:
            pass # self.c

        pass
