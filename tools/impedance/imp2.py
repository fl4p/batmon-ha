import matplotlib
import matplotlib.pyplot as plt

import tools.impedance.datasets as datasets
from tools.impedance.data import fetch_batmon_ha_sensors
from tools.impedance.stats import cov

# df = datasets.ant24_2023_07()

if 0:
    cell_index = 7
    df = datasets.ant24_23_11_12_fry(freq="1s", num_cells=1, cell_index=cell_index)
    df.loc[:, 'u'] = df.loc[:, str(cell_index)]
    df = df.loc[df.u.first_valid_index():]

    # df = datasets.ant24_23_11_11_fridge(num_cells=1, freq="1s", cell_index=1)

elif 1:
    df = datasets.batmon(
        # ('2023-11-09T03:30:00Z', '2023-11-09T06:30:00Z'), # fridge
        # ('2023-11-08T06:30:00Z', '2023-11-08T09:30:00Z'), # coffee
        # ('2023-11-04T06:30:00Z', '2023-11-04T10:30:00Z'), # 3cook
        # ('2023-11-09T06:30:00Z', '2023-11-09T08:30:00Z'), # pancakes
        # ('2023-11-10T10:30:00Z', '2023-11-10T10:50:00Z'),  # ehub test
        # ('2023-11-11T12:00:00Z', '2023-11-11T13:00:00Z'),  # varing sun
        ('2023-11-13T12:00:00Z', '2023-11-13T18:30:00Z'),  # recent
        freq="1s", device='ant24', cell_index=0,
    )
    df.loc[:, 'u'] = df.loc[:, str(0)]
    df.ffill(limit=1000, inplace=True)



else:
    df = datasets.batmon(
        ('2023-11-10T16:30:00Z', '2023-11-10T18:10:00Z'),  # dalyJKBalNoise
        freq="5s", device='daly_bms', num_cells=1,
    )

if 0:
    df = fetch_batmon_ha_sensors(("2022-01-23", "2022-01-25"), 'daly_bms', num_cells=1)
    df.loc[:, "u"] = df.loc[:, str(0)]
    df.loc[:, 'i'] *= -1
    df.loc[:, 'temp1'] = df.temp0
# df = df.iloc[9000:16000]
# df = df[df.i.abs() > 1]
df = df.rolling(2).mean()
df = df.rolling(3).mean().iloc[5:]
# df = df[df.i.pct_change().abs() > 0.1]
df = df[(df.u < 3330) & (df.u > 3300)]
# df.dropna(how="any", inplace=True)

matplotlib.use('MacOSX')
fig, ax = plt.subplots(4, 1)
ax[0].step(df.index, df.u, where='post', label='U', marker='.')
ax[1].step(df.index, df.i, where='post', label='I', marker='.')
ax[2].step(df.index, df.soc, where='post', label='soc', marker='.')
ax[3].step(df.index, df.temp0, where='post', label='temp0', marker='.')
ax[3].step(df.index, df.temp1, where='post', label='temp1', marker='.')
plt.legend()

# plt.show()

# df.loc[:,'i'] = df.i.pct_change() * abs(df.i).mean()
# df.loc[:,'u'] = df.u.pct_change() * df.u.mean()
# df = df.iloc[1:]

# plt.figure()
fig, ax = plt.subplots(1, 1)
try:
    ax.scatter(x=df.i, y=df.u, marker='.', s=1)
    ax.scatter(x=[df.i.mean()], y=[df.u.mean()], marker='x')
except Exception as e:
    print(e)

print('std i=%.2f u=%.2f u/i=%.1f' % (df.i.std(), df.u.std(), df.u.std() / df.i.std()))

m, u0 = cov(df.i, df.u)

df_nzi = df[df.i.abs() > 1]
df_nzi.loc[:, 'i'] = 1 / df_nzi.i
corr = df_nzi.corr().iloc[0, 1]

ax.plot(df.i, m * df.i + u0, 'r', label='ols %.2f mOhm (u0=%.1f, corr %.3f)' % (m, u0, corr))
ax.plot(df.i, m * df.i + u0 + df.u.std(), '-.r', label=None, alpha=.3)
ax.plot(df.i, m * df.i + u0 - df.u.std(), '-.r', label=None, alpha=.3)
plt.grid()
plt.legend()
plt.title("nSamples %d" % len(df.u))

plt.show()
