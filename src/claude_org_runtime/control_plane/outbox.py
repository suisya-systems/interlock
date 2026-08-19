"""S7 -- the outbox: resend, ack, dedup, and handlers that name their mechanism.

.. warning::

   **Spike scaffold, throwaway by default (D-0026).** This module sits on the S5
   spike schema, which carries the marking in ``spike_schema.sql`` itself: no
   migration path is promised from it, and being depended on by S7 promotes
   nothing. ``Q-0001`` (the real DDL, keys, indices and the per-item
   single-writer table) stays open, and nothing below answers it. The durable
   half of Issue ``#14`` is the test suite.

What this module is responsible for, in the words of ``ACCEPTANCE.md`` section
2's outbox rows:

* **Resend.** *Every enqueued message is eventually delivered at least once;
  nothing is lost by a kill at any of those points; retry count is durable
  across restarts.*
* **Ack.** *Ack is idempotent. A lost ack causes a resend (safe), never a lost
  message. A duplicate or late ack changes nothing.*
* **Dedup.** *Duplicate delivery causes exactly one effect.*

and the declaration that runs underneath all three:

* **Every action handler names its exactly-once mechanism**, because SQLite
  cannot tell "the side effect completed" from "the side effect never started".

Four things here are load-bearing rather than stylistic.

**At-least-once delivery, exactly-once effect.** These are different guarantees
carried by different records and it is worth being blunt about which is which.
The outbox delivers *at least once*: a lost ack is answered by a resend, and a
resend is not a failure. Exactly-once is a property of the **effect**, evidenced
by ``action.idempotency_key`` on our side and by the destination's own ledger on
the other side (:mod:`~claude_org_runtime.control_plane.destination`). This is
also why S5 left ``outbox.dedup_key`` deliberately non-unique: a sender killed
after writing an outbox row may legitimately re-enqueue, and collapsing those
rows in DDL would have moved delivery policy into the schema.

**Every write is fenced, and every refusal is recorded.** ``ACCEPTANCE.md``
section 2 requires a stale writer to be *rejected, not merged*, and requires the
rejection to be **itself durable** -- "not silently dropped". So each protected
statement carries the lease epoch and validates it atomically inside the write,
in the single-statement form ``spike_schema.sql`` documents on the ``lease``
table; and a write that matches no row is not an early ``return`` but an
``action`` row in status ``'refused'`` carrying its reason.

**S6 owns the lease; S7 only validates it.** Acquisition, renewal and expiry
policy are Issue ``#13``'s (S6), which is a sibling of this issue rather than a
dependency of it -- I-09 depends on I-07 alone. This module therefore never
acquires or renews anything. It takes the resource and holder it writes under as
constructor arguments and validates the epoch inside its own writes, which is
the coupling S5's DDL comment already specifies. Naming the resource is the
caller's job precisely because *which component may hold which resource* is the
per-item writer assignment ``Q-0001`` leaves open; a default here would answer
it.

**No retry interval appears in this file.** Not a backoff, not a visibility
timeout, not a re-notification window. ``Q-0003`` has to settle tolerable
detection latency first, and S5 kept every such number out of the schema for the
same reason. :meth:`Outbox.due` answers *what is unfinished*; **when** to call it
is the caller's, and the durable retry count is what a policy would later be
written against.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from .destination import DeliveryReceipt, DestinationRefusal

__all__ = [
    "CHECKPOINTS",
    "CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD",
    "CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT",
    "CHECKPOINT_BEFORE_DURABLE_WRITE",
    "CHECKPOINT_DELIVERED_BEFORE_ACK",
    "EXACTLY_ONCE_MECHANISMS",
    "UNOWNED_OUTBOX_QUERY",
    "AckOutcome",
    "ActionHandler",
    "AttemptOutcome",
    "HandlerRegistry",
    "HandlerRejected",
    "HumanGateRequired",
    "Outbox",
    "OutboxMessage",
    "RecoveryReport",
    "StaleWriterRefused",
]

#: The three mechanisms ``spike_schema.sql`` enumerates on
#: ``action.exactly_once_mechanism``, mirrored here so a handler can be rejected
#: at **registration** time rather than at its first INSERT. The list is not
#: this module's policy; it is ``ACCEPTANCE.md`` section 2's clause, and the
#: enumeration in the DDL and this constant are asserted equal by the suite so
#: they cannot drift.
EXACTLY_ONCE_MECHANISMS = (
    "destination_idempotency_key",
    "transactional_with_record",
    "human_gate",
)

#: Named points at which a delivery can be killed. ``ACCEPTANCE.md`` section 2
#: names the first three by description -- *before the durable write*, *after
#: the durable write but before the side effect*, *after the side effect but
#: before its result is recorded* -- and the outbox rows add the fourth, a kill
#: after delivery but before the ack is recorded.
#:
#: S9 (Issue ``#15``) builds the deterministic harness; S7's obligation is to
#: make the points **exist, be named, and be reachable**, because a window that
#: no test can stop inside is a window nobody can prove anything about. They are
#: constants rather than string literals at the call sites so that the harness
#: binds to a name the compiler checks.
CHECKPOINT_BEFORE_DURABLE_WRITE = "before_durable_write"
CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT = "after_record_before_effect"
CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD = "after_effect_before_record"
CHECKPOINT_DELIVERED_BEFORE_ACK = "delivered_before_ack"

CHECKPOINTS = (
    CHECKPOINT_BEFORE_DURABLE_WRITE,
    CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD,
    CHECKPOINT_DELIVERED_BEFORE_ACK,
)

#: "No outbox row remains in a state with no owner after recovery" as a query,
#: so the acceptance criterion can be run by hand against a database recovered
#: from a crash rather than only reached through this module (D-0001, and the
#: same reason S5 keeps ``RECONSTRUCTION_QUERIES`` as data).
#:
#: A row is **unowned** when it is unfinished and its ``writer_epoch`` is null or
#: does not match a live lease on the resource -- that is, when no living
#: claimant is entitled to advance it. Recovery's job is to make this query
#: return nothing.
UNOWNED_OUTBOX_QUERY = """
    SELECT message_id, status, retry_count, writer_epoch, enqueued_at_ms
      FROM outbox
     WHERE status <> 'acked'
       AND (writer_epoch IS NULL
            OR NOT EXISTS (SELECT 1
                             FROM lease
                            WHERE lease.resource      = :resource
                              AND lease.epoch         = outbox.writer_epoch
                              AND lease.expires_at_ms > :now_ms))
     ORDER BY enqueued_at_ms, message_id
"""

#: What :meth:`Outbox.due` reads. Unfinished means *not acked*: a delivered
#: message whose ack never arrived is exactly the resend case, so it stays due.
_DUE_QUERY = """
    SELECT message_id, run_id, recipient, payload, dedup_key, status,
           retry_count, writer_epoch, enqueued_at_ms, delivered_at_ms, acked_at_ms
      FROM outbox
     WHERE status <> 'acked'
       AND enqueued_at_ms <= :now_ms
     ORDER BY enqueued_at_ms, message_id
"""

_LOAD_QUERY = """
    SELECT message_id, run_id, recipient, payload, dedup_key, status,
           retry_count, writer_epoch, enqueued_at_ms, delivered_at_ms, acked_at_ms
      FROM outbox
     WHERE message_id = :message_id
"""

#: The fence, in the single-statement form ``spike_schema.sql`` specifies on the
#: ``lease`` table. The ``EXISTS`` clause is inside the ``UPDATE`` and not a
#: preceding ``SELECT``: check-then-write leaves precisely the race in which the
#: lease expires between the check and the write, which is the case
#: ``ACCEPTANCE.md`` section 2 injects into.
_FENCE_PREDICATE = """
       AND writer_epoch = :epoch
       AND EXISTS (SELECT 1
                     FROM lease
                    WHERE resource      = :resource
                      AND holder        = :holder
                      AND epoch         = :epoch
                      AND expires_at_ms > :now_ms)
"""


class HandlerRejected(Exception):
    """A handler was refused registration.

    Raised when a handler does not name a mechanism from
    :data:`EXACTLY_ONCE_MECHANISMS`, or names one it cannot support. Registration
    is where this belongs: the acceptance criterion is that *a later handler
    cannot be added without one*, and a check that only fires on the first
    delivery lets an undeclared handler ship.
    """


class StaleWriterRefused(Exception):
    """A protected write matched no row: the writer's fencing token is not live.

    The refusal is durable before this is raised -- see
    :meth:`Outbox._record_refusal`. ``ACCEPTANCE.md`` section 2 requires the
    rejection itself to be recorded, so an exception alone would not discharge
    it.
    """


class HumanGateRequired(Exception):
    """The handler declares ``'human_gate'``: neither mechanism is achievable.

    D-0004 makes this an explicit stop rather than a degraded automatic path.
    The action is recorded as pending and is never advanced by the outbox; a
    human moves it or nothing does. Issue ``#14``'s scope note is emphatic about
    the alternative -- *do not paper over it*.
    """


@dataclass(frozen=True)
class OutboxMessage:
    """One outbox row, as the handler sees it."""

    message_id: str
    run_id: str | None
    recipient: str
    payload: str
    dedup_key: str
    status: str
    retry_count: int
    writer_epoch: int | None
    enqueued_at_ms: int
    delivered_at_ms: int | None
    acked_at_ms: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "OutboxMessage":
        return cls(**{f: row[f] for f in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass(frozen=True)
class AttemptOutcome:
    """What one delivery attempt did."""

    message_id: str
    #: The retry count **after** this attempt's durable increment. Monotonic and
    #: restart-surviving; the increment is committed before the effect is
    #: attempted, so an attempt that dies mid-flight is still counted.
    retry_count: int
    #: ``True`` when the destination recognised the key and applied nothing.
    #: A resend of an already-applied effect lands here, and it is a success.
    deduplicated: bool
    #: The action row carrying the effect's exactly-once evidence.
    action_id: str
    idempotency_key: str
    #: The mechanism the handler declared, copied onto the outcome so a caller
    #: reading a log knows what the guarantee rested on.
    exactly_once_mechanism: str
    #: The destination's own reference to its idempotency record, or ``None``
    #: for a mechanism that has no external counterparty.
    receipt_ref: str | None


@dataclass(frozen=True)
class AckOutcome:
    """What one ack did -- which, for every ack after the first, is nothing."""

    message_id: str
    #: ``True`` only for the ack that moved the row. Duplicate and late acks
    #: report ``False`` and are not errors: idempotent means *changes nothing*,
    #: not *is rejected*.
    recorded: bool
    #: The instant stored on the row. Equal to the caller's clock unless it had
    #: to be clamped -- see :attr:`clock_clamped`.
    acked_at_ms: int
    #: ``True`` when the caller's clock ran **behind** the delivery instant and
    #: the recorded value was clamped forward to it. ``ACCEPTANCE.md`` section 2
    #: skews the clock backwards on purpose, and S5's
    #: ``acked_at_ms >= delivered_at_ms`` CHECK would refuse the row. Losing a
    #: real ack to a clock skew would be the worse failure, so the ordering is
    #: preserved and the clamp is **reported** rather than applied silently:
    #: the column is a record of lifecycle order, not a measurement of the wall
    #: clock, and a caller that cares can see that its clock disagreed.
    clock_clamped: bool


@dataclass(frozen=True)
class RecoveryReport:
    """What :meth:`Outbox.recover` found and what it did about it."""

    #: Messages that were unfinished and unowned when recovery started.
    adopted: Sequence[str] = field(default_factory=tuple)
    #: Messages still unowned afterwards. Non-empty means the acceptance
    #: criterion is violated and recovery says so rather than reporting success:
    #: it happens when the recovering holder's own lease is not live, in which
    #: case adopting anything would have been the bug.
    still_unowned: Sequence[str] = field(default_factory=tuple)


class ActionHandler:
    """A side-effect handler that **names** its exactly-once mechanism.

    Subclasses set three class attributes:

    ``recipient``
        The ``outbox.recipient`` value this handler serves. It is the registry
        key: the recipient names *where* a message goes, and the handler is
        *how* it gets there.

    ``action_kind``
        What is written to ``action.kind``.

    ``exactly_once_mechanism``
        One of :data:`EXACTLY_ONCE_MECHANISMS`. **There is no default.** A
        default would be the whole failure this criterion guards against -- a
        handler that never thought about the question and inherited an answer
        anyway -- so a subclass that omits it is refused at registration.
    """

    recipient: str = ""
    action_kind: str = ""
    exactly_once_mechanism: str = ""

    def idempotency_key(self, message: OutboxMessage) -> str:
        """The key one effect is identified by.

        The outbox dedup key namespaced by the action kind. Namespacing is not
        decoration: ``action.idempotency_key`` is unique across the whole table,
        so two handlers deriving keys from the same dedup key would have one
        silently deduplicate against the other's effect.
        """

        return f"{self.action_kind}:{message.dedup_key}"

    def apply(self, message: OutboxMessage, idempotency_key: str) -> DeliveryReceipt | None:
        """Perform the side effect, or recognise it as already performed.

        Called with the ``action`` row already durable in status ``'pending'``
        and **committed** -- that ordering is what makes the effect recoverable
        rather than merely attempted. Returning normally means the effect is
        present at the destination; raising means it is not, and the message
        stays due.
        """

        raise NotImplementedError


class HandlerRegistry:
    """Handlers by recipient, admitting only those that declare a mechanism.

    The acceptance criterion -- *the name is asserted by a test, so a later
    handler cannot be added without one* -- is discharged in two places, and it
    needs both. Here, so that an undeclared handler cannot be registered at all;
    and in the suite, which walks every registered handler and checks its
    declaration, so that the guarantee survives someone bypassing this class.
    """

    def __init__(self) -> None:
        self._by_recipient: dict[str, ActionHandler] = {}

    def register(self, handler: ActionHandler) -> ActionHandler:
        if not handler.recipient:
            raise HandlerRejected(
                f"{type(handler).__name__} does not name the recipient it serves"
            )
        if not handler.action_kind:
            raise HandlerRejected(
                f"{type(handler).__name__} does not name the action kind it records"
            )
        mechanism = handler.exactly_once_mechanism
        if mechanism not in EXACTLY_ONCE_MECHANISMS:
            raise HandlerRejected(
                f"{type(handler).__name__} declares exactly_once_mechanism "
                f"{mechanism!r}, which is not one of {EXACTLY_ONCE_MECHANISMS}. "
                "ACCEPTANCE.md section 2 requires every action handler to name "
                "which mechanism makes it exactly-once, or to declare "
                "'human_gate' because neither is achievable (D-0004) -- SQLite "
                "cannot tell a completed side effect from one that never "
                "started, so a handler that names nothing is claiming a "
                "guarantee it has no way to hold"
            )
        if handler.recipient in self._by_recipient:
            raise HandlerRejected(
                f"recipient {handler.recipient!r} already has a handler "
                f"({type(self._by_recipient[handler.recipient]).__name__})"
            )
        self._by_recipient[handler.recipient] = handler
        return handler

    def for_recipient(self, recipient: str) -> ActionHandler:
        try:
            return self._by_recipient[recipient]
        except KeyError:
            raise HandlerRejected(
                f"no handler is registered for recipient {recipient!r}"
            ) from None

    def handlers(self) -> Sequence[ActionHandler]:
        return tuple(self._by_recipient[key] for key in sorted(self._by_recipient))


def _no_checkpoint(name: str) -> None:
    """The default kill point: nothing happens."""


class Outbox:
    """Resend, ack and dedup over the S5 ``outbox`` and ``action`` tables.

    *resource* and *holder* are the lease this writer's protected statements are
    fenced against. They are required arguments with no defaults: which
    component may write which state item is ``Q-0001``, and a default would be
    an answer to it.

    *checkpoint* is called at each of :data:`CHECKPOINTS`. It exists so S9 can
    stop a delivery inside a window; raising from it is how a test kills a
    process at a named instant.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        resource: str,
        holder: str,
        registry: HandlerRegistry,
        checkpoint: Callable[[str], None] = _no_checkpoint,
    ) -> None:
        if not resource or not holder:
            raise ValueError("an outbox writer names the lease resource and holder it writes under")
        self._connection = connection
        self._resource = resource
        self._holder = holder
        self._registry = registry
        self._checkpoint = checkpoint

    # -- enqueue ----------------------------------------------------------

    def enqueue(
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
        """Write one pending outbox row.

        The row is written under *epoch* so that it has an owner from the
        instant it exists: a row enqueued with no ``writer_epoch`` would satisfy
        :data:`UNOWNED_OUTBOX_QUERY` the moment it was committed, which is the
        state the recovery criterion forbids.

        A duplicate ``message_id`` is refused by the primary key rather than
        collapsed here. Re-enqueueing the same *dedup key* under a **new**
        message id is legal and expected -- a sender killed after committing a
        row may not know it committed -- and is what makes the effect-level
        dedup in :meth:`attempt` the thing that carries exactly-once.
        """

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO outbox (message_id, run_id, recipient, payload,
                                    dedup_key, status, retry_count, writer_epoch,
                                    enqueued_at_ms)
                VALUES (:message_id, :run_id, :recipient, :payload,
                        :dedup_key, 'pending', 0, :epoch, :now_ms)
                """,
                {
                    "message_id": message_id,
                    "run_id": run_id,
                    "recipient": recipient,
                    "payload": payload,
                    "dedup_key": dedup_key,
                    "epoch": epoch,
                    "now_ms": now_ms,
                },
            )
        return self.load(message_id)

    # -- reading ----------------------------------------------------------

    def load(self, message_id: str) -> OutboxMessage:
        row = self._one(_LOAD_QUERY, {"message_id": message_id})
        if row is None:
            raise KeyError(f"no outbox row {message_id!r}")
        return OutboxMessage.from_row(row)

    def due(self, now_ms: int) -> Sequence[OutboxMessage]:
        """Everything enqueued and not yet acked, oldest first.

        A *delivered* message with no ack is due again: that is the resend, and
        it is the correct answer to a lost ack. No interval, backoff or
        visibility timeout is applied -- see this module's docstring on
        ``Q-0003``.
        """

        return tuple(
            OutboxMessage.from_row(row) for row in self._all(_DUE_QUERY, {"now_ms": now_ms})
        )

    def unowned(self, now_ms: int) -> Sequence[str]:
        """Unfinished rows with no live owner. The recovery criterion, as a read."""

        rows = self._all(
            UNOWNED_OUTBOX_QUERY, {"resource": self._resource, "now_ms": now_ms}
        )
        return tuple(str(row["message_id"]) for row in rows)

    # -- the delivery attempt ---------------------------------------------

    def attempt(self, message_id: str, *, now_ms: int, epoch: int) -> AttemptOutcome:
        """One delivery attempt, with the kill windows where they actually are.

        The ordering below is the whole point of the method, so it is spelled
        out rather than left to be reconstructed from the code:

        1. **The durable write.** ``retry_count`` is incremented and committed
           *before* anything is attempted. A kill here loses no message: the row
           is still due. Counting after a successful delivery instead would make
           the count a record of successes, and ``ACCEPTANCE.md`` section 2 asks
           it to survive *"the recipient unavailable across several retry
           attempts"* -- attempts that by construction never succeed.
        2. **The action row.** The effect's intent becomes durable, in status
           ``'pending'``, and is committed before the effect is attempted. A
           kill between here and the effect leaves a pending action that
           recovery replays; a kill *after* the effect and before its result is
           recorded leaves the same row, and it is replayed the same way. That
           the two cases are indistinguishable to us is not a defect being
           tolerated -- it is the limit ``ACCEPTANCE.md`` section 2 names, and
           the declared mechanism is what makes the replay safe instead of
           doubling the effect.
        3. **The effect**, through the handler, keyed so the destination can
           refuse a duplicate.
        4. **The record**, and then the outbox row's transition to
           ``'delivered'``.

        The ack is deliberately *not* here. Delivery and acknowledgement are
        separate events with a kill window between them, and collapsing them
        would erase the window the gate injects into.
        """

        message = self.load(message_id)
        if message.status == "acked":
            raise ValueError(
                f"{message_id!r} is already acked; an acked message is not resent"
            )
        handler = self._registry.for_recipient(message.recipient)

        if handler.exactly_once_mechanism == "human_gate":
            # D-0004: neither mechanism is achievable, so the action is recorded
            # and parked. It is never advanced by this module -- an automatic
            # recovery path here is exactly the papering-over Issue #14 forbids.
            idempotency_key = handler.idempotency_key(message)
            action_id = self._ensure_pending_action(message, handler, idempotency_key, now_ms)
            raise HumanGateRequired(
                f"{type(handler).__name__} declares 'human_gate': neither a "
                f"destination-supported idempotency key nor a transactional "
                f"commit is achievable for {message.recipient!r}, so action "
                f"{action_id} stays pending until a human moves it (D-0004)"
            )

        self._checkpoint(CHECKPOINT_BEFORE_DURABLE_WRITE)

        # (1) the durable write, fenced.
        self._fenced(
            "UPDATE outbox SET retry_count = retry_count + 1 "
            " WHERE message_id = :message_id AND status <> 'acked'",
            {"message_id": message_id},
            now_ms=now_ms,
            epoch=epoch,
            message=message,
            handler=handler,
            what="increment the retry count",
        )
        retry_count = int(self.load(message_id).retry_count)

        # (2) the effect's intent, durable and committed before the effect.
        idempotency_key = handler.idempotency_key(message)
        action_id, already_applied, prior_result = self._ensure_pending_action(
            message, handler, idempotency_key, now_ms, with_state=True
        )

        self._checkpoint(CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT)

        # (3) the effect itself -- attempted every time, including when our own
        # action row already says 'applied'.
        #
        # Short-circuiting on that row was the obvious optimisation and it is
        # the wrong call twice over. It would make our record the thing that
        # decides a duplicate, which is the *"asserts exactly-once for an
        # external effect using only our own rows"* evidence ACCEPTANCE.md
        # section 2 refuses; and it would break the resend, because a message
        # whose ack was lost would stop being offered to the destination and so
        # could never be acked. Calling through and letting the destination
        # refuse the duplicate is what at-least-once delivery with an
        # exactly-once effect actually looks like.
        try:
            receipt = handler.apply(message, idempotency_key)
        except DestinationRefusal:
            # The destination will not carry the effect. The action row stays
            # pending and the message stays due; recording it applied here would
            # be the "absence of a visible duplicate" evidence item 4 rejects.
            raise

        self._checkpoint(CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD)

        # (4) the result, then the outbox transition. S5's
        # ``action_apply_is_set_once`` trigger would abort on a second write, so
        # an already-applied action keeps the result it was recorded with.
        receipt_ref = prior_result
        if not already_applied:
            receipt_ref = receipt.receipt_ref if receipt is not None else None
            with self._connection:
                self._connection.execute(
                    """
                    UPDATE action
                       SET status = 'applied', applied_at_ms = :now_ms, result = :result
                     WHERE action_id = :action_id AND status = 'pending'
                    """,
                    {"action_id": action_id, "now_ms": now_ms, "result": receipt_ref},
                )
        self._mark_delivered(message_id, now_ms=now_ms, epoch=epoch,
                             message=message, handler=handler)
        self._checkpoint(CHECKPOINT_DELIVERED_BEFORE_ACK)

        return AttemptOutcome(
            message_id=message_id,
            retry_count=retry_count,
            deduplicated=bool(receipt is not None and receipt.deduplicated),
            action_id=action_id,
            idempotency_key=idempotency_key,
            exactly_once_mechanism=handler.exactly_once_mechanism,
            receipt_ref=receipt_ref,
        )

    # -- ack ---------------------------------------------------------------

    def record_ack(self, message_id: str, *, now_ms: int) -> AckOutcome:
        """Record the ack, idempotently.

        The first ack moves the row. Every later one -- duplicated in flight,
        delivered after the sender restarted, or replayed against an already
        acked message -- changes nothing and is **not** an error: the criterion
        is that a duplicate or late ack *changes nothing*, and raising would
        make a harmless duplicate into a failure the caller has to special-case.

        Deliberately unfenced. An ack is the recipient telling us what it
        already did; refusing to *record* that because our own lease moved on
        would turn a delivered message back into an undelivered one and cause a
        resend of an effect that is already present. The fence protects writes
        that drive effects, and this one drives none -- S5's
        ``outbox_ack_is_set_once`` trigger is what keeps it single-valued.
        """

        message = self.load(message_id)
        if message.acked_at_ms is not None:
            return AckOutcome(
                message_id=message_id,
                recorded=False,
                acked_at_ms=int(message.acked_at_ms),
                clock_clamped=False,
            )
        if message.delivered_at_ms is None:
            raise ValueError(
                f"{message_id!r} has not been delivered; an ack for an "
                "undelivered message is evidence of a lost delivery record, "
                "not of a delivery"
            )

        delivered_at_ms = int(message.delivered_at_ms)
        acked_at_ms = max(now_ms, delivered_at_ms)

        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE outbox
                   SET status = 'acked', acked_at_ms = :acked_at_ms
                 WHERE message_id = :message_id AND acked_at_ms IS NULL
                """,
                {"message_id": message_id, "acked_at_ms": acked_at_ms},
            )
            recorded = cursor.rowcount == 1

        if not recorded:
            # Another writer acked between the read and the write. Same answer
            # as a duplicate ack, which it is.
            settled = self.load(message_id)
            return AckOutcome(
                message_id=message_id,
                recorded=False,
                acked_at_ms=int(settled.acked_at_ms or acked_at_ms),
                clock_clamped=False,
            )

        return AckOutcome(
            message_id=message_id,
            recorded=True,
            acked_at_ms=acked_at_ms,
            clock_clamped=acked_at_ms != now_ms,
        )

    # -- recovery ----------------------------------------------------------

    def recover(self, *, now_ms: int, epoch: int) -> RecoveryReport:
        """Give every unfinished row a live owner, or report that it has none.

        *"No outbox row can remain in a state with no owner after recovery."*
        The rows a crash leaves behind are owned by an epoch that died with the
        process that held it, so recovery re-stamps them with the recovering
        holder's live epoch -- fenced, so a recovering process whose own lease
        is not live adopts nothing rather than adopting everything.

        The re-stamp is deliberately not conditional on the old epoch. Adopting
        only rows whose previous owner is provably dead would leave rows written
        by an epoch whose lease row was itself lost, which is the state the
        criterion is about.
        """

        candidates = self.unowned(now_ms)
        adopted = []
        for message_id in candidates:
            with self._connection:
                cursor = self._connection.execute(
                    """
                    UPDATE outbox
                       SET writer_epoch = :epoch
                     WHERE message_id = :message_id
                       AND status <> 'acked'
                       AND EXISTS (SELECT 1
                                     FROM lease
                                    WHERE resource      = :resource
                                      AND holder        = :holder
                                      AND epoch         = :epoch
                                      AND expires_at_ms > :now_ms)
                    """,
                    {
                        "message_id": message_id,
                        "epoch": epoch,
                        "resource": self._resource,
                        "holder": self._holder,
                        "now_ms": now_ms,
                    },
                )
                if cursor.rowcount == 1:
                    adopted.append(message_id)

        return RecoveryReport(adopted=tuple(adopted), still_unowned=self.unowned(now_ms))

    # -- internals ---------------------------------------------------------

    def _ensure_pending_action(
        self,
        message: OutboxMessage,
        handler: ActionHandler,
        idempotency_key: str,
        now_ms: int,
        *,
        with_state: bool = False,
    ):
        """Make the effect's intent durable, or find the record that already is.

        The insert is allowed to lose to ``action_one_effect_per_key``. Losing
        is the dedup: it means this exact effect already has a record, either
        applied (a duplicate delivery, and no second effect happens) or pending
        (a previous attempt died, and this one resumes it under the same key).
        Asking first and inserting second would leave the window between the two
        statements, which is the shape of race item 4 exists to rule out.
        """

        action_id = f"act-{idempotency_key}"
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO action (action_id, run_id, kind, idempotency_key,
                                        exactly_once_mechanism, status,
                                        writer_epoch, created_at_ms)
                    VALUES (:action_id, :run_id, :kind, :idempotency_key,
                            :mechanism, 'pending', :epoch, :now_ms)
                    """,
                    {
                        "action_id": action_id,
                        "run_id": message.run_id,
                        "kind": handler.action_kind,
                        "idempotency_key": idempotency_key,
                        "mechanism": handler.exactly_once_mechanism,
                        "epoch": message.writer_epoch,
                        "now_ms": now_ms,
                    },
                )
            existing_status, existing_result, existing_id = "pending", None, action_id
        except sqlite3.IntegrityError:
            row = self._one(
                "SELECT action_id, status, result FROM action "
                " WHERE idempotency_key = :key AND status <> 'refused'",
                {"key": idempotency_key},
            )
            if row is None:
                # The unique index did not cause this, so the row is malformed
                # rather than duplicated and swallowing it would hide it.
                raise
            existing_id = str(row["action_id"])
            existing_status = str(row["status"])
            existing_result = None if row["result"] is None else str(row["result"])

        if not with_state:
            return existing_id
        return existing_id, existing_status == "applied", existing_result

    def _mark_delivered(
        self,
        message_id: str,
        *,
        now_ms: int,
        epoch: int,
        message: OutboxMessage,
        handler: ActionHandler,
    ) -> None:
        """Move the row to ``'delivered'`` once, fenced.

        Idempotent by predicate rather than by trigger-catching: a resend of an
        already delivered message must leave the original delivery instant
        alone, and S5's ``outbox_delivery_is_set_once`` would abort the whole
        transaction if we tried to rewrite it.
        """

        self._fenced(
            "UPDATE outbox SET status = 'delivered', delivered_at_ms = :delivered_at_ms "
            " WHERE message_id = :message_id AND delivered_at_ms IS NULL",
            {
                "message_id": message_id,
                # An enqueue instant later than the delivery instant is the
                # backward clock skew case; S5's CHECK refuses the row, and the
                # delivery is real either way.
                "delivered_at_ms": max(now_ms, int(message.enqueued_at_ms)),
            },
            now_ms=now_ms,
            epoch=epoch,
            message=message,
            handler=handler,
            what="record the delivery",
            allow_no_row=True,
        )

    def _fenced(
        self,
        statement: str,
        params: Mapping[str, object],
        *,
        now_ms: int,
        epoch: int,
        message: OutboxMessage,
        handler: ActionHandler,
        what: str,
        allow_no_row: bool = False,
    ) -> None:
        """Run *statement* with the fence appended, refusing a stale writer.

        *allow_no_row* distinguishes "the fence rejected me" from "the predicate
        was already satisfied". The two are indistinguishable from ``rowcount``
        alone, so when zero rows change and the caller allows it, the fence is
        re-read on its own: if it is live, nothing was refused and the statement
        was simply a no-op.
        """

        bound = dict(params)
        bound.update(
            epoch=epoch, resource=self._resource, holder=self._holder, now_ms=now_ms
        )
        with self._connection:
            cursor = self._connection.execute(statement + _FENCE_PREDICATE, bound)
            changed = cursor.rowcount

        if changed >= 1:
            return
        if allow_no_row and self._fence_is_live(epoch=epoch, now_ms=now_ms):
            return

        reason = (
            f"refused to {what} for {message.message_id!r}: epoch {epoch} is not "
            f"a live lease on {self._resource!r} held by {self._holder!r} at "
            f"{now_ms}"
        )
        self._record_refusal(message, handler, reason, now_ms=now_ms, epoch=epoch)
        raise StaleWriterRefused(reason)

    def _fence_is_live(self, *, epoch: int, now_ms: int) -> bool:
        row = self._one(
            "SELECT 1 AS live FROM lease "
            " WHERE resource = :resource AND holder = :holder "
            "   AND epoch = :epoch AND expires_at_ms > :now_ms",
            {
                "resource": self._resource,
                "holder": self._holder,
                "epoch": epoch,
                "now_ms": now_ms,
            },
        )
        return row is not None

    def _record_refusal(
        self,
        message: OutboxMessage,
        handler: ActionHandler,
        reason: str,
        *,
        now_ms: int,
        epoch: int,
    ) -> None:
        """Persist the rejection. ``ACCEPTANCE.md`` section 2 requires it durable.

        A refused row is excluded from ``action_one_effect_per_key``, so a
        writer that keeps returning is recorded every time it is turned away
        rather than having its first refusal stand in for the rest. The action
        id carries the epoch and the attempt instant for the same reason.
        """

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO action (action_id, run_id, kind, idempotency_key,
                                    exactly_once_mechanism, status,
                                    refusal_reason, writer_epoch, created_at_ms)
                VALUES (:action_id, :run_id, :kind, :idempotency_key,
                        :mechanism, 'refused', :reason, :epoch, :now_ms)
                """,
                {
                    "action_id": f"refused-{message.message_id}-{epoch}-{now_ms}",
                    "run_id": message.run_id,
                    "kind": handler.action_kind,
                    "idempotency_key": handler.idempotency_key(message),
                    "mechanism": handler.exactly_once_mechanism,
                    "reason": reason,
                    "epoch": epoch,
                    "now_ms": now_ms,
                },
            )

    def _one(self, query: str, params: Mapping[str, object]):
        rows = self._all(query, params)
        return rows[0] if rows else None

    def _all(self, query: str, params: Mapping[str, object]):
        cursor = self._connection.execute(query, params)
        try:
            columns = [column[0] for column in cursor.description]
            return tuple(dict(zip(columns, row)) for row in cursor.fetchall())
        finally:
            cursor.close()
