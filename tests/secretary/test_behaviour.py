"""Item 8's behavioural half: the intake answers while every consumer stalls.

Each test stalls one of the three dependencies gate item 8 names — worker
monitoring, long-running work, an AI judgement — **verifiably** (the stalled
thread is parked on an Event or a pipe that this test controls and has not
released) and then drives the intake, asserting every request receives its
receipt while the stall is still in force.

No latency number is asserted anywhere: ``Q-0011`` is unresolved and this
suite does not invent a threshold. The assertions are ordering and
completeness — receipts exist, are answered, and were produced while the
consumer demonstrably made no progress. Timeouts passed to ``join()`` below
are test mechanics (they only bound how long a *failing* run hangs), not
acceptance numbers.

Durable tests (D-0026) for the **rehearsal** of gate item 8 (Issue #21,
D-0022). The discharge point is the real Secretary under genuine worker load,
before the canary starts.
"""

from __future__ import annotations

import os
import threading

from claude_org_runtime.secretary import IntakeQueue, SecretaryIntake
from claude_org_runtime.secretary.intake import ACCEPTED, REFUSED_QUEUE_FULL


def _submit_all(intake: SecretaryIntake, n: int) -> list:
    return [intake.submit({"seq": i}) for i in range(n)]


def test_intake_answers_while_an_ai_judgement_is_in_flight() -> None:
    """An open incident awaiting Dispatcher AI judgement blocks nothing.

    The consumer pulls a batch (the incident) and parks inside its
    "judgement" — an Event this test never sets while submitting.
    """

    queue = IntakeQueue(capacity=64)
    intake = SecretaryIntake(queue)
    judgement_started = threading.Event()
    judgement_done = threading.Event()

    def consumer() -> None:
        while not queue.take_batch(limit=1):
            pass  # incident not enqueued yet
        judgement_started.set()
        judgement_done.wait()  # the AI judgement, in flight

    t = threading.Thread(target=consumer, daemon=True)
    t.start()
    try:
        intake.submit({"kind": "incident", "awaiting": "dispatcher-ai"})
        assert judgement_started.wait(timeout=30), "consumer never picked up"

        receipts = _submit_all(intake, 32)

        assert not judgement_done.is_set() and t.is_alive(), (
            "the stall was released early; the test proved nothing"
        )
        assert [r.status for r in receipts] == [ACCEPTED] * 32
        assert all(r.answered_ns >= r.received_ns for r in receipts)
        assert queue.depth() == 32, "consumer progressed while parked"
    finally:
        judgement_done.set()
        t.join(timeout=30)


def test_intake_answers_while_long_running_work_holds_the_consumer() -> None:
    """A long-running task in flight blocks nothing.

    Identical boundary, different stall site: the consumer took its batch and
    is stuck in the *work*, after ``take_batch`` returned — the contract that
    processing happens outside the lock is what this exercises.
    """

    queue = IntakeQueue(capacity=64)
    intake = SecretaryIntake(queue)
    work_started = threading.Event()
    work_done = threading.Event()

    def consumer() -> None:
        while not queue.take_batch(limit=8):
            pass
        work_started.set()
        work_done.wait()  # the long-running task, in flight

    t = threading.Thread(target=consumer, daemon=True)
    t.start()
    try:
        intake.submit({"kind": "task", "shape": "long-running"})
        assert work_started.wait(timeout=30), "consumer never picked up"

        receipts = _submit_all(intake, 32)

        assert not work_done.is_set() and t.is_alive()
        assert [r.status for r in receipts] == [ACCEPTED] * 32
    finally:
        work_done.set()
        t.join(timeout=30)


def test_intake_answers_while_worker_monitoring_blocks_its_thread() -> None:
    """A monitor thread stuck in a blocking read shares nothing with intake.

    This is the C2 analogue of U6 in miniature: the blocking hazard lives in
    supervisor code (a per-child blocking ``read``), and even when that code
    *does* block — here, on a pipe with no writer activity — the intake path
    cannot inherit the stall, because no lock, queue, or call edge connects
    them. The empirical half at the worker cap lives in
    ``investigation/i16_item8_rehearsal.py``.
    """

    queue = IntakeQueue(capacity=64)
    intake = SecretaryIntake(queue)
    r_fd, w_fd = os.pipe()
    monitoring = threading.Event()

    def monitor() -> None:
        monitoring.set()
        os.read(r_fd, 1)  # a naive supervisor, blocked on a silent child

    t = threading.Thread(target=monitor, daemon=True)
    t.start()
    try:
        assert monitoring.wait(timeout=30)
        receipts = _submit_all(intake, 32)
        assert t.is_alive(), "monitor unblocked early; the test proved nothing"
        assert [r.status for r in receipts] == [ACCEPTED] * 32
    finally:
        os.write(w_fd, b"x")
        t.join(timeout=30)
        os.close(r_fd)
        os.close(w_fd)


def test_a_full_queue_is_an_immediate_recorded_refusal() -> None:
    """Backpressure is a refusal, not a wait — and the refusal is recorded."""

    queue = IntakeQueue(capacity=2)
    intake = SecretaryIntake(queue)

    first, second, third = (intake.submit({"seq": i}) for i in range(3))

    assert first.status == ACCEPTED and second.status == ACCEPTED
    assert third.status == REFUSED_QUEUE_FULL
    assert third.answered_ns >= third.received_ns
    refusals = intake.refusals()
    assert [r.request_id for r in refusals] == [third.request_id]
    assert refusals[0].queue_depth == 2
    # The refusal consumed no capacity and lost no accepted item.
    drained = queue.take_batch(limit=10)
    assert [i.request_id for i in drained] == [first.request_id,
                                               second.request_id]


def test_the_boundary_is_fifo_and_pull_only() -> None:
    """Consumers pull; an empty queue yields [] at once, order is preserved."""

    queue = IntakeQueue(capacity=8)
    intake = SecretaryIntake(queue)

    assert queue.take_batch(limit=4) == []
    receipts = _submit_all(intake, 5)
    got = queue.take_batch(limit=3)
    assert [i.request_id for i in got] == [r.request_id for r in receipts[:3]]
    assert queue.depth() == 2
    rest = queue.take_batch(limit=10)
    assert [i.request_id for i in rest] == [r.request_id for r in receipts[3:]]


def test_concurrent_producers_never_lose_or_duplicate_a_request() -> None:
    """Many windows' worth of producers against a stalled consumer.

    Every submit is answered exactly once; accepted + refused == submitted;
    accepted items all cross the boundary with distinct identities.
    """

    queue = IntakeQueue(capacity=100)
    intake = SecretaryIntake(queue)
    n_threads, per_thread = 8, 50
    receipts: list[list] = [[] for _ in range(n_threads)]

    def producer(slot: int) -> None:
        receipts[slot] = _submit_all(intake, per_thread)

    threads = [threading.Thread(target=producer, args=(i,))
               for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)

    flat = [r for slot in receipts for r in slot]
    assert len(flat) == n_threads * per_thread
    assert len({r.request_id for r in flat}) == len(flat)
    accepted = [r for r in flat if r.status == ACCEPTED]
    refused = [r for r in flat if r.status == REFUSED_QUEUE_FULL]
    assert len(accepted) + len(refused) == len(flat)
    # The lock-free capacity check is advisory-exact: with P concurrent
    # producers it may overshoot by at most P - 1 (documented on offer()).
    assert 100 <= len(accepted) <= 100 + n_threads - 1
    drained = queue.take_batch(limit=1000)
    assert len(drained) == len(accepted)  # nothing lost, nothing dropped
    assert len({i.request_id for i in drained}) == len(accepted)
    assert len(intake.refusals()) == len(refused)
