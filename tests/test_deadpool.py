import asyncio
import logging
import os
import queue
import signal
import sys
import time
from concurrent.futures import CancelledError, as_completed
from contextlib import contextmanager
from functools import partial

import pytest

import deadpool


@pytest.fixture()
def logging_initializer():
    return partial(logging.basicConfig, level=logging.DEBUG)


async def test_func():
    await asyncio.sleep(0.1)
    return 42


def f():
    return 123


def t(duration=10.0):
    time.sleep(duration)
    return duration


def f_err(exception_class, *args, **kwargs):
    raise exception_class(*args, **kwargs)


def test_cancel_all_futures():
    q = queue.Queue()
    futs = []
    for i in range(3):
        fut = deadpool.Future()
        futs.append(fut)
        pi = deadpool.PrioritizedItem(priority=0, item=(None, fut))
        q.put(pi)

    deadpool.cancel_all_futures_on_queue(q)

    for f in futs:
        assert f.cancelled()


@pytest.mark.parametrize("malloc_threshold", [None, 0, 1_000_000])
@pytest.mark.parametrize("daemon", [True, False])
@pytest.mark.parametrize("min_workers", [None, 0])
def test_simple(malloc_threshold, daemon, min_workers):
    with deadpool.Deadpool(
        malloc_trim_rss_memory_threshold_bytes=malloc_threshold,
        daemon=daemon,
        min_workers=min_workers,
        max_workers=10,
    ) as exe:
        fut = exe.submit(t, 0.05)
        result = fut.result()

    assert result == 0.05

    # Outside the context manager, no new tasks
    # can be submitted.
    with pytest.raises(deadpool.PoolClosed):
        exe.submit(f)


##### Env var propagation #####


def envtest(env_var):
    return os.environ.get(env_var)


def initializer(env: dict):
    os.environ.update(env or {})


def test_env_fails():
    os.environ["DEADPOOL_ENVTEST"] = "123"
    with deadpool.Deadpool() as exe:
        fut = exe.submit(envtest, "DEADPOOL_ENVTEST")
        result = fut.result()

    # Changes to `os.environ` do not propagate automatically.
    assert result is None


def test_env_noflag():
    """This test shows the manual way to DIY."""
    os.environ["DEADPOOL_ENVTEST"] = "123"
    with deadpool.Deadpool(initializer=partial(initializer, dict(os.environ))) as exe:
        fut = exe.submit(envtest, "DEADPOOL_ENVTEST")
        result = fut.result()

    assert result == "123"


def test_env_withflag():
    """This test shows the automatic way to propagate."""
    os.environ["DEADPOOL_ENVTEST"] = "123"
    with deadpool.Deadpool(propagate_environ=os.environ) as exe:
        fut = exe.submit(envtest, "DEADPOOL_ENVTEST")
        result = fut.result()

    assert result == "123"


def custom_init_set_env():
    os.environ["DEADPOOL_ENVTEST_2"] = "123" + os.environ["DEADPOOL_ENVTEST"]


def custom_init_job():
    return os.environ["DEADPOOL_ENVTEST_2"]


def test_env_withflag_custom_init():
    """This test shows how automatic propagation can be used
    with a custom initializer. Importantly, the custom initializer
    can read the environment variables set by the automatic
    propagation."""
    os.environ["DEADPOOL_ENVTEST"] = "123"
    with deadpool.Deadpool(
        # The custom initializer will set its own env var
        initializer=custom_init_set_env,
        # We also want to propagate our own environ
        propagate_environ=os.environ,
    ) as exe:
        fut = exe.submit(custom_init_job)
        result = fut.result()

    # The initializer will use the value of DEADPOOL_ENVTEST
    # to set its own env var, which our job reads back out.
    assert result == "123123"


def test_env_dynamic():
    """This test shows how we can dynamically
    update the environment and have that reflected
    in the worker processes."""
    os.environ["DEADPOOL_ENVTEST"] = "123"
    with deadpool.Deadpool(
        max_workers=1,
        max_tasks_per_child=1,
        # We also want to propagate our own environ
        propagate_environ=os.environ,
    ) as exe:
        fut = exe.submit(envtest, "DEADPOOL_ENVTEST")
        assert fut.result() == "123"

        os.environ["DEADPOOL_ENVTEST"] = "456"
        fut = exe.submit(envtest, "DEADPOOL_ENVTEST")
        assert fut.result() == "456"

        os.environ["DEADPOOL_ENVTEST"] = "789"
        fut = exe.submit(envtest, "DEADPOOL_ENVTEST")
        assert fut.result() == "789"


def test_env_dynamic_clear():
    """This test shows how we can dynamically
    update the environment and have that reflected
    in the worker processes."""
    os.environ["DEADPOOL_ENVTEST"] = "123"
    with deadpool.Deadpool(
        max_workers=10,
        # We also want to propagate our own environ
        propagate_environ=os.environ,
    ) as exe:
        fut = exe.submit(envtest, "DEADPOOL_ENVTEST")
        assert fut.result() == "123"

        os.environ["DEADPOOL_ENVTEST"] = "456"
        fut = exe.submit(envtest, "DEADPOOL_ENVTEST")
        # This is the old value because the worker is
        # a persistent worker and the environment is not
        # updated.
        assert fut.result() == "123"

        exe.clear_workers()
        fut = exe.submit(envtest, "DEADPOOL_ENVTEST")
        # Now it works because the worker has been replaced.
        assert fut.result() == "456"


##### End env var propagation #####


@pytest.mark.parametrize("malloc_threshold", [None, 0, 1_000_000])
def test_simple_partial(malloc_threshold):
    from functools import partial

    f = partial(t, 0.05)

    with deadpool.Deadpool(
        malloc_trim_rss_memory_threshold_bytes=malloc_threshold
    ) as exe:
        fut = exe.submit(f)
        result = fut.result()

    assert result == 0.05

    # Outside the context manager, no new tasks
    # can be submitted.
    with pytest.raises(deadpool.PoolClosed):
        exe.submit(f)


def test_simple_batch(logging_initializer):
    with deadpool.Deadpool(max_workers=1, initializer=logging_initializer) as exe:
        futs = [exe.submit(t, 0.1) for _ in range(2)]
        results = [fut.result() for fut in futs]

    assert results == [0.1] * 2


@pytest.mark.parametrize("ctx", ["spawn", "forkserver"])
def test_ctx(logging_initializer, ctx):
    with deadpool.Deadpool(mp_context=ctx, initializer=logging_initializer) as exe:
        fut = exe.submit(t, 0.05)
        result = fut.result()

    assert result == 0.05


def tsk(*args):
    return os.getpid()


@pytest.mark.parametrize(
    "max_tasks_per_child,pid_count",
    [
        (100, 1),
        (1, 10),
    ],
)
def test_max_tasks_per_child(logging_initializer, max_tasks_per_child, pid_count):
    kwargs = dict(max_workers=1, max_tasks_per_child=max_tasks_per_child)
    with deadpool.Deadpool(initializer=logging_initializer, **kwargs) as exe:
        futs = [exe.submit(tsk, 0) for _ in range(10)]
        pids = set(fut.result() for fut in deadpool.as_completed(futs))

    assert len(pids) == pid_count


@pytest.mark.parametrize("wait", [True, False])
@pytest.mark.parametrize("cancel_futures", [True, False])
def test_shutdown(logging_initializer, wait, cancel_futures):
    with deadpool.Deadpool(
        max_workers=1,
        shutdown_wait=wait,
        shutdown_cancel_futures=cancel_futures,
        initializer=logging_initializer,
    ) as exe:
        fut = exe.submit(f)
        result = fut.result()

    assert result == 123


@pytest.mark.parametrize("wait", [True, False])
@pytest.mark.parametrize("cancel_futures", [True, False])
def test_shutdown_manual(logging_initializer, wait, cancel_futures):
    logging.info("Test start")

    def callback(*args):
        logging.info(f"fut callback: {args=}")

    exe = deadpool.Deadpool(max_workers=2, initializer=logging_initializer)
    fut1 = exe.submit(t, 2)
    fut1.add_done_callback(callback)
    fut2 = exe.submit(t, 2)
    fut2.add_done_callback(callback)
    fut3 = exe.submit(t, 2)  # This one will not start executing
    fut3.add_done_callback(callback)

    # logging.info(f"{exe.submitted_jobs.qsize()=}")
    # logging.info(f"{exe.running_futs=}")
    time.sleep(0.5)
    logging.info(f"{exe.submitted_jobs.qsize()=}")
    logging.info(f"{exe.running_futs=}")
    exe.shutdown(wait=wait, cancel_futures=cancel_futures)
    logging.info("shutdown has unblocked")

    logging.debug(f"{fut1.pid=}")
    logging.debug(f"{fut2.pid=}")
    logging.debug(f"{fut3.pid=}")

    if wait is False:
        if cancel_futures is True:
            assert fut1.cancelled()
            assert fut2.cancelled()
            assert fut3.cancelled()
        else:
            assert fut1.result() == 2
            assert fut2.result() == 2
            assert fut3.cancelled()
    else:
        assert fut1.result() == 2
        assert fut2.result() == 2
        if cancel_futures:
            assert fut3.cancelled()
        else:
            assert fut3.result() == 2


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


def test_user_cancels_future_ahead_of_time():
    with deadpool.Deadpool(max_workers=1) as exe:
        fut1 = exe.submit(t, 1)
        fut2 = exe.submit(t, 2)
        fut2.cancel()
        result = fut1.result()

    assert result == 1
    assert fut2.cancelled()


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
            fut = exe.submit(t, deadpool_timeout=1.0)

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

        def pid_callback(fut: deadpool.Future):
            collector.append(fut.pid)

        f1.add_pid_callback(pid_callback)

    assert collector and isinstance(collector[0], int)


def f_sub():
    with deadpool.Deadpool(daemon=False) as exe:
        fut = exe.submit(g_sub)
        return fut.result()


def g_sub():
    with deadpool.Deadpool() as exe:
        _futs = exe.map(time.sleep, [55.0] * 10)

    return 123


def test_sub_sub_process():
    with deadpool.Deadpool(max_workers=5, daemon=False, mp_context="spawn") as exe:
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
        pytest.param(
            MyBadExceptionReduce,
            deadpool.ProcessError,
            "pickling it failed",
            marks=pytest.mark.skipif(
                sys.version_info < (3, 10), reason="different message"
            ),
        ),
        pytest.param(
            MyBadExceptionReduceRaise,
            deadpool.ProcessError,
            "pickling it failed",
            marks=pytest.mark.skipif(
                sys.version_info < (3, 10), reason="different message"
            ),
        ),
    ],
)
def test_bad_exception(logging_initializer, raises, exc_type, match):
    with deadpool.Deadpool(max_workers=1, initializer=logging_initializer) as exe:
        fut = exe.submit(raise_custom_exception, raises)

        with pytest.raises(exc_type, match=match):
            _result = fut.result()


def test_cancel_and_kill():
    with deadpool.Deadpool() as exe:
        fut = exe.submit(t, 10)
        time.sleep(0.5)
        fut.cancel_and_kill_if_running()
        with pytest.raises(deadpool.CancelledError):
            fut.result()


def test_trim_memory():
    """Just testing it doesn't fail."""
    deadpool.trim_memory()


leak_test_accumulator = bytearray()


def leak_bytes(n):
    leak_test_accumulator.extend(bytearray(n))


def leaker(n):
    leak_bytes(n)
    return os.getpid()


def test_max_memory(logging_initializer):
    # Verify that the memory threshold feature in deadpool
    # works as expected. This test will run 20 functions, 10
    # of which will consume 1MB of memory. The other 10 will
    # consume 0.1MB of memory. The memory threshold is set to
    # 1.5MB, so the first 10 functions should cause their workers
    # to be replaced by new workers, while the other 10 functions
    # should be able to run without requiring their workers to be
    # replaced. So we'll count the total number of subprocess PID
    # values seen by a task, and verify the result.

    leak_test_accumulator.clear()
    with deadpool.Deadpool(
        initializer=logging_initializer,
        max_workers=1,
        max_worker_memory_bytes=100_000_000,
    ) as exe:
        futs = []
        for _ in range(10):
            futs.append(exe.submit(leaker, 150_000_000))
            futs.append(exe.submit(leaker, 100_000))

        pids = set(f.result() for f in deadpool.as_completed(futs))

    # We should see 11 unique PIDs, because the first 10 functions
    # should have caused their workers to be replaced, while their
    # replacements should have been able to run the remaining 10
    # functions without being replaced.
    assert len(pids) == 11


def test_can_pickle_nested_function():
    # Verify that deadpool raises a ValueError
    # if the function can't be pickled.
    def f():
        pass

    with deadpool.Deadpool() as exe:
        fut = exe.submit(f)

        with pytest.raises(AttributeError, match="local object"):
            fut.result()


def test_can_pickle_nested_function_cf():
    """Check that stdlib works the same way."""
    from concurrent.futures import ProcessPoolExecutor

    def f():
        pass

    with ProcessPoolExecutor() as exe:
        fut = exe.submit(f)

        with pytest.raises(AttributeError, match="local object"):
            fut.result()


def test_can_pickle_lambda_function():
    # Verify that deadpool raises a ValueError
    # if the function can't be pickled.
    with deadpool.Deadpool() as exe:
        fut = exe.submit(lambda: 123)

        with pytest.raises(AttributeError, match="local object"):
            fut.result()


@contextmanager
def elapsed():
    t0 = time.perf_counter()
    try:
        yield
    finally:
        t1 = time.perf_counter()
        print(f"elapsed: {t1 - t0:.4g} sec")
