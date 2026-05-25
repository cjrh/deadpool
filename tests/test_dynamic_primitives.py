"""Tests for dynamic pool primitives (set_bounds, try_submit,
on_task_start, per-worker stats)."""

import multiprocessing
import time

import pytest

import deadpool


def test_job_tuple_carries_submit_ts():
    """submit() returns a working Future after the tuple shape change.

    Real shape verification is exercised below via the on_task_start hook.
    """
    with deadpool.Deadpool(max_workers=1, max_backlog=2) as pool:
        fut = pool.submit(int, 42)
        assert fut.result(timeout=10) == 42


def test_on_task_start_fires_once_per_task():
    calls = []

    def hook(submit_ts, start_ts, fn):
        calls.append((submit_ts, start_ts, fn))

    with deadpool.Deadpool(max_workers=2, on_task_start=hook) as pool:
        futs = [pool.submit(int, i) for i in range(5)]
        for f in futs:
            f.result(timeout=10)

    assert len(calls) == 5
    for submit_ts, start_ts, fn in calls:
        assert isinstance(submit_ts, float)
        assert isinstance(start_ts, float)
        assert start_ts >= submit_ts
        assert fn is int


def test_on_task_start_callback_exception_is_swallowed():
    """A raising callback must not break the pool."""

    def bad_hook(submit_ts, start_ts, fn):
        raise RuntimeError("boom")

    with deadpool.Deadpool(max_workers=1, on_task_start=bad_hook) as pool:
        fut = pool.submit(int, 7)
        assert fut.result(timeout=10) == 7


def test_worker_process_has_lifecycle_attrs():
    """WorkerProcess exposes spawn_time and current_fn_name."""
    with deadpool.Deadpool(max_workers=1) as pool:
        pool.submit(int, 1).result(timeout=10)

        with pool._workers_lock:
            workers = list(pool.existing_workers)
        assert len(workers) >= 1
        wp = workers[0]
        assert isinstance(wp.spawn_time, float)
        assert wp.spawn_time > 0
        # current_fn_name may be None (task finished) or a string mid-task.
        assert wp.current_fn_name is None or isinstance(wp.current_fn_name, str)


def test_get_statistics_includes_workers_list():
    with deadpool.Deadpool(max_workers=2) as pool:
        pool.submit(int, 1).result(timeout=10)
        stats = pool.get_statistics()
        assert "workers" in stats
        assert isinstance(stats["workers"], list)
        assert len(stats["workers"]) >= 1
        w = stats["workers"][0]
        assert set(w.keys()) >= {
            "pid",
            "state",
            "current_fn",
            "tasks_done",
            "age_s",
            "rss_bytes",
        }
        assert isinstance(w["pid"], int)
        assert w["state"] in ("idle", "busy")
        assert isinstance(w["tasks_done"], int)
        assert w["age_s"] >= 0
        assert w["rss_bytes"] > 0


def test_get_statistics_age_s_grows():
    with deadpool.Deadpool(max_workers=1) as pool:
        pool.submit(int, 1).result(timeout=10)
        a = pool.get_statistics()["workers"][0]["age_s"]
        time.sleep(0.2)
        b = pool.get_statistics()["workers"][0]["age_s"]
        assert b > a


def test_set_bounds_raises_max_no_immediate_change():
    with deadpool.Deadpool(max_workers=2, min_workers=2) as pool:
        pool.submit(int, 1).result(timeout=10)
        # New ceiling; no immediate effect. Returns None.
        assert pool.set_bounds(min_workers=2, max_workers=8) is None


def test_set_bounds_lowers_eventually_sheds_workers():
    """Lowering bounds doesn't kill workers eagerly; the existing
    shrink-when-idle path sheds them as tasks complete."""
    with deadpool.Deadpool(max_workers=4, min_workers=4) as pool:
        # Force all 4 workers to exist by running 4 tasks.
        for f in [pool.submit(int, i) for i in range(4)]:
            f.result(timeout=10)
        time.sleep(0.2)
        with pool._workers_lock:
            assert len(pool.existing_workers) == 4

        pool.set_bounds(min_workers=2, max_workers=2)

        # Bounds are updated immediately; pool converges as tasks complete.
        for f in [pool.submit(int, i) for i in range(8)]:
            f.result(timeout=10)
        time.sleep(0.2)

        with pool._workers_lock:
            alive = len(pool.existing_workers)
        assert alive == 2


def test_set_bounds_validation():
    with deadpool.Deadpool(max_workers=2) as pool:
        with pytest.raises(ValueError):
            pool.set_bounds(min_workers=-1, max_workers=4)
        with pytest.raises(ValueError):
            pool.set_bounds(min_workers=5, max_workers=2)


def test_set_bounds_after_shutdown_raises():
    pool = deadpool.Deadpool(max_workers=2)
    pool.shutdown(wait=True)
    with pytest.raises(deadpool.PoolClosed):
        pool.set_bounds(min_workers=1, max_workers=2)


def _hold(evt):
    """Worker function that blocks until the event is set."""
    evt.wait(timeout=30)
    return "done"


def test_try_submit_returns_future_when_space():
    with deadpool.Deadpool(max_workers=2, max_backlog=4) as pool:
        fut = pool.try_submit(int, 99)
        assert fut is not None
        assert fut.result(timeout=10) == 99


def test_try_submit_returns_none_when_saturated():
    evt = multiprocessing.Manager().Event()
    try:
        with deadpool.Deadpool(max_workers=1, max_backlog=1) as pool:
            f1 = pool.submit(_hold, evt)
            time.sleep(0.2)  # let the runner pick it up
            f2 = pool.try_submit(_hold, evt)
            assert f2 is not None
            # Backlog now full.
            assert pool.try_submit(int, 1) is None

            evt.set()
            f1.result(timeout=10)
            f2.result(timeout=10)
    finally:
        evt.set()


def test_try_submit_after_shutdown_raises():
    pool = deadpool.Deadpool(max_workers=1)
    pool.shutdown(wait=True)
    with pytest.raises(deadpool.PoolClosed):
        pool.try_submit(int, 1)


def test_on_task_start_reflects_backlog_wait():
    """When the pool is saturated, start_ts - submit_ts is non-trivial."""
    evt = multiprocessing.Manager().Event()
    deltas: list[float] = []

    def hook(submit_ts, start_ts, fn):
        deltas.append(start_ts - submit_ts)

    try:
        with deadpool.Deadpool(
            max_workers=1, max_backlog=2, on_task_start=hook
        ) as pool:
            pool.submit(_hold, evt)
            time.sleep(0.2)
            queued = pool.submit(int, 1)
            time.sleep(0.4)
            evt.set()
            queued.result(timeout=10)
    finally:
        evt.set()

    assert len(deltas) == 2
    assert deltas[1] >= 0.3


def test_try_submit_returns_none_then_unblocks():
    """When saturated, try_submit returns None; after release, it succeeds."""
    evt = multiprocessing.Manager().Event()
    try:
        with deadpool.Deadpool(max_workers=1, max_backlog=1) as pool:
            f1 = pool.submit(_hold, evt)
            time.sleep(0.2)
            f2 = pool.try_submit(_hold, evt)
            assert f2 is not None
            assert pool.try_submit(int, 0) is None

            evt.set()
            f1.result(timeout=10)
            f2.result(timeout=10)

            f3 = pool.try_submit(int, 99)
            assert f3 is not None
            assert f3.result(timeout=10) == 99
    finally:
        evt.set()
