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
