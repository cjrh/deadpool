from dataclasses import dataclass
from functools import partial
from pathlib import Path
import ssl

import pytest

import deadpool
from deadpool.remote import DeadpoolClient, DeadpoolServer, UnixAddress, UnixListener
from tests.remote.tasks import multiply

_CERTS = Path(__file__).with_name("certs")


@dataclass(frozen=True, slots=True)
class TLSContexts:
    """Mutually authenticated contexts backed by static test-only credentials."""

    server: ssl.SSLContext
    client: ssl.SSLContext
    missing_client_certificate: ssl.SSLContext
    untrusted_client: ssl.SSLContext


def _client_tls_context(*, certificate: str | None = "client") -> ssl.SSLContext:
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=_CERTS / "ca.pem",
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if certificate is not None:
        context.load_cert_chain(
            _CERTS / f"{certificate}.pem",
            _CERTS / f"{certificate}-key.pem",
        )
    return context


@pytest.fixture()
def tls_contexts() -> TLSContexts:
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.minimum_version = ssl.TLSVersion.TLSv1_2
    server.verify_mode = ssl.CERT_REQUIRED
    server.load_verify_locations(_CERTS / "ca.pem")
    server.load_cert_chain(_CERTS / "server.pem", _CERTS / "server-key.pem")
    return TLSContexts(
        server=server,
        client=_client_tls_context(),
        missing_client_certificate=_client_tls_context(certificate=None),
        untrusted_client=_client_tls_context(certificate="rogue-client"),
    )


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
