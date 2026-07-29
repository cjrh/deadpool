import socket
import struct
import threading

import pytest

from deadpool.remote import RemoteLimits, RemoteProtocolError
from deadpool.remote._protocol import (
    MAGIC,
    MAJOR,
    MINOR,
    Message,
    MessageReader,
    MessageType,
    send_message,
)


def small_limits(**changes):
    values = {
        "max_control_bytes": 4096,
        "max_frame_payload_bytes": 16,
        "max_message_bytes": 128,
        "max_invocation_bytes": 128,
        "max_result_bytes": 128,
        "max_metadata_bytes": 1024,
        "max_chunks": 16,
    }
    values.update(changes)
    return RemoteLimits(**values)


def test_chunked_round_trip_preserves_opaque_payload():
    sender, receiver = socket.socketpair()
    payload = bytes(range(80))
    thread = threading.Thread(
        target=send_message,
        args=(
            sender,
            Message(MessageType.RESULT, {"request_id": "r"}, payload),
            small_limits(),
        ),
    )
    thread.start()
    received = MessageReader(small_limits()).receive(receiver)
    thread.join()
    sender.close()
    receiver.close()

    assert received.kind == MessageType.RESULT
    assert received.control == {"request_id": "r"}
    assert received.payload == payload


def test_partial_frame_has_a_bounded_deadline():
    sender, receiver = socket.socketpair()
    sender.sendall(MAGIC[:1])
    with pytest.raises(TimeoutError, match="partial remote frame"):
        MessageReader(small_limits(partial_frame_timeout=0.02)).receive(receiver)
    sender.close()
    receiver.close()


def test_declared_frame_limit_is_rejected_before_payload_read():
    sender, receiver = socket.socketpair()
    prefix = struct.pack("!4sBBBBII", MAGIC, MAJOR, MINOR, MessageType.RESULT, 0, 2, 17)
    sender.sendall(prefix + b"{}")
    with pytest.raises(RemoteProtocolError, match="frame payload"):
        MessageReader(small_limits()).receive(receiver)
    sender.close()
    receiver.close()


def test_remote_limits_reject_unbounded_values():
    with pytest.raises(ValueError):
        RemoteLimits(max_message_bytes=0)
    with pytest.raises(ValueError):
        RemoteLimits(control_timeout=float("inf"))
    with pytest.raises(TypeError):
        RemoteLimits(max_chunks=1.5)
