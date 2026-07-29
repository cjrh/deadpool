import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, as_completed, wait
from functools import partial

import pytest

import deadpool
from deadpool.remote import (
    DeadpoolClient,
    DeadpoolServer,
    RemoteExecutorError,
    RemoteQueueTimeout,
    ServerState,
    SubmissionState,
    UnixAddress,
    UnixListener,
)
from tests.remote.tasks import mark, multiply, wait_then_mark


def marker(path):
    path.write_text("ran")


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    """Poll an observable state transition under a bounded deadline."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before deadline")
        time.sleep(0.005)


def assert_completes(call: Callable[[], object], timeout: float = 1.0) -> None:
    """Require a lifecycle call to return without relying on elapsed timing."""
    completed = threading.Event()
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            call()
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    threading.Thread(target=invoke, daemon=True).start()
    assert completed.wait(timeout), "call did not complete before the deadline"
    if errors:
        raise errors[0]


def test_queued_cancellation_prevents_execution(tmp_path):
    socket_path = tmp_path / "pool.sock"
    release = tmp_path / "release"
    running_marker = tmp_path / "running"
    queued_marker = tmp_path / "queued"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        running = client.submit(wait_then_mark, release, running_marker, "done")
        wait_until(running.running)
        queued = client.submit(marker, queued_marker)
        wait_until(lambda: queued.submission_state is SubmissionState.ACCEPTED_QUEUED)
        assert queued.cancel()
        assert queued.cancelled()
        done, not_done = wait([queued], timeout=0.1)
        assert done == {queued}
        assert not not_done
        assert list(as_completed([queued], timeout=0.1)) == [queued]
        release.touch()
        assert running.result(timeout=5) == "done"
        assert running_marker.read_text() == "done"
        assert not queued_marker.exists()
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_group_cancellation_is_scoped_and_reports_counts(tmp_path):
    socket_path = tmp_path / "pool.sock"
    release = tmp_path / "release"
    running_marker = tmp_path / "running"
    queued_marker = tmp_path / "queued"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        running = client.submit(
            wait_then_mark,
            release,
            running_marker,
            "done",
            deadpool_group_id="batch",
        )
        wait_until(running.running)
        queued = client.submit(mark, queued_marker, "late", deadpool_group_id="batch")
        wait_until(lambda: queued.submission_state is SubmissionState.ACCEPTED_QUEUED)
        assert client.cancel_group("batch") == {
            "cancelled": 1,
            "running": 1,
            "terminal": 0,
            "unknown": 0,
        }
        wait_until(queued.done)
        assert queued.cancelled()
        release.touch()
        assert running.result(timeout=5) == "done"
        assert not queued_marker.exists()
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_unknown_and_hard_running_group_cancellation(tmp_path):
    socket_path = tmp_path / "pool.sock"
    release = tmp_path / "release"
    running_marker = tmp_path / "running"
    queued_marker = tmp_path / "queued"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        assert client.cancel_group("missing") == {
            "cancelled": 0,
            "running": 0,
            "terminal": 0,
            "unknown": 1,
        }

        running = client.submit(
            wait_then_mark,
            release,
            running_marker,
            "running",
            deadpool_group_id="batch",
        )
        wait_until(running.running)
        queued = client.submit(mark, queued_marker, "queued", deadpool_group_id="batch")
        wait_until(lambda: queued.submission_state is SubmissionState.ACCEPTED_QUEUED)

        assert client.cancel_group("batch", hard=True) == {
            "cancelled": 2,
            "running": 0,
            "terminal": 0,
            "unknown": 0,
        }
        with pytest.raises(CancelledError):
            running.result(2)
        with pytest.raises(CancelledError):
            queued.result(2)
        assert not running_marker.exists()
        assert not queued_marker.exists()
        assert client.submit(multiply, 6, 7).result(5) == 42
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_queue_timeout_is_distinct_from_execution_timeout(tmp_path):
    socket_path = tmp_path / "pool.sock"
    release = tmp_path / "release"
    running_marker = tmp_path / "running"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        running = client.submit(wait_then_mark, release, running_marker, "done")
        wait_until(running.running)
        queued = client.submit(
            mark,
            tmp_path / "queued",
            "late",
            deadpool_queue_timeout=0.02,
        )
        with pytest.raises(RemoteQueueTimeout):
            queued.result(timeout=5)
        release.touch()
        assert running.result(timeout=5) == "done"
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_ordinary_cancel_does_not_kill_running_task(tmp_path):
    socket_path = tmp_path / "pool.sock"
    release = tmp_path / "release"
    marker_path = tmp_path / "marker"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        future = client.submit(wait_then_mark, release, marker_path, "done")
        wait_until(future.running)
        assert not future.cancel()
        assert not future.done()
        release.touch()
        assert future.result(timeout=5) == "done"
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_hard_cancel_running_future_is_terminal_and_pool_recovers(tmp_path):
    socket_path = tmp_path / "pool.sock"
    release = tmp_path / "release"
    marker_path = tmp_path / "marker"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        future = client.submit(wait_then_mark, release, marker_path, "never")
        wait_until(future.running)
        assert future.cancel_and_kill_if_running()
        assert future.done()
        assert future.cancelled()
        with pytest.raises(CancelledError):
            future.result()
        assert not marker_path.exists()
        assert client.submit(multiply, 6, 7).result(timeout=5) == 42
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_server_shutdown_deadline_cancels_running_and_queued_work(tmp_path):
    socket_path = tmp_path / "pool.sock"
    release = tmp_path / "release"
    running_marker = tmp_path / "running"
    queued_marker = tmp_path / "queued"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        running = client.submit(wait_then_mark, release, running_marker, "running")
        wait_until(running.running)

        queued = client.submit(mark, queued_marker, "queued")
        wait_until(lambda: queued.submission_state is SubmissionState.ACCEPTED_QUEUED)

        server.shutdown(wait=False, cancel_futures=False, deadline=5)
        server.shutdown(wait=False, cancel_futures=True, deadline=0.1)
        wait_until(lambda: server.state is ServerState.STOPPED, timeout=5)

        with pytest.raises(CancelledError):
            running.result(2)
        with pytest.raises(CancelledError):
            queued.result(2)
        assert not running_marker.exists()
        assert not queued_marker.exists()
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=0)


def test_nonblocking_server_shutdown_gracefully_drains_accepted_work(tmp_path):
    socket_path = tmp_path / "pool.sock"
    release = tmp_path / "release"
    marker_path = tmp_path / "marker"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        accepted = client.submit(wait_then_mark, release, marker_path, "drained")
        wait_until(accepted.running)

        assert_completes(lambda: server.shutdown(wait=False, deadline=5))
        assert server.state is ServerState.DRAINING
        assert not accepted.done()
        with pytest.raises(RemoteExecutorError, match="server_draining"):
            client.submit(multiply, 2, 3).result(5)

        release.touch()
        assert accepted.result(5) == "drained"
        wait_until(lambda: server.state is ServerState.STOPPED, timeout=5)
        assert marker_path.read_text() == "drained"
        assert not socket_path.exists()
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=0)
