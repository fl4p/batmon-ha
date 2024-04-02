import math

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import tools.impedance.datasets as datasets
from tools.impedance.data import fetch_batmon_ha_sensors

cell_results = {}

num_cells = 8
# df = fetch_batmon_ha_sensors(("2022-01-05", "2022-05-05"), 'daly_bms', num_cells=num_cells, freq="5s")
# df = fetch_batmon_ha_sensors(("2022-10-25", "2022-12-01"), 'daly_bms', num_cells=num_cells, freq="5s")
# df.loc[:, 'i'] *= -1

freq = '5s'

#df = datasets.daly22_full(num_cells=num_cells, freq=freq)

#df = datasets.jbd_full(num_cells=num_cells, freq=freq)
#df = df["2022-11-01":]

#df = datasets.jk_full(num_cells=num_cells, freq=freq)

df = datasets.batmon(
    # ('2023-11-09T03:30:00Z', '2023-11-09T06:30:00Z'), # fridge
    # ('2023-11-08T06:30:00Z', '2023-11-08T09:30:00Z'), # coffee
    # ('2023-11-04T06:30:00Z', '2023-11-04T10:30:00Z'), # 3cook
    # ('2023-11-09T06:30:00Z', '2023-11-09T08:30:00Z'), # pancakes
    # ('2023-11-10T10:30:00Z', '2023-11-10T10:50:00Z'),  # ehub test
    # ('2023-11-11T12:00:00Z', '2023-11-11T13:00:00Z'),  # varing sun
    ('2023-11-13T00:00:00Z', '2023-11-13T17:00:00Z'),  # recent
    freq="1s", device='ant24', cell_index=0, num_cells=num_cells,
)
#df.loc[:, 'u'] = df.loc[:, str(0)]
df.ffill(limit=1000, inplace=True)

# df = datasets.batmon(
# ('2023-11-10T16:30:00Z', '2023-11-10T18:10:00Z'), # dalyJKBalNoise
#    freq="5s", device='daly_bms', num_cells=num_cells,
# )

# filtering (smoothing
# df = df.rolling(5).mean()
df = df.rolling(2).mean()
df = df.rolling(3).mean()
# df = df.rolling(5).mean()
# df = df.rolling(3).mean()


# masking
# df = df[df.i.pct_change().abs() > 0.05]
# df = df[df.i.abs() > 1]
df[df.i.abs() < 0.5] = math.nan

I = df.i
i_mask = ((((I + 0.01).ewm(span=60).mean().pct_change() * 1e2) ** 2).ewm(span=10).mean() < 0.03) \
    # & ((((I + 0.01).pct_change() * 1e2) ** 2).ewm(span=40).mean() < 0.02)
# df = df[i_mask]
# df = df[df.i < -1]
# df.loc[~i_mask, 'i'] = math.nan

block_size = 1200
# block_size = 300

df = df.iloc[:len(df) - len(df) % block_size]


# cv = df.loc[:, tuple(str(ci) for ci in range(num_cells))]
# df.loc[:, 'cv_max'] = cv.max(axis=1)
# df.loc[:, 'cv_min'] = cv.min(axis=1)


def check_constraints(b):
    if "u" not in b:
        return False
    u_max, u_min = b.u.max(), b.u.min()

    if u_max - u_min < 10 or u_max - u_min > 500:
        return False
        # raise ValueError("u range too small")

    if u_min < 2700:
        return False
        # raise ValueError("u min too small")

    if u_max > 3500:
        return False
        # raise ValueError("u max too large")

    return True


for ci in range(num_cells):
    print('cell', ci)

    # df =  datasets.ant24_2023_07(cell_index=ci)
    if 0:
        df = datasets.batmon(
            # ('2023-11-08T10:31:31Z', '2023-11-08T19:50:51Z'),
            # ('2023-11-07T11:00:00Z', '2023-11-07T14:50:51Z'),
            # ('2023-10-25T06:31:31Z', '2023-11-08T20:50:51Z'), freq="5s",
            # ('2023-11-04T06:30:00Z', '2023-11-04T16:30:00Z'),freq='5s', # 3cook
            # ('2023-11-09T06:30:00Z', '2023-11-09T08:30:00Z'), freq='5s',  # pancakes
            ('2023-10-01T06:30:00Z', '2023-11-04T16:30:00Z'), freq='5s',  # autumn
            device='bat_caravan', cell_index=ci,
        )

    df.loc[:, "u"] = df.loc[:, str(ci)]

    # df = df.iloc[:int(len(df) / 2)]

    # df = df.rolling(5).mean()
    # df.dropna(how="any", inplace=True)

    if ci == 0:
        fig, ax = plt.subplots(4, 1)
        dfr = df.asfreq("15min")
        ax[0].step(dfr.index, dfr.u, where='post', label='U', marker='.')
        # ax[0].set_xlim((2, 100))

        ax[1].step(dfr.index, dfr.i, where='post', label='I', marker='.')

        ax[2].step(dfr.index, dfr.soc, where='post', label='soc', marker='.')
        ax[2].set_ylim((0, 100))

        ax[3].step(dfr.index, dfr.temp0, where='post', label='temp0')
        ax[3].step(dfr.index, dfr.temp1, where='post', label='temp1')
        ax[3].set_ylim((10, 40))
        plt.legend()

        # df.u.plot()
        plt.show()

    import tools.impedance.ac_impedance

    # TODO overlapped split
    # blocks = np.vsplit(df, int(len(df) / block_size))

    step = int(block_size / 2)
    blocks = [df.iloc[i: i + block_size] for i in range(0, len(df), step)]

    results = []
    ok = 0

    for b in blocks:
        t = b.index[-1]

        if not check_constraints(b):
            results.append((t, math.nan, math.nan))
            continue

        try:
            r, u0 = tools.impedance.ac_impedance.estimate(b.u, b.i, ignore_nan=True)
            results.append((t, r, u0))
            ok += 1
        except Exception as e:
            results.append((t, math.nan, math.nan))
            # print('error %s at block %s' % (e, t))
            pass

    results = pd.DataFrame(results, columns=['time', 'r', 'u0'])
    results.set_index("time", inplace=True)
    # results.drop(columns="time", inplace=True)
    # print(results)
    print('cell', ci, "have estimate for %d/%d blocks" % (ok, len(blocks)))
    # results.r.plot(label='c%d R(q25)=%.2f' % (ci, results.r.quantile(.25)))

    if ci == 0:
        plt.step(results.r.index, results.r.values, where='post', marker='.', alpha=.1, label='R(c%d) raw' % (ci))

    # fl = int(400)  # len(results) / 400)
    nday = 3600 * 24 / (pd.to_timedelta(freq).total_seconds() * step)
    curve = (results.r
             .ffill(limit=4)  # int(nday / 4 + 1))
             .rolling(int(nday * 3), min_periods=int(nday * 2)).median()
             .rolling(int(nday * 3), min_periods=int(nday * 1)).mean()
             )
    curve = curve.ffill()
    plt.step(curve.index,
             curve.values, where='post',
             marker='.',
             label='R(c%d) med=%.2f Q25=%.2f' % (ci, results.r.median(), results.r.quantile(.25)))

    cell_results[ci] = results

# plt.semilogy()

plt.legend()
plt.ylim((0, 6))
plt.grid()
plt.show()

if False:  # show hist
    for ci, results in cell_results.items():
        plt.hist(results.r, bins=30, range=(results.r.quantile(.2), results.r.quantile(.8)), alpha=0.5,
                 label='c%i' % ci)

    plt.legend()
    plt.grid()
    plt.show()
