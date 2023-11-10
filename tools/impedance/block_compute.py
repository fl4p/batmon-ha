import math

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import tools.impedance.datasets as datasets
from tools.impedance.data import fetch_batmon_ha_sensors

cell_results = {}

num_cells = 8
#df = fetch_batmon_ha_sensors(("2022-01-05", "2022-02-05"), 'daly_bms', num_cells=num_cells)
#df = fetch_batmon_ha_sensors(("2022-01-05", "2023-02-05"), 'daly_bms', num_cells=num_cells)

df = fetch_batmon_ha_sensors(("2022-01-05", "2022-05-05"), 'daly_bms', num_cells=num_cells)

df.loc[:,'i'] *= -1

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

    df.loc[:,"u"] = df.loc[:,str(ci)]

    block_size = 1200

    # df = df.iloc[:int(len(df) / 2)]

    # df = df.rolling(5).mean()
    # df.dropna(how="any", inplace=True)

    df = df.iloc[:len(df) - len(df) % block_size]

    if ci == 0:
        fig, ax = plt.subplots(4, 1)
        dfr = df.resample("2min").mean()
        ax[0].step(dfr.index, dfr.u, where='post', label='U', marker='.')
        #ax[0].set_xlim((2, 100))
        ax[1].step(dfr.index, dfr.i, where='post', label='I', marker='.')
        ax[2].step(dfr.index, dfr.soc, where='post', label='soc', marker='.')
        ax[2].set_xlim((0,100))
        ax[3].step(dfr.index, dfr.temp0, where='post', label='temp0')
        ax[3].step(dfr.index, dfr.temp1, where='post', label='temp1')
        plt.ylim((10, 40))
        plt.legend()

        # df.u.plot()
        plt.show()

    import tools.impedance.ac_impedance

    # TODO overlapped split
    # blocks = np.vsplit(df, int(len(df) / block_size))

    step = int(block_size / 2)
    blocks = [df.iloc[i: i + block_size] for i in range(0, len(df), step)]

    results = []

    for b in blocks:
        t = b.index[-1]
        try:
            r, u0 = tools.impedance.ac_impedance.estimate(b.u, b.i)
            results.append((t, r, u0))
        except Exception as e:
            # print('error %s at block %s' % (e, t))
            pass

    results = pd.DataFrame(results, columns=['time', 'r', 'u0'])
    results.set_index("time", inplace=True)
    # results.drop(columns="time", inplace=True)
    # print(results)
    print('cell', ci, "have estimate for %d/%d blocks" % (len(results), len(blocks)))
    # results.r.plot(label='c%d R(q25)=%.2f' % (ci, results.r.quantile(.25)))

    if ci == 0:
        plt.step(results.r.index, results.r.values, where='post', marker='.', alpha=.2, label='R(c%d) raw' % (ci))

    fl = int(len(results) / 60)
    plt.step(results.r.index, results.r.rolling(fl).median().rolling(fl*2).mean().values, where='post', marker='.',
             label='R(c%d) med=%.2f Q25=%.2f' % (ci, results.r.median(), results.r.quantile(.25)))

    cell_results[ci] = results

# plt.semilogy()

plt.legend()
plt.ylim((0, 6))
plt.grid()
plt.show()

for ci, results in cell_results.items():
    plt.hist(results.r, bins=30, range=(results.r.quantile(.2), results.r.quantile(.8)), alpha=0.5, label='c%i' % ci)

plt.legend()
plt.grid()
plt.show()
