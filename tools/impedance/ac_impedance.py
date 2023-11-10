from tools.impedance.stats import cov


def estimate(u,i):
    assert len(u) == len(i), "lengths do not match"
    assert len(u) > 4, "not enough samples"

    i_abs = i.abs()
    if i_abs.mean() < 0.1 or i_abs.max() < 1:
        raise ValueError("i too close to 0")

    if i.std() / i_abs.mean() < 0.05:
        raise ValueError("not enough i variance")

    if u.std() / u.mean() < 0.0005:
        raise ValueError("not enough u variance")

    if u.mean() < 2000.0:
        raise ValueError("u too small")

    u_mi = u.min()
    u_mx = u.max()
    if u_mx - u_mi < 10:
        raise ValueError("u range too small")

    if u_mi < 2000:
        raise ValueError("u min too small")

    if u_mx > 4000:
        raise ValueError("u max too large")

    res, u0 = cov(i, u)

    return res, u0