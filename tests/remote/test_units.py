from concurrent.futures import as_completed, wait

import pytest

from deadpool.remote import (
    AcceptanceCertainty,
    ExecutionCertainty,
    PickleSerializer,
    RemoteFuture,
    RemoteLimits,
    RemoteQueueFull,
    SerializationLimitError,
    SubmissionState,
)
from deadpool.remote._scheduler import FairScheduler
from deadpool.remote.serializer import _LimitedWriter


def test_pickle_serializer_enforces_limit_and_round_trips():
    serializer = PickleSerializer()
    payload = serializer.dumps({"answer": 42}, limit=1000)
    assert serializer.loads(payload) == {"answer": 42}
    with pytest.raises(SerializationLimitError):
        serializer.dumps(b"x" * 100, limit=10)


def test_infrastructure_error_exposes_retry_certainty():
    error = RemoteQueueFull("full", request_id="client:1")
    assert error.request_id == "client:1"
    assert error.acceptance_certainty is AcceptanceCertainty.NOT_ACCEPTED
    assert error.execution_certainty is ExecutionCertainty.NOT_STARTED


def test_scheduler_is_strict_priority_and_principal_fair():
    scheduler = FairScheduler()
    scheduler.put("a1", priority=2, principal="a")
    scheduler.put("a2", priority=2, principal="a")
    scheduler.put("b1", priority=2, principal="b")
    scheduler.put("urgent", priority=1, principal="a")

    assert [scheduler.pop() for _ in range(4)] == ["urgent", "a1", "b1", "a2"]


def test_scheduler_removal_preserves_other_principals_and_priorities():
    scheduler = FairScheduler()
    scheduler.put("a1", priority=2, principal="a")
    scheduler.put("a2", priority=2, principal="a")
    scheduler.put("b1", priority=2, principal="b")
    scheduler.put("urgent", priority=1, principal="a")

    assert scheduler.remove("a1")
    assert not scheduler.remove("missing")
    assert scheduler.remove("urgent")
    assert len(scheduler) == 2
    assert [scheduler.pop(), scheduler.pop()] == ["a2", "b1"]
    with pytest.raises(IndexError, match="empty"):
        scheduler.pop()


def test_limited_writer_rejects_atomically_and_validates_limit():
    with pytest.raises(ValueError, match="non-negative"):
        _LimitedWriter(-1)

    writer = _LimitedWriter(3)
    assert writer.write(b"abc") == 3
    before = bytes(writer.buffer)
    with pytest.raises(SerializationLimitError):
        writer.write(b"d")
    assert bytes(writer.buffer) == before


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"max_frame_payload_bytes": 65 * 1024 * 1024}, "frame_payload"),
        ({"max_invocation_bytes": 65 * 1024 * 1024}, "invocation"),
        ({"max_result_bytes": 65 * 1024 * 1024}, "result"),
        ({"max_frame_payload_bytes": 1, "max_chunks": 1}, "max_chunks"),
        (
            {"max_connections_global": 1, "max_connections_per_principal": 2},
            "connections_per_principal",
        ),
        (
            {"max_pending_global": 1, "max_pending_per_principal": 2},
            "pending_per_principal",
        ),
        (
            {
                "max_retained_outcomes_global": 1,
                "max_retained_outcomes_per_session": 2,
            },
            "retained outcome count",
        ),
        (
            {
                "max_retained_outcome_bytes_global": 1,
                "max_retained_outcome_bytes_per_session": 2,
            },
            "retained outcome bytes",
        ),
    ],
)
def test_remote_limit_cross_field_relationships(changes, message):
    with pytest.raises(ValueError, match=message):
        RemoteLimits(**changes)


class CallbackClient:
    def __init__(self):
        self.cancel_calls = 0

    def _schedule_callbacks(self, callbacks, future):
        for callback in callbacks:
            callback(future)

    def _schedule_callback(self, callback, future):
        callback(future)

    def _cancel(self, future, *, hard):
        self.cancel_calls += 1
        return False


def test_future_terminal_state_and_callbacks_are_idempotent():
    client = CallbackClient()
    future = RemoteFuture("request:1", client)
    callbacks = []
    future.add_done_callback(lambda done: callbacks.append(done.result()))
    future._set_running(pid=123, worker_id="worker:123")
    future._set_result("ok")

    assert not future._set_sent()
    future._set_accepted()
    future._set_running(pid=456, worker_id="worker:456")
    future._set_exception(RuntimeError("late"))
    future._set_cancelled()

    assert future.result() == "ok"
    assert future.submission_state is SubmissionState.SUCCEEDED
    assert future.pid == 123
    assert future.worker_id == "worker:123"
    assert callbacks == ["ok"]


def test_future_local_cancellation_never_contacts_server():
    client = CallbackClient()
    future = RemoteFuture("request:2", client)
    callbacks = []
    future.add_done_callback(lambda done: callbacks.append(done.cancelled()))

    assert future.cancel()
    assert future.cancelled()
    assert future.submission_state is SubmissionState.CANCELLED
    assert client.cancel_calls == 0
    assert callbacks == [True]
    done, not_done = wait([future], timeout=0.1)
    assert done == {future}
    assert not not_done
    assert list(as_completed([future], timeout=0.1)) == [future]
