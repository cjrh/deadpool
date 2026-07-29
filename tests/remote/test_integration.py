import importlib
import socket
import threading
import time
from types import SimpleNamespace
from functools import partial

import pytest

import deadpool
from deadpool.remote import (
    DeadpoolClient,
    DeadpoolServer,
    RemoteAuthenticationError,
    RemoteCompatibilityError,
    RemoteLimits,
    RemoteProtocolError,
    RemoteQueueFull,
    RemoteSubmissionTimeout,
    RemoteResultEncodingError,
    RemoteTaskError,
    UnixAddress,
    UnixListener,
)
from deadpool.remote._protocol import (
    Message,
    MessageReader,
    MessageType,
    _wire_limits,
    send_message,
)
from deadpool.remote.serializer import PickleSerializer


def add(left, right):
    return left + right


def delayed(value, delay):
    time.sleep(delay)
    return value


def fail_value_error():
    raise ValueError("remote boom")


def unencodable_result():
    return lambda: None


class UnpicklableError(Exception):
    def __init__(self):
        super().__init__("cannot pickle me")
        self.callback = lambda: None


def fail_unpicklable():
    raise UnpicklableError()


def test_callable_registered_stats_and_health(remote_pair):
    server, client = remote_pair

    assert client.check_health()
    assert client.submit(add, 2, 3).result(timeout=5) == 5
    assert client.submit_task("multiply", 6, 7).result(timeout=5) == 42
    stats = client.get_statistics()
    assert stats["remote_tasks_terminal"] == 2
    assert stats["tasks_received"] == 2
    assert server.state == "RUNNING"


def test_independent_clients_share_server_without_owning_it(remote_pair):
    server, first = remote_pair
    second = DeadpoolClient(UnixAddress(server.bound_addresses[0]))
    try:
        assert first.submit(add, 1, 2).result(timeout=5) == 3
        assert second.submit(add, 3, 4).result(timeout=5) == 7
        second.shutdown()
        assert first.submit(add, 5, 6).result(timeout=5) == 11
        assert server.state == "RUNNING"
    finally:
        second.shutdown()


def test_authentication_rejection_is_typed(tmp_path):
    socket_path = tmp_path / "pool.sock"

    def reject(peer, hello):
        raise PermissionError("no")

    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        authenticator=reject,
    ).start()
    try:
        with pytest.raises(RemoteAuthenticationError):
            DeadpoolClient(UnixAddress(socket_path))
    finally:
        server.shutdown(cancel_futures=True, deadline=5)


def test_duplicate_live_client_instance_is_rejected(remote_pair, monkeypatch):
    server, first = remote_pair
    client_module = importlib.import_module("deadpool.remote.client")
    monkeypatch.setattr(
        client_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=first._client_id),
    )
    with pytest.raises(RemoteCompatibilityError):
        DeadpoolClient(UnixAddress(server.bound_addresses[0]))


@pytest.mark.parametrize(
    "server_limit_changes",
    [
        {"max_control_bytes": 32 * 1024},
        {"max_frame_payload_bytes": 512 * 1024},
        {"max_message_bytes": 128 * 1024 * 1024},
        {"max_metadata_bytes": 8 * 1024},
        {"max_chunks": 128},
    ],
)
def test_handshake_rejects_asymmetric_wire_limits(
    tmp_path, server_limit_changes
):
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        limits=RemoteLimits(**server_limit_changes),
    ).start()
    try:
        with pytest.raises(RemoteCompatibilityError, match="wire_limits"):
            DeadpoolClient(UnixAddress(socket_path))
    finally:
        server.shutdown(cancel_futures=True, deadline=5)


def test_client_rejects_server_selected_asymmetric_wire_limits(tmp_path):
    socket_path = tmp_path / "pool.sock"
    limits = RemoteLimits()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()
    listener.settimeout(5)
    server_errors = []

    def serve_incompatible_welcome() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                MessageReader(limits).receive(connection)
                selected = _wire_limits(limits)
                selected["max_chunks"] -= 1
                send_message(
                    connection,
                    Message(
                        MessageType.WELCOME,
                        {
                            "wire": "experimental-deadpool-private-v1",
                            "wire_limits": selected,
                        },
                    ),
                    limits,
                )
        except BaseException as error:
            server_errors.append(error)

    server_thread = threading.Thread(target=serve_incompatible_welcome)
    server_thread.start()
    try:
        with pytest.raises(RemoteCompatibilityError, match="wire limits"):
            DeadpoolClient(UnixAddress(socket_path))
    finally:
        listener.close()
        server_thread.join(5)
        socket_path.unlink(missing_ok=True)

    assert not server_thread.is_alive()
    assert server_errors == []


def test_callable_mode_never_pickles_registered_task_registry(tmp_path):
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        task_registry={"deliberately.unpicklable": lambda: None},
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        assert client.submit(add, 20, 22).result(timeout=5) == 42
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_out_of_order_correlation_and_ordered_map(remote_pair):
    _, client = remote_pair
    slow = client.submit(delayed, "slow", 0.2)
    fast = client.submit(delayed, "fast", 0.01)

    assert fast.result(timeout=5) == "fast"
    assert slow.result(timeout=5) == "slow"
    assert list(client.map(add, [1, 2, 3], [10, 20, 30])) == [11, 22, 33]


def test_invalid_control_metadata_fails_before_transport_use(remote_pair):
    _, client = remote_pair
    with pytest.raises(RemoteProtocolError):
        client.submit(add, 1, 2, deadpool_metadata={"bad": object()})
    assert client.submit(add, 2, 3).result(timeout=5) == 5


def test_task_exception_and_encoding_boundaries(remote_pair):
    _, client = remote_pair

    with pytest.raises(ValueError, match="remote boom"):
        client.submit(fail_value_error).result(timeout=5)
    with pytest.raises(RemoteTaskError) as captured:
        client.submit(fail_unpicklable).result(timeout=5)
    assert "UnpicklableError" in captured.value.remote_traceback
    with pytest.raises(RemoteResultEncodingError):
        client.submit(unencodable_result).result(timeout=5)


class RejectResultSerializer(PickleSerializer):
    def loads(self, payload):
        raise ValueError("cannot decode outcome")


class SlowSerializer(PickleSerializer):
    def dumps(self, value, *, limit):
        time.sleep(0.1)
        return super().dumps(value, limit=limit)


def test_failed_result_decode_is_not_acknowledged(tmp_path):
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(
        UnixAddress(socket_path),
        serializer=RejectResultSerializer(),
    )
    try:
        future = client.submit(add, 1, 2)
        with pytest.raises(RemoteProtocolError, match="cannot decode outcome"):
            future.result(timeout=5)
        assert server.get_statistics()["remote_retained_outcomes"] == 1
    finally:
        client.shutdown()
        server.shutdown(cancel_futures=True, deadline=5)


def test_submission_timeout_includes_serialization(tmp_path):
    socket_path = tmp_path / "pool.sock"
    serializer = SlowSerializer()
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        serializer=serializer,
    ).start()
    client = DeadpoolClient(
        UnixAddress(socket_path),
        serializer=serializer,
        submission_timeout=0.02,
    )
    try:
        with pytest.raises(RemoteSubmissionTimeout):
            client.submit(add, 1, 2)
        assert server.get_statistics()["remote_tasks_received"] == 0
    finally:
        client.shutdown()
        server.shutdown(cancel_futures=True, deadline=5)


def test_retained_outcome_capacity_is_reserved_and_released(tmp_path):
    socket_path = tmp_path / "pool.sock"
    limits = RemoteLimits(
        max_retained_outcomes_per_session=1,
        max_retained_outcomes_global=1,
    )
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        limits=limits,
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path), limits=limits)
    original_enqueue = client._enqueue_control

    def omit_result_ack(kind, control):
        if kind != MessageType.RESULT_ACK:
            original_enqueue(kind, control)

    client._enqueue_control = omit_result_ack
    try:
        first = client.submit(add, 1, 2)
        assert first.result(timeout=5) == 3
        assert server.get_statistics()["remote_retained_outcomes"] == 1
        with pytest.raises(RemoteQueueFull):
            client.submit(add, 2, 3).result(timeout=5)
        original_enqueue(MessageType.RESULT_ACK, {"request_id": first.request_id})
        deadline = time.monotonic() + 2
        while (
            server.get_statistics()["remote_retained_outcomes"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert server.get_statistics()["remote_retained_outcomes"] == 0
        assert client.submit(add, 3, 4).result(timeout=5) == 7
    finally:
        client._enqueue_control = original_enqueue
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_live_session_releases_outcomes_across_idle_cycles(tmp_path):
    socket_path = tmp_path / "pool.sock"
    limits = RemoteLimits(
        max_retained_outcomes_per_session=1,
        max_retained_outcomes_global=1,
    )
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        limits=limits,
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path), limits=limits)
    try:
        for args, expected in [((1, 2), 3), ((3, 4), 7)]:
            assert client.submit(add, *args).result(timeout=5) == expected
            deadline = time.monotonic() + 2
            while (
                server.get_statistics()["remote_retained_outcomes"]
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            stats = server.get_statistics()
            assert stats["remote_retained_outcomes"] == 0
            assert stats["remote_sessions"] == 1
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_continue_disconnect_releases_unreachable_terminal_outcome(tmp_path):
    socket_path = tmp_path / "pool.sock"
    limits = RemoteLimits(
        max_retained_outcomes_per_session=1,
        max_retained_outcomes_global=1,
    )
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        limits=limits,
        disconnect_policy="continue",
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path), limits=limits)
    original_enqueue = client._enqueue_control

    def omit_result_ack(kind, control):
        if kind != MessageType.RESULT_ACK:
            original_enqueue(kind, control)

    client._enqueue_control = omit_result_ack
    try:
        assert client.submit(add, 1, 2).result(timeout=5) == 3
        assert server.get_statistics()["remote_retained_outcomes"] == 1
        sock = client._socket
        assert sock is not None
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
        deadline = time.monotonic() + 2
        while (
            server.get_statistics()["remote_retained_outcomes"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert server.get_statistics()["remote_retained_outcomes"] == 0
        assert server.get_statistics()["remote_sessions"] == 0
    finally:
        client._enqueue_control = original_enqueue
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_callback_capacity_one_allows_reentrant_registration(tmp_path):
    socket_path = tmp_path / "pool.sock"
    limits = RemoteLimits(callback_queue_size=1)
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path), limits=limits)
    finished = threading.Event()
    try:
        future = client.submit(add, 2, 3)

        def first_callback(done):
            done.add_done_callback(lambda _: finished.set())

        future.add_done_callback(first_callback)
        assert future.result(timeout=5) == 5
        assert finished.wait(2)
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_slow_callback_does_not_block_results_or_health(remote_pair):
    _, client = remote_pair
    callback_started = threading.Event()
    callback_release = threading.Event()

    first = client.submit(add, 1, 1)

    def block_callback(future):
        callback_started.set()
        callback_release.wait(5)

    first.add_done_callback(block_callback)
    assert first.result(timeout=5) == 2
    assert callback_started.wait(5)
    second = client.submit(add, 2, 2)
    assert second.result(timeout=5) == 4
    assert client.check_health()
    callback_release.set()

    callback_order = []
    ordered = client.submit(add, 3, 3)
    ordered.add_done_callback(lambda future: callback_order.append(1))
    ordered.add_done_callback(lambda future: callback_order.append(2))
    assert ordered.result(timeout=5) == 6
    deadline = time.monotonic() + 2
    while len(callback_order) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert callback_order == [1, 2]
