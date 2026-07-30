import errno
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

import deadpool
from deadpool.remote import (
    DeadpoolClient,
    DeadpoolServer,
    RemoteLimits,
    ServerState,
    UnixAddress,
    UnixListener,
)
from deadpool.remote import _transport
from tests.remote.tasks import multiply


def pool_factory() -> deadpool.Deadpool:
    return deadpool.Deadpool(max_workers=1, mp_context="forkserver")


def test_unix_context_sets_mode_and_removes_owned_socket(tmp_path: Path) -> None:
    path = tmp_path / "pool.sock"
    with DeadpoolServer(
        pool_factory,
        listeners=[UnixListener(path, mode=0o620)],
    ) as server:
        assert stat.S_IMODE(path.stat().st_mode) == 0o620
        with DeadpoolClient(UnixAddress(path)) as client:
            assert client.submit(multiply, 6, 7).result(5) == 42
        client.shutdown()
        assert server.bound_addresses == (path,)

    assert server.state is ServerState.STOPPED
    assert not path.exists()


@pytest.mark.parametrize("path_kind", ["file", "symlink"])
def test_failed_startup_is_stopped_repeatable_and_non_destructive(
    tmp_path: Path, path_kind: str
) -> None:
    path = tmp_path / "pool.sock"
    if path_kind == "file":
        path.write_text("keep")
    else:
        path.symlink_to(tmp_path / "missing-target")
    server = DeadpoolServer(pool_factory, listeners=[UnixListener(path)])

    with pytest.raises(ValueError, match="unexpected Unix socket path"):
        server.start()
    assert server.state is ServerState.STOPPED
    with pytest.raises(ValueError, match="unexpected Unix socket path"):
        server.start()
    assert path.is_symlink() if path_kind == "symlink" else path.read_text() == "keep"


def test_thread_start_failure_is_terminal_and_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pool.sock"
    server = DeadpoolServer(pool_factory, listeners=[UnixListener(path)])

    def fail_thread_start(thread: threading.Thread) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_thread_start)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        server.start()

    assert server.state is ServerState.STOPPED
    assert server.ready.is_set()
    assert server._stopped.is_set()
    with pytest.raises(RuntimeError, match="thread unavailable"):
        server.start()
    server.shutdown()


def test_direct_serve_forever_propagates_startup_failure(tmp_path: Path) -> None:
    path = tmp_path / "pool.sock"
    path.write_text("keep")
    server = DeadpoolServer(pool_factory, listeners=[UnixListener(path)])

    with pytest.raises(ValueError, match="unexpected Unix socket path"):
        server.serve_forever()

    assert server.state is ServerState.STOPPED
    assert path.read_text() == "keep"


def test_concurrent_start_waits_for_shared_readiness(tmp_path: Path) -> None:
    path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        pool_factory,
        listeners=[UnixListener(path)],
        limits=RemoteLimits(handshake_timeout=0.02),
    )
    initialize = server._initialize
    initialization_entered = threading.Event()
    allow_initialization = threading.Event()
    second_returned = threading.Event()
    errors = []

    def delayed_initialize() -> None:
        initialization_entered.set()
        assert allow_initialization.wait(5)
        initialize()

    def start_server(returned: threading.Event | None = None) -> None:
        try:
            server.start()
        except BaseException as error:
            errors.append(error)
        finally:
            if returned is not None:
                returned.set()

    server._initialize = delayed_initialize
    first = threading.Thread(target=start_server)
    second = threading.Thread(target=start_server, args=(second_returned,))
    try:
        first.start()
        assert initialization_entered.wait(5)
        second.start()
        assert not second_returned.wait(0.1)
        assert first.is_alive()
        assert errors == []
        allow_initialization.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert server.state is ServerState.RUNNING
    finally:
        allow_initialization.set()
        if first.ident is not None:
            first.join(5)
        if second.ident is not None:
            second.join(5)
        server.shutdown(cancel_futures=True, deadline=5)


def test_start_keeps_a_published_readiness_outcome_during_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pool.sock"
    server = DeadpoolServer(pool_factory, listeners=[UnixListener(path)])
    readiness_observed = threading.Event()
    release_waiter = threading.Event()
    start_errors = []
    wait_for_ready = server.ready.wait

    def delayed_wait(timeout=None) -> bool:
        result = wait_for_ready(timeout)
        readiness_observed.set()
        assert release_waiter.wait(5)
        return result

    monkeypatch.setattr(server.ready, "wait", delayed_wait)

    def start_server() -> None:
        try:
            server.start()
        except BaseException as error:
            start_errors.append(error)

    startup_thread = threading.Thread(target=start_server)
    try:
        startup_thread.start()
        assert readiness_observed.wait(5)
        assert server.state is ServerState.RUNNING
        server.shutdown(cancel_futures=True, deadline=5)
        assert server.state is ServerState.STOPPED
    finally:
        release_waiter.set()
        startup_thread.join(5)
        server.shutdown(cancel_futures=True, deadline=5)

    assert not startup_thread.is_alive()
    assert start_errors == []


def test_shutdown_during_startup_cleans_unpublished_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pool.sock"
    pool_created = threading.Event()
    allow_factory_return = threading.Event()
    shutdown_complete = threading.Event()
    startup_errors = []

    class RecordingPool:
        def __init__(self) -> None:
            self.shutdown_calls = []

        def shutdown(self, wait=True, *, cancel_futures=False) -> None:
            self.shutdown_calls.append((wait, cancel_futures))

    pool = RecordingPool()
    close_listener = _transport.BoundListener.close

    def close_then_fail(listener: _transport.BoundListener) -> None:
        close_listener(listener)
        raise OSError("simulated listener cleanup failure")

    monkeypatch.setattr(_transport.BoundListener, "close", close_then_fail)

    def slow_pool_factory() -> RecordingPool:
        pool_created.set()
        assert allow_factory_return.wait(5)
        return pool

    server = DeadpoolServer(slow_pool_factory, listeners=[UnixListener(path)])

    def start_server() -> None:
        try:
            server.start()
        except BaseException as error:
            startup_errors.append(error)

    def stop_server() -> None:
        server.shutdown()
        shutdown_complete.set()

    startup_thread = threading.Thread(target=start_server)
    shutdown_thread = threading.Thread(target=stop_server)
    try:
        startup_thread.start()
        assert pool_created.wait(5)
        shutdown_thread.start()
        deadline = time.monotonic() + 2
        while server.state is not ServerState.STOPPING and time.monotonic() < deadline:
            time.sleep(0.005)
        assert server.state is ServerState.STOPPING
        assert not shutdown_complete.wait(0.05)
    finally:
        allow_factory_return.set()
        startup_thread.join(5)
        shutdown_thread.join(5)

    assert not startup_thread.is_alive()
    assert not shutdown_thread.is_alive()
    assert len(startup_errors) == 1
    assert isinstance(startup_errors[0], RuntimeError)
    assert str(startup_errors[0]) == "remote server stopped before becoming ready"
    assert pool.shutdown_calls == [(True, True)]
    assert server.state is ServerState.STOPPED
    assert server.bound_addresses == ()
    assert not path.exists()


def test_optional_listener_failure_does_not_prevent_valid_listener(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("keep")
    valid = tmp_path / "valid.sock"
    server = DeadpoolServer(
        pool_factory,
        listeners=[UnixListener(blocked, optional=True), UnixListener(valid)],
    ).start()
    client = DeadpoolClient(UnixAddress(valid))
    try:
        assert server.bound_addresses == (valid,)
        assert client.submit(multiply, 2, 4).result(5) == 8
        assert blocked.read_text() == "keep"
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_stale_listener_requires_force_unlink_and_then_has_normal_lifecycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    original = path.lstat()

    refusing = DeadpoolServer(pool_factory, listeners=[UnixListener(path)])
    with pytest.raises(FileExistsError):
        refusing.start()
    assert (path.lstat().st_dev, path.lstat().st_ino) == (
        original.st_dev,
        original.st_ino,
    )

    server = DeadpoolServer(
        pool_factory,
        listeners=[UnixListener(path, stale_policy="force_unlink")],
    ).start()
    client = DeadpoolClient(UnixAddress(path))
    try:
        assert client.submit(multiply, 6, 7).result(5) == 42
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)
    assert not path.exists()

    with DeadpoolServer(pool_factory, listeners=[UnixListener(path)]) as restarted:
        with DeadpoolClient(UnixAddress(path)) as restarted_client:
            assert restarted_client.submit(multiply, 2, 4).result(5) == 8
        assert restarted.state is ServerState.RUNNING
    assert not path.exists()


def test_stale_probe_does_not_unlink_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "stale.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    real_socket = socket.socket

    class RacingProbe:
        winner: socket.socket | None = None

        def settimeout(self, timeout: float) -> None:
            return None

        def connect(self, address: str) -> None:
            path.unlink()
            self.winner = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.winner.bind(address)
            self.winner.listen()
            raise ConnectionRefusedError

        def close(self) -> None:
            return None

    probe = RacingProbe()
    replacement_was_verified = False

    class ReplacementVerifier:
        def settimeout(self, timeout: float) -> None:
            return None

        def connect(self, address: str) -> None:
            nonlocal replacement_was_verified
            replacement_was_verified = True

        def close(self) -> None:
            return None

    verifier = ReplacementVerifier()
    calls = 0

    def socket_factory(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return probe
        if calls == 2:
            return verifier
        return real_socket(*args, **kwargs)

    monkeypatch.setattr(_transport.socket, "socket", socket_factory)
    try:
        with pytest.raises(OSError, match="Address already in use"):
            _transport.bind_listener(UnixListener(path, stale_policy="force_unlink"))
        assert replacement_was_verified
        assert probe.winner is not None
        verifier = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            verifier.connect(str(path))
        finally:
            verifier.close()
    finally:
        if probe.winner is not None:
            probe.winner.close()
        path.unlink(missing_ok=True)


def test_live_force_unlink_listener_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "live.sock"
    winner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    winner.bind(str(path))
    winner.listen()
    server = DeadpoolServer(
        pool_factory,
        listeners=[UnixListener(path, stale_policy="force_unlink")],
    )
    try:
        with pytest.raises(OSError, match="already live"):
            server.start()
        assert server.state is ServerState.STOPPED
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        finally:
            probe.close()
    finally:
        winner.close()
        path.unlink(missing_ok=True)


def test_insecure_socket_directory_is_rejected_without_creating_path(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "insecure"
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    path = directory / "pool.sock"
    server = DeadpoolServer(pool_factory, listeners=[UnixListener(path)])
    try:
        with pytest.raises(PermissionError, match="group/world writable"):
            server.start()
        assert server.state is ServerState.STOPPED
        assert not path.exists()
    finally:
        directory.chmod(0o700)


def test_shutdown_preserves_replacement_at_listener_path(tmp_path: Path) -> None:
    path = tmp_path / "pool.sock"
    server = DeadpoolServer(pool_factory, listeners=[UnixListener(path)]).start()
    path.unlink()
    path.write_text("replacement")

    server.shutdown(cancel_futures=True, deadline=5)

    assert path.read_text() == "replacement"


def test_bind_failure_never_unlinks_a_racing_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loser must not clean a pathname it never successfully bound."""
    path = tmp_path / "pool.sock"
    real_socket = socket.socket

    class RacingSocket:
        winner: socket.socket | None = None

        def bind(self, address: str) -> None:
            self.winner = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.winner.bind(address)
            self.winner.listen()
            raise OSError(errno.EADDRINUSE, "address in use")

        def close(self) -> None:
            return None

    racing = RacingSocket()
    monkeypatch.setattr(_transport.socket, "socket", lambda *args, **kwargs: racing)

    with pytest.raises(OSError, match="address in use"):
        _transport.bind_listener(UnixListener(path))

    assert path.exists()
    probe = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    finally:
        probe.close()
        assert racing.winner is not None
        racing.winner.close()
        path.unlink()
