"""S8 -- the ``MessageBus`` facade: send, poll, ack, over the S7 outbox.

.. warning::

   **Spike scaffold, throwaway by default (D-0026).** The contract the suite
   pins is durable; this implementation of it is not.

**What this class is, and what it deliberately is not.** The S7 outbox already
holds every delivery decision this bus makes: :meth:`Outbox.due` answers what
is unfinished, :meth:`Outbox.attempt` runs one fenced delivery attempt with its
kill windows in the right order, and :meth:`Outbox.record_ack` settles a
message idempotently. What S8 adds is the **worker-outbound shape** around
those verbs -- a sender-side :meth:`MessageBus.send` and a recipient-side
:meth:`MessageBus.poll` / :meth:`MessageBus.ack` pair -- and nothing else. The
existing outbox API is used as found, not modified (Issue ``#19``'s scope
note), so the fault-injection evidence S7 accumulated keeps describing the
delivery path this bus actually takes.

**Pull replaces claim-then-confirm.** In v1 a sidecar claimed rows over HTTP
and confirmed them under a generation fence. Here the worker *polls*: each poll
re-runs :meth:`Outbox.attempt` for every due message addressed to it, which
marks the row delivered and re-presents the payload. A poll response lost on
the wire changes nothing durable on the worker's side and leaves the row
delivered-but-unacked, so it stays due and the next poll re-presents it --
resend is the default, not a recovery mode. The ack is the one message-level
settlement, and it is idempotent and deliberately unfenced
(:meth:`Outbox.record_ack`'s own rationale), so however many times a worker
repeats it, exactly one ack is recorded.

**Delivery decisions are SQLite-only.** ``poll`` reads :meth:`Outbox.due` and
nothing else. There is no session readout, no liveness probe, and no way to
consult one: this module has no import edge to
:mod:`claude_org_runtime.session`, and ``tests/messagebus/test_import_graph.py``
keeps it that way. A provider readout that is stale or wrong -- a session id
whose child is gone, a ``read_state`` that answers "could not observe" --
cannot alter what this bus delivers, because no code path exists from the one
to the other (gate item 6, translated for C2 where there is no UI to detach).

**Exceptions are the outbox's own.** ``send`` to a recipient no handler serves
raises :class:`HandlerRejected` at the enqueue boundary -- the carried v1
invariant that a message could only be enqueued to a registered binding,
re-expressed against the handler registry, which is the only recipient roster
this layer has. ``poll`` propagates :class:`StaleWriterRefused`,
:class:`HumanGateRequired` and destination refusals exactly as
:meth:`Outbox.attempt` raises them; wrapping them here would hide the fence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Sequence

from ..control_plane.outbox import (
    AckOutcome,
    HandlerRegistry,
    Outbox,
    OutboxMessage,
    StaleWriterRefused,
)

__all__ = [
    "DeliveredEnvelope",
    "MessageBus",
]


@dataclass(frozen=True)
class DeliveredEnvelope:
    """One message as presented to a polling worker.

    The envelope carries the attempt's outcome alongside the payload so the
    worker can see, per presentation, whether the effect behind it was fresh or
    deduplicated -- the at-least-once transport made visible, with the
    exactly-once evidence attached.
    """

    #: The durable message identity; the argument :meth:`MessageBus.ack` takes.
    message_id: str
    #: The recipient the message was addressed to (always the polling one).
    recipient: str
    #: The payload as enqueued.
    payload: str
    #: The sender's dedup key, so the worker can deduplicate re-presentations
    #: of the same intent even across a re-enqueue under a new message id.
    dedup_key: str
    #: Attempts so far, this presentation included.
    retry_count: int
    #: ``True`` when the destination recognised the idempotency key and applied
    #: no second effect -- i.e. this is a re-presentation, not a first delivery.
    deduplicated: bool
    #: The destination's own reference for the effect, when it issued one.
    receipt_ref: str | None


class MessageBus:
    """Worker-outbound send/poll/ack over one S7 :class:`Outbox`.

    One bus serves one lease-fenced writer (*resource*, *holder*), exactly as
    the outbox underneath it does; which component holds which resource is
    ``Q-0001``'s business and stays out of this layer.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        resource: str,
        holder: str,
        registry: HandlerRegistry,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._registry = registry
        extra = {"checkpoint": checkpoint} if checkpoint is not None else {}
        self._outbox = Outbox(
            connection,
            resource=resource,
            holder=holder,
            registry=registry,
            **extra,
        )

    @property
    def outbox(self) -> Outbox:
        """The S7 outbox this bus fronts, exposed for inspection, not bypass."""

        return self._outbox

    def send(
        self,
        *,
        message_id: str,
        recipient: str,
        payload: str,
        dedup_key: str,
        now_ms: int,
        epoch: int,
        run_id: str | None = None,
    ) -> OutboxMessage:
        """Enqueue one message to a recipient a handler is registered for.

        The registry lookup runs *before* the durable write, so a send to an
        unregistered recipient is refused (:class:`HandlerRejected`) without
        leaving a row nothing can ever deliver -- the carried
        "enqueue only to registered" invariant, whose roster here is the
        handler registry rather than v1's pane bind table.
        """

        self._registry.for_recipient(recipient)
        return self._outbox.enqueue(
            message_id=message_id,
            recipient=recipient,
            payload=payload,
            dedup_key=dedup_key,
            now_ms=now_ms,
            epoch=epoch,
            run_id=run_id,
        )

    def poll(
        self,
        recipient: str,
        *,
        now_ms: int,
        epoch: int,
        clock: Callable[[], int] | None = None,
    ) -> Sequence[DeliveredEnvelope]:
        """One pull: attempt every due message for *recipient*, oldest first.

        Each returned envelope corresponds to one completed
        :meth:`Outbox.attempt` -- the row is marked delivered and the effect is
        applied or recognised as already applied before the payload is
        presented. A response the worker never receives therefore loses
        nothing: the rows stay due (delivered-but-unacked) and the next poll
        presents them again. What is due is read from SQLite and nowhere else.

        Presentation is at-least-once all the way to the wire: an ack that
        lands concurrently with a poll already carrying the same message --
        after its attempt completed, or while the response is in flight --
        can put one more presentation of a just-settled message in front of
        the worker. That race has no server-side fix (the response cannot be
        recalled), which is why every envelope carries the sender's
        ``dedup_key``: the recipient deduplicates, exactly as it must for the
        resend path. Settlement stops *future* polls from presenting the
        message; it cannot retract one already leaving.

        *clock*, when given, is read again for **every** attempt; *now_ms*
        then only anchors the due() snapshot. A poll that outlives its lease
        must not keep delivering on the timestamp it started with -- the fence
        evaluates expiry against the instant of each write, so with a live
        clock a long poll dies loudly (:class:`StaleWriterRefused`, refusal
        recorded) at the first attempt past the expiry instead of draining
        the whole batch under a dead lease. Callers with a fixed *now_ms* and
        no clock get the deterministic single-instant semantics the tests
        use.
        """

        envelopes: list[DeliveredEnvelope] = []
        for message in self._outbox.due(now_ms):
            if message.recipient != recipient:
                continue
            if self._outbox.load(message.message_id).status == "acked":
                # Settled since the due() snapshot -- the common shape of a
                # late ack (an endpoint restart overlapping its predecessor's
                # unflushed ack). Re-reading here keeps the ordinary race out
                # of attempt() entirely, so no refusal is durably recorded for
                # what is simply a settled message.
                continue
            # One instant per attempt, by the outbox's own contract:
            # Outbox.attempt takes a single now_ms, and S7 already decided how
            # the window inside one attempt is handled -- the in-attempt lease
            # re-read narrows it, and a writer paused past its lease inside
            # the attempt is refused by the *destination's* fencing token
            # (StaleTokenRefused), the only party still running. Re-sampling
            # the clock inside the attempt is the outbox's business, not this
            # facade's.
            attempt_now = clock() if clock is not None else now_ms
            try:
                outcome = self._outbox.attempt(
                    message.message_id, now_ms=attempt_now, epoch=epoch
                )
            except (ValueError, StaleWriterRefused):
                # The residual window: an ack that lands after the re-read
                # above but inside attempt() itself. The outbox surfaces it as
                # "already acked" (ValueError) or as the fenced attempt-count
                # update finding no row to move. A settled message is a poll's
                # success case, not its error: skip it and keep presenting the
                # rest. Anything else re-raises -- a fence refusal on a
                # genuinely unsettled row must stay loud. Known cost, accepted:
                # on the StaleWriterRefused branch the outbox has already
                # durably recorded a refusal row before raising; inside this
                # residual window that row is audit noise (an attempt refused
                # because the message was settled), never a delivery fault --
                # eliminating it would need the outbox itself to classify why
                # the fenced update moved no row, which Issue #19 keeps out of
                # scope (the outbox API is used as found).
                if self._outbox.load(message.message_id).status == "acked":
                    continue
                raise
            envelopes.append(
                DeliveredEnvelope(
                    message_id=message.message_id,
                    recipient=message.recipient,
                    payload=message.payload,
                    dedup_key=message.dedup_key,
                    retry_count=outcome.retry_count,
                    deduplicated=outcome.deduplicated,
                    receipt_ref=outcome.receipt_ref,
                )
            )
        return tuple(envelopes)

    def ack(self, message_id: str, *, now_ms: int, recipient: str) -> AckOutcome:
        """Settle one delivered message. Idempotent; unfenced by design.

        *recipient* must be the recipient the message was addressed to -- the
        carried v1 invariant that a confirm from anyone but the owner is
        refused, re-expressed without the credential machinery: the endpoint
        serves one recipient and states it, and an ack across that boundary is
        a caller bug, not a settlement.

        Otherwise delegates to :meth:`Outbox.record_ack` unchanged: exactly
        one ack is ever recorded per message, later acks are no-ops, and an
        ack for a message never marked delivered is refused as evidence of a
        lost delivery record.
        """

        message = self._outbox.load(message_id)
        if message.recipient != recipient:
            raise ValueError(
                f"{message_id!r} is addressed to {message.recipient!r}; an ack "
                f"from {recipient!r} does not settle it"
            )
        return self._outbox.record_ack(message_id, now_ms=now_ms)
