import math


class Integrator():

    def __init__(self, dx_max, reset=False):
        self._last_x = math.nan
        self._last_y = math.nan
        self._integrator = 0
        self.dx_max = dx_max
        self.reset = reset

    def __iadd__(self, other):
        """
        integrator_object += x, y

        :param other:
        :return:
        """
        assert isinstance(other, tuple)
        return self.add_linear(*other)

    def add_linear(self, x, y):
        # assert timestamp, ""

        if self._last_x is not None:
            dx = (x - self._last_x)
            if dx > self.dx_max:
                dy = (self._last_y + y) / 2
                self._integrator += dx * dy
            elif self.reset:
                self._integrator = 0

        self._last_x = x
        self._last_y = y

    def get(self):
        return self._integrator
