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
    RemoteLimits,
    TcpAddress,
    TcpListener,
)


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


def test_plaintext_tcp_requires_explicit_loopback_opt_in():
    with pytest.raises(ValueError):
        TcpAddress("127.0.0.1", 1)
    with pytest.raises(ValueError):
        TcpListener("0.0.0.0", 0, insecure=True)
