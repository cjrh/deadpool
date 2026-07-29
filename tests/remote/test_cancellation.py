import time
from concurrent.futures import CancelledError
from functools import partial

import pytest

import deadpool
from deadpool.remote import (
    DeadpoolClient,
    DeadpoolServer,
    RemoteQueueTimeout,
    ServerState,
    UnixAddress,
    UnixListener,
)


def delayed(value, delay):
    time.sleep(delay)
    return value


def marker(path):
    path.write_text("ran")


def test_queued_cancellation_prevents_execution(tmp_path):
    socket_path = tmp_path / "pool.sock"
    marker_path = tmp_path / "marker"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        running = client.submit(delayed, "done", 0.3)
        queued = client.submit(marker, marker_path)
        deadline = time.monotonic() + 2
        while client.get_status(queued) == "UNKNOWN" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert queued.cancel()
        assert queued.cancelled()
        assert running.result(timeout=5) == "done"
        assert not marker_path.exists()
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_group_cancellation_is_scoped_and_reports_counts(tmp_path):
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        running = client.submit(delayed, "done", 0.2, deadpool_group_id="batch")
        queued = client.submit(delayed, "late", 0.01, deadpool_group_id="batch")
        deadline = time.monotonic() + 3
        while not running.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        counts = client.cancel_group("batch")
        assert counts == {
            "cancelled": 1,
            "running": 1,
            "terminal": 0,
            "unknown": 0,
        }
        deadline = time.monotonic() + 2
        while not queued.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert queued.cancelled()
        assert running.result(timeout=5) == "done"
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_queue_timeout_is_distinct_from_execution_timeout(tmp_path):
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        running = client.submit(delayed, "done", 0.25)
        queued = client.submit(delayed, "late", 0.01, deadpool_queue_timeout=0.02)
        with pytest.raises(RemoteQueueTimeout):
            queued.result(timeout=5)
        assert running.result(timeout=5) == "done"
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_ordinary_cancel_does_not_kill_running_task(tmp_path):
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        future = client.submit(delayed, "done", 0.2)
        deadline = time.monotonic() + 3
        while not future.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert future.running()
        assert not future.cancel()
        assert future.result(timeout=5) == "done"
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_hard_cancel_running_future_is_terminal_and_pool_recovers(tmp_path):
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        future = client.submit(delayed, "never", 5)
        deadline = time.monotonic() + 3
        while not future.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert future.running()
        assert future.cancel_and_kill_if_running()
        assert future.done()
        assert future.cancelled()
        with pytest.raises(CancelledError):
            future.result()
        assert (
            client.submit(delayed, "recovered", 0.01).result(timeout=5) == "recovered"
        )
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_server_shutdown_policy_can_be_strengthened(tmp_path):
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    future = client.submit(delayed, "never", 5)
    deadline = time.monotonic() + 3
    while not future.running() and time.monotonic() < deadline:
        time.sleep(0.01)
    server.shutdown(wait=False, cancel_futures=False, deadline=5)
    server.shutdown(wait=False, cancel_futures=True, deadline=0.05)
    deadline = time.monotonic() + 3
    while server.state != ServerState.STOPPED and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.state == ServerState.STOPPED
    assert future.done()
    client.shutdown()


def test_nonblocking_client_and_server_shutdown(tmp_path):
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    future = client.submit(delayed, "done", 0.2)

    started = time.monotonic()
    client.shutdown(wait=False)
    assert time.monotonic() - started < 0.15
    assert future.result(timeout=5) == "done"

    started = time.monotonic()
    server.shutdown(wait=False, deadline=5)
    assert time.monotonic() - started < 0.15
    deadline = time.monotonic() + 5
    while server.state != ServerState.STOPPED and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.state == ServerState.STOPPED
    assert not socket_path.exists()
