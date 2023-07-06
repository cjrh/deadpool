import asyncio
import logging
import os
import queue
import signal
import sys
import time
import unittest
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


class TestDeadPool(unittest.TestCase):
    async def test_acquire(self):
        async with deadpool.DeadPool(size=2) as pool:
            fut1 = pool.acquire()
            fut2 = pool.acquire()
            await asyncio.sleep(0.1)  # ensure both workers are running
            self.assertEqual(pool.num_workers(), 2)
            pool.release(await fut1)
            pool.release(await fut2)
        self.assertEqual(pool.num_workers(), 0)

    async def test_concurrent_tasks(self):
        async with deadpool.DeadPool(size=2) as pool:
            results = await asyncio.gather(
                pool.run_in_executor(test_func), pool.run_in_executor(test_func)
            )
            self.assertEqual(len(results), 2)
            self.assertIn(42, results)

    async def test_oversubscription(self):
        async with deadpool.DeadPool(size=2) as pool:
            fut1 = pool.acquire()
            fut2 = pool.acquire()
            fut3 = pool.acquire()
            await asyncio.sleep(0.1)  # ensure all workers are running
            self.assertEqual(pool.num_workers(), 2)
            pool.release(await fut1)
            pool.release(await fut2)
            pool.release(await fut3)
        self.assertEqual(pool.num_workers(), 0)

    async def test_closed_pool(self):
        pool = deadpool.DeadPool(size=2)
        await pool.acquire()
        await pool.acquire()
        pool.close()
        with self.assertRaises(RuntimeError):
            await pool.acquire()

    async def test_worker_exceptions(self):
        async def failing_func():
            await asyncio.sleep(0.1)
            raise ValueError("failed")

        async with deadpool.DeadPool(size=2) as pool:
            results = await asyncio.gather(
                pool.run_in_executor(failing_func), pool.run_in_executor(test_func)
            )
            self.assertEqual(len(results), 2)
            self.assertIn(42, results)
            self.assertIsInstance(results[0], ValueError)


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
def test_simple(malloc_threshold):
    with deadpool.Deadpool(
        malloc_trim_rss_memory_threshold_bytes=malloc_threshold
    ) as exe:
        fut = exe.submit(t, 0.5)
        result = fut.result()

    assert result == 0.5

    # Outside the context manager, no new tasks
    # can be submitted.
    with pytest.raises(deadpool.PoolClosed):
        exe.submit(f)


def test_simple_batch(logging_initializer):
    with deadpool.Deadpool(max_workers=1, initializer=logging_initializer) as exe:
        futs = [exe.submit(t, 0.1) for _ in range(2)]
        results = [fut.result() for fut in futs]

    assert results == [0.1] * 2


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


@contextmanager
def elapsed():
    t0 = time.perf_counter()
    try:
        yield
    finally:
        t1 = time.perf_counter()
        print(f"elapsed: {t1 - t0:.4g} sec")
