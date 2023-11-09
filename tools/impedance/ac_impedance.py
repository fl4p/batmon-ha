from tools.impedance.stats import cov


def estimate(u,i):
    assert len(u) == len(i), "lengths do not match"
    assert len(u) > 4, "not enough samples"

    if i.abs().mean() < 0.1:
        raise ValueError("i too close to 0")

    if i.std() / i.abs().mean() < 0.05:
        raise ValueError("not enough i variance")

    if u.std() / u.mean() < 0.0005:
        raise ValueError("not enough u variance")

    if u.mean() < 2000.0:
        raise ValueError("u too small")

    if u.max() - u.min() < 10:
        raise ValueError("u range too small")

    res, u0 = cov(i, u)

    return res, u0