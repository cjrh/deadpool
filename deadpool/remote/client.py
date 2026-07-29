"""Synchronous Executor-compatible client for a remote Deadpool server."""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import math
import os
import platform
import queue
import socket
import sys
import threading
import time
import uuid
from typing import Callable, Iterable

from deadpool import TimeoutError as DeadpoolTimeoutError

from ._future import RemoteFuture, SubmissionState
from ._protocol import (
    MAJOR,
    MINOR,
    Message,
    MessageReader,
    MessageType,
    send_message,
    validate_control,
)
from ._transport import connect_address
from .config import RemoteLimits, TcpAddress, UnixAddress
from .errors import (
    AcceptanceCertainty,
    ExecutionCertainty,
    RemoteAuthenticationError,
    RemoteCancellationOutcomeUnknown,
    RemoteCompatibilityError,
    RemoteConnectionLost,
    RemoteExecutorError,
    RemoteExecutorUnavailable,
    RemoteProcessError,
    RemoteProtocolError,
    RemoteQueueFull,
    RemoteQueueTimeout,
    RemoteResultEncodingError,
    RemoteResultTooLarge,
    RemoteSubmissionTimeout,
    RemoteTaskError,
    SubmissionOutcomeUnknown,
)
from .serializer import PickleSerializer, Serializer

logger = logging.getLogger("deadpool.remote")


class DeadpoolClient(concurrent.futures.Executor):
    """A process-local client whose submitted work runs in a server-owned pool."""

    def __init__(
        self,
        address: UnixAddress | TcpAddress,
        *,
        serializer: Serializer | None = None,
        authenticator: Callable[[], dict] | None = None,
        application_fingerprint: str | None = None,
        registry_fingerprint: str | None = None,
        limits: RemoteLimits | None = None,
        submission_timeout: float = 5.0,
        control_timeout: float = 5.0,
    ) -> None:
        self.address = address
        if serializer is None:
            logger.warning(
                "Deadpool remote callable mode uses pickle and grants trusted "
                "clients code execution as the worker account"
            )
        self.serializer = serializer or PickleSerializer()
        self.authenticator = authenticator
        self.application_fingerprint = application_fingerprint
        self.registry_fingerprint = registry_fingerprint
        self.limits = limits or RemoteLimits()
        self.submission_timeout = _positive_timeout(
            submission_timeout, "submission_timeout"
        )
        self.control_timeout = _positive_timeout(control_timeout, "control_timeout")
        self._owner_pid = os.getpid()
        self._client_id = uuid.uuid4().hex
        self._sequence = 0
        self._lock = threading.RLock()
        self._socket: socket.socket | None = None
        self._reader: MessageReader | None = None
        self._outbound: queue.Queue[tuple[Message | None, RemoteFuture | None]] = (
            queue.Queue(self.limits.outbound_queue_size)
        )
        self._futures: dict[str, RemoteFuture] = {}
        self._terminal_received: set[str] = set()
        self._rpc: dict[str, tuple[threading.Event, dict]] = {}
        self._closed = False
        self._transport_failed = False
        # One ordered state lane preserves wire order. User callbacks have a
        # separate pool and cannot block transport/state progression.
        self._completion_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="deadpool.remote.completion",
        )
        self._serialization_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.limits.completion_workers,
            thread_name_prefix="deadpool.remote.serialization",
        )
        self._callback_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.limits.completion_workers,
            thread_name_prefix="deadpool.remote.callback",
        )
        self._completion_slots = threading.BoundedSemaphore(
            self.limits.inbound_queue_size
        )
        self._callback_slots = threading.BoundedSemaphore(
            self.limits.callback_queue_size
        )
        self._serialization_slots = threading.BoundedSemaphore(
            self.limits.outbound_queue_size
        )
        self.server_id: str | None = None
        self.server_epoch: str | None = None
        self.session_id: str | None = None
        self.negotiated_limits: dict = {}
        self._connect()

    def _connect(self) -> None:
        try:
            sock = connect_address(self.address)
            sock.settimeout(self.limits.handshake_timeout)
            hello = {
                "versions": [[MAJOR, MINOR]],
                "features": ["chunking"],
                "wire": "experimental-deadpool-private-v1",
                "client_instance_id": self._client_id,
                "serializer": self.serializer.name,
                "serializer_protocol": self.serializer.protocol_version,
                "python": [sys.version_info.major, sys.version_info.minor],
                "implementation": platform.python_implementation(),
                "deadpool_version": _deadpool_version(),
                "capabilities": ["callable", "registered"],
                "application_fingerprint": self.application_fingerprint,
                "registry_fingerprint": self.registry_fingerprint,
                "authentication": self.authenticator() if self.authenticator else None,
                "max_result_bytes": self.limits.max_result_bytes,
            }
            send_message(sock, Message(MessageType.HELLO, hello), self.limits)
            reader = MessageReader(self.limits)
            response = reader.receive(
                sock,
                deadline=time.monotonic() + self.limits.handshake_timeout,
            )
            if response.kind == MessageType.HANDSHAKE_REJECTED:
                reason = response.control.get("reason")
                error_type = (
                    RemoteAuthenticationError
                    if reason == "authentication"
                    else RemoteCompatibilityError
                )
                raise error_type(f"remote handshake rejected: {reason}")
            if response.kind != MessageType.WELCOME:
                raise RemoteProtocolError("server did not send WELCOME")
            if response.control.get("wire") != "experimental-deadpool-private-v1":
                raise RemoteCompatibilityError(
                    "server selected an incompatible wire protocol"
                )
            sock.settimeout(None)
        except RemoteExecutorError:
            try:
                sock.close()
            except (UnboundLocalError, OSError):
                pass
            raise
        except BaseException as error:
            try:
                sock.close()
            except (UnboundLocalError, OSError):
                pass
            raise RemoteExecutorUnavailable(str(error)) from error
        with self._lock:
            self._socket = sock
            self._reader = reader
            self.server_id = response.control.get("server_id")
            self.server_epoch = response.control.get("epoch")
            self.session_id = response.control.get("session_id")
            self.negotiated_limits = dict(response.control.get("limits") or {})
            self._transport_failed = False
        threading.Thread(
            target=self._sender_loop, name="deadpool.remote.sender", daemon=True
        ).start()
        threading.Thread(
            target=self._receiver_loop, name="deadpool.remote.receiver", daemon=True
        ).start()

    def submit(self, fn: Callable, /, *args, **kwargs) -> RemoteFuture:
        return self._submit("callable", fn, args, kwargs)

    def submit_task(self, operation: str, /, *args, **kwargs) -> RemoteFuture:
        if not isinstance(operation, str) or not operation:
            raise ValueError("operation must be a non-empty string")
        return self._submit("registered", operation, args, kwargs)

    def _submit(
        self, mode: str, target: object, args: tuple, kwargs: dict
    ) -> RemoteFuture:
        self._ensure_process()
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
        options = _submission_options(kwargs, self.submission_timeout)
        priority = options["priority"]
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            raise ValueError("deadpool_priority must be a non-negative integer")
        deadline = time.monotonic() + options["submission_timeout"]
        invocation_value = (
            (target, args, kwargs) if mode == "callable" else (args, kwargs)
        )
        limit = min(
            self.limits.max_invocation_bytes,
            int(
                self.negotiated_limits.get(
                    "max_invocation_bytes", self.limits.max_invocation_bytes
                )
            ),
        )
        payload = self._serialize_invocation(invocation_value, limit, deadline)
        with self._lock:
            # Serialization deliberately runs outside the state lock. Recheck
            # both shutdown and transport state when linearizing registration,
            # because either may have changed while serialization was running.
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if self._transport_failed or self._socket is None:
                raise RemoteExecutorUnavailable("connection is unavailable")
            self._sequence += 1
            request_id = f"{self._client_id}:{self._sequence}"
            future = RemoteFuture(request_id, self)
            self._futures[request_id] = future
        control = {
            "request_id": request_id,
            "digest": hashlib.sha256(payload).hexdigest(),
            "mode": mode,
            "operation": target if mode == "registered" else None,
            "priority": priority,
            "execution_timeout": options["execution_timeout"],
            "queue_timeout": options["queue_timeout"],
            "group_id": options["group_id"],
            "metadata": options["metadata"],
        }
        try:
            validate_control(control, self.limits)
        except BaseException:
            with self._lock:
                self._futures.pop(request_id, None)
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with self._lock:
                self._futures.pop(request_id, None)
            raise RemoteSubmissionTimeout(
                "local submission work exceeded its timeout", request_id=request_id
            )
        try:
            self._outbound.put(
                (Message(MessageType.SUBMIT, control, payload), future),
                timeout=remaining,
            )
        except queue.Full as error:
            with self._lock:
                self._futures.pop(request_id, None)
            raise RemoteSubmissionTimeout(
                "client outbound queue is full", request_id=request_id
            ) from error
        return future

    def _serialize_invocation(
        self,
        invocation: object,
        limit: int,
        deadline: float,
    ) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._serialization_slots.acquire(timeout=remaining):
            raise RemoteSubmissionTimeout("serializer capacity wait timed out")
        try:
            work = self._serialization_executor.submit(
                self.serializer.dumps,
                invocation,
                limit=limit,
            )
        except BaseException:
            self._serialization_slots.release()
            raise
        work.add_done_callback(lambda _: self._serialization_slots.release())
        remaining = deadline - time.monotonic()
        try:
            return work.result(timeout=max(0.0, remaining))
        except concurrent.futures.TimeoutError as error:
            work.cancel()
            raise RemoteSubmissionTimeout(
                "invocation serialization exceeded submission timeout"
            ) from error

    def submit_many(
        self, submissions: Iterable[tuple[Callable, tuple, dict]]
    ) -> list[RemoteFuture]:
        return [self.submit(fn, *args, **kwargs) for fn, args, kwargs in submissions]

    def _sender_loop(self) -> None:
        while True:
            message, future = self._outbound.get()
            try:
                if message is None:
                    return
                if future is not None and not future._set_sent():
                    self._forget(future)
                    continue
                with self._lock:
                    sock = self._socket
                if sock is None:
                    raise OSError("connection is closed")
                send_message(sock, message, self.limits)
            except BaseException as error:
                self._connection_lost(error)
                return
            finally:
                self._outbound.task_done()

    def _receiver_loop(self) -> None:
        try:
            while True:
                with self._lock:
                    sock, reader = self._socket, self._reader
                if sock is None or reader is None:
                    return
                message = reader.receive(sock)
                self._receive(message)
        except BaseException as error:
            self._connection_lost(error)

    def _receive(self, message: Message) -> None:
        request_id = message.control.get("request_id")
        with self._lock:
            future = (
                self._futures.get(request_id) if isinstance(request_id, str) else None
            )
        if message.kind == MessageType.ACCEPTED and future is not None:
            self._schedule_completion(future._set_accepted)
        elif message.kind == MessageType.RUNNING and future is not None:
            self._schedule_completion(
                future._set_running,
                pid=message.control.get("pid"),
                worker_id=message.control.get("worker_id"),
            )
        elif (
            message.kind
            in {
                MessageType.RESULT,
                MessageType.TASK_ERROR,
                MessageType.TIMED_OUT,
                MessageType.CANCELLED,
                MessageType.WORKER_LOST,
                MessageType.RESULT_ENCODING_FAILED,
                MessageType.RESULT_TOO_LARGE,
                MessageType.QUEUE_TIMED_OUT,
            }
            and future is not None
        ):
            with self._lock:
                self._terminal_received.add(future.request_id)
            self._schedule_completion(self._complete, future, message)
        elif message.kind == MessageType.REJECTED and future is not None:
            self._schedule_completion(self._reject, future, message.control)
        elif message.kind in {
            MessageType.CANCEL_RESPONSE,
            MessageType.CANCEL_GROUP_RESPONSE,
            MessageType.STATUS_RESPONSE,
            MessageType.STATS_RESPONSE,
            MessageType.PONG,
            MessageType.CLOSE_SESSION_RESPONSE,
        }:
            token = message.control.get("token") or message.control.get("nonce")
            if isinstance(token, str):
                with self._lock:
                    rpc = self._rpc.pop(token, None)
                    if rpc is not None:
                        rpc[1].update(message.control)
                if rpc is not None:
                    rpc[0].set()
        elif message.kind == MessageType.GOAWAY:
            self._connection_lost(RemoteConnectionLost("server closed the session"))
        elif message.kind in {
            MessageType.PROTOCOL_ERROR,
            MessageType.HANDSHAKE_REJECTED,
        }:
            self._connection_lost(RemoteProtocolError(str(message.control)))

    def _complete(self, future: RemoteFuture, message: Message) -> None:
        acknowledge = False
        try:
            if message.kind == MessageType.RESULT:
                future._set_result(self.serializer.loads(message.payload))
            elif message.kind == MessageType.TASK_ERROR:
                error = None
                if message.payload:
                    try:
                        decoded = self.serializer.loads(message.payload)
                        if isinstance(decoded, BaseException):
                            error = decoded
                    except BaseException:
                        pass
                if error is None:
                    error = RemoteTaskError(
                        str(message.control.get("message", "remote task failed")),
                        request_id=future.request_id,
                        remote_traceback=str(message.control.get("traceback", "")),
                    )
                future._set_exception(error, task=True)
            elif message.kind == MessageType.TIMED_OUT:
                future._set_exception(
                    DeadpoolTimeoutError(
                        message.control.get("message", "remote task timed out")
                    )
                )
            elif message.kind == MessageType.CANCELLED:
                future._set_cancelled()
            elif message.kind == MessageType.QUEUE_TIMED_OUT:
                future._set_exception(
                    RemoteQueueTimeout(
                        "remote queue wait expired", request_id=future.request_id
                    )
                )
            elif message.kind == MessageType.WORKER_LOST:
                future._set_exception(
                    RemoteProcessError(
                        str(message.control.get("message", "worker lost")),
                        request_id=future.request_id,
                    )
                )
            elif message.kind == MessageType.RESULT_ENCODING_FAILED:
                future._set_exception(
                    RemoteResultEncodingError(
                        str(message.control.get("message", "result encoding failed")),
                        request_id=future.request_id,
                    )
                )
            elif message.kind == MessageType.RESULT_TOO_LARGE:
                future._set_exception(
                    RemoteResultTooLarge(
                        str(message.control.get("message", "result too large")),
                        request_id=future.request_id,
                    )
                )
            acknowledge = True
        except BaseException as error:
            future._set_exception(
                error
                if isinstance(error, RemoteExecutorError)
                else RemoteProtocolError(str(error), request_id=future.request_id)
            )
        finally:
            if acknowledge:
                try:
                    self._enqueue_control(
                        MessageType.RESULT_ACK, {"request_id": future.request_id}
                    )
                finally:
                    self._forget(future)
                    with self._lock:
                        self._terminal_received.discard(future.request_id)

    def _reject(self, future: RemoteFuture, control: dict) -> None:
        reason = control.get("reason", "rejected")
        if reason == "queue_full":
            error = RemoteQueueFull(
                "server queue is full", request_id=future.request_id
            )
        elif reason == "queue_timeout":
            error = RemoteQueueTimeout(
                "server queue wait expired", request_id=future.request_id
            )
        else:
            error = RemoteExecutorError(
                f"remote submission rejected: {reason}",
                request_id=future.request_id,
                acceptance_certainty=AcceptanceCertainty.NOT_ACCEPTED,
                execution_certainty=ExecutionCertainty.NOT_STARTED,
            )
        future._set_exception(error)
        self._forget(future)

    def _cancel(self, future: RemoteFuture, *, hard: bool) -> bool:
        token = uuid.uuid4().hex
        event, response = threading.Event(), {}
        with self._lock:
            if self._socket is None:
                raise RemoteCancellationOutcomeUnknown(
                    "connection is unavailable", request_id=future.request_id
                )
            self._rpc[token] = (event, response)
        try:
            self._enqueue_control(
                MessageType.CANCEL_REQUEST,
                {"request_id": future.request_id, "hard": hard, "token": token},
            )
            if not event.wait(self.control_timeout):
                raise RemoteCancellationOutcomeUnknown(
                    "timed out waiting for cancellation decision",
                    request_id=future.request_id,
                )
            decision = response.get("response")
            if decision == "cancelled":
                # The response is authoritative enough for cancel()'s return
                # value, but retain the request until the terminal CANCELLED
                # frame is received and acknowledged.
                future._set_cancelled()
                return True
            if decision in {"running", "terminal"}:
                return False
            raise RemoteCancellationOutcomeUnknown(
                f"cancellation decision is {decision!r}", request_id=future.request_id
            )
        finally:
            with self._lock:
                self._rpc.pop(token, None)

    def cancel_group(
        self,
        group_id: str,
        *,
        hard: bool = False,
        timeout: float | None = None,
    ) -> dict[str, int]:
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("group_id must be a non-empty string")
        response = self._rpc_request(
            MessageType.CANCEL_GROUP,
            {"group_id": group_id, "hard": hard},
            timeout,
        )
        return {
            name: int(response.get(name, 0))
            for name in ("cancelled", "running", "terminal", "unknown")
        }

    def check_health(self, timeout: float | None = None) -> bool:
        response = self._rpc_request(MessageType.PING, {}, timeout)
        return "nonce" in response

    def get_statistics(self, timeout: float | None = None) -> dict:
        response = self._rpc_request(MessageType.STATS_REQUEST, {}, timeout)
        if "error" in response:
            raise RemoteExecutorError(response["error"])
        return dict(response.get("statistics") or {})

    def get_status(
        self, future_or_request_id: RemoteFuture | str, timeout: float | None = None
    ) -> str:
        request_id = (
            future_or_request_id.request_id
            if isinstance(future_or_request_id, RemoteFuture)
            else future_or_request_id
        )
        response = self._rpc_request(
            MessageType.STATUS_REQUEST, {"request_id": request_id}, timeout
        )
        return str(response.get("state", "UNKNOWN"))

    def _rpc_request(
        self, kind: MessageType, control: dict, timeout: float | None
    ) -> dict:
        self._ensure_process()
        token = uuid.uuid4().hex
        event, response = threading.Event(), {}
        with self._lock:
            if self._socket is None:
                raise RemoteConnectionLost("connection is unavailable")
            self._rpc[token] = (event, response)
        key = "nonce" if kind == MessageType.PING else "token"
        try:
            self._enqueue_control(kind, {**control, key: token})
            if not event.wait(timeout or self.control_timeout):
                raise TimeoutError("remote control request timed out")
            transport_error = response.get("_transport_error")
            if transport_error is not None:
                raise RemoteConnectionLost(str(transport_error))
            return response
        finally:
            with self._lock:
                self._rpc.pop(token, None)

    def _enqueue_control(self, kind: MessageType, control: dict) -> None:
        try:
            self._outbound.put(
                (Message(kind, control), None), timeout=self.control_timeout
            )
        except queue.Full as error:
            raise RemoteConnectionLost(
                "client outbound control queue is full"
            ) from error

    def _connection_lost(self, error: BaseException) -> None:
        with self._lock:
            if self._transport_failed:
                return
            self._transport_failed = True
            sock, self._socket = self._socket, None
            futures = list(self._futures.values())
            rpc = list(self._rpc.values())
            self._rpc.clear()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        for event, response in rpc:
            response["_transport_error"] = error
            event.set()
        for future in futures:
            with self._lock:
                terminal_received = future.request_id in self._terminal_received
            if future.done() or terminal_received:
                continue
            state = future.submission_state
            if state == SubmissionState.LOCAL_PENDING:
                exception = RemoteExecutorUnavailable(
                    str(error), request_id=future.request_id
                )
            elif state == SubmissionState.SENT_UNACKNOWLEDGED:
                exception = SubmissionOutcomeUnknown(
                    str(error), request_id=future.request_id
                )
            else:
                exception = RemoteConnectionLost(
                    str(error),
                    request_id=future.request_id,
                    acceptance_certainty=AcceptanceCertainty.ACCEPTED,
                    execution_certainty=ExecutionCertainty.MAY_HAVE_RUN,
                )
            self._schedule_completion(self._fail_and_forget, future, exception)

    def _fail_and_forget(
        self,
        future: RemoteFuture,
        exception: BaseException,
    ) -> None:
        future._set_exception(exception)
        self._forget(future)

    def _schedule_completion(self, function, *args, **kwargs) -> None:
        self._completion_slots.acquire()
        try:
            self._completion_executor.submit(
                _run_bounded,
                self._completion_slots,
                function,
                args,
                kwargs,
            )
        except RuntimeError:
            self._completion_slots.release()
            function(*args, **kwargs)

    def _forget(self, future: RemoteFuture) -> None:
        with self._lock:
            self._futures.pop(future.request_id, None)

    def _reserve_callback(self) -> None:
        self._callback_slots.acquire()

    def _schedule_callback(self, callback, future: RemoteFuture) -> None:
        try:
            self._callback_executor.submit(
                _run_reserved_callback,
                self._callback_slots,
                callback,
                future,
            )
        except RuntimeError:
            self._callback_slots.release()
            _call_callback(callback, future)

    def _schedule_callbacks(self, callbacks: list, future: RemoteFuture) -> None:
        try:
            self._callback_executor.submit(
                _run_callbacks,
                self._callback_slots,
                callbacks,
                future,
            )
        except RuntimeError:
            for callback in callbacks:
                self._callback_slots.release()
                _call_callback(callback, future)

    def _ensure_process(self) -> None:
        if os.getpid() == self._owner_pid:
            return
        # Threads and locks do not survive fork coherently. Close only the
        # child-side descriptor and build a wholly new process-local session.
        try:
            if self._socket is not None:
                self._socket.close()
        except OSError:
            pass
        self._owner_pid = os.getpid()
        self._client_id = uuid.uuid4().hex
        self._sequence = 0
        self._lock = threading.RLock()
        self._socket = None
        self._reader = None
        self._outbound = queue.Queue(self.limits.outbound_queue_size)
        self._futures = {}
        self._terminal_received = set()
        self._rpc = {}
        self._closed = False
        self._transport_failed = False
        self._completion_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="deadpool.remote.completion",
        )
        self._serialization_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.limits.completion_workers,
            thread_name_prefix="deadpool.remote.serialization",
        )
        self._callback_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.limits.completion_workers,
            thread_name_prefix="deadpool.remote.callback",
        )
        self._completion_slots = threading.BoundedSemaphore(
            self.limits.inbound_queue_size
        )
        self._callback_slots = threading.BoundedSemaphore(
            self.limits.callback_queue_size
        )
        self._serialization_slots = threading.BoundedSemaphore(
            self.limits.outbound_queue_size
        )
        self._connect()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._ensure_process()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = list(self._futures.values())
        if cancel_futures:
            for future in futures:
                if not future.done():
                    try:
                        future.cancel()
                    except RemoteExecutorError:
                        pass
        lifecycle = threading.Thread(
            target=self._finish_shutdown,
            args=(futures,),
            name="deadpool.remote.client-shutdown",
            daemon=False,
        )
        lifecycle.start()
        if wait:
            lifecycle.join()

    def _finish_shutdown(self, futures: list[RemoteFuture]) -> None:
        for future in futures:
            try:
                future.result()
            except BaseException:
                pass
        with self._lock:
            can_close_cleanly = self._socket is not None and not self._transport_failed
        if can_close_cleanly:
            try:
                self._rpc_request(
                    MessageType.CLOSE_SESSION,
                    {},
                    self.control_timeout,
                )
            except BaseException:
                pass
        try:
            self._outbound.put((None, None), timeout=self.control_timeout)
        except queue.Full:
            pass
        with self._lock:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        _drain_semaphore(
            self._completion_slots,
            self.limits.inbound_queue_size,
        )
        self._completion_executor.shutdown(wait=False)
        self._serialization_executor.shutdown(wait=False)
        self._callback_executor.shutdown(wait=False)

    def __enter__(self) -> "DeadpoolClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.shutdown()
        return False


def _submission_options(kwargs: dict, default_submission_timeout: float) -> dict:
    values = {
        "priority": kwargs.pop("deadpool_priority", 0),
        "execution_timeout": kwargs.pop("deadpool_timeout", None),
        "queue_timeout": kwargs.pop("deadpool_queue_timeout", None),
        "submission_timeout": kwargs.pop(
            "deadpool_submission_timeout", default_submission_timeout
        ),
        "group_id": kwargs.pop("deadpool_group_id", None),
        "metadata": kwargs.pop("deadpool_metadata", {}),
    }
    for name in ("execution_timeout", "queue_timeout"):
        value = values[name]
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be finite and non-negative")
    values["submission_timeout"] = _positive_timeout(
        values["submission_timeout"], "submission_timeout"
    )
    if values["group_id"] is not None:
        if not isinstance(values["group_id"], str):
            raise TypeError("deadpool_group_id must be a string or None")
        if not values["group_id"]:
            raise ValueError("deadpool_group_id must be non-empty")
    if not isinstance(values["metadata"], dict):
        raise TypeError("deadpool_metadata must be a dict")
    return values


def _positive_timeout(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and greater than zero")
    return float(value)


def _drain_semaphore(semaphore: threading.BoundedSemaphore, size: int) -> None:
    for _ in range(size):
        semaphore.acquire()
    for _ in range(size):
        semaphore.release()


def _run_bounded(semaphore, function, args: tuple, kwargs: dict) -> None:
    try:
        function(*args, **kwargs)
    finally:
        semaphore.release()


def _run_callbacks(
    semaphore: threading.BoundedSemaphore,
    callbacks: list,
    future: RemoteFuture,
) -> None:
    for callback in callbacks:
        semaphore.release()
        _call_callback(callback, future)


def _run_reserved_callback(
    semaphore: threading.BoundedSemaphore,
    callback,
    future: RemoteFuture,
) -> None:
    semaphore.release()
    _call_callback(callback, future)


def _call_callback(callback, future: RemoteFuture) -> None:
    try:
        callback(future)
    except BaseException:
        import logging

        logging.getLogger("deadpool.remote").exception(
            "exception calling RemoteFuture callback"
        )


def _deadpool_version() -> str:
    from deadpool import __version__

    return __version__
