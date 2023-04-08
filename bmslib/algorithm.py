import time
from typing import Optional, Union

from bmslib.bms import BmsSample
from bmslib.util import get_logger

logger = get_logger()


class BatterySwitches:
    def __init__(self, charge: Optional[bool] = None, discharge: Optional[bool] = None):
        self.charge = charge
        self.discharge = discharge

    def __str__(self):
        s = 'BatSw('
        if self.charge is not None:
            s += 'chg=%i ' % self.charge
        if self.discharge is not None:
            s += 'dis=%i ' % self.discharge
        return s.strip() + ')'

    def __getitem__(self, item):
        return self.__dict__[item]


class UpdateResult:
    def __init__(self, switches: BatterySwitches):
        self.switches = switches

    def __str__(self):
        return f'{self.switches}'


class BaseAlgorithm:
    state = None

    def update(self, sample: BmsSample) -> UpdateResult:
        raise NotImplementedError()



class SocArgs:
    def __init__(self, charge_stop, charge_start, discharge_stop=None, discharge_start=None, calibration_interval=None):
        charge_stop = float(charge_stop.strip('%'))
        charge_start = float(charge_start.strip('%'))
        assert charge_stop >= charge_start
        self.charge_stop = charge_stop
        self.charge_start = charge_start
        self.discharge_stop = discharge_stop
        self.discharge_start = discharge_start
        self.calibration_interval = calibration_interval


class SocState:
    def __init__(self, charging: bool, last_calibration_time: float):
        self.charging = charging
        self.last_calibration_time = last_calibration_time

    def __str__(self):
        return f'SocState(chg={self.charging}, t_calib={int(self.last_calibration_time)})'


class Soc(BaseAlgorithm):

    def __init__(self, args: SocArgs, state: SocState):
        self.args = args
        self.state: SocState = state
        # self._debug_state = {}

    # def restore(self, charging, last_calibration_time):

    def update(self, sample: BmsSample) -> UpdateResult:
        SOC_SPAN_MARGIN = 1 / 5

        if self.args.calibration_interval:
            need_calibration = sample.timestamp - self.state.last_calibration_time > self.args.calibration_interval
            if need_calibration:
                pass

        if self.state.charging:
            if sample.soc >= self.args.charge_stop:
                self.state.charging = False
                if sample.switches['charge']:
                    logger.info('Max Soc reached, stop charging')
                    return UpdateResult(switches=BatterySwitches(charge=False))
        else:
            if sample.soc <= self.args.charge_start:
                self.state.charging = True
                if not sample.switches['charge']:
                    logger.info('Min Soc reached, start charging')
                    return UpdateResult(switches=BatterySwitches(charge=True))


            # span = self.args.charge_stop - self.args.charge_start
            # if self.args.charge_stop - max(span*SOC_SPAN_MARGIN, 1) < sample.soc < self.args.charge_stop:
            #    if sample.switches['charge']:


def create_algorithm(repr: Union[dict, str]) -> BaseAlgorithm:
    classes = dict(soc=Soc)
    if isinstance(repr, dict):
        repr = dict(repr)
        name = repr.pop('name')
        return classes[name](
            args=SocArgs(**repr),
            state=SocState(charging=True, last_calibration_time=time.time())
        )
    else:
        raise NotImplementedError()
