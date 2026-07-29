"""Payload serializers.

Pickle callable mode is remote code execution by design and is only appropriate
between mutually trusted, compatible Python processes.
"""

from __future__ import annotations

import pickle
from typing import Protocol, runtime_checkable


class SerializationLimitError(ValueError): ...


@runtime_checkable
class Serializer(Protocol):
    name: str
    protocol_version: str

    def dumps(self, value: object, *, limit: int) -> bytes: ...

    def loads(self, payload: bytes) -> object: ...


class PickleSerializer:
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
