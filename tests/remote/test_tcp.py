from functools import partial
import socket
import ssl
import time
import pytest

import deadpool
from deadpool.remote import (
    DeadpoolClient,
    DeadpoolServer,
    RemoteCompatibilityError,
    RemoteExecutorError,
    RemoteExecutorUnavailable,
    Principal,
    RemoteLimits,
    TcpAddress,
    TcpListener,
)
from tests.remote.tasks import multiply


def identity(value):
    return value


def test_insecure_loopback_tcp_uses_same_protocol():
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[TcpListener("127.0.0.1", 0, insecure=True)],
        authorizer=lambda principal, operation, metadata: True,
    ).start()
    host, port = server.bound_addresses[0]
    client = DeadpoolClient(TcpAddress(host, port, insecure=True))
    try:
        assert client.submit(identity, "tcp").result(timeout=5) == "tcp"
    finally:
        client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


def test_loopback_mutual_tls_authenticates_client_identity(tls_contexts):
    authenticated_common_names = []
    authorized_calls = []

    def authenticate(peer, hello):
        subject = {
            key: value
            for distinguished_name in peer.certificate["subject"]
            for key, value in distinguished_name
        }
        authenticated_common_names.append(subject["commonName"])
        return Principal(subject["commonName"])

    def authorize(principal, operation, metadata):
        authorized_calls.append((principal.name, operation))
        return True

    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[TcpListener("127.0.0.1", 0, ssl_context=tls_contexts.server)],
        task_registry={"multiply": multiply},
        authenticator=authenticate,
        authorizer=authorize,
    ).start()
    host, port = server.bound_addresses[0]
    client = None
    try:
        client = DeadpoolClient(
            TcpAddress(
                host,
                port,
                ssl_context=tls_contexts.client,
                server_hostname="localhost",
            )
        )
        assert client.submit_task("multiply", 6, 7).result(5) == 42
        assert client.check_health()
        assert authenticated_common_names == ["deadpool-test-client"]
        assert authorized_calls == [("deadpool-test-client", "submit_task:multiply")]
    finally:
        if client is not None:
            client.shutdown(cancel_futures=True)
        server.shutdown(cancel_futures=True, deadline=5)


@pytest.mark.parametrize(
    "context_name",
    ["missing_client_certificate", "untrusted_client"],
)
def test_mutual_tls_rejects_missing_or_untrusted_client_certificate(
    tls_contexts, context_name
):
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[TcpListener("127.0.0.1", 0, ssl_context=tls_contexts.server)],
        authorizer=lambda principal, operation, metadata: True,
    ).start()
    host, port = server.bound_addresses[0]
    try:
        with pytest.raises(RemoteExecutorUnavailable):
            DeadpoolClient(
                TcpAddress(
                    host,
                    port,
                    ssl_context=getattr(tls_contexts, context_name),
                    server_hostname="localhost",
                )
            )
    finally:
        server.shutdown(cancel_futures=True, deadline=5)


def test_tls_rejects_untrusted_server_hostname(tls_contexts):
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[TcpListener("127.0.0.1", 0, ssl_context=tls_contexts.server)],
        authorizer=lambda principal, operation, metadata: True,
    ).start()
    host, port = server.bound_addresses[0]
    try:
        with pytest.raises(RemoteExecutorUnavailable, match="(?i)hostname"):
            DeadpoolClient(
                TcpAddress(
                    host,
                    port,
                    ssl_context=tls_contexts.client,
                    server_hostname="untrusted.invalid",
                )
            )
    finally:
        server.shutdown(cancel_futures=True, deadline=5)


def test_tcp_without_authorizer_is_default_deny():
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[TcpListener("127.0.0.1", 0, insecure=True)],
    ).start()
    host, port = server.bound_addresses[0]
    client = DeadpoolClient(TcpAddress(host, port, insecure=True))
    try:
        with pytest.raises(RemoteExecutorError, match="unauthorized"):
            client.submit(identity, "tcp").result(timeout=5)
        with pytest.raises(RemoteExecutorError, match="unauthorized"):
            client.get_statistics()
    finally:
        client.shutdown()
        server.shutdown(cancel_futures=True, deadline=5)


def test_tcp_principal_connection_limit_cannot_be_multiplied():
    limits = RemoteLimits(max_connections_per_principal=1)
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[TcpListener("127.0.0.1", 0, insecure=True)],
        authorizer=lambda principal, operation, metadata: True,
        limits=limits,
    ).start()
    host, port = server.bound_addresses[0]
    first = DeadpoolClient(TcpAddress(host, port, insecure=True), limits=limits)
    try:
        with pytest.raises(RemoteCompatibilityError):
            DeadpoolClient(TcpAddress(host, port, insecure=True), limits=limits)
    finally:
        first.shutdown()
        server.shutdown(cancel_futures=True, deadline=5)


def test_unauthenticated_connection_limit_is_bounded():
    limits = RemoteLimits(
        max_unauthenticated_connections=1,
        max_connections_global=2,
        max_connections_per_principal=2,
    )
    server = DeadpoolServer(
        partial(deadpool.Deadpool, max_workers=1, mp_context="forkserver"),
        listeners=[TcpListener("127.0.0.1", 0, insecure=True)],
        authorizer=lambda principal, operation, metadata: True,
        limits=limits,
    ).start()
    host, port = server.bound_addresses[0]
    stalled = socket.create_connection((host, port))
    try:
        deadline = time.monotonic() + 2
        while (
            server.get_statistics()["remote_connections"] < 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        with pytest.raises(RemoteExecutorUnavailable):
            DeadpoolClient(TcpAddress(host, port, insecure=True), limits=limits)
    finally:
        stalled.close()
    deadline = time.monotonic() + 2
    while server.get_statistics()["remote_connections"] and time.monotonic() < deadline:
        time.sleep(0.01)
    client = DeadpoolClient(TcpAddress(host, port, insecure=True), limits=limits)
    client.shutdown()
    server.shutdown(cancel_futures=True, deadline=5)


def test_tls_configuration_requires_mutual_verification():
    client_context = ssl.create_default_context()
    client_context.minimum_version = ssl.TLSVersion.TLSv1_2
    TcpAddress("example.com", 443, ssl_context=client_context)

    insecure_client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure_client.check_hostname = False
    insecure_client.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="certificates and hostnames"):
        TcpAddress("example.com", 443, ssl_context=insecure_client)

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises(ValueError, match="client certificates"):
        TcpListener("127.0.0.1", 443, ssl_context=server_context)


def test_tls_configuration_rejects_versions_older_than_tls_1_2():
    client_context = ssl.create_default_context()
    with pytest.warns(DeprecationWarning):
        client_context.minimum_version = ssl.TLSVersion.TLSv1
    with pytest.raises(ValueError, match="client TLS must require TLS 1.2"):
        TcpAddress("example.com", 443, ssl_context=client_context)

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.verify_mode = ssl.CERT_REQUIRED
    with pytest.warns(DeprecationWarning):
        server_context.minimum_version = ssl.TLSVersion.TLSv1
    with pytest.raises(ValueError, match="server TLS must require TLS 1.2"):
        TcpListener("127.0.0.1", 443, ssl_context=server_context)


def test_plaintext_tcp_requires_explicit_loopback_opt_in():
    with pytest.raises(ValueError):
        TcpAddress("127.0.0.1", 1)
    with pytest.raises(ValueError, match="addresses are restricted to loopback"):
        TcpAddress("example.com", 443, insecure=True)
    with pytest.raises(ValueError, match="addresses are restricted to loopback"):
        TcpAddress("localhost", 443, insecure=True)
    with pytest.raises(ValueError, match="listeners are restricted to loopback"):
        TcpListener("0.0.0.0", 0, insecure=True)
    with pytest.raises(ValueError, match="listeners are restricted to loopback"):
        TcpListener("localhost", 0, insecure=True)
