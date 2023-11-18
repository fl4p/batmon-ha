"""

Battery tracker:
- detect the weakest cell:
    if same cell is empty first and full first, pack capacity is limited by this cell.
    balance of other cells then doesn't affect back capacity
    if we found the weakest cell, we can consider the battery is balanced, as balancing would not increase available cap
- track capacity through current integration TODO
- track SoC error when empty or full (TODO
- track battery eff TODO
- estimate cell charge offset by curve fitting of voltages

"""

from typing import Optional, Tuple

import numpy as np

from bmslib.util import dotdict, get_logger

logger = get_logger()


class Lifepo4:
    cell_voltage_min_valid = 2000,
    cell_voltage_max_valid = 4500,
    cell_voltage_empty = 2500,
    cell_voltage_almost_empty = 2700,
    cell_voltage_full = 3650,
    cell_voltage_almost_full = 3500,


chemistry = Lifepo4()


class BatteryTrackerState:
    def __init__(self):
        self.emptiest_cell: Optional[Tuple[int, int]] = None
        self.fullest_cell: Optional[Tuple[int, int]] = None
        self.weakest_cell: Optional[int] = None


class BatteryTracker:

    def __init__(self):
        self.state = BatteryTrackerState()

    def _detect_weakest_cell(self, cell_low, cell_high):
        max_idx, max_v = cell_high
        min_idx, min_v = cell_low
        s = self.state

        if max_v > chemistry.cell_voltage_almost_full:
            if s.emptiest_cell:
                if max_idx == s.emptiest_cell[0]:
                    logger.info("found weakest cell %d (it was empty at %s, now almost full at %s)", max_idx,
                                s.emptiest_cell[1], max_v)
                    s.weakest_cell = max_idx
                else:
                    logger.info("cell %d almost full at %s, emptiest cell was %d at %s", max_idx, max_v,
                                *s.emptiest_cell)
                    s.weakest_cell = None
            else:
                s.weakest_cell = None

        if min_v < chemistry.cell_voltage_almost_empty:
            if s.fullest_cell:
                if min_idx == s.fullest_cell[0]:
                    logger.info("found weakest cell %d (it was full at %s, now almost empty at %s)", min_idx,
                                s.fullest_cell[1], min_v)
                    s.weakest_cell = min_idx
                else:
                    logger.info("cell %d almost empty at %s, fullest cell was %d at %s", min_idx, min_v,
                                *s.fullest_cell)
                    s.weakest_cell = None
            else:
                s.weakest_cell = None

    def update_cell_voltages(self, voltages):
        min_idx = np.argmin(voltages)
        max_idx = np.argmax(voltages)

        min_v = voltages[min_idx]
        max_v = voltages[max_idx]

        if min_v < chemistry.cell_voltage_min_valid:
            logger.warn("cell %d voltage %d lower than expected", min_idx, min_v)
            return False

        if max_v > chemistry.cell_voltage_max_valid:
            logger.warn("cell %d voltage %d higher than expected", max_idx, max_v)
            return False

        s = self.state

        if not s.emptiest_cell or min_v < s.emptiest_cell[1]:
            s.emptiest_cell = (min_idx, min_v)

        if not s.fullest_cell or max_v > s.fullest_cell[1]:
            s.fullest_cell = (max_idx, max_v)

        self._detect_weakest_cell((min_idx, min_v), (max_idx, max_v))
