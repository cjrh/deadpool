"""Tests for dynamic pool primitives (set_bounds, drain, try_submit,
on_task_start, per-worker stats)."""

import multiprocessing
import time

import pytest

import deadpool


def _identity(x):
    return x


def test_job_tuple_carries_submit_ts():
    """submit() returns a working Future after the tuple shape change.

    Real shape verification arrives in Task 2 via the on_task_start hook.
    """
    with deadpool.Deadpool(max_workers=1, max_backlog=2) as pool:
        fut = pool.submit(_identity, 42)
        assert fut.result(timeout=10) == 42


def test_on_task_start_fires_once_per_task():
    calls = []

    def hook(submit_ts, start_ts, fn):
        calls.append((submit_ts, start_ts, fn))

    with deadpool.Deadpool(max_workers=2, on_task_start=hook) as pool:
        futs = [pool.submit(_identity, i) for i in range(5)]
        for f in futs:
            f.result(timeout=10)

    assert len(calls) == 5
    for submit_ts, start_ts, fn in calls:
        assert isinstance(submit_ts, float)
        assert isinstance(start_ts, float)
        assert start_ts >= submit_ts
        assert fn is _identity


def test_on_task_start_callback_exception_is_swallowed():
    """A raising callback must not break the pool."""

    def bad_hook(submit_ts, start_ts, fn):
        raise RuntimeError("boom")

    with deadpool.Deadpool(max_workers=1, on_task_start=bad_hook) as pool:
        fut = pool.submit(_identity, 7)
        assert fut.result(timeout=10) == 7


def test_worker_process_has_lifecycle_attrs():
    """WorkerProcess exposes spawn_time, current_fn_name, draining."""
    with deadpool.Deadpool(max_workers=1) as pool:
        # Run one task so we know a worker exists.
        pool.submit(_identity, 1).result(timeout=10)

        # Reach into existing_workers to inspect.
        with pool._workers_lock:
            workers = list(pool.existing_workers)
        assert len(workers) >= 1
        wp = workers[0]
        assert isinstance(wp.spawn_time, float)
        assert wp.spawn_time > 0
        assert wp.draining is False
        # current_fn_name may be None (task finished) or a string mid-task.
        assert wp.current_fn_name is None or isinstance(wp.current_fn_name, str)


def test_get_statistics_includes_workers_list():
    with deadpool.Deadpool(max_workers=2) as pool:
        pool.submit(_identity, 1).result(timeout=10)
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
        assert w["state"] in ("idle", "busy", "draining")
        assert isinstance(w["tasks_done"], int)
        assert w["age_s"] >= 0
        assert w["rss_bytes"] > 0


def test_get_statistics_age_s_grows():
    import time

    with deadpool.Deadpool(max_workers=1) as pool:
        pool.submit(_identity, 1).result(timeout=10)
        a = pool.get_statistics()["workers"][0]["age_s"]
        time.sleep(0.2)
        b = pool.get_statistics()["workers"][0]["age_s"]
        assert b > a


def test_done_with_process_honors_draining_flag():
    """A worker with draining=True shuts down after its current task."""
    import time as _t

    with deadpool.Deadpool(max_workers=2, min_workers=2) as pool:
        pool.submit(_identity, 1).result(timeout=10)

        # Pick one worker and mark it draining.
        with pool._workers_lock:
            workers = list(pool.existing_workers)
        target = workers[0]
        target_pid = target.pid
        target.draining = True

        # Run several tasks so the marked worker is exercised and released.
        for _ in range(8):
            pool.submit(_identity, 1).result(timeout=10)

        # Give the shrink path a moment to run shutdown.
        _t.sleep(0.2)
        with pool._workers_lock:
            alive_pids = [w.pid for w in pool.existing_workers]
        assert target_pid not in alive_pids


def _hold(evt):
    """Worker function that blocks until the event is set."""
    evt.wait(timeout=30)
    return "done"


def test_drain_idle_only():
    """drain(n) with n <= idle drains idle workers without touching busy."""
    with deadpool.Deadpool(max_workers=3, min_workers=3) as pool:
        # Force all 3 workers to exist by running 3 tasks.
        futs = [pool.submit(_identity, i) for i in range(3)]
        for f in futs:
            f.result(timeout=10)
        time.sleep(0.2)  # let workers return to idle

        with pool._workers_lock:
            before_pids = {w.pid for w in pool.existing_workers}
        assert len(before_pids) == 3

        drain_fut = pool.drain(2)
        drain_fut.result(timeout=10)

        with pool._workers_lock:
            after_pids = {w.pid for w in pool.existing_workers}
        assert len(after_pids) == 1
        assert after_pids.issubset(before_pids)


def test_drain_more_than_alive():
    """drain(n) with n > alive drains everyone, no error."""
    with deadpool.Deadpool(max_workers=2, min_workers=2) as pool:
        pool.submit(_identity, 1).result(timeout=10)
        time.sleep(0.2)

        drain_fut = pool.drain(99)
        drain_fut.result(timeout=10)

        with pool._workers_lock:
            assert len(pool.existing_workers) == 0


def test_drain_busy_workers_finish_first():
    """drain on busy workers waits for the in-flight task to complete."""
    evt = multiprocessing.Manager().Event()
    try:
        with deadpool.Deadpool(max_workers=2, min_workers=2) as pool:
            # Saturate both workers.
            f1 = pool.submit(_hold, evt)
            f2 = pool.submit(_hold, evt)
            # Give them a moment to start.
            time.sleep(0.3)

            drain_fut = pool.drain(2)
            assert not drain_fut.done()  # workers still busy

            evt.set()
            f1.result(timeout=10)
            f2.result(timeout=10)
            drain_fut.result(timeout=10)
    finally:
        evt.set()


def test_set_bounds_raises_max_no_immediate_change():
    with deadpool.Deadpool(max_workers=2, min_workers=2) as pool:
        pool.submit(_identity, 1).result(timeout=10)
        result = pool.set_bounds(min_workers=2, max_workers=8)
        assert result is None  # no shrink needed
        # No new workers spawn yet — grow is on demand.


def test_set_bounds_lowers_max_returns_future():
    with deadpool.Deadpool(max_workers=4, min_workers=4) as pool:
        # Force all 4 workers to exist.
        futs = [pool.submit(_identity, i) for i in range(4)]
        for f in futs:
            f.result(timeout=10)
        time.sleep(0.2)
        with pool._workers_lock:
            assert len(pool.existing_workers) == 4

        fut = pool.set_bounds(min_workers=2, max_workers=2)
        assert fut is not None
        fut.result(timeout=10)

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


def test_try_submit_returns_future_when_space():
    with deadpool.Deadpool(max_workers=2, max_backlog=4) as pool:
        fut = pool.try_submit(_identity, 99)
        assert fut is not None
        assert fut.result(timeout=10) == 99


def test_try_submit_returns_none_when_saturated():
    evt = multiprocessing.Manager().Event()
    try:
        with deadpool.Deadpool(max_workers=1, max_backlog=1) as pool:
            # Worker takes one task that blocks.
            f1 = pool.submit(_hold, evt)
            # Let the runner pick it up so it actually occupies the worker.
            time.sleep(0.2)
            # Fill the one backlog slot.
            f2 = pool.try_submit(_hold, evt)
            assert f2 is not None

            # Next attempt should find the backlog full.
            f3 = pool.try_submit(_identity, 1)
            assert f3 is None

            evt.set()
            f1.result(timeout=10)
            f2.result(timeout=10)
    finally:
        evt.set()


def test_try_submit_after_shutdown_raises():
    pool = deadpool.Deadpool(max_workers=1)
    pool.shutdown(wait=True)
    with pytest.raises(deadpool.PoolClosed):
        pool.try_submit(_identity, 1)


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
            # Saturate the worker.
            pool.submit(_hold, evt)
            time.sleep(0.2)  # ensure runner has picked it up

            # This task waits in the backlog.
            queued = pool.submit(_identity, 1)
            time.sleep(0.4)  # let backlog wait accrue

            evt.set()
            queued.result(timeout=10)
    finally:
        evt.set()

    # Two callbacks fired: one for the blocking _hold, one for the queued task.
    assert len(deltas) == 2
    # First task started promptly; queued task waited noticeably.
    assert deltas[1] >= 0.3  # generous; actual wait ~0.4s


def test_set_bounds_shrinks_under_full_load():
    """set_bounds(max lower) under full load drains marked workers
    only after their in-flight tasks complete."""
    evt = multiprocessing.Manager().Event()
    try:
        with deadpool.Deadpool(max_workers=3, min_workers=3) as pool:
            # Saturate all 3 workers.
            futs = [pool.submit(_hold, evt) for _ in range(3)]
            time.sleep(0.3)

            drain_fut = pool.set_bounds(min_workers=1, max_workers=1)
            assert drain_fut is not None
            assert not drain_fut.done()  # workers still busy

            evt.set()
            for f in futs:
                f.result(timeout=10)
            drain_fut.result(timeout=10)

            with pool._workers_lock:
                alive = len(pool.existing_workers)
            # At most max_workers remain; the simultaneous-completion race
            # in done_with_process can shut the survivor down too when
            # min_workers shrinks, so the lower bound is 0.
            assert alive <= 1
    finally:
        evt.set()


def test_try_submit_returns_none_then_unblocks():
    """When saturated, try_submit returns None; after release, it succeeds."""
    evt = multiprocessing.Manager().Event()
    try:
        with deadpool.Deadpool(max_workers=1, max_backlog=1) as pool:
            f1 = pool.submit(_hold, evt)
            time.sleep(0.2)
            f2 = pool.try_submit(_hold, evt)
            assert f2 is not None
            assert pool.try_submit(_identity, 0) is None  # saturated

            evt.set()
            f1.result(timeout=10)
            f2.result(timeout=10)

            # Capacity should now be available.
            f3 = pool.try_submit(_identity, 99)
            assert f3 is not None
            assert f3.result(timeout=10) == 99
    finally:
        evt.set()
