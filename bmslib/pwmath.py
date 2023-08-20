import math


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

if __name__ == "__main__":
    test_integrator()
    test_diff_abs_sum()