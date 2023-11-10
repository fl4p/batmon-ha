import matplotlib
import matplotlib.pyplot as plt

import tools.impedance.datasets as datasets
from tools.impedance.data import fetch_batmon_ha_sensors
from tools.impedance.stats import cov

df = datasets.ant24_2023_07()
df = datasets.batmon(
    #('2023-11-09T03:30:00Z', '2023-11-09T06:30:00Z'), # fridge
#('2023-11-08T06:30:00Z', '2023-11-08T09:30:00Z'), # coffee
#('2023-11-04T06:30:00Z', '2023-11-04T10:30:00Z'), # 3cook
#('2023-11-09T06:30:00Z', '2023-11-09T08:30:00Z'), # pancakes
('2023-11-10T10:30:00Z', '2023-11-10T10:50:00Z'), # ehub test
    freq="5s", device='jk_bms', cell_index=0,
)

if 0:
    df = fetch_batmon_ha_sensors(("2022-01-23", "2022-01-25"), 'daly_bms', num_cells=1)
    df.loc[:, "u"] = df.loc[:, str(0)]
    df.loc[:,'i'] *= -1
    df.loc[:,'temp1'] = df.temp0
#df = df.iloc[9000:16000]
#df = df[df.i.abs() > 1]
#df = df.rolling(3).mean()
df = df.rolling(3).mean()
df = df[df.i.pct_change().abs() > 0.1]
df.dropna(how="any",inplace=True)

matplotlib.use('MacOSX')
fig, ax = plt.subplots(4, 1)
ax[0].step(df.index, df.u, where='post', label='U', marker='.')
ax[1].step(df.index, df.i, where='post', label='I', marker='.')
ax[2].step(df.index, df.soc, where='post', label='soc', marker='.')
ax[3].step(df.index, df.temp0, where='post', label='temp0', marker='.')
ax[3].step(df.index, df.temp1, where='post', label='temp1', marker='.')
plt.legend()

# plt.show()

#df.loc[:,'i'] = df.i.pct_change() * abs(df.i).mean()
#df.loc[:,'u'] = df.u.pct_change() * df.u.mean()
#df = df.iloc[1:]

# plt.figure()
fig, ax = plt.subplots(1, 1)
try:
    ax.scatter(x=df.i, y=df.u, marker='.', s=1)
    ax.scatter(x=[df.i.mean()], y=[df.u.mean()], marker='x')
except Exception as e:
    print(e)

m, u0 = cov(df.i, df.u)

df_nzi = df[df.i.abs() > 1]
df_nzi.loc[:,'i'] = 1 / df_nzi.i
corr = df_nzi.corr().iloc[0, 1]

ax.plot(df.i, m * df.i + u0, 'r', label='ols %.2f mOhm (u0=%.1f, corr %.3f)' % (m, u0, corr))
ax.plot(df.i, m * df.i + u0 + df.u.std(), '-.r', label=None)
ax.plot(df.i, m * df.i + u0 - df.u.std(), '-.r', label=None)
plt.grid()
plt.legend()
plt.title("nSamples %d" % len(df.u))

plt.show()
