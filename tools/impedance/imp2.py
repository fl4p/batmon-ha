import matplotlib
import matplotlib.pyplot as plt
import numpy as np

from tools.impedance.datasets import ant24_2023_07
df = ant24_2023_07()

matplotlib.use('MacOSX')

fig, ax = plt.subplots(2, 1)

df = df.iloc[9000:16000]
df = df[df.i.abs() > 1]

ax[0].plot(df.u, label='U')
ax[1].plot(df.i, label='I')

# plt.show()

# plt.figure()
fig, ax = plt.subplots(1, 1)
try:
    ax.scatter(x=df.i, y=df.u, marker='.', s=1)
    ax.scatter(x=[df.i.mean()], y=[df.u.mean()], marker='x')
except Exception as e:
    print(e)



m, u0 = cov(df.i, df.u)

df_nzi = df[df.i.abs() > 1]
df_nzi.i = 1/df_nzi.i
corr = df_nzi.corr().iloc[0, 1]

ax.plot(df.i, m * df.i + u0, 'r', label='ols %.2f mOhm (u0=%.1f, corr %.3f)' % (m, u0, corr))
ax.plot(df.i, m * df.i + u0 + df.u.std(), '-.r', label=None)
ax.plot(df.i, m * df.i + u0 - df.u.std(), '-.r', label=None)
plt.grid()
plt.legend()

plt.show()
