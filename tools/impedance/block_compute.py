import math

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import tools.impedance.datasets as datasets

for ci in range(8):
    # df =  datasets.ant24_2023_07(cell_index=ci)
    df = datasets.batmon(device='bat_caravan', cell_index=0)

    block_size = 120

    df = df.iloc[:int(len(df) / 2)]

    df = df.iloc[:len(df) - len(df) % block_size]

    import tools.impedance.ac_impedance

    blocks = np.vsplit(df, int(len(df) / block_size))
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
    #print(results)
    print("have estimate for %d/%d blocks" % (len(results), len(blocks)))
    # results.r.plot(label='c%d R(q25)=%.2f' % (ci, results.r.quantile(.25)))
    plt.step(results.r.index, results.r.values, where='post', label='c%d R(q25)=%.2f' % (ci, results.r.quantile(.25)))

# df.u.plot()
# plt.show()


# plt.semilogy()

plt.legend()
plt.ylim((0.1, 2))
plt.grid()
plt.show()
