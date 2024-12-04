import time
import atexit

import deadpool


module_pool = deadpool.Deadpool(daemon=False)
atexit.register(module_pool.shutdown)


def t(duration=10.0):
    time.sleep(duration)
    return duration


def test_module_level():
    fut = module_pool.submit(t, 0.05)
    assert fut.result() == 0.05
