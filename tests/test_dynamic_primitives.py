"""Tests for dynamic pool primitives (set_bounds, drain, try_submit,
on_task_start, per-worker stats)."""

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
            "pid", "state", "current_fn", "tasks_done", "age_s", "rss_bytes",
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
