from bmslib.bms import BmsSample
from bmslib.group import sum_parallel


def test_add_parallel():

    samples = [
        BmsSample(12.2, 2, charge=33, capacity=100),
        BmsSample(12.4, 3, charge=77, capacity=100)
    ]
    assert samples[0].power == (12.2*2)
    assert samples[0].soc == 33

    ss = sum_parallel(samples)
    assert ss.voltage == 12.3
    assert ss.current == 5
    assert ss.charge == 110
    assert ss.capacity == 200
    assert ss.soc == (33+77) / 2


test_add_parallel()