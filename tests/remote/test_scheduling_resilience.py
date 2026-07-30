import socket
import threading
import time
from functools import partial
from pathlib import Path
from typing import Callable

import pytest

import deadpool
from deadpool._pool import WorkerProcess
from deadpool.remote import (
    DeadpoolClient,
    DeadpoolServer,
    Principal,
    RemoteCompatibilityError,
    RemoteConnectionLost,
    RemoteExecutorError,
    RemoteLimits,
    RemoteQueueFull,
    SubmissionState,
    UnixAddress,
    UnixListener,
)
from tests.remote.tasks import append_line, mark, multiply, wait_then_mark


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll a cross-thread/process observation without assuming scheduler speed."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before deadline")
        time.sleep(0.005)


def principal_from_hello(peer: object, hello: dict) -> Principal:
    authentication = hello.get("authentication") or {}
    return Principal(authentication.get("principal", "anonymous"))


def allow_all(principal: Principal, operation: str, metadata: dict) -> bool:
    return True


def authenticated_client(
    path: Path, principal: str, **kwargs: object
) -> DeadpoolClient:
    return DeadpoolClient(
        UnixAddress(path),
        authenticator=lambda: {"principal": principal},
        **kwargs,
    )


def disconnect_abruptly(client: DeadpoolClient) -> None:
    """Inject a real transport break without invoking clean session shutdown."""
    sock = client._socket
    assert sock is not None
    sock.shutdown(socket.SHUT_RDWR)
    sock.close()


def test_fingerprint_compatibility_and_authorization_are_end_to_end(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pool.sock"

    def authorize(principal: Principal, operation: str, metadata: dict) -> bool:
        return operation != "submit_task:multiply"

    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(path)],
        task_registry={"multiply": multiply},
        registry_fingerprint="registry-v1",
        application_fingerprint="app-v1",
        authenticator=principal_from_hello,
        authorizer=authorize,
    ).start()
    client = authenticated_client(
        path,
        "alice",
        registry_fingerprint="registry-v1",
        application_fingerprint="app-v1",
    )
    try:
        assert client.submit(multiply, 6, 7).result(5) == 42
        with pytest.raises(RemoteExecutorError, match="unauthorized"):
            client.submit_task("multiply", 2, 3).result(5)

        with pytest.raises(RemoteCompatibilityError):
            authenticated_client(
                path,
                "alice",
                registry_fingerprint="registry-v2",
                application_fingerprint="app-v1",
            )
        with pytest.raises(RemoteCompatibilityError):
            authenticated_client(
                path,
                "alice",
                registry_fingerprint="registry-v1",
                application_fingerprint="app-v2",
            )
        assert client.check_health()
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_worker_pipe_write_does_not_hold_broker_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original_submit_job = WorkerProcess.submit_job

    def blocked_submit_job(self: WorkerProcess, job: object) -> None:
        entered.set()
        if not release.wait(5):
            raise TimeoutError("worker pipe test barrier was not released")
        original_submit_job(self, job)

    monkeypatch.setattr(WorkerProcess, "submit_job", blocked_submit_job)
    path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(path)],
    ).start()
    client = DeadpoolClient(UnixAddress(path))
    marker = tmp_path / "must-not-run"
    future = client.submit(mark, marker)
    try:
        assert entered.wait(2)
        queued = client.submit(multiply, 6, 7)
        wait_until(
            lambda: queued.submission_state is SubmissionState.ACCEPTED_QUEUED
        )
        assert future.cancel_and_kill_if_running()
        release.set()

        assert future.cancelled()
        assert queued.result(5) == 42
        assert not marker.exists()
    finally:
        release.set()
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_worker_pipe_broken_pipe_retry_preserves_running_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted_pids: list[int] = []
    original_submit_job = WorkerProcess.submit_job

    def fail_first_submit(self: WorkerProcess, job: object) -> None:
        attempted_pids.append(self.pid)
        if len(attempted_pids) == 1:
            raise BrokenPipeError("injected worker pipe failure")
        original_submit_job(self, job)

    monkeypatch.setattr(WorkerProcess, "submit_job", fail_first_submit)
    path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(path)],
    ).start()
    client = DeadpoolClient(UnixAddress(path))
    try:
        future = client.submit(multiply, 6, 7)
        assert future.result(5) == 42
        assert len(attempted_pids) == 2
        assert attempted_pids[0] != attempted_pids[1]
        assert future.pid == attempted_pids[-1]
        assert server.get_statistics()["remote_tasks_running"] == 1
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_global_admission_recovers_for_another_principal(tmp_path: Path) -> None:
    path = tmp_path / "pool.sock"
    limits = RemoteLimits(
        max_pending_per_session=1,
        max_pending_per_principal=1,
        max_pending_global=1,
    )
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(path)],
        authenticator=principal_from_hello,
        authorizer=allow_all,
        limits=limits,
    ).start()
    first = authenticated_client(path, "alice", limits=limits)
    second = authenticated_client(path, "bob", limits=limits)
    release = tmp_path / "release"
    marker_path = tmp_path / "first"
    try:
        active = first.submit(wait_then_mark, release, marker_path, "active")
        wait_until(active.running)
        with pytest.raises(RemoteQueueFull):
            second.submit(multiply, 2, 3).result(5)

        release.touch()
        assert active.result(5) == "active"
        wait_until(lambda: server.get_statistics()["remote_retained_outcomes"] == 0)
        assert second.submit(multiply, 6, 7).result(5) == 42
    finally:
        release.touch(exist_ok=True)
        first.shutdown(cancel_futures=True)
        second.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_priority_and_principal_fairness_are_observable(tmp_path: Path) -> None:
    path = tmp_path / "pool.sock"
    limits = RemoteLimits(max_staged_tasks=1)
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(path)],
        authenticator=principal_from_hello,
        authorizer=allow_all,
        limits=limits,
    ).start()
    alice = authenticated_client(path, "alice", limits=limits)
    bob = authenticated_client(path, "bob", limits=limits)
    release = tmp_path / "release"
    blocker_marker = tmp_path / "blocker"
    order_path = tmp_path / "order"
    try:
        blocker = alice.submit(
            wait_then_mark, release, blocker_marker, "blocker", deadpool_priority=5
        )
        wait_until(blocker.running)
        alice_one = alice.submit(
            append_line, order_path, "alice-1", deadpool_priority=2
        )
        wait_until(lambda: alice.get_status(alice_one) == "ACCEPTED_QUEUED")
        alice_two = alice.submit(
            append_line, order_path, "alice-2", deadpool_priority=2
        )
        wait_until(lambda: alice.get_status(alice_two) == "ACCEPTED_QUEUED")
        bob_one = bob.submit(append_line, order_path, "bob-1", deadpool_priority=2)
        wait_until(lambda: bob.get_status(bob_one) == "ACCEPTED_QUEUED")
        urgent = bob.submit(append_line, order_path, "urgent", deadpool_priority=0)
        wait_until(lambda: bob.get_status(urgent) == "ACCEPTED_QUEUED")
        queued = (alice_one, alice_two, bob_one, urgent)

        release.touch()
        assert blocker.result(5) == "blocker"
        assert [future.result(5) for future in queued] == [
            "alice-1",
            "alice-2",
            "bob-1",
            "urgent",
        ]
        assert order_path.read_text().splitlines() == [
            "urgent",
            "alice-1",
            "bob-1",
            "alice-2",
        ]
    finally:
        release.touch(exist_ok=True)
        alice.shutdown(cancel_futures=True)
        bob.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


@pytest.mark.parametrize(
    ("policy", "first_runs", "queued_runs"),
    [
        ("cancel_queued", True, False),
        ("continue", True, True),
        ("terminate", False, False),
    ],
)
def test_disconnect_policies_have_observable_worker_side_effects(
    tmp_path: Path, policy: str, first_runs: bool, queued_runs: bool
) -> None:
    path = tmp_path / "pool.sock"
    limits = RemoteLimits(max_staged_tasks=1)
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(path)],
        limits=limits,
        disconnect_policy=policy,
    ).start()
    client = DeadpoolClient(UnixAddress(path), limits=limits)
    release = tmp_path / "release"
    first_marker = tmp_path / "first"
    queued_marker = tmp_path / "queued"
    try:
        active = client.submit(wait_then_mark, release, first_marker, "first")
        wait_until(active.running)
        queued = client.submit(mark, queued_marker, "queued")
        wait_until(
            lambda: client.get_status(queued) == "ACCEPTED_QUEUED"
            and queued.submission_state is SubmissionState.ACCEPTED_QUEUED
        )

        disconnect_abruptly(client)
        wait_until(lambda: server.get_statistics()["remote_connections"] == 0)
        with pytest.raises(RemoteConnectionLost):
            active.result(2)
        with pytest.raises(RemoteConnectionLost):
            queued.result(2)
        release.touch()

        recovery = DeadpoolClient(UnixAddress(path), limits=limits)
        try:
            assert recovery.submit(multiply, 6, 7).result(5) == 42
        finally:
            recovery.shutdown(cancel_futures=True)

        assert first_marker.exists() is first_runs
        assert queued_marker.exists() is queued_runs
    finally:
        release.touch(exist_ok=True)
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)
