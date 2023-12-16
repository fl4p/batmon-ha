import math

import pandas as pd
from matplotlib import pyplot as plt

import tools.impedance.datasets as datasets

num_cells = 8
#df = datasets.daly22_1(num_cells=num_cells, freq='5s')
df = datasets.batmon(("2023-11-13", "2023-11-18"), 'daly_bms', num_cells=num_cells, freq='5s')

df.loc[:,'i'] =  df.loc[:,'i'].fillna(0)
#df = datasets.jbd22(num_cells=num_cells, freq='5s')
#df = df.iloc[18000:25000]


cv = df.loc[:, tuple(str(ci) for ci in range(num_cells))]
cf_filt = cv.rolling(3).median()
is_empty = cf_filt.min(axis=1) < 2700
is_full = cf_filt.max(axis=1) > 3450
ef = pd.concat([is_empty, is_full], axis=1)

q = df.i.fillna(0).cumsum() * 5 / 3600
q.asfreq("15min").plot()

plt.figure()
df.soc.ffill().diff().cumsum().asfreq("15min").plot()

df.loc[:,'soc'] = df.soc.ffill()

#plt.figure()
#cv.asfreq("15min").plot()
plt.show()

soc = -1
t_full = df.index[0]
q_full = math.nan
ci_full = -1
t_empty = df.index[0]
q_empty = math.nan
ci_empty = -1

for (t, e, f) in ef.itertuples():
    if e and f:
        # print('empty and full', t, df.loc[t])
        continue
        # break
    if e and cv.loc[t].min() < 2000:
        # open sense wire
        continue
    if e:
        if soc:
            print(t, cv.loc[t].min(), cv.loc[t].argmin(), df.soc.loc[t], "empty after", t - t_full,
                  'q=%.2f Ah' % (q.loc[t] - q_full),
                  'C=%.2f Ah' % (-float(q.loc[t] - q_full) * 100. / (100. - float(df.soc.loc[t]))),
                  )
            t_empty = t
            q_empty = q.loc[t]
            ci_empty = cv.loc[t].argmin()
            if ci_empty == ci_full:
                print('balanced, weak cell', ci_empty)
        soc = 0

    if f:
        if soc < 100:
            print(t, cv.loc[t].max(),cv.loc[t].argmax(), df.soc.loc[t], "full after", t - t_empty, 'q=%.2f Ah' % (q.loc[t] - q_empty))
            t_full = t
            q_full = q.loc[t]
            ci_full = cv.loc[t].argmax()
            if ci_empty == ci_full:
                print('balanced, weak cell', ci_full)
        soc = 100
