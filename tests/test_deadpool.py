import os
import signal
from contextlib import contextmanager
import time
from concurrent.futures import CancelledError, as_completed

import pytest

import deadpool


def f():
    return 123


def t(duration=10.0):
    time.sleep(duration)


def f_err(exception_class, *args, **kwargs):
    raise exception_class(*args, **kwargs)


def test_abc():
    assert 1 == 1


def test_simple():
    with deadpool.Deadpool() as exe:
        fut = exe.submit(f)
        result = fut.result()

    print('got result:', result)
    assert result == 123


def init(x, error=False):
    if error:
        raise Exception(f"{x}")
    else:
        print(x)


def finit(x, error=False):
    if error:
        raise Exception(f"{x}")
    else:
        print(x)


@pytest.mark.parametrize('raises', [False, True])
def test_simple_init(raises):
    with deadpool.Deadpool(
        initializer=init,
        initargs=(1, raises),
        finalizer=finit,
        finalargs=(3, raises),
    ) as exe:
        fut = exe.submit(f)
        result = fut.result()

    print('got result:', result)
    assert result == 123


def test_timeout():
    with elapsed():
        with deadpool.Deadpool() as exe:
            fut = exe.submit(t, timeout=1.0)

            with pytest.raises(deadpool.TimeoutError):
                fut.result()


@pytest.mark.parametrize('exc_type', [
    Exception,
    CancelledError,
    BaseException,
])
def test_exception(exc_type):
    with deadpool.Deadpool() as exe:
        fut = exe.submit(f_err, exc_type)

        with pytest.raises(exc_type):
            fut.result()

def ac(t0, duration=0.01):
    t1 = time.perf_counter()
    time.sleep(duration)
    return t1 - t0


def test_throttle_as_completed():
    with deadpool.Deadpool(max_workers=2) as exe:
        t0 = time.perf_counter()
        futs = [exe.submit(ac, t0) for _ in range(20)]
        results = [f"{f.result() * 1000 // 10:.0f}" for f in as_completed(futs)]

    print(results)
    assert len(results) == 20


def m(x):
    return x + 10


def test_map():
    with deadpool.Deadpool(max_workers=5) as exe:
        results = list(exe.map(m, range(10)))

    assert results == [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]


def k(duration=1):
    print('hi')
    time.sleep(duration)
    print('hi2')
    return duration


@pytest.mark.parametrize('sig', [signal.SIGTERM, signal.SIGKILL])
def test_kill(sig):
    with deadpool.Deadpool(max_workers=5) as exe:
        f1 = exe.submit(k, 3)
        f2 = exe.submit(k, 1)
        time.sleep(0.5)
        print(f"{f1.pid=}")
        assert f1.pid
        os.kill(f1.pid, sig)
        exe.submit(k, 1)
        exe.submit(k, 1)

        with pytest.raises(deadpool.ProcessError):
            f1.result()

        assert f2.result() == 1


@contextmanager
def elapsed():
    t0 = time.perf_counter()
    try:
        yield
    finally:
        t1 = time.perf_counter()
        print(f'elapsed: {t1 - t0:.4g} sec')

