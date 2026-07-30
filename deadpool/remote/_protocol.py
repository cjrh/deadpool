"""Experimental Deadpool remote wire protocol.

The behavioral specification deliberately does not freeze wire octets.  This
private version is therefore suitable only between matching Deadpool releases;
it must not be advertised as a stable interoperability protocol.
"""

from __future__ import annotations

import hashlib
import json
import math
import select
import socket
import ssl
import struct
import time
import uuid
from dataclasses import dataclass
from enum import IntEnum

from .config import RemoteLimits
from .errors import RemoteProtocolError

MAGIC = b"DPR1"
MAJOR = 1
MINOR = 0
_PREFIX = struct.Struct("!4sBBBBII")
_FLAG_CHUNKED = 1
_WIRE_LIMIT_FIELDS = (
    "max_control_bytes",
    "max_frame_payload_bytes",
    "max_message_bytes",
    "max_metadata_bytes",
    "max_chunks",
)


def _wire_limits(limits: RemoteLimits) -> dict[str, int]:
    return {name: getattr(limits, name) for name in _WIRE_LIMIT_FIELDS}


def _validate_wire_limits(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(_WIRE_LIMIT_FIELDS):
        raise RemoteProtocolError("wire limits have invalid fields")
    for name, limit in value.items():
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise RemoteProtocolError(f"wire limit {name} must be a positive integer")
    return dict(value)


class MessageType(IntEnum):
    HELLO = 1
    WELCOME = 2
    HANDSHAKE_REJECTED = 3
    PING = 4
    PONG = 5
    GOAWAY = 6
    PROTOCOL_ERROR = 7
    SUBMIT = 10
    ACCEPTED = 11
    REJECTED = 12
    RUNNING = 13
    RESULT = 20
    TASK_ERROR = 21
    TIMED_OUT = 22
    CANCELLED = 23
    WORKER_LOST = 24
    RESULT_ENCODING_FAILED = 25
    RESULT_TOO_LARGE = 26
    RESULT_ACK = 27
    QUEUE_TIMED_OUT = 28
    CANCEL_REQUEST = 30
    CANCEL_RESPONSE = 31
    STATUS_REQUEST = 32
    STATUS_RESPONSE = 33
    STATS_REQUEST = 34
    STATS_RESPONSE = 35
    CANCEL_GROUP = 36
    CANCEL_GROUP_RESPONSE = 37
    CLOSE_SESSION = 38
    CLOSE_SESSION_RESPONSE = 39


@dataclass(frozen=True, slots=True)
class Message:
    kind: MessageType
    control: dict
    payload: bytes = b""


@dataclass(slots=True)
class _Chunks:
    kind: MessageType
    control: dict
    count: int
    total: int
    digest: str
    next_index: int
    parts: list[bytes]
    received: int = 0


def _json_loads(data: bytes, limits: RemoteLimits) -> dict:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RemoteProtocolError(f"invalid control JSON: {error}") from error
    _validate_json(value, limits)
    if not isinstance(value, dict):
        raise RemoteProtocolError("control header must be a JSON object")
    return value


def _unique_object(items: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _validate_json(value: object, limits: RemoteLimits, depth: int = 0) -> None:
    if depth > 12:
        raise RemoteProtocolError("control JSON is too deeply nested")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > limits.max_metadata_bytes:
            raise RemoteProtocolError("control string exceeds metadata limit")
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int):
        if not -(1 << 63) <= value < (1 << 63):
            raise RemoteProtocolError("control integer exceeds signed 64-bit range")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise RemoteProtocolError("control number must be finite")
    elif isinstance(value, list):
        if len(value) > 1024:
            raise RemoteProtocolError("control list is too large")
        for item in value:
            _validate_json(item, limits, depth + 1)
    elif isinstance(value, dict):
        if len(value) > 128:
            raise RemoteProtocolError("control object has too many keys")
        for key, item in value.items():
            if not isinstance(key, str):
                raise RemoteProtocolError("control object keys must be strings")
            _validate_json(key, limits, depth + 1)
            _validate_json(item, limits, depth + 1)
    else:
        raise RemoteProtocolError(f"unsupported control value {type(value).__name__}")


def _json_dumps(value: dict, limits: RemoteLimits) -> bytes:
    _validate_json(value, limits)
    payload = json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    if len(payload) > limits.max_control_bytes:
        raise RemoteProtocolError("control header exceeds configured limit")
    return payload


def validate_control(control: dict, limits: RemoteLimits) -> None:
    """Validate a logical control object without serializing application data."""
    _json_dumps(control, limits)


def validate_message_control(
    control: dict, payload_size: int, limits: RemoteLimits
) -> None:
    """Validate control data inside its complete first-frame envelope."""
    count = _message_chunk_count(payload_size, limits)
    _encode_frame_control(
        "0" * 32,
        index=0,
        count=count,
        total=payload_size,
        digest="0" * 64,
        control=control,
        limits=limits,
    )


def _message_chunk_count(payload_size: int, limits: RemoteLimits) -> int:
    if payload_size < 0 or payload_size > limits.max_message_bytes:
        raise RemoteProtocolError("message payload exceeds configured limit")
    count = max(
        1,
        (payload_size + limits.max_frame_payload_bytes - 1)
        // limits.max_frame_payload_bytes,
    )
    if count > limits.max_chunks:
        raise RemoteProtocolError("message requires too many chunks")
    return count


def _encode_frame_control(
    message_id: str,
    *,
    index: int,
    count: int,
    total: int,
    digest: str,
    control: dict | None,
    limits: RemoteLimits,
) -> bytes:
    return _json_dumps(
        {
            "message_id": message_id,
            "index": index,
            "count": count,
            "total": total,
            "digest": digest,
            "control": control,
        },
        limits,
    )


def send_message(
    sock: socket.socket,
    message: Message,
    limits: RemoteLimits,
) -> None:
    """Send one logical message as one or more independently bounded frames."""
    payload = memoryview(message.payload)
    frame_size = limits.max_frame_payload_bytes
    count = _message_chunk_count(len(payload), limits)
    message_id = uuid.uuid4().hex
    digest = hashlib.sha256(payload).hexdigest()
    for index in range(count):
        start = index * frame_size
        chunk = payload[start : start + frame_size]
        control = _encode_frame_control(
            message_id,
            index=index,
            count=count,
            total=len(payload),
            digest=digest,
            control=message.control if index == 0 else None,
            limits=limits,
        )
        prefix = _PREFIX.pack(
            MAGIC,
            MAJOR,
            MINOR,
            int(message.kind),
            _FLAG_CHUNKED if count > 1 else 0,
            len(control),
            len(chunk),
        )
        _send_exact(sock, prefix, limits.partial_frame_timeout)
        _send_exact(sock, control, limits.partial_frame_timeout)
        if chunk:
            _send_exact(sock, chunk, limits.partial_frame_timeout)


class MessageReader:
    """Incrementally reassemble bounded, potentially interleaved messages."""

    def __init__(self, limits: RemoteLimits) -> None:
        self.limits = limits
        self._messages: dict[str, _Chunks] = {}
        self._incomplete_bytes = 0

    def receive(
        self,
        sock: socket.socket,
        *,
        deadline: float | None = None,
    ) -> Message:
        while True:
            prefix = _recv_exact(
                sock,
                _PREFIX.size,
                timeout=_bounded_timeout(
                    self.limits.partial_frame_timeout,
                    deadline,
                ),
                allow_idle=True,
            )
            magic, major, minor, raw_kind, flags, control_len, payload_len = (
                _PREFIX.unpack(prefix)
            )
            if magic != MAGIC:
                raise RemoteProtocolError("invalid protocol magic")
            if major != MAJOR or minor > MINOR:
                raise RemoteProtocolError(
                    f"unsupported protocol version {major}.{minor}"
                )
            if flags & ~_FLAG_CHUNKED:
                raise RemoteProtocolError("unknown mandatory frame flags")
            if control_len > self.limits.max_control_bytes:
                raise RemoteProtocolError("declared control header is too large")
            if payload_len > self.limits.max_frame_payload_bytes:
                raise RemoteProtocolError("declared frame payload is too large")
            try:
                kind = MessageType(raw_kind)
            except ValueError as error:
                raise RemoteProtocolError(f"unknown message type {raw_kind}") from error
            frame_control = _json_loads(
                _recv_exact(
                    sock,
                    control_len,
                    timeout=_bounded_timeout(
                        self.limits.partial_frame_timeout,
                        deadline,
                    ),
                ),
                self.limits,
            )
            payload = _recv_exact(
                sock,
                payload_len,
                timeout=_bounded_timeout(
                    self.limits.partial_frame_timeout,
                    deadline,
                ),
            )
            complete = self._accept(kind, frame_control, payload)
            if complete is not None:
                return complete

    def _accept(
        self, kind: MessageType, header: dict, payload: bytes
    ) -> Message | None:
        required = {"message_id", "index", "count", "total", "digest", "control"}
        if set(header) != required:
            raise RemoteProtocolError("invalid chunk control fields")
        message_id = header["message_id"]
        index = header["index"]
        count = header["count"]
        total = header["total"]
        digest = header["digest"]
        if not isinstance(message_id, str) or len(message_id) > 64:
            raise RemoteProtocolError("invalid message ID")
        if not all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in (index, count, total)
        ):
            raise RemoteProtocolError("chunk indices and lengths must be integers")
        if count < 1 or count > self.limits.max_chunks or not 0 <= index < count:
            raise RemoteProtocolError("invalid chunk index/count")
        if total < 0 or total > self.limits.max_message_bytes:
            raise RemoteProtocolError("declared message payload is too large")
        if not isinstance(digest, str) or len(digest) != 64:
            raise RemoteProtocolError("invalid payload digest")

        state = self._messages.get(message_id)
        if state is None:
            if index != 0 or not isinstance(header["control"], dict):
                raise RemoteProtocolError("chunk sequence must start at index zero")
            if len(self._messages) >= self.limits.max_incomplete_messages:
                raise RemoteProtocolError("too many incomplete messages")
            state = _Chunks(kind, header["control"], count, total, digest, 0, [])
            self._messages[message_id] = state
        elif (
            state.kind != kind
            or state.count != count
            or state.total != total
            or state.digest != digest
            or header["control"] is not None
        ):
            raise RemoteProtocolError("conflicting chunk metadata")
        if index != state.next_index:
            raise RemoteProtocolError("duplicate or out-of-order chunk")
        if self._incomplete_bytes + len(payload) > self.limits.max_message_bytes:
            raise RemoteProtocolError("aggregate incomplete payload is too large")
        state.parts.append(payload)
        state.received += len(payload)
        self._incomplete_bytes += len(payload)
        state.next_index += 1
        if state.received > state.total:
            raise RemoteProtocolError("chunk sequence exceeds declared total")
        if index + 1 != count:
            return None
        del self._messages[message_id]
        self._incomplete_bytes -= state.received
        if state.received != state.total:
            raise RemoteProtocolError(
                "chunk sequence length does not match declared total"
            )
        complete = b"".join(state.parts)
        if hashlib.sha256(complete).hexdigest() != state.digest:
            raise RemoteProtocolError("message payload digest mismatch")
        return Message(kind, state.control, complete)


def _bounded_timeout(timeout: float, deadline: float | None) -> float:
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("remote handshake timed out")
    return min(timeout, remaining)


def _send_exact(sock: socket.socket, data: bytes | memoryview, timeout: float) -> None:
    """Write bytes within one monotonic deadline instead of unbounded sendall."""
    view = memoryview(data)
    sent = 0
    deadline = time.monotonic() + timeout
    while sent < len(view):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("remote frame write timed out")
        try:
            _, writable, _ = select.select([], [sock], [], remaining)
            if not writable:
                raise TimeoutError("remote frame write timed out")
            count = sock.send(view[sent:])
        except ssl.SSLWantReadError:
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                raise TimeoutError("remote TLS frame write timed out")
            continue
        except ssl.SSLWantWriteError:
            continue
        if count == 0:
            raise EOFError("connection closed during frame write")
        sent += count


def _recv_exact(
    sock: socket.socket,
    size: int,
    *,
    timeout: float,
    allow_idle: bool = False,
) -> bytes:
    data = bytearray(size)
    view = memoryview(data)
    received = 0
    deadline = None
    while received < size:
        if not (allow_idle and received == 0):
            if deadline is None:
                deadline = time.monotonic() + timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _socket_readable(sock, remaining):
                raise TimeoutError("partial remote frame timed out")
        count = sock.recv_into(view[received:])
        if count == 0:
            raise EOFError("connection closed during frame")
        received += count
        if deadline is None:
            deadline = time.monotonic() + timeout
    return bytes(data)


def _socket_readable(sock: socket.socket, timeout: float) -> bool:
    pending = getattr(sock, "pending", None)
    if pending is not None and pending():
        return True
    readable, _, _ = select.select([sock], [], [], timeout)
    return bool(readable)
