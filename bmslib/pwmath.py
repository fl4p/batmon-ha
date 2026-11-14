import math


class EWMA:
    # Implement Exponential Weighted Moving Average
    def __init__(self, span: int):
        self.alpha = math.nan
        self.y = math.nan
        self.update_span(span)

    def update_span(self, span):
        self.alpha = (2 / (span + 1))

    def reset(self):
        """Forget accumulated history. The next add() seeds the filter with its value."""
        self.y = math.nan

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

    def add(self, x) -> float:
        if math.isnan(x):
            return self.last
        if math.isnan(self.last):
            self.last = x
        self.ewma.add(x)
        # quantize(mean((last,x,x))
        m = (self.last + 2 * self.ewma.value) / 3
        self.last = round(m * 2 / self.inp_q) * .5 * self.inp_q
        return self.last


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
