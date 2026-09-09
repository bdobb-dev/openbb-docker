import re
import time
from tick_vault.ids import new_id, PREFIXES


def test_new_id_format():
    i = new_id("lst")
    assert re.fullmatch(r"lst_[0-9a-f]{32}", i)


def test_new_id_rejects_unknown_prefix():
    try:
        new_id("bogus")
        raise AssertionError("ValueError not raised")
    except ValueError:
        pass


def test_new_id_time_ordered():
    a = new_id("cap")
    time.sleep(0.002)
    b = new_id("cap")
    assert a < b  # uuid7 timestamp prefix makes string order = time order
