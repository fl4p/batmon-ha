"""`algorithm: soc CHARGE_STOP% [CHARGE_START%]` argument parsing."""
import logging

import pytest

import bmslib.store
from bmslib.algorithm import SocAlgorithm, create_algorithm


@pytest.fixture(autouse=True)
def _no_state_file(monkeypatch):
    monkeypatch.setattr(bmslib.store, 'store_algorithm_state', lambda *a, **k: None)


def test_two_args():
    a = create_algorithm('soc 80% 70%', bms_name='b')
    assert isinstance(a, SocAlgorithm)
    assert (a.args.charge_stop, a.args.charge_start) == (80, 70)
    assert not hasattr(a.args, 'discharge_stop')


def test_one_arg_no_hysteresis():
    a = create_algorithm('soc 90%', bms_name='b')
    assert (a.args.charge_stop, a.args.charge_start) == (90, 90)


def test_extra_spaces_do_not_shift_args():
    a = create_algorithm(' soc  80%  70% ', bms_name='b')
    assert (a.args.charge_stop, a.args.charge_start) == (80, 70)


def test_extra_args_are_ignored_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        a = create_algorithm('soc 80% 70% 20% 30% 5', bms_name='b')
    assert (a.args.charge_stop, a.args.charge_start) == (80, 70)
    assert a.args.calibration_interval_s == 24 * 14 * 3600  # not taken from the 5th arg
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any('20% 30% 5' in m and 'ignored' in m for m in msgs), msgs


def test_no_warning_for_valid_args(caplog):
    with caplog.at_level(logging.WARNING):
        create_algorithm('soc 80% 70%', bms_name='b')
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_charge_start_above_stop_means_no_hysteresis():
    # doc/Algorithms.md: `soc 90% 100%` is the same as `soc 90%`
    from bmslib.algorithm import SocArgs
    a = SocArgs('90%', '100%')
    assert a.charge_stop == 90 and a.charge_start == 90
