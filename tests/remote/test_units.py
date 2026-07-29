import pytest

from deadpool.remote import (
    AcceptanceCertainty,
    ExecutionCertainty,
    PickleSerializer,
    RemoteQueueFull,
    SerializationLimitError,
)
from deadpool.remote._scheduler import FairScheduler


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
