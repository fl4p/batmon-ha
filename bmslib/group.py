import asyncio
import math
import statistics
from copy import copy
from typing import Dict, Iterable, List

from bmslib.bms import BmsSample
from bmslib.bt import BtBms
from bmslib.util import get_logger


class BmsGroup:

    def __init__(self, name):
        self.name = name
        self.bms_names = list()
        self.samples: Dict[str, BmsSample] = {}
        self.voltages: Dict[str, List[int]] = {}
        # self.max_sample_age = 0

    def update(self, bms: BtBms, sample: BmsSample):
        assert bms.name in self.bms_names, "bms %s not in group %s" % (bms.name, self.bms_names)
        self.samples[bms.name] = copy(sample)

    def update_voltages(self, bms: BtBms, voltages: List[int]):
        assert bms.name in self.bms_names, "bms %s not in group %s" % (bms.name, self.bms_names)
        self.voltages[bms.name] = copy(voltages)

    def fetch(self) -> BmsSample:
        # ts_expire = time.time() - self.max_sample_age
        # expired = set(k for k,s in self.samples.items() if s.timestamp < ts_expire)
        return sum_parallel(self.samples.values())

    def fetch_voltages(self):
        try:
            return sum((self.voltages[name] for name in self.bms_names), [])
        except KeyError as e:
            raise GroupNotReady(e)


class GroupNotReady(Exception):
    pass
    # TODO rename GroupMissingData ?



class VirtualGroupBms:
    # TODO inherit from bms base class
    def __init__(self, address: str, name=None, verbose_log=False, **kwargs):
        self.address = address
        self.name = name
        self.group = BmsGroup(name)
        self.verbose_log = verbose_log
        self.members: List[BtBms] = []
        self.logger = get_logger(verbose_log)

    def __str__(self):
        return 'VirtualGroupBms(%s,[%s])' % (self.name, self.address)

    @property
    def is_connected(self):
        return set(self.group.samples.keys()) == set(self.group.bms_names)

    @property
    def is_virtual(self):
        return True

    @property
    def connect_time(self):
        return max(bms.connect_time for bms in self.members)

    def debug_data(self):
        return "missing %s" % (set(self.group.bms_names) - set(self.group.samples.keys()))

    async def fetch(self) -> BmsSample:
        # TODO wait for update with timeout
        return self.group.fetch()

    async def fetch_voltages(self):
        return self.group.fetch_voltages()

    def set_keep_alive(self, keep):
        pass

    def add_member(self, bms: BtBms):
        self.group.bms_names.append(bms.name)
        self.members.append(bms)

    def get_member_refs(self):
        return set(filter(bool, self.address.split(',')))

    def get_member_names(self):
        return self.group.bms_names

    async def connect(self):
        for i in range(10):
            if self.is_connected:
                return
            await asyncio.sleep(0.2)

        raise GroupNotReady("group %s waiting for member data %s" % (self.name, self.debug_data()))

    async def disconnect(self):
        pass

    async def __aenter__(self):
        await self.connect()

    async def __aexit__(self, *args):
        pass

    def __await__(self):
        pass

    async def set_switch(self, switch: str, state: bool):
        for bms in self.members:
            try:
                await bms.set_switch(switch, state)
            except Exception as ex:
                self.logger.error("Group %s failed to set %s switch for %s: %s", self.name, switch, bms.name, ex)

    async def fetch_device_info(self):
        raise NotImplementedError()


def is_finite(x):
    return x is not None and math.isfinite(x)


def finite_or_fallback(x, fallback):
    return x if is_finite(x) else fallback


def sum_parallel(samples: Iterable[BmsSample]) -> BmsSample:
    return BmsSample(
        voltage=statistics.mean(s.voltage for s in samples),
        current=sum(s.current for s in samples),
        power=sum(s.power for s in samples),
        charge=sum(s.charge for s in samples),
        capacity=sum(s.capacity for s in samples),
        cycle_capacity=sum(s.cycle_capacity for s in samples),
        num_cycles=statistics.mean(s.num_cycles for s in samples),
        soc=sum(s.soc * s.capacity for s in samples) / sum(s.capacity for s in samples),
        temperatures=sum(((s.temperatures or []) for s in samples), []),
        mos_temperature=max((s.mos_temperature for s in samples if is_finite(s.mos_temperature)), default=math.nan),
        switches={k: v for s in samples for k, v in (s.switches or {}).items()},
        timestamp=min(s.timestamp for s in samples),
    )
