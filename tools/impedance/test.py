import math

import numpy as np

import stats


def near(a, b, e, reg=1e-6):
    if isinstance(a, tuple):
        assert len(a) == len(b)
        for i in range(len(a)):
            if not near(a[i], b[i], e):
                return False
        return True

    re = abs((abs(a) + reg) / (abs(b) + reg) - 1)
    return re < e


def test_reg_impl(x, y):
    r_ols = stats.ols(x, y)  # reference
    r_cov = stats.cov(x, y)
    r_cov2 = stats.cov2(x, y)
    r_cov3 = stats.cov2_nans(x, y)

    assert near(r_cov, r_ols, 1e-6)
    assert near(r_cov2, r_ols, 1e-6)
    assert near(r_cov3, r_ols, 1e-6)

    return True


def test1():
    x = [1, 2, 3, 4]
    y = [2, 4, 6, 8]

    r_ols = stats.ols(x, y)
    r_cov = stats.cov(x, y)
    r_cov2 = stats.cov2(x, y)


    assert near(r_cov, r_ols, 1e-6)
    assert near(r_cov2, r_ols, 1e-6)
    #assert near(r_cov3, r_ols, 1e-6)

    for i in range(10, 2000):
        x = np.random.random(i)
        y = np.random.random(i)
        assert test_reg_impl(x, y)


def test_nan():
    x = [1, 2, 3, 4, math.nan]
    y = [2, 4, 6, 8, math.nan]

    assert near(stats.cov2_nans(x, y), stats.cov2(x[:-1], y[:-1]), 1e-6)


test_nan()
test1()
