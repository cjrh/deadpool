"""Server-side broker owning one local :class:`deadpool.Deadpool`."""

from __future__ import annotations

import hashlib
import itertools
import logging
import math
import os
import pickle
import queue
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from deadpool import Future as LocalFuture
from deadpool import ProcessError, TimeoutError

from ._protocol import (
    MAJOR,
    MINOR,
    Message,
    MessageReader,
    MessageType,
    _validate_wire_limits,
    _wire_limits,
    send_message,
)
from ._scheduler import FairScheduler
from ._transport import (
    BoundListener,
    accept_socket,
    bind_listener,
    peer_info,
    prepare_accepted_socket,
)
from ._worker import WorkerOutcome, execute_opaque
from .config import (
    Authenticator,
    Authorizer,
    Principal,
    RemoteLimits,
    TcpListener,
    UnixListener,
)
from .errors import RemoteProtocolError
from .serializer import PickleSerializer, Serializer

logger = logging.getLogger("deadpool.remote")


class ServerState(str, Enum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class RequestState(str, Enum):
    ACCEPTED_QUEUED = "ACCEPTED_QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    TASK_FAILED = "TASK_FAILED"
    EXECUTION_TIMED_OUT = "EXECUTION_TIMED_OUT"
    CANCELLED = "CANCELLED"
    QUEUE_TIMED_OUT = "QUEUE_TIMED_OUT"
    WORKER_LOST = "WORKER_LOST"
    RESULT_ENCODING_FAILED = "RESULT_ENCODING_FAILED"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"


_TERMINAL = {
    RequestState.SUCCEEDED,
    RequestState.TASK_FAILED,
    RequestState.EXECUTION_TIMED_OUT,
    RequestState.CANCELLED,
    RequestState.QUEUE_TIMED_OUT,
    RequestState.WORKER_LOST,
    RequestState.RESULT_ENCODING_FAILED,
    RequestState.RESULT_TOO_LARGE,
}


@dataclass(slots=True, eq=False)
class _Request:
    request_id: str
    digest: str
    principal: str
    session_id: str
    connection: "_ServerConnection"
    mode: str
    operation: str | None
    payload: bytes
    priority: int
    execution_timeout: float | None
    queue_deadline: float | None
    group_id: str | None
    max_result_bytes: int
    reserved_result_bytes: int
    state: RequestState = RequestState.ACCEPTED_QUEUED
    local_future: LocalFuture | None = None
    terminal_kind: MessageType | None = None
    terminal_payload: bytes = b""
    terminal_control: dict = field(default_factory=dict)
    reservation_released: bool = False


@dataclass(slots=True)
class _Session:
    session_id: str
    client_id: str
    principal: str
    connection: "_ServerConnection"
    max_result_bytes: int
    watermark: int = 0
    reserved_outcomes: int = 0
    reserved_outcome_bytes: int = 0
    requests: dict[str, _Request] = field(default_factory=dict)


class _PoolFuture(LocalFuture):
    def __init__(self, server: "DeadpoolServer", record: _Request) -> None:
        super().__init__()
        self._before_submit = lambda pid: server._begin_dispatch(record, pid)
        self._after_submit = lambda pid: server._commit_running(record, pid)
        self._submit_failed = lambda pid: server._abort_dispatch(record, pid)


@dataclass(slots=True)
class _WriterBarrier:
    reached: threading.Event = field(default_factory=threading.Event)


class _ServerConnection:
    def __init__(
        self, server: "DeadpoolServer", sock: socket.socket, transport: str
    ) -> None:
        self.server = server
        self.sock = sock
        self.transport = transport
        self.reader = MessageReader(server.limits)
        self.session: _Session | None = None
        self.closed = False
        self.clean_close = False
        self._close_lock = threading.Lock()
        self._sequence = itertools.count()
        self._outbound: queue.PriorityQueue[
            tuple[int, int, Message | _WriterBarrier | None]
        ] = queue.PriorityQueue(server.limits.outbound_queue_size)
        threading.Thread(
            target=self._writer_loop,
            name="deadpool.remote.writer",
            daemon=True,
        ).start()

    def send(self, kind: MessageType, control: dict, payload: bytes = b"") -> bool:
        if self.closed:
            return False
        try:
            # FIFO is intentional: state transitions and terminal outcomes may
            # never be overtaken by GOAWAY or their control acknowledgements.
            self._outbound.put_nowait(
                (0, next(self._sequence), Message(kind, control, payload))
            )
            return True
        except queue.Full:
            self.close()
            return False

    def _writer_loop(self) -> None:
        while True:
            _, _, item = self._outbound.get()
            try:
                if item is None:
                    return
                if isinstance(item, _WriterBarrier):
                    item.reached.set()
                    continue
                send_message(self.sock, item, self.server.limits)
            except (OSError, EOFError, TimeoutError, RemoteProtocolError):
                self.close()
                return
            finally:
                self._outbound.task_done()

    def flush(self, timeout: float) -> bool:
        if self.closed:
            return False
        barrier = _WriterBarrier()
        try:
            self._outbound.put_nowait((0, next(self._sequence), barrier))
        except queue.Full:
            self.close()
            return False
        return barrier.reached.wait(timeout)

    def close(self) -> None:
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
            try:
                self._outbound.put_nowait((0, next(self._sequence), None))
            except queue.Full:
                pass
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()


class DeadpoolServer:
    """Own a Deadpool and expose it over bounded Unix/TCP connections.

    The wire format is private and experimental until a separate stable wire
    reference is published. Pickle callable mode grants trusted clients code
    execution as the worker account.
    """

    def __init__(
        self,
        pool_factory: Callable[[], object],
        *,
        listeners: list[UnixListener | TcpListener],
        serializer: Serializer | None = None,
        task_registry: dict[str, Callable] | None = None,
        registry_fingerprint: str | None = None,
        application_fingerprint: str | None = None,
        authenticator: Authenticator | None = None,
        authorizer: Authorizer | None = None,
        limits: RemoteLimits | None = None,
        scheduler=None,
        disconnect_policy: str = "cancel_queued",
    ) -> None:
        if not listeners:
            raise ValueError("at least one listener is required")
        if disconnect_policy not in {"cancel_queued", "continue", "terminate"}:
            raise ValueError("invalid disconnect policy")
        self.pool_factory = pool_factory
        self.listeners = list(listeners)
        if serializer is None:
            logger.warning(
                "Deadpool remote callable mode uses pickle and grants trusted "
                "clients code execution as the worker account"
            )
        self.serializer = serializer or PickleSerializer()
        self.task_registry = dict(task_registry or {})
        self.registry_fingerprint = registry_fingerprint
        self.application_fingerprint = application_fingerprint
        self.authenticator = authenticator
        self.authorizer = authorizer
        self.limits = limits or RemoteLimits()
        self.disconnect_policy = disconnect_policy
        self.server_id = uuid.uuid4().hex
        self.epoch = uuid.uuid4().hex
        self.state = ServerState.CREATED
        self.ready = threading.Event()
        self._stopped = threading.Event()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._bound: list[BoundListener] = []
        self._connections: set[_ServerConnection] = set()
        self._raw_sockets: set[socket.socket] = set()
        self._sessions: dict[str, _Session] = {}
        self._client_sessions: dict[str, _Session] = {}
        self._requests: dict[str, _Request] = {}
        self._reserved_outcomes = 0
        self._reserved_outcome_bytes = 0
        self._scheduler: FairScheduler[_Request] = scheduler or FairScheduler()
        self._staged = 0
        self._pool = None
        self._serve_thread: threading.Thread | None = None
        self._shutdown_thread: threading.Thread | None = None
        self._shutdown_cancel_futures = False
        self._shutdown_deadline_at: float | None = None
        self._startup_error: BaseException | None = None
        self._statistics = {
            "remote_tasks_received": 0,
            "remote_tasks_accepted": 0,
            "remote_tasks_rejected": 0,
            "remote_tasks_running": 0,
            "remote_tasks_terminal": 0,
            "remote_connections": 0,
        }

    @property
    def bound_addresses(self) -> tuple[object, ...]:
        return tuple(item.address for item in self._bound)

    def start(self) -> "DeadpoolServer":
        with self._lock:
            if self._serve_thread is None:
                if self.state != ServerState.CREATED:
                    raise RuntimeError("a stopped remote server cannot be started")
                self.state = ServerState.STARTING
                self._serve_thread = threading.Thread(
                    target=self.serve_forever,
                    name="deadpool.remote.server",
                    daemon=False,
                )
                try:
                    self._serve_thread.start()
                except BaseException as error:
                    self._startup_error = error
                    self.state = ServerState.STOPPED
                    self.ready.set()
                    self._stopped.set()
                    raise
        # Pool construction and listener binding are not handshake operations.
        # Every caller waits for their shared readiness outcome.
        if not self.wait_ready():
            raise RuntimeError("remote server stopped before becoming ready")
        return self

    def wait_ready(self, timeout: float | None = None) -> bool:
        if not self.ready.wait(timeout):
            raise TimeoutError("remote server did not become ready")
        if self._startup_error is not None:
            raise self._startup_error
        return self._pool is not None

    def serve_forever(self) -> None:
        with self._condition:
            current_thread = threading.current_thread()
            background_start = self._serve_thread is not None
            if self._serve_thread is None:
                if self.state != ServerState.CREATED:
                    raise RuntimeError("a stopped remote server cannot be served")
                self._serve_thread = current_thread
                self.state = ServerState.STARTING
            elif self._serve_thread is not current_thread:
                raise RuntimeError("remote server is already being served")
        try:
            self._initialize()
        except BaseException as error:
            with self._lock:
                self._startup_error = error
                self.state = ServerState.STOPPED
            self._close_bound()
            self.ready.set()
            self._stopped.set()
            if not background_start:
                raise
            return
        self._stopped.wait()

    def _initialize(self) -> None:
        with self._condition:
            if self.state != ServerState.STARTING:
                if self.state == ServerState.STOPPING:
                    self.state = ServerState.STOPPED
                    self.ready.set()
                    self._stopped.set()
                    self._condition.notify_all()
                return
        pool = self.pool_factory()
        bound: list[BoundListener] = []
        try:
            for listener in self.listeners:
                try:
                    bound.append(bind_listener(listener))
                except BaseException:
                    if not listener.optional:
                        raise
            if not bound:
                raise OSError("no listeners could be bound")
        except BaseException:
            self._discard_startup_resources(pool, bound)
            raise
        with self._condition:
            if self.state != ServerState.STARTING:
                startup_cancelled = True
            else:
                startup_cancelled = False
                self._pool = pool
                self._bound = bound
                self.state = ServerState.RUNNING
                threading.Thread(
                    target=self._dispatch_loop,
                    name="deadpool.remote.broker",
                    daemon=True,
                ).start()
                for item in bound:
                    threading.Thread(
                        target=self._accept_loop,
                        args=(item,),
                        name="deadpool.remote.accept",
                        daemon=True,
                    ).start()
                self.ready.set()
        if startup_cancelled:
            self._discard_startup_resources(pool, bound)
            with self._condition:
                self.state = ServerState.STOPPED
                self.ready.set()
                self._stopped.set()
                self._condition.notify_all()

    def _discard_startup_resources(
        self, pool: object, bound: list[BoundListener]
    ) -> None:
        for item in bound:
            try:
                item.close()
            except BaseException:
                logger.exception("failed to close an unpublished remote listener")
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except BaseException:
            logger.exception("failed to shut down an unpublished remote pool")

    def _accept_loop(self, bound: BoundListener) -> None:
        while self.state in {ServerState.RUNNING, ServerState.DRAINING}:
            try:
                sock = accept_socket(bound)
            except OSError:
                if self.state in {ServerState.STOPPING, ServerState.STOPPED}:
                    return
                continue
            with self._lock:
                unauthenticated = len(self._raw_sockets) + sum(
                    connection.session is None for connection in self._connections
                )
                total = len(self._raw_sockets) + len(self._connections)
                if (
                    unauthenticated >= self.limits.max_unauthenticated_connections
                    or total >= self.limits.max_connections_global
                ):
                    sock.close()
                    continue
                self._raw_sockets.add(sock)
            threading.Thread(
                target=self._prepare_connection,
                args=(bound, sock),
                name="deadpool.remote.handshake",
                daemon=True,
            ).start()

    def _prepare_connection(self, bound: BoundListener, raw: socket.socket) -> None:
        connection = None
        try:
            sock = prepare_accepted_socket(
                bound,
                raw,
                handshake_timeout=self.limits.handshake_timeout,
            )
            transport = "unix" if isinstance(bound.config, UnixListener) else "tcp"
            connection = _ServerConnection(self, sock, transport)
            with self._lock:
                self._raw_sockets.discard(raw)
                if self.state not in {ServerState.RUNNING, ServerState.DRAINING}:
                    connection.close()
                    return
                self._connections.add(connection)
                self._statistics["remote_connections"] = len(self._connections)
            self._connection_loop(connection)
        except (OSError, TimeoutError):
            try:
                raw.close()
            except OSError:
                pass
        finally:
            with self._lock:
                self._raw_sockets.discard(raw)
            if connection is not None and not connection.closed:
                self._disconnect(connection)

    def _connection_loop(self, connection: _ServerConnection) -> None:
        connection.sock.settimeout(self.limits.handshake_timeout)
        try:
            hello = connection.reader.receive(
                connection.sock,
                deadline=time.monotonic() + self.limits.handshake_timeout,
            )
            if hello.kind != MessageType.HELLO or hello.payload:
                raise RemoteProtocolError("HELLO must be the first message")
            self._handshake(connection, hello.control)
            connection.sock.settimeout(None)
            while not connection.closed:
                message = connection.reader.receive(connection.sock)
                self._handle_message(connection, message)
        except (EOFError, OSError, TimeoutError):
            pass
        except Exception as error:
            protocol_error = (
                error
                if isinstance(error, RemoteProtocolError)
                else RemoteProtocolError(f"malformed peer message: {error}")
            )
            connection.send(
                MessageType.PROTOCOL_ERROR,
                {"message": str(protocol_error)[:1024]},
            )
            connection.flush(min(self.limits.control_timeout, 1.0))
        finally:
            self._disconnect(connection)

    def _handshake(self, connection: _ServerConnection, hello: dict) -> None:
        _validate_hello(hello)
        if hello["wire_limits"] != _wire_limits(self.limits):
            connection.send(
                MessageType.HANDSHAKE_REJECTED,
                {"reason": "wire_limits"},
            )
            raise RemoteProtocolError("wire limit compatibility rejected")
        if [MAJOR, MINOR] not in hello["versions"]:
            connection.send(MessageType.HANDSHAKE_REJECTED, {"reason": "protocol"})
            raise RemoteProtocolError("no compatible protocol version")
        if hello["serializer"] != self.serializer.name or str(
            hello["serializer_protocol"]
        ) != str(self.serializer.protocol_version):
            connection.send(MessageType.HANDSHAKE_REJECTED, {"reason": "serializer"})
            raise RemoteProtocolError("serializer compatibility rejected")
        if "callable" in hello["capabilities"] and hello["python"] != [
            sys.version_info.major,
            sys.version_info.minor,
        ]:
            connection.send(MessageType.HANDSHAKE_REJECTED, {"reason": "python"})
            raise RemoteProtocolError("callable mode requires matching Python")
        if (
            self.registry_fingerprint is not None
            and hello.get("registry_fingerprint") != self.registry_fingerprint
        ):
            connection.send(
                MessageType.HANDSHAKE_REJECTED,
                {"reason": "registry_fingerprint"},
            )
            raise RemoteProtocolError("task registry compatibility rejected")
        if (
            self.application_fingerprint is not None
            and hello.get("application_fingerprint") != self.application_fingerprint
        ):
            connection.send(
                MessageType.HANDSHAKE_REJECTED,
                {"reason": "application_fingerprint"},
            )
            raise RemoteProtocolError("application compatibility rejected")
        peer = peer_info(connection.sock, connection.transport)
        try:
            if self.authenticator is not None:
                principal = self.authenticator(peer, hello)
            elif connection.transport == "unix":
                if peer.uid is not None and peer.uid != os.getuid():
                    raise PermissionError("Unix peer UID is not authorized")
                principal = Principal(
                    f"uid:{peer.uid if peer.uid is not None else os.getuid()}"
                )
            elif peer.certificate:
                identity = hashlib.sha256(
                    repr(peer.certificate).encode("utf-8")
                ).hexdigest()
                principal = Principal(f"tls:{identity}")
            else:
                # Plaintext is restricted by configuration to loopback. Its
                # identity deliberately excludes the ephemeral source port so
                # opening more sessions cannot increase scheduler share.
                principal = Principal("tcp:loopback")
            if not isinstance(principal, Principal) or not principal.name:
                raise TypeError("authenticator must return a non-empty Principal")
        except Exception as error:
            connection.send(
                MessageType.HANDSHAKE_REJECTED,
                {"reason": "authentication"},
            )
            raise RemoteProtocolError("peer authentication rejected") from error
        offered_result_limit = hello.get("max_result_bytes")
        if (
            isinstance(offered_result_limit, bool)
            or not isinstance(offered_result_limit, int)
            or offered_result_limit <= 0
        ):
            raise RemoteProtocolError("client result limit must be a positive integer")
        effective_result_limit = min(
            offered_result_limit,
            self.limits.max_result_bytes,
            self.limits.max_retained_outcome_bytes_per_session,
            self.limits.max_retained_outcome_bytes_global,
        )
        client_id = hello["client_instance_id"]
        session_id = uuid.uuid4().hex
        session = _Session(
            session_id,
            client_id,
            principal.name,
            connection,
            effective_result_limit,
        )
        with self._condition:
            if client_id in self._client_sessions:
                connection.send(
                    MessageType.HANDSHAKE_REJECTED,
                    {"reason": "duplicate_client_instance"},
                )
                raise RemoteProtocolError("client instance already has a live session")
            principal_connections = sum(
                item.principal == principal.name for item in self._sessions.values()
            )
            if principal_connections >= self.limits.max_connections_per_principal:
                connection.send(
                    MessageType.HANDSHAKE_REJECTED,
                    {"reason": "connection_limit"},
                )
                raise RemoteProtocolError("principal connection limit exceeded")
            connection.session = session
            self._sessions[session_id] = session
            self._client_sessions[client_id] = session
        connection.send(
            MessageType.WELCOME,
            {
                "version": [MAJOR, MINOR],
                "features": ["callable", "registered", "chunking"],
                "wire": "experimental-deadpool-private-v1",
                "wire_limits": _wire_limits(self.limits),
                "server_id": self.server_id,
                "epoch": self.epoch,
                "session_id": session_id,
                "serializer": self.serializer.name,
                "serializer_protocol": self.serializer.protocol_version,
                "registry_fingerprint": self.registry_fingerprint,
                "application_fingerprint": self.application_fingerprint,
                "limits": {
                    "max_invocation_bytes": self.limits.max_invocation_bytes,
                    "max_result_bytes": effective_result_limit,
                    "max_pending_per_session": self.limits.max_pending_per_session,
                    "max_pending_per_principal": self.limits.max_pending_per_principal,
                    "max_pending_global": self.limits.max_pending_global,
                },
                "resumption": False,
            },
        )

    def _handle_message(self, connection: _ServerConnection, message: Message) -> None:
        if message.kind == MessageType.PING:
            connection.send(MessageType.PONG, {"nonce": message.control.get("nonce")})
        elif message.kind == MessageType.SUBMIT:
            self._submit(connection, message)
        elif message.kind == MessageType.CANCEL_REQUEST:
            self._cancel(connection, message.control)
        elif message.kind == MessageType.CANCEL_GROUP:
            self._cancel_group(connection, message.control)
        elif message.kind == MessageType.STATUS_REQUEST:
            self._status(connection, message.control)
        elif message.kind == MessageType.STATS_REQUEST:
            if self._authorized(connection, "statistics", message.control):
                connection.send(
                    MessageType.STATS_RESPONSE,
                    {
                        "statistics": self.get_statistics(),
                        "token": message.control.get("token"),
                    },
                )
            else:
                connection.send(
                    MessageType.STATS_RESPONSE,
                    {"error": "unauthorized", "token": message.control.get("token")},
                )
        elif message.kind == MessageType.RESULT_ACK:
            with self._condition:
                request = self._owned_request(
                    connection, message.control.get("request_id")
                )
                if request is not None and request.state in _TERMINAL:
                    self._remove_request_locked(request)
                    self._condition.notify_all()
        elif message.kind == MessageType.CLOSE_SESSION:
            session = connection.session
            with self._condition:
                if session is None or any(
                    record.state not in _TERMINAL
                    for record in session.requests.values()
                ):
                    raise RemoteProtocolError(
                        "a session may close cleanly only after all requests are terminal"
                    )
                connection.clean_close = True
            connection.send(
                MessageType.CLOSE_SESSION_RESPONSE,
                {"token": message.control.get("token")},
            )
        else:
            raise RemoteProtocolError(
                f"message {message.kind.name} is invalid in this state"
            )

    def _submit(self, connection: _ServerConnection, message: Message) -> None:
        session = connection.session
        if session is None:
            raise RemoteProtocolError("submission before handshake")
        control = message.control
        request_id = control.get("request_id")
        self._statistics["remote_tasks_received"] += 1
        if not isinstance(request_id, str) or not request_id.startswith(
            session.client_id + ":"
        ):
            raise RemoteProtocolError(
                "request ID does not belong to the client instance"
            )
        try:
            sequence = int(request_id.rsplit(":", 1)[1])
        except (ValueError, IndexError) as error:
            raise RemoteProtocolError("invalid request sequence") from error
        digest = hashlib.sha256(message.payload).hexdigest()
        if control.get("digest") != digest:
            raise RemoteProtocolError("invocation digest mismatch")
        with self._condition:
            existing = session.requests.get(request_id)
            if existing is not None:
                if existing.digest != digest:
                    raise RemoteProtocolError(
                        "request ID reused with different payload"
                    )
                self._send_record(existing)
                return
            if sequence <= session.watermark:
                self._reject(connection, request_id, "stale_request")
                return
            session.watermark = sequence
            if self.state != ServerState.RUNNING:
                self._reject(connection, request_id, "server_draining")
                return
            # Active and unacknowledged-terminal records both consume finite
            # bookkeeping. Reserve worst-case result retention before ACCEPTED
            # so an accepted execution can always commit an explicit outcome.
            principal_records = sum(
                record.principal == session.principal
                for record in self._requests.values()
            )
            if (
                len(session.requests) >= self.limits.max_pending_per_session
                or principal_records >= self.limits.max_pending_per_principal
                or len(self._requests) >= self.limits.max_pending_global
                or session.reserved_outcomes
                >= self.limits.max_retained_outcomes_per_session
                or self._reserved_outcomes >= self.limits.max_retained_outcomes_global
                or session.reserved_outcome_bytes + session.max_result_bytes
                > self.limits.max_retained_outcome_bytes_per_session
                or self._reserved_outcome_bytes + session.max_result_bytes
                > self.limits.max_retained_outcome_bytes_global
            ):
                self._reject(connection, request_id, "queue_full")
                return
            if len(message.payload) > self.limits.max_invocation_bytes:
                self._reject(connection, request_id, "invocation_too_large")
                return
            mode = control.get("mode")
            operation = control.get("operation")
            if mode not in {"callable", "registered"}:
                self._reject(connection, request_id, "invalid_mode")
                return
            if mode == "registered" and operation not in self.task_registry:
                self._reject(connection, request_id, "unknown_operation")
                return
            action = (
                "submit_callable" if mode == "callable" else f"submit_task:{operation}"
            )
            if not self._authorized(connection, action, control):
                self._reject(connection, request_id, "unauthorized")
                return
            priority = _finite_nonnegative(control.get("priority", 0), "priority")
            execution_timeout = _optional_finite_nonnegative(
                control.get("execution_timeout"), "execution_timeout"
            )
            queue_timeout = _optional_finite_nonnegative(
                control.get("queue_timeout"), "queue_timeout"
            )
            record = _Request(
                request_id,
                digest,
                session.principal,
                session.session_id,
                connection,
                mode,
                operation,
                message.payload,
                int(priority),
                execution_timeout,
                time.monotonic() + queue_timeout if queue_timeout is not None else None,
                control.get("group_id"),
                session.max_result_bytes,
                session.max_result_bytes,
            )
            session.requests[request_id] = record
            self._requests[request_id] = record
            session.reserved_outcomes += 1
            session.reserved_outcome_bytes += record.reserved_result_bytes
            self._reserved_outcomes += 1
            self._reserved_outcome_bytes += record.reserved_result_bytes
            self._scheduler.put(
                record, priority=record.priority, principal=record.principal
            )
            self._statistics["remote_tasks_accepted"] += 1
            connection.send(
                MessageType.ACCEPTED, {"request_id": request_id, "state": record.state}
            )
            self._condition.notify_all()

    def _reject(
        self, connection: _ServerConnection, request_id: str, reason: str
    ) -> None:
        self._statistics["remote_tasks_rejected"] += 1
        connection.send(
            MessageType.REJECTED, {"request_id": request_id, "reason": reason}
        )

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                self._expire_queued_locked()
                while self.state in {ServerState.RUNNING, ServerState.DRAINING} and (
                    not self._scheduler or self._staged >= self.limits.max_staged_tasks
                ):
                    self._condition.wait(0.05)
                    self._expire_queued_locked()
                if self.state in {ServerState.STOPPING, ServerState.STOPPED}:
                    return
                try:
                    record = self._scheduler.pop()
                except IndexError:
                    continue
                if record.state != RequestState.ACCEPTED_QUEUED:
                    continue
                local = _PoolFuture(self, record)
                record.local_future = local
                self._staged += 1
            try:
                self._pool._submit_future(
                    local,
                    execute_opaque,
                    (
                        self.serializer,
                        record.mode,
                        record.payload,
                        record.operation,
                        (
                            {record.operation: self.task_registry[record.operation]}
                            if record.mode == "registered"
                            else None
                        ),
                        record.max_result_bytes,
                    ),
                    {},
                    record.execution_timeout,
                    record.priority,
                )
            except BaseException as error:
                self._complete_local(record, error)
            else:
                # The local queue owns the bytes through its job tuple now; the
                # broker record no longer retains a duplicate invocation.
                record.payload = b""
                local.add_done_callback(
                    lambda future, record=record: self._pool_done(record, future)
                )

    def _expire_queued_locked(self) -> None:
        now = time.monotonic()
        for record in list(self._requests.values()):
            if (
                record.state == RequestState.ACCEPTED_QUEUED
                and record.queue_deadline is not None
                and now >= record.queue_deadline
            ):
                self._scheduler.remove(record)
                self._terminal_locked(
                    record,
                    RequestState.QUEUE_TIMED_OUT,
                    MessageType.QUEUE_TIMED_OUT,
                    {"reason": "queue_timeout"},
                )
                if record.local_future is not None:
                    record.local_future.cancel()

    def _begin_dispatch(self, record: _Request, pid: int) -> bool:
        # Hold the arbitration lock across the small pipe send. Cancellation,
        # queue timeout, and dispatch can therefore have exactly one winner.
        self._condition.acquire()
        if record.state != RequestState.ACCEPTED_QUEUED:
            self._condition.release()
            return False
        return True

    def _commit_running(self, record: _Request, pid: int) -> None:
        try:
            record.state = RequestState.RUNNING
            self._statistics["remote_tasks_running"] += 1
            record.connection.send(
                MessageType.RUNNING,
                {
                    "request_id": record.request_id,
                    "pid": pid,
                    "worker_id": f"worker:{pid}",
                },
            )
        finally:
            self._condition.release()

    def _abort_dispatch(self, record: _Request, pid: int) -> None:
        # The worker pipe did not accept bytes, so the request remains queued
        # and Deadpool may retry it with another worker under the same ID.
        self._condition.release()

    def _pool_done(self, record: _Request, future: LocalFuture) -> None:
        try:
            outcome = future.result()
        except BaseException as error:
            self._complete_local(record, error)
        else:
            self._complete_local(record, outcome)

    def _complete_local(self, record: _Request, outcome: object) -> None:
        with self._condition:
            if record.state in _TERMINAL:
                if self._staged:
                    self._staged -= 1
                if record.connection.closed:
                    session = self._sessions.get(record.session_id)
                    if session is not None:
                        self._purge_session_locked(session)
                self._condition.notify_all()
                return
            if isinstance(outcome, WorkerOutcome):
                mapping = {
                    "result": (RequestState.SUCCEEDED, MessageType.RESULT),
                    "task_error": (RequestState.TASK_FAILED, MessageType.TASK_ERROR),
                    "result_encoding_failed": (
                        RequestState.RESULT_ENCODING_FAILED,
                        MessageType.RESULT_ENCODING_FAILED,
                    ),
                    "result_too_large": (
                        RequestState.RESULT_TOO_LARGE,
                        MessageType.RESULT_TOO_LARGE,
                    ),
                }
                state, kind = mapping[outcome.kind]
                self._terminal_locked(
                    record, state, kind, outcome.descriptor or {}, outcome.payload
                )
            elif isinstance(outcome, TimeoutError):
                self._terminal_locked(
                    record,
                    RequestState.EXECUTION_TIMED_OUT,
                    MessageType.TIMED_OUT,
                    {"message": str(outcome)},
                )
            elif isinstance(outcome, (ProcessError, pickle.PicklingError)):
                self._terminal_locked(
                    record,
                    RequestState.WORKER_LOST,
                    MessageType.WORKER_LOST,
                    {"message": str(outcome)},
                )
            elif isinstance(outcome, BaseException):
                self._terminal_locked(
                    record,
                    RequestState.WORKER_LOST,
                    MessageType.WORKER_LOST,
                    {"message": repr(outcome)},
                )
            else:
                self._terminal_locked(
                    record,
                    RequestState.WORKER_LOST,
                    MessageType.WORKER_LOST,
                    {"message": "invalid worker outcome"},
                )
            if self._staged:
                self._staged -= 1
            if record.connection.closed:
                session = self._sessions.get(record.session_id)
                if session is not None:
                    self._purge_session_locked(session)
            self._condition.notify_all()

    def _terminal_locked(
        self,
        record: _Request,
        state: RequestState,
        kind: MessageType,
        control: dict,
        payload: bytes = b"",
    ) -> None:
        if record.state in _TERMINAL:
            return
        record.state = state
        record.payload = b""
        record.terminal_kind = kind
        record.terminal_control = dict(control)
        record.terminal_payload = payload
        self._statistics["remote_tasks_terminal"] += 1
        record.connection.send(
            kind, {"request_id": record.request_id, **control}, payload
        )

    def _cancel(self, connection: _ServerConnection, control: dict) -> None:
        request_id = control.get("request_id")
        hard = bool(control.get("hard", False))
        token = control.get("token")
        with self._condition:
            record = self._owned_request(connection, request_id)
            if record is None:
                response = "unknown"
            elif record.state == RequestState.ACCEPTED_QUEUED:
                self._scheduler.remove(record)
                self._terminal_locked(
                    record,
                    RequestState.CANCELLED,
                    MessageType.CANCELLED,
                    {"reason": "cancelled"},
                )
                if record.local_future is not None:
                    record.local_future.cancel()
                response = "cancelled"
            elif (
                record.state == RequestState.RUNNING
                and hard
                and self._authorized(connection, "hard_cancel", control)
            ):
                self._terminal_locked(
                    record,
                    RequestState.CANCELLED,
                    MessageType.CANCELLED,
                    {"reason": "terminated"},
                )
                if record.local_future is not None:
                    record.local_future.cancel_and_kill_if_running()
                response = "cancelled"
            elif record.state == RequestState.RUNNING:
                response = "running"
            else:
                response = "terminal"
            connection.send(
                MessageType.CANCEL_RESPONSE,
                {"request_id": request_id, "response": response, "token": token},
            )
            self._condition.notify_all()

    def _cancel_group(self, connection: _ServerConnection, control: dict) -> None:
        group_id = control.get("group_id")
        hard = bool(control.get("hard", False))
        counts = {"cancelled": 0, "running": 0, "terminal": 0, "unknown": 0}
        if not isinstance(group_id, str):
            counts["unknown"] = 1
        else:
            with self._condition:
                session = connection.session
                records = (
                    [
                        record
                        for record in session.requests.values()
                        if record.group_id == group_id
                    ]
                    if session is not None
                    else []
                )
                if not records:
                    counts["unknown"] = 1
                for record in records:
                    if record.state == RequestState.ACCEPTED_QUEUED:
                        self._scheduler.remove(record)
                        self._terminal_locked(
                            record,
                            RequestState.CANCELLED,
                            MessageType.CANCELLED,
                            {"reason": "group_cancelled"},
                        )
                        if record.local_future is not None:
                            record.local_future.cancel()
                        counts["cancelled"] += 1
                    elif record.state == RequestState.RUNNING:
                        if hard and self._authorized(
                            connection, "hard_cancel", control
                        ):
                            self._terminal_locked(
                                record,
                                RequestState.CANCELLED,
                                MessageType.CANCELLED,
                                {"reason": "group_terminated"},
                            )
                            if record.local_future is not None:
                                record.local_future.cancel_and_kill_if_running()
                            counts["cancelled"] += 1
                        else:
                            counts["running"] += 1
                    else:
                        counts["terminal"] += 1
                self._condition.notify_all()
        connection.send(
            MessageType.CANCEL_GROUP_RESPONSE,
            {**counts, "token": control.get("token")},
        )

    def _status(self, connection: _ServerConnection, control: dict) -> None:
        record = self._owned_request(connection, control.get("request_id"))
        connection.send(
            MessageType.STATUS_RESPONSE,
            {
                "request_id": control.get("request_id"),
                "state": record.state if record else "UNKNOWN",
                "token": control.get("token"),
            },
        )

    def _owned_request(
        self, connection: _ServerConnection, request_id: object
    ) -> _Request | None:
        session = connection.session
        if session is None or not isinstance(request_id, str):
            return None
        return session.requests.get(request_id)

    def _send_record(self, record: _Request) -> None:
        if record.state == RequestState.ACCEPTED_QUEUED:
            record.connection.send(
                MessageType.ACCEPTED,
                {"request_id": record.request_id, "state": record.state},
            )
        elif record.state == RequestState.RUNNING:
            record.connection.send(
                MessageType.RUNNING,
                {
                    "request_id": record.request_id,
                    "pid": record.local_future.pid if record.local_future else None,
                    "worker_id": None,
                },
            )
        elif record.terminal_kind is not None:
            record.connection.send(
                record.terminal_kind,
                {"request_id": record.request_id, **record.terminal_control},
                record.terminal_payload,
            )

    def _authorized(
        self, connection: _ServerConnection, operation: str, metadata: dict
    ) -> bool:
        session = connection.session
        if session is None:
            return False
        if self.authorizer is None:
            # Filesystem permissions plus same-UID peer authentication form the
            # explicit built-in local policy. TCP is default-deny even with TLS;
            # operators must map certificate principals to allowed operations.
            return (
                connection.transport == "unix"
                and session.principal == f"uid:{os.getuid()}"
            )
        return bool(self.authorizer(Principal(session.principal), operation, metadata))

    def _disconnect(self, connection: _ServerConnection) -> None:
        connection.close()
        with self._condition:
            self._connections.discard(connection)
            self._statistics["remote_connections"] = len(self._connections)
            session = connection.session
            if (
                session is not None
                and not connection.clean_close
                and self.disconnect_policy in {"cancel_queued", "terminate"}
            ):
                # Hard cancellation can complete a worker concurrently and
                # purge terminal records, so disconnect arbitration iterates a
                # stable snapshot of the session's requests.
                for record in list(session.requests.values()):
                    if record.state == RequestState.ACCEPTED_QUEUED:
                        self._scheduler.remove(record)
                        self._terminal_locked(
                            record,
                            RequestState.CANCELLED,
                            MessageType.CANCELLED,
                            {"reason": "disconnected"},
                        )
                        if record.local_future is not None:
                            record.local_future.cancel()
                    elif (
                        record.state == RequestState.RUNNING
                        and self.disconnect_policy == "terminate"
                        and record.local_future is not None
                    ):
                        self._terminal_locked(
                            record,
                            RequestState.CANCELLED,
                            MessageType.CANCELLED,
                            {"reason": "disconnected_terminate"},
                        )
                        record.local_future.cancel_and_kill_if_running()
                self._purge_session_locked(session)
            elif session is not None:
                # Resumption is not negotiated by this wire version. Retaining
                # terminal outcomes after disconnect would make them
                # unreachable and eventually exhaust the bounded result
                # reservation. Active ``continue`` requests stay attached to
                # the session and purge themselves when they become terminal.
                self._purge_session_locked(session)
            self._condition.notify_all()

    def _release_reservation_locked(self, session: _Session, record: _Request) -> None:
        if record.reservation_released:
            return
        record.reservation_released = True
        session.reserved_outcomes -= 1
        session.reserved_outcome_bytes -= record.reserved_result_bytes
        self._reserved_outcomes -= 1
        self._reserved_outcome_bytes -= record.reserved_result_bytes

    def _remove_request_locked(self, record: _Request) -> None:
        session = self._sessions.get(record.session_id)
        if session is None:
            return
        self._release_reservation_locked(session, record)
        record.payload = b""
        record.terminal_payload = b""
        session.requests.pop(record.request_id, None)
        self._requests.pop(record.request_id, None)

    def _purge_session_locked(self, session: _Session) -> None:
        for record in list(session.requests.values()):
            if record.state in _TERMINAL:
                self._remove_request_locked(record)
        if not session.requests:
            self._sessions.pop(session.session_id, None)
            if self._client_sessions.get(session.client_id) is session:
                self._client_sessions.pop(session.client_id, None)

    def get_statistics(self) -> dict:
        with self._lock:
            stats = dict(self._statistics)
            stats["remote_sessions"] = len(self._sessions)
            stats["remote_queued"] = len(self._scheduler)
            stats["remote_staged"] = self._staged
            stats["remote_retained_outcomes"] = self._reserved_outcomes
            stats["remote_retained_outcome_bytes"] = self._reserved_outcome_bytes
        if self._pool is not None:
            stats.update(self._pool.get_statistics())
        return stats

    def shutdown(
        self,
        wait: bool = True,
        *,
        cancel_futures: bool = False,
        deadline: float | None = None,
    ) -> None:
        validated_deadline = _validate_shutdown_deadline(deadline)
        absolute_deadline = (
            time.monotonic() + validated_deadline
            if validated_deadline is not None
            else None
        )
        startup_stopping = False
        with self._condition:
            if self.state == ServerState.STOPPED:
                return
            if self.state == ServerState.CREATED:
                self.state = ServerState.STOPPED
                self.ready.set()
                self._stopped.set()
                return
            if self.state == ServerState.STARTING:
                self.state = ServerState.STOPPING
                startup_stopping = True
            elif self.state == ServerState.STOPPING and not self.ready.is_set():
                startup_stopping = True
            else:
                if self.state == ServerState.RUNNING:
                    self.state = ServerState.DRAINING
                self._shutdown_cancel_futures |= cancel_futures
                if absolute_deadline is not None and (
                    self._shutdown_deadline_at is None
                    or absolute_deadline < self._shutdown_deadline_at
                ):
                    self._shutdown_deadline_at = absolute_deadline
                if self._shutdown_thread is None:
                    self._shutdown_thread = threading.Thread(
                        target=self._finish_shutdown,
                        name="deadpool.remote.shutdown",
                        daemon=False,
                    )
                    self._shutdown_thread.start()
            self._condition.notify_all()
        if startup_stopping:
            if wait and self._serve_thread is not threading.current_thread():
                self._stopped.wait()
        elif wait and self._shutdown_thread is not threading.current_thread():
            self._shutdown_thread.join()

    def _finish_shutdown(self) -> None:
        with self._condition:
            while any(
                record.state not in _TERMINAL for record in self._requests.values()
            ):
                if self._shutdown_cancel_futures:
                    for record in list(self._requests.values()):
                        if record.state == RequestState.ACCEPTED_QUEUED:
                            self._scheduler.remove(record)
                            self._terminal_locked(
                                record,
                                RequestState.CANCELLED,
                                MessageType.CANCELLED,
                                {"reason": "server_shutdown"},
                            )
                            if record.local_future is not None:
                                record.local_future.cancel()
                deadline = self._shutdown_deadline_at
                if deadline is not None and time.monotonic() >= deadline:
                    for record in list(self._requests.values()):
                        if record.state == RequestState.ACCEPTED_QUEUED:
                            self._scheduler.remove(record)
                            self._terminal_locked(
                                record,
                                RequestState.CANCELLED,
                                MessageType.CANCELLED,
                                {"reason": "shutdown_deadline"},
                            )
                            if record.local_future is not None:
                                record.local_future.cancel()
                        elif (
                            record.state == RequestState.RUNNING
                            and record.local_future is not None
                        ):
                            self._terminal_locked(
                                record,
                                RequestState.CANCELLED,
                                MessageType.CANCELLED,
                                {"reason": "shutdown_deadline"},
                            )
                            record.local_future.cancel_and_kill_if_running()
                    break
                self._condition.wait(0.05)
            self.state = ServerState.STOPPING
        self._close_bound()
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=False)
        flush_timeout = min(self.limits.control_timeout, 1.0)
        for connection in list(self._connections):
            # First drain every previously queued terminal outcome, then send
            # GOAWAY and drain it. The explicit writer barriers cannot suffer
            # the Event clear/enqueue lost-wakeup race.
            connection.flush(flush_timeout)
            connection.send(MessageType.GOAWAY, {"reason": "server_stopped"})
            connection.flush(flush_timeout)
            connection.close()
        with self._condition:
            self.state = ServerState.STOPPED
            self._stopped.set()
            self._condition.notify_all()

    def _close_bound(self) -> None:
        for item in self._bound:
            item.close()
        self._bound.clear()
        with self._lock:
            raw_sockets, self._raw_sockets = self._raw_sockets, set()
        for sock in raw_sockets:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "DeadpoolServer":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.shutdown()
        return False


def _validate_shutdown_deadline(value: object) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("deadline must be finite and non-negative")
    return float(value)


def _validate_hello(hello: dict) -> None:
    required = {
        "versions",
        "client_instance_id",
        "serializer",
        "serializer_protocol",
        "python",
        "capabilities",
        "max_result_bytes",
        "wire",
        "wire_limits",
    }
    if not required <= set(hello):
        raise RemoteProtocolError("HELLO is missing required fields")
    versions = hello["versions"]
    python_version = hello["python"]
    capabilities = hello["capabilities"]
    if not (
        isinstance(versions, list)
        and all(
            isinstance(version, list)
            and len(version) == 2
            and all(
                isinstance(item, int) and not isinstance(item, bool) for item in version
            )
            for version in versions
        )
    ):
        raise RemoteProtocolError("HELLO versions must be integer pairs")
    if not (
        isinstance(python_version, list)
        and len(python_version) == 2
        and all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in python_version
        )
    ):
        raise RemoteProtocolError("HELLO python version must be an integer pair")
    if not (
        isinstance(capabilities, list)
        and all(isinstance(item, str) for item in capabilities)
    ):
        raise RemoteProtocolError("HELLO capabilities must be strings")
    for name in ("client_instance_id", "serializer", "wire"):
        if not isinstance(hello[name], str) or not hello[name]:
            raise RemoteProtocolError(f"HELLO {name} must be a non-empty string")
    if len(hello["client_instance_id"]) > 128:
        raise RemoteProtocolError("HELLO client instance ID is too long")
    serializer_protocol = hello["serializer_protocol"]
    if isinstance(serializer_protocol, bool) or not isinstance(
        serializer_protocol, (str, int)
    ):
        raise RemoteProtocolError("HELLO serializer protocol is invalid")
    _validate_wire_limits(hello["wire_limits"])
    result_limit = hello["max_result_bytes"]
    if (
        isinstance(result_limit, bool)
        or not isinstance(result_limit, int)
        or result_limit <= 0
    ):
        raise RemoteProtocolError("client result limit must be a positive integer")


def _finite_nonnegative(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise RemoteProtocolError(f"{name} must be finite and non-negative")
    return float(value)


def _optional_finite_nonnegative(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _finite_nonnegative(value, name)
