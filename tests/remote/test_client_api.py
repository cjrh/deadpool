import concurrent.futures
import logging
import os
import queue
import threading
import time
from types import SimpleNamespace
from functools import partial
from pathlib import Path
from typing import Callable

import pytest

import deadpool
from deadpool.remote import (
    DeadpoolClient,
    DeadpoolServer,
    RemoteConnectionLost,
    RemoteExecutorError,
    RemoteExecutorUnavailable,
    RemoteLimits,
    RemoteProcessError,
    RemoteProtocolError,
    RemoteQueueFull,
    RemoteResultTooLarge,
    ServerState,
    SubmissionState,
    UnixAddress,
    UnixListener,
)
from deadpool.remote._protocol import Message, MessageType
from deadpool.remote.serializer import PickleSerializer
from tests.remote.tasks import (
    delayed,
    exit_abruptly,
    make_bytes,
    multiply,
    wait_then_mark,
)


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    """Poll an observable condition under a bounded monotonic deadline."""
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


def make_pair(
    socket_path: Path, *, limits: RemoteLimits | None = None
) -> tuple[DeadpoolServer, DeadpoolClient]:
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        task_registry={"multiply": multiply},
        limits=limits,
    ).start()
    return server, DeadpoolClient(UnixAddress(socket_path), limits=limits)


class BlockingSerializer(PickleSerializer):
    """Expose invocation serialization as a deterministic lifecycle barrier."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def dumps(self, value: object, *, limit: int) -> bytes:
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("serializer test barrier was not released")
        return super().dumps(value, limit=limit)


class RejectingResultSerializer(PickleSerializer):
    """Model a result whose Python type is unavailable on the client."""

    def loads(self, payload: bytes) -> object:
        value = super().loads(payload)
        if value == "unavailable-client-type":
            raise ModuleNotFoundError("client cannot import result type")
        return value


def test_result_decode_failure_acknowledges_and_forgets_request(tmp_path: Path) -> None:
    path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(path)],
    ).start()
    client = DeadpoolClient(UnixAddress(path), serializer=RejectingResultSerializer())
    try:
        future = client.submit(delayed, "unavailable-client-type", 0)
        with pytest.raises(RemoteProtocolError, match="cannot import result type"):
            future.result(5)

        wait_until(lambda: server.get_statistics()["remote_retained_outcomes"] == 0)
        assert future.request_id not in client._futures
        assert future.request_id not in client._terminal_received
        assert client.check_health()
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_result_ack_enqueue_failure_closes_session_and_forgets_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, client = make_pair(tmp_path / "pool.sock")
    release = tmp_path / "release"
    marker = tmp_path / "marker"
    future = client.submit(wait_then_mark, release, marker, "done")
    original_enqueue = client._enqueue_control

    def reject_result_ack(kind: MessageType, control: dict) -> None:
        if kind == MessageType.RESULT_ACK:
            raise RemoteConnectionLost("client outbound control queue is full")
        original_enqueue(kind, control)

    try:
        wait_until(future.running)
        monkeypatch.setattr(client, "_enqueue_control", reject_result_ack)
        release.touch()
        assert future.result(5) == "done"

        wait_until(lambda: client._socket is None)
        wait_until(lambda: server.get_statistics()["remote_retained_outcomes"] == 0)
        assert future.request_id not in client._futures
        assert future.request_id not in client._terminal_received
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_submit_rechecks_shutdown_after_serialization(tmp_path: Path) -> None:
    path = tmp_path / "pool.sock"
    serializer = BlockingSerializer()
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(path)],
    ).start()
    client = DeadpoolClient(UnixAddress(path), serializer=serializer)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as calls:
        submission = calls.submit(client.submit, multiply, 2, 3)
        try:
            assert serializer.entered.wait(2)
            client.shutdown(wait=False)
            serializer.release.set()
            with pytest.raises(RuntimeError, match="after shutdown"):
                submission.result(2)
        finally:
            serializer.release.set()
            client.shutdown(cancel_futures=True)
            server.shutdown(cancel_futures=True, deadline=5)


def test_client_server_contexts_and_repeated_shutdown(tmp_path: Path) -> None:
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        task_registry={"multiply": multiply},
    )

    with server:
        assert server.state is ServerState.RUNNING
        with DeadpoolClient(UnixAddress(socket_path)) as client:
            assert client.submit_many([(multiply, (2, 3), {})])[0].result(5) == 6
        client.shutdown()
        with pytest.raises(RuntimeError, match="after shutdown"):
            client.submit(multiply, 1, 2)

    server.shutdown()
    assert server.state is ServerState.STOPPED
    assert not socket_path.exists()


def test_known_server_loss_rejects_submit_and_control_rpc(tmp_path: Path) -> None:
    server, client = make_pair(tmp_path / "pool.sock")
    try:
        assert client.check_health()
        server.shutdown(cancel_futures=True, deadline=0)

        def transport_is_lost() -> bool:
            try:
                client.check_health(timeout=0.05)
            except RemoteConnectionLost:
                return True
            except TimeoutError:
                return False
            return False

        wait_until(transport_is_lost)
        with pytest.raises(RemoteConnectionLost):
            client.get_status("never-existed")
        with pytest.raises(RemoteExecutorUnavailable, match="connection"):
            client.submit(multiply, 2, 3)

        assert_completes(client.shutdown)
        assert_completes(client.shutdown)
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=0)


def test_rpc_response_claims_token_before_following_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete response wins even if transport loss wakes immediately after it."""

    class PausedWake:
        def __init__(self) -> None:
            self.signaled = threading.Event()
            self.waiter_reached = threading.Event()
            self.release_waiter = threading.Event()

        def set(self) -> None:
            self.signaled.set()

        def wait(self, timeout: float | None = None) -> bool:
            if not self.signaled.wait(timeout):
                return False
            self.waiter_reached.set()
            return self.release_waiter.wait(timeout)

    class SocketStub:
        def shutdown(self, how: int) -> None:
            pass

        def close(self) -> None:
            pass

    client = object.__new__(DeadpoolClient)
    client._owner_pid = os.getpid()
    client._lock = threading.RLock()
    client._socket = SocketStub()
    client._rpc = {}
    client._transport_failed = False
    client._futures = {}
    client._terminal_received = set()
    client.control_timeout = 2.0
    request_sent = threading.Event()
    sent_control: dict[str, object] = {}

    def capture_request(kind: MessageType, control: dict) -> None:
        sent_control.update(control)
        request_sent.set()

    client._enqueue_control = capture_request
    paused_wake = PausedWake()
    client_module = __import__("deadpool.remote.client", fromlist=["client"])
    real_threading = client_module.threading
    monkeypatch.setattr(
        client_module,
        "threading",
        SimpleNamespace(Event=lambda: paused_wake),
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as calls:
        rpc = calls.submit(client.check_health)
        assert request_sent.wait(1)
        token = sent_control["nonce"]
        client._receive(Message(MessageType.PONG, {"nonce": token}))
        assert paused_wake.waiter_reached.wait(1)
        client._connection_lost(OSError("disconnect after response"))
        paused_wake.release_waiter.set()
        assert rpc.result(1) is True

    monkeypatch.setattr(client_module, "threading", real_threading)
    assert client._rpc == {}


def test_connection_loss_during_rpc_is_not_a_normal_error_response(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pool.sock"
    entered = threading.Event()
    release = threading.Event()

    def blocking_authorizer(principal: object, operation: str, metadata: dict) -> bool:
        if operation == "statistics":
            entered.set()
            release.wait(5)
        return True

    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(path)],
        authorizer=blocking_authorizer,
    ).start()
    client = DeadpoolClient(UnixAddress(path), control_timeout=1)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    rpc = executor.submit(client.get_statistics)
    try:
        assert entered.wait(2)
        server.shutdown(cancel_futures=True, deadline=0)
        with pytest.raises(RemoteConnectionLost):
            rpc.result(2)
    finally:
        release.set()
        executor.shutdown()
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=0)


def test_control_enqueue_failure_releases_rpc_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, client = make_pair(tmp_path / "pool.sock")
    release = tmp_path / "release"
    marker = tmp_path / "marker"
    running = client.submit(wait_then_mark, release, marker, "done")
    original_put = client._outbound.put

    def raise_full(*args: object, **kwargs: object) -> None:
        raise queue.Full

    try:
        wait_until(running.running)
        monkeypatch.setattr(client._outbound, "put", raise_full)
        with pytest.raises(RemoteConnectionLost, match="control queue is full"):
            client.check_health()
        assert client._rpc == {}

        with pytest.raises(RemoteConnectionLost, match="control queue is full"):
            running.cancel()
        assert client._rpc == {}
    finally:
        monkeypatch.setattr(client._outbound, "put", original_put)
        release.touch(exist_ok=True)
        running.result(5)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_worker_abrupt_death_is_typed_and_pool_recovers(tmp_path: Path) -> None:
    server, client = make_pair(tmp_path / "pool.sock")
    try:
        with pytest.raises(RemoteProcessError):
            client.submit(exit_abruptly).result(5)
        assert client.submit(multiply, 6, 7).result(5) == 42
        assert client.check_health()
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_unknown_task_and_oversized_result_do_not_poison_session(
    tmp_path: Path,
) -> None:
    limits = RemoteLimits(max_result_bytes=256)
    server, client = make_pair(tmp_path / "pool.sock", limits=limits)
    try:
        with pytest.raises(RemoteExecutorError, match="unknown_operation"):
            client.submit_task("missing.operation").result(5)
        with pytest.raises(RemoteResultTooLarge):
            client.submit(make_bytes, 2048).result(5)
        assert client.submit_task("multiply", 6, 7).result(5) == 42
        assert client.check_health()
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_status_transitions_cover_queued_running_terminal_and_unknown(
    tmp_path: Path,
) -> None:
    server, client = make_pair(tmp_path / "pool.sock")
    release = tmp_path / "release"
    marker = tmp_path / "marker"
    try:
        running = client.submit(wait_then_mark, release, marker, "first")
        wait_until(running.running)
        assert running.pid is not None
        assert client.get_status(running) == "RUNNING"

        queued = client.submit(delayed, "second", 0.01)
        wait_until(
            lambda: client.get_status(queued) == "ACCEPTED_QUEUED"
            and queued.submission_state is SubmissionState.ACCEPTED_QUEUED
        )
        assert client.get_status("unknown-request") == "UNKNOWN"

        release.touch()
        assert running.result(5) == "first"
        assert queued.result(5) == "second"
        wait_until(lambda: client.get_status(queued) == "UNKNOWN")
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_submission_rejects_empty_group_id_without_reaching_server(
    tmp_path: Path,
) -> None:
    server, client = make_pair(tmp_path / "pool.sock")
    try:
        before = server.get_statistics()["remote_tasks_received"]
        with pytest.raises(ValueError, match="group_id must be non-empty"):
            client.submit(multiply, 2, 3, deadpool_group_id="")
        assert server.get_statistics()["remote_tasks_received"] == before
        assert client.submit(multiply, 3, 4).result(5) == 12
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_per_session_admission_recovers_after_completion(tmp_path: Path) -> None:
    limits = RemoteLimits(max_pending_per_session=1)
    server, client = make_pair(tmp_path / "pool.sock", limits=limits)
    release = tmp_path / "release"
    marker = tmp_path / "marker"
    try:
        first = client.submit(wait_then_mark, release, marker, "first")
        wait_until(first.running)
        with pytest.raises(RemoteQueueFull):
            client.submit(delayed, "rejected", 0).result(5)

        release.touch()
        assert first.result(5) == "first"
        wait_until(lambda: server.get_statistics()["remote_retained_outcomes"] == 0)
        assert client.submit(delayed, "recovered", 0).result(5) == "recovered"
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_public_future_exception_pid_and_callback_isolation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server, client = make_pair(tmp_path / "pool.sock")
    release = tmp_path / "release"
    marker = tmp_path / "marker"
    callbacks: list[str] = []
    callbacks_done = threading.Event()
    caplog.set_level(logging.ERROR, logger="deadpool.remote")
    try:
        running = client.submit(wait_then_mark, release, marker, "done")
        wait_until(running.running)
        assert running.pid is not None
        release.touch()
        assert running.result(5) == "done"

        failed = client.submit(int, "not-an-int")
        error = failed.exception(5)
        assert isinstance(error, ValueError)

        def broken_callback(future: object) -> None:
            callbacks.append("broken")
            raise RuntimeError("callback exploded")

        def healthy_callback(future: object) -> None:
            callbacks.append("healthy")
            callbacks_done.set()

        callback_future = client.submit(multiply, 6, 7)
        callback_future.add_done_callback(broken_callback)
        callback_future.add_done_callback(healthy_callback)
        assert callback_future.result(5) == 42
        assert callbacks_done.wait(2)
        callback_future.add_done_callback(lambda future: callbacks.append("late"))
        wait_until(lambda: callbacks == ["broken", "healthy", "late"])
        assert "callback exploded" in caplog.text
        assert client.submit(multiply, 3, 4).result(5) == 12
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)
