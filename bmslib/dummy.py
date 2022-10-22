"""
This is code for a dummy BMS wich doesn't physically exist.

"""

from .bms import BmsSample
from .bt import BtBms


class DummyBt(BtBms):
    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._switches = dict(charge=True, discharge=True)

    async def connect(self, **kwargs):
        pass

    async def disconnect(self):
        pass

    async def fetch(self) -> BmsSample:
        sample = BmsSample(
            voltage=12,
            current=0,
            charge=50,
            capacity=100,
            num_cycles=3,
            temperatures=[21],
            switches=self._switches,
        )
        return sample

    async def fetch_voltages(self):
        return [3000, 3001, 3002, 3003]

    async def set_switch(self, switch: str, state: bool):
        self.logger.info('set_switch', switch, state)
