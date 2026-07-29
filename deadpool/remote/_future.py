"""Process-local Future representing one remote request."""

from __future__ import annotations

import concurrent.futures
import os
import threading
import weakref
from enum import Enum
from typing import TYPE_CHECKING

from .errors import RemoteForkedProcessError

if TYPE_CHECKING:
    from .client import DeadpoolClient


class SubmissionState(str, Enum):
    LOCAL_PENDING = "LOCAL_PENDING"
    SENT_UNACKNOWLEDGED = "SENT_UNACKNOWLEDGED"
    ACCEPTED_QUEUED = "ACCEPTED_QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    TASK_FAILED = "TASK_FAILED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class RemoteFuture(concurrent.futures.Future):
    """A Future with request identity and authoritative remote state."""

    def __init__(self, request_id: str, client: DeadpoolClient) -> None:
        super().__init__()
        self.request_id = request_id
        self.submission_state = SubmissionState.LOCAL_PENDING
        self.worker_id: str | None = None
        self._pid: int | None = None
        self._owner_pid = os.getpid()
        self._client = weakref.ref(client)
        self._state_lock = threading.RLock()
        self._remote_callbacks: list[tuple[object, bool]] = []

    @property
    def pid(self) -> int | None:
        self._check_process()
        return self._pid

    def cancel(self) -> bool:
        self._check_process()
        with self._state_lock:
            if self.done():
                return False
            if self.submission_state == SubmissionState.LOCAL_PENDING:
                self.submission_state = SubmissionState.CANCELLED
                cancelled = self._cancel_and_notify_waiters()
                self._dispatch_callbacks()
                return cancelled
        client = self._client()
        if client is None:
            return False
        return client._cancel(self, hard=False)

    def cancel_and_kill_if_running(self) -> bool:
        self._check_process()
        client = self._client()
        if client is None or self.done():
            return False
        return client._cancel(self, hard=True)

    def add_done_callback(self, fn) -> None:
        self._check_process()
        client = self._client()
        reserved = client is not None
        if client is not None:
            client._reserve_callback()
        with self._state_lock:
            if not self.done():
                self._remote_callbacks.append((fn, reserved))
                return
        self._schedule_callback(fn, reserved)

    def result(self, timeout: float | None = None):
        self._check_process()
        return super().result(timeout)

    def exception(self, timeout: float | None = None):
        self._check_process()
        return super().exception(timeout)

    def running(self) -> bool:
        self._check_process()
        with self._state_lock:
            return (
                self.submission_state == SubmissionState.RUNNING and not super().done()
            )

    def done(self) -> bool:
        self._check_process()
        return super().done()

    def cancelled(self) -> bool:
        self._check_process()
        return super().cancelled()

    def _set_sent(self) -> bool:
        with self._state_lock:
            if self.done():
                return False
            self.submission_state = SubmissionState.SENT_UNACKNOWLEDGED
            return True

    def _set_accepted(self) -> None:
        with self._state_lock:
            if not self.done():
                self.submission_state = SubmissionState.ACCEPTED_QUEUED

    def _set_running(self, *, pid: int | None, worker_id: str | None) -> None:
        # Keep the base Future pending. Remote RUNNING is authoritative broker
        # metadata, while a later authorized hard cancellation still needs to
        # make the local Future genuinely cancelled and terminal.
        with self._state_lock:
            if super().done():
                return
            self._pid = pid
            self.worker_id = worker_id
            self.submission_state = SubmissionState.RUNNING

    def _set_result(self, value: object) -> None:
        with self._state_lock:
            if self.done():
                return
            self.submission_state = SubmissionState.SUCCEEDED
            super().set_result(value)
        self._dispatch_callbacks()

    def _set_exception(self, error: BaseException, *, task: bool = False) -> None:
        with self._state_lock:
            if self.done():
                return
            self.submission_state = (
                SubmissionState.TASK_FAILED if task else SubmissionState.FAILED
            )
            super().set_exception(error)
        self._dispatch_callbacks()

    def _set_cancelled(self) -> None:
        with self._state_lock:
            if self.done():
                return
            self.submission_state = SubmissionState.CANCELLED
            self._cancel_and_notify_waiters()
        self._dispatch_callbacks()

    def _cancel_and_notify_waiters(self) -> bool:
        # stdlib waiters require the executor-side cancellation notification.
        cancelled = super().cancel()
        if cancelled:
            super().set_running_or_notify_cancel()
        return cancelled

    def _dispatch_callbacks(self) -> None:
        with self._state_lock:
            callbacks, self._remote_callbacks = self._remote_callbacks, []
        if not callbacks:
            return
        client = self._client()
        if client is not None and all(reserved for _, reserved in callbacks):
            client._schedule_callbacks(
                [callback for callback, _ in callbacks],
                self,
            )
            return
        for callback, reserved in callbacks:
            self._schedule_callback(callback, reserved)

    def _schedule_callback(self, callback, reserved: bool) -> None:
        client = self._client()
        if client is None or not reserved:
            callback(self)
        else:
            client._schedule_callback(callback, self)

    def _check_process(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RemoteForkedProcessError(
                "RemoteFuture was inherited across fork and is not valid in the child",
                request_id=self.request_id,
            )
