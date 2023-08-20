import datetime

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


class BatteryResistanceTracker():
    params: BatteryResistanceTrackerParams

    def __init__(self, params: BatteryResistanceTrackerParams):
        self.params = params

        self.charging_for_sec = 0

    def update(self, dt:float, u:float, i:float):

        if i > self.params.chg_current_threshold:
            self.charging_for_sec += dt
        else:
            pass # self.c

        pass
