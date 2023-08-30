import time
from typing import Optional, Union

from bmslib.bms import BmsSample
from bmslib.util import get_logger, dict_to_short_string

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

    def __init__(self, name: str):
        self.name = name

    def update(self, sample: BmsSample) -> UpdateResult:
        raise NotImplementedError()


class SocArgs:
    def __init__(self, charge_stop, charge_start='100%', discharge_stop=None, discharge_start=None,
                 calibration_interval=None):
        charge_stop = float(charge_stop.strip('%'))
        charge_start = float(charge_start.strip('%'))
        # assert charge_stop >= charge_start

        self.charge_stop = charge_stop
        self.charge_start = charge_start
        self.discharge_stop = discharge_stop
        self.discharge_start = discharge_start
        self.calibration_interval = calibration_interval

    def __str__(self):
        return dict_to_short_string(self.__dict__)


class SocState:
    def __init__(self, charging: bool, last_calibration_time: float):
        self.charging = charging
        self.last_calibration_time = last_calibration_time

    def __str__(self):
        return f'SocState(chg={self.charging}, t_calib={int(self.last_calibration_time)})'


class Soc(BaseAlgorithm):

    def __init__(self, name, args: SocArgs, state: SocState):
        super().__init__(name=name)
        self.args = args
        self.state: SocState = state
        # self._debug_state = {}

    # def restore(self, charging, last_calibration_time):

    def update(self, sample: BmsSample) -> UpdateResult:
        # SOC_SPAN_MARGIN = 1 / 5

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
            if sample.soc <= min(self.args.charge_start, self.args.charge_stop - 0.2):
                self.state.charging = True
                if not sample.switches['charge']:
                    logger.info('Min Soc reached, start charging')
                    return UpdateResult(switches=BatterySwitches(charge=True))

            # span = self.args.charge_stop - self.args.charge_start
            # if self.args.charge_stop - max(span*SOC_SPAN_MARGIN, 1) < sample.soc < self.args.charge_stop:
            #    if sample.switches['charge']:


# noinspection PyShadowingBuiltins
def create_algorithm(repr: Union[dict, str], bms_name=None) -> BaseAlgorithm:
    classes = dict(soc=Soc)
    args, kwargs = [], {}
    if isinstance(repr, dict):
        repr = dict(repr)
        name = repr.pop('name')
        kwargs = repr
    else:
        repr = repr.strip().split(' ')
        name = repr.pop(0)
        args = repr

    from bmslib.store import store_algorithm_state
    state = store_algorithm_state(bms_name, algorithm_name=name)
    algo = classes[name](
        name=name,
        args=SocArgs(*args, **kwargs),
        state=SocState(**state) if state else SocState(charging=True, last_calibration_time=time.time())
    )

    if state:
        logger.info('Restored %s algo [args=%s] state %s', name, algo.args, dict_to_short_string(state))
    else:
        logger.info('Initialized %s algo [args=%s]', name, algo.args)

    return algo
