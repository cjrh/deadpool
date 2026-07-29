"""Opaque bytes-in/bytes-out worker entry point."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Callable

from .serializer import SerializationLimitError, Serializer


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    kind: str
    payload: bytes = b""
    descriptor: dict | None = None


def execute_opaque(
    serializer: Serializer,
    mode: str,
    invocation: bytes,
    operation: str | None,
    registry: dict[str, Callable] | None,
    max_result_bytes: int,
) -> WorkerOutcome:
    """Materialize application objects only inside the selected worker."""
    try:
        if mode == "callable":
            fn, args, kwargs = serializer.loads(invocation)
        elif mode == "registered":
            if registry is None or operation not in registry:
                raise LookupError(f"unknown registered operation {operation!r}")
            fn = registry[operation]
            args, kwargs = serializer.loads(invocation)
        else:
            raise ValueError(f"unknown invocation mode {mode!r}")
        result = fn(*args, **kwargs)
    except BaseException as error:
        descriptor = _describe_exception(error)
        try:
            payload = serializer.dumps(error, limit=max_result_bytes)
        except BaseException as serialization_error:
            descriptor["serialization_error"] = _safe_repr(serialization_error)
            payload = b""
        return WorkerOutcome("task_error", payload, descriptor)

    try:
        payload = serializer.dumps(result, limit=max_result_bytes)
    except SerializationLimitError as error:
        return WorkerOutcome(
            "result_too_large", descriptor={"message": _safe_repr(error)}
        )
    except BaseException as error:
        return WorkerOutcome(
            "result_encoding_failed",
            descriptor=_describe_exception(error),
        )
    return WorkerOutcome("result", payload)


def _describe_exception(error: BaseException) -> dict:
    return {
        "module": type(error).__module__,
        "type": type(error).__qualname__,
        "message": _safe_repr(error),
        "traceback": "".join(traceback.format_exception(error)),
    }


def _safe_repr(value: object) -> str:
    try:
        return repr(value)[:4096]
    except BaseException:
        return f"<{type(value).__module__}.{type(value).__qualname__}>"
