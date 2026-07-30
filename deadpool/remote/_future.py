"""Process-local Future representing one remote request."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import weakref
from enum import Enum
from typing import TYPE_CHECKING, Callable

from .errors import RemoteForkedProcessError

if TYPE_CHECKING:
    from .client import DeadpoolClient

logger = logging.getLogger("deadpool.remote")


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
        self._remote_callbacks: list[Callable[[RemoteFuture], object]] = []

    @property
    def pid(self) -> int | None:
        self._check_process()
        return self._pid

    def cancel(self) -> bool:
        """Request an authoritative cancellation decision from the broker.

        Locally pending work cancels immediately. Once submitted, this call may
        block up to the client's control timeout or raise when the distributed
        cancellation outcome cannot be determined.
        """
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

    def add_done_callback(self, fn: Callable[[RemoteFuture], object]) -> None:
        """Register ``fn``, invoking it inline when already terminal.

        Callbacks registered before completion are dispatched on the client's
        isolated callback executor. Matching the standard Future contract,
        callbacks registered after completion run synchronously before this
        method returns.
        """
        self._check_process()
        with self._state_lock:
            if not self.done():
                self._remote_callbacks.append(fn)
                return
        self._invoke_callback(fn)

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
        if client is None:
            for callback in callbacks:
                self._invoke_callback(callback)
            return
        client._schedule_callbacks(callbacks, self)

    def _invoke_callback(self, callback: Callable[[RemoteFuture], object]) -> None:
        """Invoke one callback through the shared exception boundary."""
        try:
            callback(self)
        except BaseException:
            logger.exception("exception calling RemoteFuture callback")

    def _check_process(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RemoteForkedProcessError(
                "RemoteFuture was inherited across fork and is not valid in the child",
                request_id=self.request_id,
            )
