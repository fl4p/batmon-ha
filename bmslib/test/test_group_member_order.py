"""Group cell and temperature indices must follow configured pack order."""
from types import SimpleNamespace

import pytest

from bmslib.bms import BmsSample
from bmslib.group import VirtualGroupBms, SeriesGroupBms


@pytest.mark.parametrize('group_class', [VirtualGroupBms, SeriesGroupBms])
def test_group_indices_follow_configuration_not_hash_or_sample_arrival(group_class):
    names = ['port', 'starboard', 'bow', 'stern', 'reserve', 'house']
    group = group_class(','.join(names), name='bank')
    members = {name: SimpleNamespace(name=name) for name in names}
    for ref in group.get_member_refs():
        group.add_member(members[ref])
    # Responses can arrive in any order; both sensor lists still use config order.
    for i in reversed(range(len(names))):
        member = members[names[i]]
        group.group.update(member, BmsSample(12, 1, temperatures=[20 + i], charge=10, capacity=20))
        group.group.update_voltages(member, [3300 + i])
    assert group.group.fetch_voltages() == [3300 + i for i in range(len(names))]
    assert group.group.fetch().temperatures == [20 + i for i in range(len(names))]


@pytest.mark.parametrize('group_class', [VirtualGroupBms, SeriesGroupBms])
def test_group_temperatures_follow_member_order_when_samples_arrive_backwards(group_class):
    group = group_class('a,b', name='bank')
    a, b = SimpleNamespace(name='a'), SimpleNamespace(name='b')
    group.add_member(a)
    group.add_member(b)
    group.group.update(b, BmsSample(12, 1, temperatures=[30], charge=10, capacity=20))
    group.group.update(a, BmsSample(12, 1, temperatures=[20, 21], charge=10, capacity=20))
    assert group.group.fetch().temperatures == [20, 21, 30]


def test_member_refs_strip_whitespace_and_keep_first_occurrence():
    group = VirtualGroupBms(' a,b, a, ,c,b,', name='bank')
    assert list(group.get_member_refs()) == ['a', 'b', 'c']
