import math


class Integrator():

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
            if dx < self.dx_max:
                y_hat = (self._last_y + y) / 2
                self._integrator += dx * y_hat

        self._last_x = x
        self._last_y = y

    def get(self):
        return self._integrator

    def restore(self, value):
        self._integrator = value
