import math
import pytest

from bmslib.bms import BmsSample
from bmslib.group import BmsGroup, GroupNotReady, SeriesGroupBms, VirtualGroupBms, resolve_member_ref
from bmslib.models import get_bms_model_class
from bmslib.wire.aggregate import sum_parallel, sum_series


def test_sum_series_power():
    s1 = BmsSample(12.0, 10)
    s2 = BmsSample(24.0, 12)
    res = sum_series([s1, s2])
    assert res.voltage == 36.0
    assert res.current == 11.0
    assert res.power == 36.0 * 11.0 == 396.0
    assert res.power != 408.0


def test_sum_series_limiting_member():
    s1 = BmsSample(12.0, 5, charge=20, capacity=100)
    s2 = BmsSample(12.0, 5, charge=8, capacity=10)
    assert s1.soc == 20.0
    assert s2.soc == 80.0

    res = sum_series([s1, s2])
    assert res.charge == 8
    assert res.capacity == 10
    assert res.soc == 80.0


def test_sum_series_switches_tristate():
    s1 = BmsSample(12.0, 0, switches={'charge': True, 'discharge': True})
    s2 = BmsSample(12.0, 0, switches={'charge': True})
    res1 = sum_series([s1, s2])
    assert res1.switches == {'charge': True}
    assert res1.switches is not None and 'discharge' not in res1.switches

    s3 = BmsSample(12.0, 0, switches={'charge': True, 'discharge': True})
    s4 = BmsSample(12.0, 0, switches={'discharge': False})
    res2 = sum_series([s3, s4])
    assert res2.switches is not None and res2.switches.get('discharge') is False

    s5 = BmsSample(12.0, 0)
    s6 = BmsSample(12.0, 0)
    res3 = sum_series([s5, s6])
    assert res3.switches is None


def test_sum_series_problem_tristate():
    res = sum_series([BmsSample(12.0, 0, problem=None), BmsSample(12.0, 0, problem=None)])
    assert res.problem is None

    res = sum_series([BmsSample(12.0, 0, problem=False), BmsSample(12.0, 0, problem=None)])
    assert res.problem is None

    res = sum_series([BmsSample(12.0, 0, problem=False), BmsSample(12.0, 0, problem=False)])
    assert res.problem is False

    res = sum_series([BmsSample(12.0, 0, problem=True), BmsSample(12.0, 0, problem=None)])
    assert res.problem is True

    res = sum_series([BmsSample(12.0, 0, problem=True), BmsSample(12.0, 0, problem=False)])
    assert res.problem is True


def test_sum_series_all_unknown_charge():
    s1 = BmsSample(12.0, 0, charge=math.nan)
    s2 = BmsSample(12.0, 0, charge=math.nan)
    res = sum_series([s1, s2])
    assert math.isnan(res.charge)
    assert math.isnan(res.capacity)
    assert math.isnan(res.soc)
    assert math.isnan(res.soh)
    assert math.isnan(res.aged_capacity)


def test_sum_series_single_member():
    s = BmsSample(12.0, 5, problem_code=42, balancing_cells=0b1011)
    res = sum_series([s])
    assert res.problem_code == 42
    assert res.balancing_cells == 0b1011
    assert res.voltage == 12.0
    assert res.current == 5


def test_sum_parallel_zero_capacity():
    s1 = BmsSample(12.0, 1, soc=50, capacity=0)
    s2 = BmsSample(12.0, 1, soc=60, capacity=0)
    res = sum_parallel([s1, s2])
    assert res.capacity == 0
    assert res.soc == 55.0


def test_sum_parallel_problem_propagation():
    s1 = BmsSample(12.0, 1, problem=True)
    s2 = BmsSample(12.0, 1, problem=False)
    res = sum_parallel([s1, s2])
    assert res.problem is True


def test_sum_series_constructor_soc_derivation():
    # Observed constructor behaviour:
    # When a sample has charge=1, soc=33 (int), capacity=nan, BmsSample.__init__ derives:
    #   capacity = round(charge / soc * 100) = round(1 / 33 * 100) = 3
    # and keeps soc=33 as an int.
    #
    # When sum_series selects this limiting member and reconstructs BmsSample with
    # charge=1, capacity=3, and soc=33, BmsSample.__init__ executes:
    #   if capacity > 0 and (math.isnan(soc) or (isinstance(soc, int) and charge > 0)):
    #     soc = round(charge / capacity * 100, 2)
    # Because soc is an int, it recomputes soc = round(1 / 3 * 100, 2) = 33.33 (float).
    # Thus round-tripping through sum_series refines soc from int 33 to float 33.33.
    s1 = BmsSample(12.0, 0, charge=1, soc=33, capacity=math.nan)
    s2 = BmsSample(12.0, 0, charge=10, soc=50, capacity=20)

    assert s1.charge == 1
    assert s1.capacity == 3
    assert s1.soc == 33
    assert isinstance(s1.soc, int)

    res = sum_series([s1, s2])
    assert res.charge == 1
    assert res.capacity == 3
    assert res.soc == 33.33
    assert isinstance(res.soc, float)


def test_sum_series_other_fields():
    t1 = 1000.0
    t2 = 2000.0
    s1 = BmsSample(12.0, 10, charge=20, capacity=100,
                   total_charge_throughput=100, total_charge_net=50, num_cycles=10,
                   temperatures=[25.0, 26.0], mos_temperature=35.0,
                   runtime=3600, uptime=7200, timestamp=t1)
    s2 = BmsSample(12.0, 10, charge=20, capacity=100,
                   total_charge_throughput=200, total_charge_net=70, num_cycles=20,
                   temperatures=[27.0], mos_temperature=40.0,
                   runtime=1800, uptime=14400, timestamp=t2)
    res = sum_series([s1, s2])
    assert res.total_charge_throughput == 150.0
    assert res.total_charge_net == 60.0
    assert res.num_cycles == 15.0
    assert res.temperatures == [25.0, 26.0, 27.0]
    assert res.mos_temperature == 40.0
    assert res.runtime == 1800.0
    assert res.uptime == 7200.0
    assert res.timestamp == t1
    assert math.isnan(res.balance_current)
    assert res.balancing_cells is None
    assert res.problem_code is None


def test_nested_group_guard_isolated():
    outer = SeriesGroupBms(address='nested_grp', name='outer_grp')
    nested = VirtualGroupBms(address='dev1', name='nested_grp')

    bms_list = [outer, nested]
    bms_by_name = {bms.address: bms for bms in bms_list if not bms.is_virtual}
    bms_by_name.update({bms.name: bms for bms in bms_list})

    with pytest.raises(Exception, match="nested groups are not supported") as exc_info:
        for bms in bms_list:
            if isinstance(bms, VirtualGroupBms):
                for member_ref in bms.get_member_refs():
                    member = resolve_member_ref(bms_by_name, member_ref)
                    if getattr(member, 'is_virtual', False):
                        raise Exception(
                            "group %s contains group %s: nested groups are not supported (topology is one level deep)"
                            % (bms, member)
                        )
    assert "outer_grp" in str(exc_info.value)
    assert "nested_grp" in str(exc_info.value)


def test_group_plumbing():
    vg = VirtualGroupBms(address='b1', name='vgrp')
    assert vg.KIND == 'parallel'
    assert vg.group.kind == 'parallel'
    assert vg.supports_set_soc() is False

    sg = SeriesGroupBms(address='b1', name='sgrp')
    assert sg.KIND == 'series'
    assert sg.group.kind == 'series'
    assert sg.supports_set_soc() is False

    cls = get_bms_model_class('group_serial')
    assert cls is SeriesGroupBms

    grp = BmsGroup('test_grp', kind='series')
    grp.bms_names = ['b1', 'b2']
    with pytest.raises(GroupNotReady, match="missing"):
        grp.fetch()

    s1 = BmsSample(12.0, 5, charge=10, capacity=20)
    grp.samples['b1'] = s1
    with pytest.raises(GroupNotReady, match="b2"):
        grp.fetch()

    s2 = BmsSample(12.0, 5, charge=10, capacity=20)
    grp.samples['b2'] = s2
    res = grp.fetch()
    assert res.voltage == 24.0
