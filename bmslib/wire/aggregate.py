import math
import statistics
from typing import Iterable

from bmslib.bms import BmsSample


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
        total_charge_throughput=sum(s.total_charge_throughput for s in samples),
        num_cycles=statistics.mean(s.num_cycles for s in samples),
        soc=sum(s.soc * s.capacity for s in samples) / sum(s.capacity for s in samples),
        soh=sum(s.soh * s.capacity for s in samples) / sum(s.capacity for s in samples),
        aged_capacity=sum(s.aged_capacity for s in samples),
        temperatures=sum(((s.temperatures or []) for s in samples), []),
        mos_temperature=max((s.mos_temperature for s in samples if is_finite(s.mos_temperature)), default=math.nan),
        switches={k: v for s in samples for k, v in (s.switches or {}).items()},
        timestamp=min(s.timestamp for s in samples),
    )
