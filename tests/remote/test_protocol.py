import hashlib
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
    _json_loads,
    _send_exact,
    send_message,
    validate_control,
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


def test_partial_frame_write_has_one_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PartialSendSocket:
        def __init__(self) -> None:
            self.sent = 0

        def send(self, data: memoryview) -> int:
            self.sent += 1
            return 1

    now = [-0.03]

    def monotonic() -> float:
        now[0] += 0.03
        return now[0]

    monkeypatch.setattr("deadpool.remote._protocol.time.monotonic", monotonic)
    monkeypatch.setattr(
        "deadpool.remote._protocol.select.select",
        lambda readable, writable, exceptional, timeout: ([], writable, []),
    )
    sock = PartialSendSocket()

    with pytest.raises(TimeoutError, match="remote frame write"):
        _send_exact(sock, b"slow", timeout=0.05)
    assert sock.sent < len(b"slow")


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


@pytest.mark.parametrize("value", [-(2**63), 2**63 - 1])
def test_control_accepts_signed_64_bit_boundaries(value):
    validate_control({"value": value}, small_limits())


@pytest.mark.parametrize("value", [-(2**63) - 1, 2**63])
def test_control_rejects_values_outside_signed_64_bit_range(value):
    with pytest.raises(RemoteProtocolError, match="signed 64-bit"):
        validate_control({"value": value}, small_limits())


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\xff", "invalid control JSON"),
        (b'{"key":1,"key":2}', "duplicate JSON key"),
        (b'{"value":NaN}', "non-finite JSON number"),
        (b"[]", "JSON object"),
    ],
)
def test_hostile_control_json_is_rejected(payload, message):
    with pytest.raises(RemoteProtocolError, match=message):
        _json_loads(payload, small_limits())


@pytest.mark.parametrize(
    ("prefix", "message"),
    [
        (struct.pack("!4sBBBBII", b"NOPE", MAJOR, MINOR, 20, 0, 0, 0), "magic"),
        (struct.pack("!4sBBBBII", MAGIC, MAJOR + 1, MINOR, 20, 0, 0, 0), "version"),
        (struct.pack("!4sBBBBII", MAGIC, MAJOR, MINOR, 20, 2, 0, 0), "flags"),
        (
            struct.pack("!4sBBBBII", MAGIC, MAJOR, MINOR, 20, 0, 4097, 0),
            "control header",
        ),
        (struct.pack("!4sBBBBII", MAGIC, MAJOR, MINOR, 255, 0, 0, 0), "message type"),
    ],
)
def test_invalid_prefix_is_rejected_without_waiting_for_a_body(prefix, message):
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(prefix)
        with pytest.raises(RemoteProtocolError, match=message):
            MessageReader(small_limits(partial_frame_timeout=0.02)).receive(receiver)
    finally:
        sender.close()
        receiver.close()


def chunk_header(**changes):
    values = {
        "message_id": "message",
        "index": 0,
        "count": 1,
        "total": 0,
        "digest": hashlib.sha256(b"").hexdigest(),
        "control": {},
    }
    values.update(changes)
    return values


@pytest.mark.parametrize(
    ("header", "payload", "message"),
    [
        (chunk_header(index=True), b"", "must be integers"),
        (chunk_header(index=1), b"", "index/count"),
        (chunk_header(index=1, count=2), b"", "start at index zero"),
        (chunk_header(total=0), b"x", "exceeds declared total"),
        (chunk_header(total=1), b"x", "digest mismatch"),
    ],
)
def test_chunk_state_machine_rejects_inconsistent_input(header, payload, message):
    with pytest.raises(RemoteProtocolError, match=message):
        MessageReader(small_limits())._accept(MessageType.RESULT, header, payload)


def test_deep_control_json_is_a_typed_protocol_error() -> None:
    payload = b"[" * 2000 + b"0" + b"]" * 2000
    with pytest.raises(RemoteProtocolError, match="too deeply nested"):
        _json_loads(payload, small_limits())


def test_maximum_json_nesting_is_accepted() -> None:
    payload = b'{"value":' + b"[" * 12 + b"]" * 12 + b"}"
    assert _json_loads(payload, small_limits())


def test_forbidden_json_nesting_is_rejected_before_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("over-depth JSON reached the recursive decoder")

    monkeypatch.setattr("deadpool.remote._protocol.json.loads", fail_if_called)
    payload = b'{"escaped":"\\\\","value":' + b"[" * 13 + b"]" * 13 + b"}"
    with pytest.raises(RemoteProtocolError, match="too deeply nested"):
        _json_loads(payload, small_limits())


def test_json_nesting_check_ignores_delimiters_in_strings() -> None:
    payload = b'{"value":"' + b"\\\"" + b"[" * 20 + b'"}'
    assert _json_loads(payload, small_limits()) == {"value": '"' + "[" * 20}


def test_json_decoder_recursion_error_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recurse(*args: object, **kwargs: object) -> object:
        raise RecursionError("decoder recursion limit")

    monkeypatch.setattr("deadpool.remote._protocol.json.loads", recurse)
    with pytest.raises(RemoteProtocolError, match="invalid control JSON"):
        _json_loads(b"{}", small_limits())


def test_chunk_state_bounds_incomplete_messages_and_conflicting_metadata():
    reader = MessageReader(small_limits(max_incomplete_messages=1))
    digest = hashlib.sha256(b"xx").hexdigest()
    first = chunk_header(count=2, total=2, digest=digest)
    assert reader._accept(MessageType.RESULT, first, b"x") is None

    with pytest.raises(RemoteProtocolError, match="too many incomplete"):
        reader._accept(
            MessageType.RESULT,
            chunk_header(message_id="other", count=2, total=2, digest=digest),
            b"x",
        )
    with pytest.raises(RemoteProtocolError, match="conflicting chunk metadata"):
        reader._accept(
            MessageType.RESULT,
            chunk_header(index=1, count=2, total=3, digest=digest, control=None),
            b"x",
        )

    completed = reader._accept(
        MessageType.RESULT,
        chunk_header(index=1, count=2, total=2, digest=digest, control=None),
        b"x",
    )
    assert completed == Message(MessageType.RESULT, {}, b"xx")


def test_chunk_state_bounds_aggregate_incomplete_payload_bytes() -> None:
    limits = small_limits(
        max_frame_payload_bytes=2,
        max_message_bytes=4,
        max_invocation_bytes=4,
        max_result_bytes=4,
        max_chunks=2,
        max_incomplete_messages=4,
    )
    digest = hashlib.sha256(b"xxxx").hexdigest()
    reader = MessageReader(limits)
    for message_id in ("first", "second"):
        assert (
            reader._accept(
                MessageType.RESULT,
                chunk_header(message_id=message_id, count=2, total=4, digest=digest),
                b"xx",
            )
            is None
        )

    with pytest.raises(RemoteProtocolError, match="aggregate incomplete payload"):
        reader._accept(
            MessageType.RESULT,
            chunk_header(message_id="third", count=2, total=4, digest=digest),
            b"x",
        )

    completing = MessageReader(limits)
    assert (
        completing._accept(
            MessageType.RESULT,
            chunk_header(message_id="complete", count=2, total=4, digest=digest),
            b"xx",
        )
        is None
    )
    assert completing._accept(
        MessageType.RESULT,
        chunk_header(
            message_id="complete",
            index=1,
            count=2,
            total=4,
            digest=digest,
            control=None,
        ),
        b"xx",
    ) == Message(MessageType.RESULT, {}, b"xxxx")
    assert completing._incomplete_bytes == 0


def test_oversized_outbound_message_is_rejected_before_socket_io():
    class NoIoSocket:
        def send(self, data):
            raise AssertionError("send must not be called")

    with pytest.raises(RemoteProtocolError, match="message payload"):
        send_message(
            NoIoSocket(),
            Message(MessageType.RESULT, {}, b"x" * 129),
            small_limits(),
        )
