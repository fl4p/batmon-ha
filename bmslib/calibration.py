import re
from typing import List, Tuple


class CalibrationTable:
    def __init__(self, repr: str):
        if not repr:
            self.base = 1
            self.table = None
            return
        # assert repr, 'empty calibration repr str'
        repr = re.sub(r"\s+", ' ', repr).strip()
        s = repr.split(' ')
        if len(s):
            pass
        table = []
        base = float(s[0])
        for p in s[1:]:
            ps = p.split(':')
            assert len(ps) == 2, "need exactly one : in %s" % p
            table.append(tuple(map(float, ps)))
        self.table: List[Tuple[float, float]] = table
        self.base = base

    def __call__(self, x: float):
        if self.table:
            xa = abs(x)
            xs = x >= 0
            for (t, c) in self.table:
                if (t >= 0) == xs and xa <= abs(c):
                    if xa < abs(t):
                        return c/t
                    else:
                        return c/x
        return self.base

    def __bool__(self):
        return self.base != 1 or self.table is not None


def test_calibration_table():
    t = CalibrationTable('1.0')
    assert t(1) == 1
    assert t(2) == 1
    assert t(-1) == 1
    assert t(float('nan')) == 1

    t = CalibrationTable('1.2')
    assert t(1) == 1.2
    assert t(2) == 1.2

    t = CalibrationTable('1.2 2:1.4')
    assert t(1) == 1.4
    assert t(2) == 1.4
    assert t(-4) == 1.2
    assert t(-2) == 1.2
    assert t(-1) == 1.2
    assert t(0) == 1.4

    t = CalibrationTable('1.1 -2:1.8')
    assert t(1) == 1.1
    assert t(-1) == 1.8
    assert t(2) == 1.1
    assert t(-4) == 1.1
    assert t(-2) == 1.8
    assert t(-1) == 1.8
    assert t(0) == 1.1


if __name__ == "__main__":
    test_calibration_table()
