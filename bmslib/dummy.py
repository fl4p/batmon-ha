"""
This is code for a dummy BMS wich doesn't physically exist.

"""
import time

import math

from .bms import BmsSample
from .bt import BtBms


class DummyBt(BtBms):
    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._switches = dict(charge=True, discharge=True)
        self._t0 = time.time()

    async def connect(self, **kwargs):
        pass

    async def disconnect(self):
        pass

    async def fetch(self) -> BmsSample:
        sample = BmsSample(
            voltage=12 - math.sin(time.time() / 4) * .5,
            current=math.sin(time.time() / 4),
            charge=50,
            capacity=100,
            num_cycles=3,
            temperatures=[21],
            switches=self._switches,
            uptime=(time.time() - self._t0)
        )
        return sample

    async def fetch_voltages(self):
        return [3000, 3001, 3002, 3003]

    async def set_switch(self, switch: str, state: bool):
        self.logger.info('set_switch %s %s', switch, state)
        assert isinstance(state, bool)
        self._switches[switch] = state
