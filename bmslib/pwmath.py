import math


class EWMA:
    # Implement Exponential Weighted Moving Average
    def __init__(self, span: int):
        self.alpha = math.nan
        self.y = math.nan
        self.update_span(span)

    def update_span(self, span):
        self.alpha = (2 / (span + 1))

    def add(self, x):
        if not math.isfinite(x):
            return
        if not math.isfinite(self.y):
            self.y = x
        self.y = (1 - self.alpha) * self.y + self.alpha * x

    @property
    def value(self):
        return self.y


class LHQ:
    """
    Low-Pass hysteresis quantizer

    1. smooth the already quantized signal with a exponential weighted moving average
    2. Hysteresis
    3. Quantize to 2x input precision (add 1 bit)
    """

    def __init__(self, span=20, inp_q=0.1):
        self.ewma = EWMA(span=span)
        self.last = math.nan
        self.inp_q = inp_q

    def add(self, x):
        self.ewma.add(x)
        # quantize(mean((last,x,x))
        m = (self.last + 2 * self.ewma.value) / 3
        if math.isnan(m):
            if math.isnan(self.last):
                self.last = x
            return math.nan
        self.last = round(m * 2 / self.inp_q) * .5 * self.inp_q
        return self.last


class EWM:
    # Implement EWMA statistics mean and stddev
    def __init__(self, span: int, std_regularisation: float):
        self.avg = EWMA(span)
        self.std = EWMA(span)
        self._last_x = math.nan
        self.std_regularisation = std_regularisation

    def add(self, x):
        self.avg.add(x)
        if self.std_regularisation != 0:
            x = (-1 if x < 0 else 1) * (abs(x) + self.std_regularisation)
        ex = self._last_x
        # ex = self.avg.value
        if math.isfinite(ex):
            pct = (x - ex) / ex
            pct = min(abs(pct), 1.4)
            self.std.add(pct * pct)
        self._last_x = x

    @property
    def stddev(self):
        return abs(self.avg.value * self.std.value)

    def z_score(self, x):
        return (x - self.avg.value) / (self.stddev + self.std_regularisation)


class Integrator:
    """
    Implement a trapezoidal integration, discarding samples with dx > dx_max.
    """

    def __init__(self, name, dx_max, value=0.):
        self.name = name
        self._last_x = math.nan
        self._last_y = math.nan
        self._integrator = value
        self.dx_max = dx_max

    def __iadd__(self, other):
        """
        integrator_object += x, y

        :param other:
        :return:
        """
        assert isinstance(other, tuple)
        self.add_linear(*other)
        return self

    def add_linear(self, x, y):
        # assert timestamp, ""
        # trapezoidal sum

        if not math.isnan(self._last_x):
            dx = (x - self._last_x)
            if dx < 0:
                raise ValueError("x must be monotonic increasing (given %s, last %s)" % (x, self._last_x))
            if dx <= self.dx_max:
                y_hat = (self._last_y + y) / 2
                self._integrator += dx * y_hat

        self._last_x = x
        self._last_y = y

    def get(self):
        return self._integrator

    def restore(self, value):
        self._integrator = value


class DiffAbsSum(Integrator):
    """
    Implement a differential absolute sum, discarding samples with dx > dx_max.
    """

    def __init__(self, name, dx_max, dy_max, value=0.):
        super().__init__(name, dx_max=dx_max, value=value)
        self.dy_max = dy_max

    def add_linear(self, x, y):
        raise NotImplementedError()

    def add_diff(self, x, y):
        if not math.isnan(self._last_x):
            dx = (x - self._last_x)
            if dx < 0:
                raise ValueError("x must be monotonic increasing (given %s, last %s)" % (x, self._last_x))
            if dx <= self.dx_max:
                dy_abs = abs(y - self._last_y)
                if dy_abs <= self.dy_max:
                    self._integrator += dy_abs

        self._last_x = x
        self._last_y = y

    def __iadd__(self, other):
        assert isinstance(other, tuple)
        self.add_diff(*other)
        return self


def test_integrator():
    i = Integrator("test", dx_max=1)
    i += (0, 1)
    assert i.get() == 0
    i += (1, 1)
    assert i.get() == 1
    i += (1, 2)
    assert i.get() == 1
    i += (2, 2)
    assert i.get() == 3
    i += (3, 3)  # test trapezoid
    assert i.get() == (3 + 2.5)
    i += (5, 3)  # skip (>dt_max)
    assert i.get() == (3 + 2.5)


def test_diff_abs_sum():
    i = DiffAbsSum("test", dx_max=1, dy_max=0.1)
    i += (0, 1)
    assert i.get() == 0
    i += (1, 1)
    assert i.get() == 0
    i += (1, 1.1)
    assert i.get() == 0
    i += (2, 1.2)
    assert round(i.get(), 5) == 0.1
    i += (3, 1.25)
    assert round(i.get(), 5) == 0.15
    i += (4, 1)
    assert round(i.get(), 5) == 0.15
    i += (5, 0.95)
    assert round(i.get(), 5) == 0.2


def test_lhq():
    l = LHQ(span=2, inp_q=.1)
    l.add(0)
    assert l.add(0.1) == 0.05
    assert l.add(0.1) == 0.1


if __name__ == "__main__":
    test_integrator()
    test_diff_abs_sum()
    test_lhq()
