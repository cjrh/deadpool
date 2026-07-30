"""Public failure taxonomy for remote execution."""

from __future__ import annotations

from enum import Enum


class AcceptanceCertainty(str, Enum):
    NOT_ACCEPTED = "not_accepted"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"


class ExecutionCertainty(str, Enum):
    NOT_STARTED = "not_started"
    MAY_HAVE_RUN = "may_have_run"
    COMPLETED = "completed"


class RemoteExecutorError(Exception):
    """Base class carrying the facts an application needs before retrying."""

    default_acceptance = AcceptanceCertainty.UNKNOWN
    default_execution = ExecutionCertainty.MAY_HAVE_RUN

    def __init__(
        self,
        message: str = "",
        *,
        request_id: str | None = None,
        acceptance_certainty: AcceptanceCertainty | str | None = None,
        execution_certainty: ExecutionCertainty | str | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.acceptance_certainty = AcceptanceCertainty(
            acceptance_certainty or self.default_acceptance
        )
        self.execution_certainty = ExecutionCertainty(
            execution_certainty or self.default_execution
        )


class RemoteExecutorUnavailable(RemoteExecutorError):
    default_acceptance = AcceptanceCertainty.NOT_ACCEPTED
    default_execution = ExecutionCertainty.NOT_STARTED


class RemoteAuthenticationError(RemoteExecutorError):
    default_acceptance = AcceptanceCertainty.NOT_ACCEPTED
    default_execution = ExecutionCertainty.NOT_STARTED


class RemoteCompatibilityError(RemoteAuthenticationError): ...


class RemoteQueueFull(RemoteExecutorError):
    default_acceptance = AcceptanceCertainty.NOT_ACCEPTED
    default_execution = ExecutionCertainty.NOT_STARTED


class RemoteSubmissionTimeout(RemoteQueueFull): ...


class SubmissionOutcomeUnknown(RemoteExecutorError): ...


class RemoteQueueTimeout(RemoteExecutorError):
    default_acceptance = AcceptanceCertainty.ACCEPTED
    default_execution = ExecutionCertainty.NOT_STARTED


class RemoteTaskError(RemoteExecutorError):
    default_acceptance = AcceptanceCertainty.ACCEPTED
    default_execution = ExecutionCertainty.COMPLETED

    def __init__(
        self,
        message: str = "Remote task failed",
        *,
        remote_traceback: str = "",
        **kwargs,
    ) -> None:
        super().__init__(message, **kwargs)
        self.remote_traceback = remote_traceback


class RemoteResultEncodingError(RemoteTaskError): ...


class RemoteResultTooLarge(RemoteTaskError): ...


class RemoteCancellationOutcomeUnknown(RemoteExecutorError): ...


class RemoteForkedProcessError(RemoteExecutorError): ...


class RemoteProcessError(RemoteExecutorError):
    default_acceptance = AcceptanceCertainty.ACCEPTED
    default_execution = ExecutionCertainty.MAY_HAVE_RUN


class RemoteCancelledError(RemoteExecutorError):
    default_acceptance = AcceptanceCertainty.ACCEPTED
    default_execution = ExecutionCertainty.NOT_STARTED


class RemoteConnectionLost(RemoteExecutorError): ...


class RemoteServerRestarted(RemoteConnectionLost): ...


class RemoteProtocolError(RemoteExecutorError): ...


class RemoteResultLost(RemoteExecutorError):
    default_acceptance = AcceptanceCertainty.ACCEPTED
    default_execution = ExecutionCertainty.COMPLETED
