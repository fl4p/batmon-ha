from copy import copy
import math
import statistics
from typing import Dict, Iterable, List, Optional

from bmslib.bms import BmsSample


def is_finite(x):
    return x is not None and math.isfinite(x)


def finite_or_fallback(x, fallback):
    return x if is_finite(x) else fallback


def _aggregate_problem(samples: List[BmsSample]) -> Optional[bool]:
    if any(s.problem is True for s in samples):
        return True
    elif all(s.problem is not None for s in samples):
        return False
    return None


def sum_parallel(samples: Iterable[BmsSample]) -> BmsSample:
    samples = list(samples)
    total_capacity = sum(s.capacity for s in samples)
    if total_capacity == 0:
        soc_known = [s.soc for s in samples if is_finite(s.soc)]
        soc = statistics.mean(soc_known) if soc_known else math.nan
        soh_known = [s.soh for s in samples if is_finite(s.soh)]
        soh = statistics.mean(soh_known) if soh_known else math.nan
    else:
        soc = sum(s.soc * s.capacity for s in samples) / total_capacity
        soh = sum(s.soh * s.capacity for s in samples) / total_capacity

    return BmsSample(
        voltage=statistics.mean(s.voltage for s in samples),
        current=sum(s.current for s in samples),
        power=sum(s.power for s in samples),
        charge=sum(s.charge for s in samples),
        capacity=total_capacity,
        total_charge_throughput=sum(s.total_charge_throughput for s in samples),
        num_cycles=statistics.mean(s.num_cycles for s in samples),
        soc=soc,
        soh=soh,
        aged_capacity=sum(s.aged_capacity for s in samples),
        temperatures=sum(((s.temperatures or []) for s in samples), []),
        mos_temperature=max((s.mos_temperature for s in samples if is_finite(s.mos_temperature)), default=math.nan),
        switches={k: v for s in samples for k, v in (s.switches or {}).items()},
        problem=_aggregate_problem(samples),
        timestamp=min(s.timestamp for s in samples),
    )


def sum_series(samples: Iterable[BmsSample]) -> BmsSample:
    samples = list(samples)
    if len(samples) == 1:
        return copy(samples[0])

    known_v = [s.voltage for s in samples if is_finite(s.voltage)]
    voltage = sum(known_v) if known_v else math.nan

    known_i = [s.current for s in samples if is_finite(s.current)]
    current = statistics.mean(known_i) if known_i else math.nan

    power = (voltage * current) if (is_finite(voltage) and is_finite(current)) else math.nan

    finite_charge_samples = [s for s in samples if is_finite(s.charge)]
    if finite_charge_samples:
        limiting = min(finite_charge_samples, key=lambda s: s.charge)
        charge = limiting.charge
        capacity = limiting.capacity
        soc = limiting.soc
        soh = limiting.soh
        aged_capacity = limiting.aged_capacity
    else:
        charge = math.nan
        capacity = math.nan
        soc = math.nan
        soh = math.nan
        aged_capacity = math.nan

    def _mean_known(field):
        vals = [getattr(s, field) for s in samples if is_finite(getattr(s, field, None))]
        return statistics.mean(vals) if vals else math.nan

    total_charge_throughput = _mean_known('total_charge_throughput')
    total_charge_net = _mean_known('total_charge_net')
    num_cycles = _mean_known('num_cycles')

    temperatures = sum(((s.temperatures or []) for s in samples), [])

    finite_mos = [s.mos_temperature for s in samples if is_finite(s.mos_temperature)]
    mos_temperature = max(finite_mos) if finite_mos else math.nan

    finite_runtime = [s.runtime for s in samples if is_finite(s.runtime)]
    runtime = min(finite_runtime) if finite_runtime else math.nan

    finite_uptime = [s.uptime for s in samples if is_finite(s.uptime)]
    uptime = min(finite_uptime) if finite_uptime else math.nan

    finite_ts = [s.timestamp for s in samples if is_finite(s.timestamp)]
    timestamp = min(finite_ts) if finite_ts else None

    balance_current = math.nan
    balancing_cells = None
    problem_code = None

    all_switch_names = set(k for s in samples if s.switches for k in s.switches)
    if all_switch_names:
        res_switches = {}
        for sw in sorted(all_switch_names):
            has_false = False
            all_true = True
            for s in samples:
                if s.switches is not None and sw in s.switches:
                    val = s.switches[sw]
                    if val is False:
                        has_false = True
                        break
                    elif val is not True:
                        all_true = False
                else:
                    all_true = False
            if has_false:
                res_switches[sw] = False
            elif all_true:
                res_switches[sw] = True
        switches = res_switches if res_switches else None
    else:
        switches = None

    problem = _aggregate_problem(samples)

    return BmsSample(
        voltage=voltage,
        current=current,
        power=power,
        charge=charge,
        capacity=capacity,
        total_charge_throughput=total_charge_throughput,
        num_cycles=num_cycles,
        soc=soc,
        soh=soh,
        aged_capacity=aged_capacity,
        balance_current=balance_current,
        temperatures=temperatures,
        mos_temperature=mos_temperature,
        switches=switches,
        problem=problem,
        problem_code=problem_code,
        runtime=runtime,
        total_charge_net=total_charge_net,
        balancing_cells=balancing_cells,
        uptime=uptime,
        timestamp=timestamp,
    )
