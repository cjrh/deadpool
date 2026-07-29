"""Validated addressing and resource policy for the remote executor."""

from __future__ import annotations

import math
import ssl
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Protocol, runtime_checkable

_INSECURE_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


@dataclass(frozen=True, slots=True)
class UnixAddress:
    path: str | Path
    connect_timeout: float = 5.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        _positive(self.connect_timeout, "connect_timeout")


@dataclass(frozen=True, slots=True)
class TcpAddress:
    host: str
    port: int
    ssl_context: ssl.SSLContext | None = None
    server_hostname: str | None = None
    insecure: bool = False
    connect_timeout: float = 5.0

    def __post_init__(self) -> None:
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise TypeError("port must be an integer")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        _positive(self.connect_timeout, "connect_timeout")
        if self.ssl_context is None and not self.insecure:
            raise ValueError("TCP requires ssl_context or explicit insecure=True")
        if self.ssl_context is not None:
            _validate_client_tls(self.ssl_context)
        if self.insecure and self.host not in _INSECURE_LOOPBACK_HOSTS:
            raise ValueError("insecure TCP addresses are restricted to loopback")


@dataclass(frozen=True, slots=True)
class UnixListener:
    """Unix listener; ``force_unlink`` is explicit unsafe stale cleanup."""

    path: str | Path
    mode: int = 0o600
    owner_uid: int | None = None
    owner_gid: int | None = None
    backlog: int = 128
    stale_policy: str = "error"
    optional: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        if self.stale_policy not in {"error", "force_unlink"}:
            raise ValueError("stale_policy must be 'error' or 'force_unlink'")
        if isinstance(self.mode, bool) or not isinstance(self.mode, int):
            raise TypeError("mode must be an integer")
        if not 0 <= self.mode <= 0o777:
            raise ValueError("mode must be a permission mode")
        _positive_int(self.backlog, "backlog")
        for name, value in (
            ("owner_uid", self.owner_uid),
            ("owner_gid", self.owner_gid),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class TcpListener:
    host: str
    port: int
    ssl_context: ssl.SSLContext | None = None
    insecure: bool = False
    backlog: int = 128
    keepalive: bool = True
    optional: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise TypeError("port must be an integer")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        _positive_int(self.backlog, "backlog")
        if self.ssl_context is None and not self.insecure:
            raise ValueError("TCP requires ssl_context or explicit insecure=True")
        if self.ssl_context is not None:
            _validate_server_tls(self.ssl_context)
        if self.insecure and self.host not in _INSECURE_LOOPBACK_HOSTS:
            raise ValueError("insecure TCP listeners are restricted to loopback")


@dataclass(frozen=True, slots=True)
class RemoteLimits:
    """Finite defaults bound protocol/resource queues and byte buffers."""

    max_control_bytes: int = 64 * 1024
    max_frame_payload_bytes: int = 1024 * 1024
    max_message_bytes: int = 64 * 1024 * 1024
    max_invocation_bytes: int = 32 * 1024 * 1024
    max_result_bytes: int = 32 * 1024 * 1024
    max_metadata_bytes: int = 16 * 1024
    max_chunks: int = 256
    max_incomplete_messages: int = 16
    max_unauthenticated_connections: int = 64
    max_connections_global: int = 256
    max_connections_per_principal: int = 64
    max_pending_per_session: int = 1024
    max_pending_per_principal: int = 2048
    max_pending_global: int = 4096
    max_retained_outcomes_per_session: int = 64
    max_retained_outcomes_global: int = 1024
    max_retained_outcome_bytes_per_session: int = 256 * 1024 * 1024
    max_retained_outcome_bytes_global: int = 1024 * 1024 * 1024
    max_staged_tasks: int = 128
    outbound_queue_size: int = 1024
    inbound_queue_size: int = 1024
    # Bounds callback batches waiting to start; registration itself is nonblocking.
    callback_queue_size: int = 1024
    completion_workers: int = 4
    control_timeout: float = 5.0
    handshake_timeout: float = 5.0
    partial_frame_timeout: float = 10.0

    def __post_init__(self) -> None:
        duration_fields = {
            "control_timeout",
            "handshake_timeout",
            "partial_frame_timeout",
        }
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in duration_fields:
                _positive(value, item.name)
            else:
                _positive_int(value, item.name)
        if self.max_frame_payload_bytes > self.max_message_bytes:
            raise ValueError("max_frame_payload_bytes cannot exceed max_message_bytes")
        if self.max_invocation_bytes > self.max_message_bytes:
            raise ValueError("max_invocation_bytes cannot exceed max_message_bytes")
        if self.max_result_bytes > self.max_message_bytes:
            raise ValueError("max_result_bytes cannot exceed max_message_bytes")
        if self.max_frame_payload_bytes * self.max_chunks < self.max_message_bytes:
            raise ValueError("max_chunks cannot represent max_message_bytes")
        if self.max_connections_per_principal > self.max_connections_global:
            raise ValueError(
                "max_connections_per_principal cannot exceed max_connections_global"
            )
        if self.max_pending_per_principal > self.max_pending_global:
            raise ValueError(
                "max_pending_per_principal cannot exceed max_pending_global"
            )
        if self.max_retained_outcomes_per_session > self.max_retained_outcomes_global:
            raise ValueError(
                "per-session retained outcome count cannot exceed the global count"
            )
        if (
            self.max_retained_outcome_bytes_per_session
            > self.max_retained_outcome_bytes_global
        ):
            raise ValueError(
                "per-session retained outcome bytes cannot exceed the global bytes"
            )


@dataclass(frozen=True, slots=True)
class PeerInfo:
    transport: str
    address: object
    uid: int | None = None
    gid: int | None = None
    pid: int | None = None
    certificate: dict | None = None


@dataclass(frozen=True, slots=True)
class Principal:
    name: str


@runtime_checkable
class Authenticator(Protocol):
    def __call__(self, peer: PeerInfo, hello: dict) -> Principal: ...


@runtime_checkable
class Authorizer(Protocol):
    def __call__(
        self, principal: Principal, operation: str, metadata: dict
    ) -> bool: ...


def _validate_client_tls(context: ssl.SSLContext) -> None:
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError("TCP client TLS must verify certificates and hostnames")
    if context.minimum_version < ssl.TLSVersion.TLSv1_2:
        raise ValueError("TCP client TLS must require TLS 1.2 or newer")


def _validate_server_tls(context: ssl.SSLContext) -> None:
    if context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("TCP server TLS must require client certificates")
    if context.minimum_version < ssl.TLSVersion.TLSv1_2:
        raise ValueError("TCP server TLS must require TLS 1.2 or newer")


def _positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _positive(value: int | float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    if value <= 0 or not math.isfinite(value):
        raise ValueError(f"{name} must be finite and greater than zero")
