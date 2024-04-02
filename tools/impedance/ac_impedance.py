import math

import numpy as np

from tools.impedance import stats
from tools.impedance.stats import cov


def estimate(u, i, ignore_nan=False):
    assert len(u) == len(i), "lengths do not match"
    assert len(u) > 4, "not enough samples"

    i_abs = i.abs()
    if i_abs.mean() < 0.1:  # or i_abs.max() < 1:
        raise ValueError("i too close to 0")

    if u.std() / i.std() > 15:
        raise ValueError("variance u/i too high")

    if i.std() / i_abs.mean() < 0.05:
        raise ValueError("not enough i variance")

    if u.std() / u.mean() < 0.0005:
        raise ValueError("not enough u variance")

    if ignore_nan:
        x = i
        y = u

        ex = np.nanmean(x)
        if math.isnan(ex):
            raise ValueError("empty x")

        ey = np.nanmean(y)
        if math.isnan(ey):
            raise ValueError("empty y")

        sx = np.nanvar(x)

        m = (np.nanmean(np.multiply(x, y)) - ex * ey) / sx

        res = m
        u0 = ey - m * ex

    else:

        res, u0 = stats.cov2(i, u)
        # res, u0 = cov(i, u)

    return res, u0
