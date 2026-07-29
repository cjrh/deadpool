"""End-to-end coverage for public remote client/server interfaces."""

from __future__ import annotations

import multiprocessing
from functools import partial
from pathlib import Path

import deadpool
from deadpool.remote import (
    DeadpoolClient,
    DeadpoolServer,
    RemoteLimits,
    UnixAddress,
    UnixListener,
)
from tests.remote.tasks import echo_bytes, multiply


def _spawn_submitter(
    socket_path: str,
    index: int,
    start: multiprocessing.synchronize.Event,
    output: multiprocessing.queues.Queue,
) -> None:
    """Connect after the shared gate and report only public client outcomes."""
    output.put(("ready", index))
    if not start.wait(10):
        output.put(("error", index, "start gate timed out"))
        return

    client = None
    try:
        client = DeadpoolClient(UnixAddress(socket_path))
        result = client.submit_task("multiply", index, index + 1).result(10)
        output.put(("result", index, result))
    except BaseException as error:
        output.put(("error", index, f"{type(error).__name__}: {error}"))
    finally:
        if client is not None:
            client.shutdown(wait=True, cancel_futures=True)


def test_spawn_clients_submit_concurrently_to_one_server_pool(tmp_path: Path) -> None:
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=2, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        task_registry={"multiply": multiply},
    ).start()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    process_count = 4
    processes = [
        context.Process(
            target=_spawn_submitter,
            args=(str(socket_path), index, start, output),
        )
        for index in range(1, process_count + 1)
    ]

    try:
        for process in processes:
            process.start()
        ready = {output.get(timeout=10) for _ in processes}
        assert ready == {("ready", index) for index in range(1, process_count + 1)}

        start.set()
        outcomes = [output.get(timeout=15) for _ in processes]
        errors = [outcome for outcome in outcomes if outcome[0] == "error"]
        assert errors == []
        assert sorted(outcomes) == [
            ("result", index, index * (index + 1))
            for index in range(1, process_count + 1)
        ]
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            assert not process.is_alive()
        output.close()
        output.join_thread()
        server.shutdown(cancel_futures=True, deadline=5)


def test_chunked_invocation_and_result_round_trip_and_session_health(
    tmp_path: Path,
) -> None:
    frame_size = 8 * 1024
    limits = RemoteLimits(
        max_frame_payload_bytes=frame_size,
        max_message_bytes=128 * 1024,
        max_invocation_bytes=64 * 1024,
        max_result_bytes=64 * 1024,
        max_chunks=16,
    )
    socket_path = tmp_path / "pool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        task_registry={"echo_bytes": echo_bytes, "multiply": multiply},
        limits=limits,
    ).start()
    client = DeadpoolClient(UnixAddress(socket_path), limits=limits)
    payload = bytes(range(256)) * 40 + b"exact-tail"
    assert len(payload) > frame_size
    try:
        assert client.submit_task("echo_bytes", payload).result(10) == payload
        assert client.submit_task("multiply", 6, 7).result(5) == 42
        assert client.check_health()
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)
