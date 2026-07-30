"""Payload serializers for a bidirectional remote execution protocol.

The default :class:`PickleSerializer` requires mutually trusted clients and
servers in both callable and registered-task modes. Registered operation
selection is authorization, not a deserialization sandbox: invocation arguments
are still unpickled by workers. Results and task exceptions are unpickled by
clients, establishing the reverse trust relationship as well.
"""

from __future__ import annotations

import pickle
from typing import Protocol, runtime_checkable

PICKLE_TRUST_WARNING = (
    "The default PickleSerializer requires mutually trusted clients and servers "
    "in both callable and registered-task modes: registered operation "
    "authorization is not a deserialization sandbox, and result/exception "
    "payloads require clients to trust servers in reverse"
)


class SerializationLimitError(ValueError): ...


@runtime_checkable
class Serializer(Protocol):
    """Encode invocations and outcomes across the client/server boundary.

    Implementations define their own trust model. The bundled pickle
    implementation is unsafe for untrusted input in either direction.
    """

    name: str
    protocol_version: str

    def dumps(self, value: object, *, limit: int) -> bytes: ...

    def loads(self, payload: bytes) -> object: ...


class PickleSerializer:
    """Serialize protocol payloads with pickle for mutually trusted peers.

    This serializer unpickles callable or registered-task invocation data on
    the server and result or exception data on the client. Registered task
    authorization limits callable selection but does not make unpickling safe.
    """

    name = "pickle"
    protocol_version = str(pickle.HIGHEST_PROTOCOL)

    def __init__(self, protocol: int = pickle.HIGHEST_PROTOCOL) -> None:
        self.protocol = protocol
        self.protocol_version = str(protocol)

    def dumps(self, value: object, *, limit: int) -> bytes:
        writer = _LimitedWriter(limit)
        pickle.Pickler(writer, protocol=self.protocol).dump(value)
        return bytes(writer.buffer)

    def loads(self, payload: bytes) -> object:
        return pickle.loads(payload)


class _LimitedWriter:
    """Pickle target which aborts before retaining bytes above the limit."""

    def __init__(self, limit: int) -> None:
        if limit < 0:
            raise ValueError("serialization limit must be non-negative")
        self.limit = limit
        self.buffer = bytearray()

    def write(self, chunk: bytes) -> int:
        size = len(chunk)
        if len(self.buffer) + size > self.limit:
            raise SerializationLimitError(
                f"serialized payload exceeds the {self.limit}-byte limit"
            )
        self.buffer.extend(chunk)
        return size
