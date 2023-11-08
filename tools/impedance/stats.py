import numpy as np


def normalize_std(s):
    return (s - s.mean()) / s.std()


def ols(x, y=None):
    if y is None:
        y = x.iloc[:, 1]
        x = x.iloc[:, 0]
    A = np.vstack([x, np.ones(len(x))]).T
    m, y0 = np.linalg.lstsq(A, y, rcond=None)[0]
    return m, y0


def cov(x, y=None):
    if y is None:
        y = x.iloc[:, 1]
        x = x.iloc[:, 0]
    (n, m), (m2, v) = np.cov(x, y)
    # assert _1 == 1, "%s != 1 %s" % (_1, np.cov(x, y))
    assert m == m2, "m m2"
    assert v, "v"  # todo
    return m / n, np.mean(y) - m / n * np.mean(x)