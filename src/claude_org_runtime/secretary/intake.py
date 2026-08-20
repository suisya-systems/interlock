"""Stub Secretary intake with an explicit queue boundary.

Gate item 8 (``ACCEPTANCE.md`` §1) requires that no Secretary response is
blocked *behind* worker monitoring, long-running work, or an AI judgement —
structurally, by showing intake and the queue boundary are asynchronous, and
empirically, by a baseline-vs-load latency comparison. This module is the
structural half of the **rehearsal** (Issue #21, D-0022): a deliberately
minimal intake whose non-blocking property is a property of the code, enforced
by ``tests/secretary/`` rather than by convention.

The design rule, stated once and asserted three ways:

1. **The intake path performs no blocking call.** ``submit()`` stamps a
   receipt, offers the request to the queue without waiting, and returns. It
   never joins a thread, waits on an event, reads a pipe, sleeps, or calls
   into any consumer. ``tests/secretary/test_structural.py`` bans the blocking
   primitives from this package's syntax tree.

2. **The boundary is lock-free, so there is nothing to wait on even
   implicitly.** A ``with lock:`` is a blocking ``acquire()`` whenever the
   holder is descheduled, which would reintroduce through the back door
   exactly the wait the contract forbids. This package therefore takes **no
   lock at all** — asserted on the syntax tree — and relies on CPython's
   atomic ``deque.append`` / ``deque.popleft`` and C-implemented iterator
   steps for its shared state. The price is stated in
   :meth:`IntakeQueue.offer`: the capacity check is exact under one producer
   and approximate within the number of concurrent producers otherwise.

3. **This module depends on no other Interlock module.** In particular it has
   no dependency edge to the ``session`` package (the supervisor / provider
   side) or to ``dispatcher``. Worker monitoring and AI judgement cannot block
   a code path that cannot reach them. Asserted structurally, following the
   precedent of ``tests/control_plane/test_lease.py``.

**Backpressure is a refusal, not a wait.** When the bounded queue is full the
request is refused and the refusal is recorded on the receipt and in the
refusal log — the intake still answers immediately. Whether a refusal, and at
what depth, is *acceptable* is a Secretary-design question outside this
rehearsal; what the rehearsal fixes is only that the alternative to acceptance
is an immediate recorded refusal, never a block.

**Spike scaffold, throwaway by default (D-0026).** State is in-memory on
purpose: durable intake (an SQLite-backed inbox) is the real Secretary's
concern and is not rehearsed here. No numeric latency threshold appears
anywhere in this package — ``Q-0011`` is unresolved and this rehearsal does
not invent one.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import dataclass

__all__ = [
    "IntakeQueue",
    "IntakeReceipt",
    "IntakeRefused",
    "SecretaryIntake",
]

#: Closed vocabulary for receipt statuses.
ACCEPTED = "accepted"
REFUSED_QUEUE_FULL = "refused_queue_full"


@dataclass(frozen=True)
class IntakeReceipt:
    """What the requester gets back, immediately, in every case.

    ``received_ns`` / ``answered_ns`` are ``time.monotonic_ns()`` stamps taken
    at entry to and exit from :meth:`SecretaryIntake.submit`; the empirical
    harness derives request→response latency from them. ``queue_depth`` is the
    depth the accept/refuse decision **observed** — the single read the
    decision was made on, so receipt and decision cannot contradict each
    other; under concurrent producers/consumers it is approximate by
    construction (see :meth:`IntakeQueue.offer`).
    """

    request_id: int
    status: str
    queue_depth: int
    received_ns: int
    answered_ns: int

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED


@dataclass(frozen=True)
class IntakeRefused:
    """A recorded refusal: the queue was observed full at ``queue_depth``."""

    request_id: int
    queue_depth: int
    refused_ns: int


@dataclass
class _Item:
    """What crosses the boundary: the payload plus its intake identity."""

    request_id: int
    payload: object
    enqueued_ns: int


class IntakeQueue:
    """The explicit, bounded, one-way, lock-free boundary.

    The producer side is :meth:`offer` — non-blocking, refuses when full.
    The consumer side is :meth:`take_batch` — pops what is there and returns
    at once; the consumer processes items **after** the call returns.
    Consumers *pull*; nothing on the consumer side is ever invoked, signalled,
    or waited for by the producer side, and there is no lock through which a
    stalled consumer could be waited on even implicitly.

    Shared state is a single :class:`collections.deque`, whose ``append`` and
    ``popleft`` are atomic in CPython. **The capacity bound is advisory-exact:**
    with one producer it is exact; with *P* concurrent producers the
    check-then-append race can overshoot ``capacity`` by at most ``P - 1``
    items. That is the deliberate price of having no lock on the response
    path, and it is bounded by the thing D-0017 already caps.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._items: deque[_Item] = deque()

    def offer(self, item: _Item) -> tuple[bool, int]:
        """Append without waiting. Returns ``(accepted, observed_depth)``.

        ``observed_depth`` is the single ``len`` read the decision was made
        on (post-append for an acceptance), so the caller can record the
        depth its outcome actually saw.
        """
        n = len(self._items)
        if n >= self.capacity:
            return False, n
        self._items.append(item)
        return True, n + 1

    def take_batch(self, limit: int) -> list[_Item]:
        """Consumer side: pop up to ``limit`` items and return at once.

        Never blocks waiting for items; an empty queue yields an empty list.
        Safe against concurrent producers and consumers: each ``popleft`` is
        atomic and a concurrent pop simply ends the batch early.
        """
        out: list[_Item] = []
        while len(out) < limit:
            try:
                out.append(self._items.popleft())
            except IndexError:
                break
        return out

    def depth(self) -> int:
        return len(self._items)


class SecretaryIntake:
    """The stub Secretary window: stamp, offer, answer. Nothing else.

    ``submit()`` is the entire response path. Its receipt is the response —
    acceptance into the queue or a recorded refusal — and producing it involves
    no interaction with whatever consumes the queue. Request ids come from a
    C-implemented ``itertools.count``, whose ``next`` is atomic under the GIL;
    the refusal log is a list, whose ``append`` is likewise atomic.
    """

    def __init__(self, queue: IntakeQueue) -> None:
        self._queue = queue
        self._ids = itertools.count(1)
        self._refusals: list[IntakeRefused] = []

    def submit(self, payload: object) -> IntakeReceipt:
        received_ns = time.monotonic_ns()
        request_id = next(self._ids)
        item = _Item(request_id=request_id, payload=payload,
                     enqueued_ns=received_ns)
        accepted, depth = self._queue.offer(item)
        if not accepted:
            self._refusals.append(
                IntakeRefused(request_id=request_id, queue_depth=depth,
                              refused_ns=time.monotonic_ns()))
        return IntakeReceipt(
            request_id=request_id,
            status=ACCEPTED if accepted else REFUSED_QUEUE_FULL,
            queue_depth=depth,
            received_ns=received_ns,
            answered_ns=time.monotonic_ns(),
        )

    def refusals(self) -> list[IntakeRefused]:
        """The recorded refusals, oldest first (a snapshot copy)."""
        return list(self._refusals)
