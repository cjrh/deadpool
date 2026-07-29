"""Strict-priority, principal-fair broker scheduler."""

from __future__ import annotations

import heapq
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class _Priority(Generic[T]):
    principals: deque[str] = field(default_factory=deque)
    queued: dict[str, deque[T]] = field(default_factory=lambda: defaultdict(deque))


class FairScheduler(Generic[T]):
    """Select lower numeric priorities first and round-robin principals within one."""

    def __init__(self) -> None:
        self._priorities: dict[int, _Priority[T]] = {}
        self._heap: list[int] = []
        self._size = 0

    def put(self, item: T, *, priority: int, principal: str) -> None:
        bucket = self._priorities.get(priority)
        if bucket is None:
            bucket = self._priorities[priority] = _Priority()
            heapq.heappush(self._heap, priority)
        queue = bucket.queued[principal]
        if not queue:
            bucket.principals.append(principal)
        queue.append(item)
        self._size += 1

    def pop(self) -> T:
        if not self._size:
            raise IndexError("scheduler is empty")
        priority = self._heap[0]
        bucket = self._priorities[priority]
        principal = bucket.principals.popleft()
        queue = bucket.queued[principal]
        item = queue.popleft()
        self._size -= 1
        if queue:
            bucket.principals.append(principal)
        else:
            del bucket.queued[principal]
        if not bucket.principals:
            heapq.heappop(self._heap)
            del self._priorities[priority]
        return item

    def remove(self, item: T) -> bool:
        for priority, bucket in list(self._priorities.items()):
            for principal, queue in list(bucket.queued.items()):
                try:
                    queue.remove(item)
                except ValueError:
                    continue
                self._size -= 1
                if not queue:
                    del bucket.queued[principal]
                    try:
                        bucket.principals.remove(principal)
                    except ValueError:
                        pass
                if not bucket.principals:
                    del self._priorities[priority]
                    self._heap.remove(priority)
                    heapq.heapify(self._heap)
                return True
        return False

    def __bool__(self) -> bool:
        return bool(self._size)

    def __len__(self) -> int:
        return self._size
