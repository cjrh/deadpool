"""Experimental socket-based access to a server-owned Deadpool.

Security warning
----------------
Pickle callable mode intentionally executes code supplied by authenticated
clients. TLS authenticates peers; it does not sandbox Python deserialization or
execution. Use this package only across a mutually trusted boundary.

The current wire layout is private and versioned but not a stable interoperability
contract. Session resumption, compression, IPv6, out-of-band pickle buffers,
and durable request state are intentionally not negotiated by this release.
"""

from ._future import RemoteFuture, SubmissionState
from .client import DeadpoolClient
from .config import (
    Authenticator,
    Authorizer,
    PeerInfo,
    Principal,
    RemoteLimits,
    TcpAddress,
    TcpListener,
    UnixAddress,
    UnixListener,
)
from .errors import (
    AcceptanceCertainty,
    ExecutionCertainty,
    RemoteAuthenticationError,
    RemoteCancellationOutcomeUnknown,
    RemoteCancelledError,
    RemoteCompatibilityError,
    RemoteConnectionLost,
    RemoteExecutorError,
    RemoteExecutorUnavailable,
    RemoteForkedProcessError,
    RemoteProcessError,
    RemoteProtocolError,
    RemoteQueueFull,
    RemoteQueueTimeout,
    RemoteResultEncodingError,
    RemoteResultLost,
    RemoteResultTooLarge,
    RemoteServerRestarted,
    RemoteSubmissionTimeout,
    RemoteTaskError,
    SubmissionOutcomeUnknown,
)
from .serializer import PickleSerializer, SerializationLimitError, Serializer
from .server import DeadpoolServer, RequestState, ServerState

__all__ = [
    "DeadpoolClient",
    "DeadpoolServer",
    "RemoteFuture",
    "SubmissionState",
    "RequestState",
    "ServerState",
    "UnixAddress",
    "TcpAddress",
    "UnixListener",
    "TcpListener",
    "RemoteLimits",
    "PeerInfo",
    "Principal",
    "Authenticator",
    "Authorizer",
    "Serializer",
    "PickleSerializer",
    "SerializationLimitError",
    "AcceptanceCertainty",
    "ExecutionCertainty",
    "RemoteExecutorError",
    "RemoteExecutorUnavailable",
    "RemoteAuthenticationError",
    "RemoteCompatibilityError",
    "RemoteQueueFull",
    "RemoteSubmissionTimeout",
    "SubmissionOutcomeUnknown",
    "RemoteQueueTimeout",
    "RemoteTaskError",
    "RemoteResultEncodingError",
    "RemoteResultTooLarge",
    "RemoteCancellationOutcomeUnknown",
    "RemoteForkedProcessError",
    "RemoteProcessError",
    "RemoteCancelledError",
    "RemoteConnectionLost",
    "RemoteServerRestarted",
    "RemoteProtocolError",
    "RemoteResultLost",
]
