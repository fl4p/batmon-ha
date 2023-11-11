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
    if len(x) == 0:
        raise ValueError('empty x')
    (n, m), (m2, v) = np.cov(x, y)
    # assert _1 == 1, "%s != 1 %s" % (_1, np.cov(x, y))
    assert m == m2, "m m2 %s" % m
    assert v, "v"  # todo
    return m / n, np.mean(y) - m / n * np.mean(x)


def cov2(x, y=None):
    if y is None:
        y = x.iloc[:, 1]
        x = x.iloc[:, 0]
    if len(x) == 0:
        raise ValueError('empty x')
    ex = np.mean(x)
    ey = np.mean(y)
    m = (np.multiply(x,y).mean() - ex * ey) / np.var(x)
    #m = np.mean(np.subtract(x, ex) * np.subtract(y, ey)) / np.var(x) # this is a bit slower
    return m, ey - m * ex

def cov2_nans(x, y=None):
    if y is None:
        y = x.iloc[:, 1]
        x = x.iloc[:, 0]
    if len(x) == 0:
        raise ValueError('empty x')
    ex = np.nanmean(x )
    ey = np.nanmean(y)
    m = (np.nanmean(np.multiply(x,y)) - ex * ey) / np.nanvar(x)
    #m = np.mean(np.subtract(x, ex) * np.subtract(y, ey)) / np.var(x) # this is a bit slower
    return m, ey - m * ex

