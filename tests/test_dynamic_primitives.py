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
