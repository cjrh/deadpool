from functools import partial

import pytest

import deadpool
from deadpool.remote import DeadpoolClient, DeadpoolServer, UnixAddress, UnixListener
from tests.remote.tasks import multiply


@pytest.fixture()
def remote_pair(tmp_path):
    socket_path = tmp_path / "deadpool.sock"
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=2, mp_context="forkserver"),
        listeners=[UnixListener(socket_path)],
        task_registry={"multiply": multiply},
    )
    server.start()
    client = DeadpoolClient(UnixAddress(socket_path))
    try:
        yield server, client
    finally:
        client.shutdown(wait=True, cancel_futures=True)
        server.shutdown(wait=True, cancel_futures=True, deadline=5)
