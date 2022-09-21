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

    print("got result:", result)
    assert result == 123

    # Outside the context manager, no new tasks
    # can be submitted.
    with pytest.raises(deadpool.PoolClosed):
        exe.submit(f)


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


@pytest.mark.parametrize("raises", [False, True])
def test_simple_init(raises):
    with deadpool.Deadpool(
        initializer=init,
        initargs=(1, raises),
        finalizer=finit,
        finalargs=(3, raises),
    ) as exe:
        fut = exe.submit(f)
        result = fut.result()

    print("got result:", result)
    assert result == 123


def test_timeout():
    with elapsed():
        with deadpool.Deadpool() as exe:
            fut = exe.submit(t, timeout=1.0)

            with pytest.raises(deadpool.TimeoutError, match="timed out"):
                fut.result()


@pytest.mark.parametrize(
    "exc_type",
    [
        Exception,
        CancelledError,
        BaseException,
    ],
)
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
    time.sleep(duration)
    return duration


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
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


def test_pid_callback():
    collector = []

    with deadpool.Deadpool(max_workers=5) as exe:
        f1 = exe.submit(k, 1)

        def pid_callback(pid: int):
            collector.append(pid)

        f1.add_pid_callback(pid_callback)

    assert collector and isinstance(collector[0], int)


def f_sub():
    with deadpool.Deadpool() as exe:
        fut = exe.submit(g_sub)
        return fut.result()


def g_sub():
    with deadpool.Deadpool() as exe:
        futs = exe.map(time.sleep, [55.0] * 10)

    return 123


def test_sub_sub_process():
    with deadpool.Deadpool(max_workers=5) as exe:
        f1 = exe.submit(f_sub)
        time.sleep(0.5)
        assert f1.pid
        # Note: this doesn't kill the children, only the subprocess
        # of the task itself. The children continue to run.
        os.kill(f1.pid, signal.SIGKILL)
        with pytest.raises(deadpool.ProcessError):
            f1.result()


class MyBadException(Exception):
    def __init__(self, a, b, c):
        self.a = a
        self.b = b
        self.c = c


class MyBadExceptionSetState(Exception):
    def __init__(self, a, b, c):
        self.a = a
        self.b = b
        self.c = c

    def __setstate__(self, d):
        raise ValueError("I failed to unpickle")


class MyBadExceptionReduce(Exception):
    def __init__(self, a, b, c):
        self.a = a
        self.b = b
        self.c = c

    def __reduce__(self):
        return None


class MyBadExceptionReduceRaise(Exception):
    def __init__(self, a, b, c):
        self.a = a
        self.b = b
        self.c = c

    def __reduce__(self):
        raise ValueError("I failed to pickle")


def raise_custom_exception(exc_class):
    raise exc_class(1, 2, 3)


@pytest.mark.parametrize(
    "raises,exc_type,match",
    [
        (MyBadException, MyBadException, "(1, 2, 3)"),
        (MyBadExceptionSetState, ValueError, "failed to unpickle"),
        (MyBadExceptionReduce, deadpool.ProcessError, "completed unexpectedly"),
        (MyBadExceptionReduceRaise, deadpool.ProcessError, "completed unexpectedly"),
    ],
)
def test_bad_exception(raises, exc_type, match):
    with deadpool.Deadpool() as exe:
        fut = exe.submit(raise_custom_exception, raises)

        with pytest.raises(exc_type, match=match):
            result = fut.result()


@contextmanager
def elapsed():
    t0 = time.perf_counter()
    try:
        yield
    finally:
        t1 = time.perf_counter()
        print(f"elapsed: {t1 - t0:.4g} sec")
