import time

from calc.ops import add


def test_slow_integration():
    time.sleep(45)  # simulates a slow end-to-end suite (used to demo the kill switch)
    assert add(1, 1) == 2
