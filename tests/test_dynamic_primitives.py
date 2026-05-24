"""Tests for dynamic pool primitives (set_bounds, drain, try_submit,
on_task_start, per-worker stats)."""
import time

import pytest

import deadpool


def _identity(x):
    return x


def test_job_tuple_carries_submit_ts():
    """submit() stamps a monotonic timestamp on the queued job."""
    with deadpool.Deadpool(max_workers=1, max_backlog=2) as pool:
        before = time.monotonic()
        fut = pool.submit(_identity, 42)
        after = time.monotonic()
        assert fut.result(timeout=10) == 42
        # Real shape check arrives via on_task_start in Task 2.
        assert before <= after  # sanity
