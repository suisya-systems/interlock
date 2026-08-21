r"""The event spine: one append transaction, and a drain that is per consumer.

``#64`` asks for a single spine --- a CI outcome is written once and every
consumer reads it from the same table --- and the design review found the hole
in that sentence: **"undrained" has no meaning until it is defined per
consumer.** With one ``drained_at`` column on ``event``, the first consumer to
finish marks the row drained and hides every other consumer's backlog. That is
``tools/relay_scan.py``'s documented failure reached through a different
mechanism: 134 terminal events accumulating undelivered for twenty days, with a
scan that looked clean the whole time because a silent no-op leaves nothing
behind. So consumption is **fanned out at append time**, one
``event_consumption`` row per (event, subscribed consumer), and every drain
quantity in this module takes a ``consumer_id``. There is no global
``undrained()`` here and there deliberately never will be --- see
:func:`backlog_depth`, :func:`drain_frontier`, :func:`head_of_line_age_ms`.

**The append is one transaction, and that is the property, not the tidiness.**
:func:`append_event` implements ``docs/production-schema.md`` section 5.4 and
``D-0030``: insert the event, ``SELECT`` the subscribers *inside the same
transaction* so a concurrent subscription change cannot interleave between the
fan-out decision and the fan-out write, insert one ``pending`` consumption row
per subscriber, insert the ``outbox`` row for each ``delivery`` subscriber and
link it into that consumption, then run the caller's typed side table insert.
The whole thing commits or none of it does. There is therefore no window in
which an event exists with no delivery record --- which is exactly the window
v1's best-effort push and relay scan existed to cover, two delivery paths
because neither alone was transactional with the fact. Here the enqueue *is*
part of the append, so the outbox is the only delivery path and the reconcile
pass is a backstop over the same rows rather than a second path.

Two exactly-once steps, each naming its ``ACCEPTANCE.md`` section 2 mechanism:
fact to enqueued is ``transactional_with_record`` (this module); enqueued to
delivered stays the outbox's ``destination_idempotency_key`` and is not ours.

**Duplicates are a no-op, not an error.** A producer that re-polls, restarts
mid-append or re-fetches the same page presents the same ``dedup_key``. That is
the *normal* recovery path, so it returns ``AppendedEvent(duplicate=True)``
rather than raising: an at-least-once producer over an idempotent append is the
whole point of the identity-uniqueness rule in section 4.2, which is what lets
several producers share one spine with no single-writer lease over the table.

**Every settle is fenced.** ``ACCEPTANCE.md`` section 2 rules out the
check-then-write shape outright: the epoch is validated *inside* the ``UPDATE``,
in the single-statement form ``docs/lease-fencing.md`` specifies, and a zero-row
result is :class:`StaleConsumerRefused` rather than an early ``return``. A
refusal that is reported as "nothing to do" is the twenty-day failure again.

**A skip must leave evidence.** ``skipped`` exists so a subscription a consumer
decides is inapplicable to a particular event settles explicitly instead of
sitting ``pending`` forever and being counted as backlog. Section 5.3 requires
the reason to travel in an event rather than in ``last_error`` (a skip is not an
error, and the ``CHECK`` on that column enforces the distinction), so
:func:`mark_skipped` appends ``consumption_skipped`` **in the same transaction**
as the settle. A ``skipped`` row with no such event is unreachable through this
module, which is what keeps a skip distinguishable from a consumer quietly
dropping work.

**This module implements the detection half of section 5.6, and only that
half.** Section 5.6's table is normative in both of its columns: each pass has
a detection ("what to look for") *and* an "On a hit" remedy -- raise a
``consumer_backlog`` incident and re-attempt the failed rows; re-attempt the
orphaned outbox rows. :func:`backlogged_consumers` and :func:`orphaned_outbox`
are the detection half and are pure ``SELECT``\ s that take a ``revision_id``
the caller resolved. The remedy half belongs to the reconcile-pass driver,
which **does not exist in this branch**: nothing in ``src/`` writes an
``incident`` row or re-attempts a delivery, so until that driver is written
these two functions have no caller in ``src/`` at all. That is a scope
boundary, not a claim that section 5.6 is satisfied here.

Splitting it this way is deliberate rather than merely convenient -- a
detector that acted would be writing under no lease and inflating the very
evidence an operator reads, and a pure ``SELECT`` can be run anywhere at any
frequency without deciding anything -- but the caller that owes the other half
is still owed. Neither function resolves a policy revision for itself, because
``D-0031``'s corollary makes an unbound ``policy_*`` read a defect and a
convenience default is how the binding goes missing.

Time is the caller's throughout. Every timestamp is an ``INTEGER`` of epoch
milliseconds supplied as an argument; nothing here calls a clock and no column
this module writes has a ``DEFAULT``. ``ACCEPTANCE.md`` section 2 injects clock
skew across expiry boundaries, and a value the database chose for itself makes
that case untestable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from .policy import resolve_tolerance_ms
from .schema import ControlPlaneRefusal
from .txn import transaction

__all__ = [
    "BACKLOG_INCIDENT_CLASS",
    "CONSUMER_FENCE_SQL",
    "DEGRADED_ORPHANED_OUTBOX_SQL",
    "EVENT_TYPES",
    "ORPHANED_OUTBOX_SQL",
    "OUTBOX_DELIVERY_INCIDENT_CLASS",
    "AppendedEvent",
    "EventSpineRefusal",
    "EventSpineUsageError",
    "StaleConsumerRefused",
    "append_event",
    "backlog_depth",
    "backlogged_consumers",
    "drain_frontier",
    "head_of_line_age_ms",
    "mark_consumed",
    "mark_failed",
    "mark_skipped",
    "orphaned_outbox",
    "register_consumer",
    "subscribe",
    "undrained",
    "unsubscribe",
]


#: The event vocabulary **this implementation produces**. The DDL leaves
#: ``event.event_type`` open text on purpose -- a closed ``CHECK`` would make
#: every new producer a schema change, and section 4.2's writer rule for the
#: spine is identity uniqueness rather than a controlled vocabulary. So this is
#: not a schema constraint and nothing here validates against it; it is the
#: single place a reader can find out what G3/G4/G6 actually emit, and what a
#: subscription is worth subscribing to.
EVENT_TYPES = frozenset(
    {
        "ci_observed",
        "pr_head_updated",
        "pr_merged",
        "pr_closed",
        "pr_reopened",
        "worker_escalation_raised",
        "gate_expired",
        "gate_closed",
        "consumption_skipped",
        "watcher_heartbeat_refused",
    }
)

#: The fence every consumption settle carries, in the single-statement shape
#: ``docs/lease-fencing.md`` requires. It resolves the consumer's own
#: ``lease_resource`` rather than taking a resource argument, because the
#: binding of a consumer to the lease that authorises its settles is state, not
#: something a caller should be able to re-point per call.
#:
#: The holder is validated *transitively*: the ``lease`` trigger makes an epoch
#: strictly increasing and makes a change of holder raise it, so an epoch that
#: still matches the live row can only belong to the party that took it. That is
#: also why an epoch, and not an expiry, is what a write validates -- under
#: clock skew two claimants can overlap in true time, but write authority
#: cannot, because a takeover invalidates the old token.
CONSUMER_FENCE_SQL = (
    "EXISTS (SELECT 1 FROM lease\n"
    "                    JOIN consumer ON consumer.lease_resource = lease.resource\n"
    "                   WHERE consumer.consumer_id = :consumer_id\n"
    "                     AND lease.epoch = :writer_epoch\n"
    "                     AND lease.expires_at_ms > :now_ms)"
)

#: Consumers registered with ``backfill=True`` in the transaction that is open
#: right now, keyed by ``id(connection)``. Transaction-local and nothing else:
#: section 5.4's back-fill covers "a subscription added in the same
#: transaction", so :func:`subscribe` has to know whether it is running inside
#: its consumer's registration. The entry is dropped the moment either entry
#: point observes the connection is no longer in a transaction, so a stale key
#: cannot outlive the transaction that made it -- which also disposes of the
#: identity-reuse hazard of keying by ``id()``, since a fresh connection is
#: never mid-transaction at the entry to either function.
_REGISTERING_WITH_BACKFILL: dict[int, set[str]] = {}


class EventSpineRefusal(ControlPlaneRefusal):
    """A spine operation was refused. Nothing was written past the refusal.

    In the ``ControlPlaneRefusal`` family because the answer is the same one
    ``R3`` gives for a database that cannot be verified: refuse, and leave the
    state exactly as it stood, rather than proceeding on a guess.
    """


class StaleConsumerRefused(EventSpineRefusal):
    """A consumption settle was refused: its fencing token was not live.

    Raised when the fenced ``UPDATE`` matched no row. The reachable causes are
    a ``writer_epoch`` that is not the live epoch of the consumer's
    ``lease_resource``, a lease that has expired at the caller's own clock, a
    consumption row that is already terminal (``consumed``/``skipped`` are not
    reopened), and a (consumer, event) pair that was never fanned out.

    It is a typed exception rather than a ``False`` on purpose: ``ACCEPTANCE.md``
    section 2 requires a stale writer to be *rejected, not merged*, and a
    rejection returned as a falsy value is one an ``if`` nobody wrote will
    swallow. :attr:`observed` carries the consumption row as it actually stood,
    so the refusal can be diagnosed without a second query racing the first.
    """

    def __init__(self, message: str, *, observed: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.observed = observed


class EventSpineUsageError(ValueError):
    """The caller used this module in a way that would break its guarantees.

    A programming error, not a runtime condition: a timestamp that is not an
    integer of epoch milliseconds, an empty identity, a payload that is not
    JSON. Raised before any statement runs, so a malformed call cannot land a
    half-append and then fail a ``CHECK`` from inside the database.
    """


class _DuplicateFact(Exception):
    """Internal: the ``dedup_key`` is already on the spine, so abandon the block.

    Raised inside the append transaction so that the context manager rolls back
    everything the block had written, which is what makes a re-append a genuine
    no-op rather than a partially applied one. Never escapes this module.
    """

    def __init__(self, event_id: str) -> None:
        super().__init__(event_id)
        self.event_id = event_id


@dataclass(frozen=True)
class AppendedEvent:
    """What one append did, as the append itself saw it.

    :attr:`seq` is ``None`` if and only if :attr:`duplicate` is ``True``: a
    duplicate append assigned no sequence number because it wrote no row.
    :attr:`event_id` then names the event that *does* hold the fact, which may
    differ from the id the caller offered -- a producer that regenerates an id
    per poll still collides on the ``dedup_key``, and the useful answer is where
    the fact already lives, not the id that was refused.

    :attr:`consumptions` and :attr:`messages` are the fan-out made visible: the
    consumers given a ``pending`` row, and the outbox rows enqueued for the
    ``delivery`` ones among them. Both are empty for a duplicate.
    """

    seq: int | None
    event_id: str
    duplicate: bool
    consumptions: tuple[str, ...]
    messages: tuple[str, ...]


# --------------------------------------------------------------------------
# argument validation -- refuse before writing, never inside a half-append
# --------------------------------------------------------------------------


def _require_identifier(field: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EventSpineUsageError(f"{field} must be a non-empty string, got {value!r}")


def _require_epoch_ms(field: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise EventSpineUsageError(
            f"{field} must be an int of epoch milliseconds, got {value!r}"
        )


def _require_positive_epoch(field: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EventSpineUsageError(f"{field} must be a positive int, got {value!r}")


def _require_json(field: str, value: str) -> str:
    try:
        json.loads(value)
    except (TypeError, ValueError) as error:
        raise EventSpineUsageError(
            f"{field} must be a JSON document; the payload column has a "
            f"json_valid CHECK and would refuse this ({error})"
        ) from error
    return value


def _forget_stale_backfill_scope(connection: sqlite3.Connection) -> None:
    """Drop any back-fill scope left by a transaction that has already ended."""

    if not connection.in_transaction:
        _REGISTERING_WITH_BACKFILL.pop(id(connection), None)


# --------------------------------------------------------------------------
# append
# --------------------------------------------------------------------------


def _subscribers(
    connection: sqlite3.Connection, event_type: str
) -> tuple[tuple[str, str, str | None], ...]:
    """The (consumer_id, kind, recipient) triples subscribed to *event_type*.

    Read inside the append transaction, never before it. A subscription with
    ``removed_at_ms`` set is not a subscription and a consumer with
    ``retired_at_ms`` set is not a consumer: both are kept as rows because the
    fan-out history has to stay explicable, and both are excluded here because
    fanning out to them would manufacture a backlog nobody will ever drain.
    """

    rows = connection.execute(
        """
        SELECT s.consumer_id, c.kind, s.recipient
          FROM consumer_subscription s
          JOIN consumer c ON c.consumer_id = s.consumer_id
         WHERE s.event_type = :event_type
           AND s.removed_at_ms IS NULL
           AND c.retired_at_ms IS NULL
         ORDER BY s.consumer_id
        """,
        {"event_type": event_type},
    ).fetchall()
    return tuple((str(row[0]), str(row[1]), row[2]) for row in rows)


def _fan_out(
    connection: sqlite3.Connection,
    *,
    seq: int,
    event_id: str,
    event_type: str,
    run_id: str | None,
    payload: str,
    created_at_ms: int,
    delivery_payload: Callable[[str, str], str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Insert one consumption row per subscriber, plus the delivery outbox rows.

    The outbox row's ``dedup_key`` and ``message_id`` are the same string,
    ``event/<event_id>/<consumer_id>``, which section 5.4 fixes. Making the
    primary key carry the identity is what makes the enqueue idempotent under a
    retry of the whole append: a second attempt collides on the key rather than
    enqueuing a second copy of one delivery. ``outbox.dedup_key`` stays
    deliberately non-unique for hand-enqueued messages; uniqueness here comes
    from the message id, not from that column.
    """

    consumptions: list[str] = []
    messages: list[str] = []
    for consumer_id, kind, recipient in _subscribers(connection, event_type):
        message_id: str | None = None
        if kind == "delivery":
            message_id = f"event/{event_id}/{consumer_id}"
            body = payload
            if delivery_payload is not None:
                body = _require_json(
                    "delivery_payload(...)", delivery_payload(consumer_id, recipient or "")
                )
            connection.execute(
                """
                INSERT INTO outbox (message_id, run_id, recipient, payload, dedup_key,
                                    status, retry_count, enqueued_at_ms)
                VALUES (:message_id, :run_id, :recipient, :payload, :dedup_key,
                        'pending', 0, :enqueued_at_ms)
                """,
                {
                    "message_id": message_id,
                    "run_id": run_id,
                    "recipient": recipient,
                    "payload": body,
                    "dedup_key": message_id,
                    "enqueued_at_ms": created_at_ms,
                },
            )
            messages.append(message_id)
        connection.execute(
            """
            INSERT INTO event_consumption (consumer_id, event_seq, status, attempt_count,
                                           message_id, created_at_ms)
            VALUES (:consumer_id, :event_seq, 'pending', 0, :message_id, :created_at_ms)
            """,
            {
                "consumer_id": consumer_id,
                "event_seq": seq,
                "message_id": message_id,
                "created_at_ms": created_at_ms,
            },
        )
        consumptions.append(consumer_id)
    return tuple(consumptions), tuple(messages)


def _append_within_transaction(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    event_type: str,
    subject_kind: str,
    subject_id: str,
    dedup_key: str,
    producer: str,
    occurred_at_ms: int,
    ingested_at_ms: int,
    run_id: str | None,
    producer_epoch: int | None,
    payload: str,
    side_effect: Callable[[sqlite3.Connection, int], None] | None,
    delivery_payload: Callable[[str, str], str] | None,
) -> AppendedEvent:
    """Steps 1-5 of section 5.4, with the transaction assumed to be open.

    Split out from :func:`append_event` so that :func:`mark_skipped` can put a
    settle and this append in **one** transaction. The duplicate check is a
    ``SELECT`` rather than a caught ``IntegrityError`` because the transaction
    already holds the write lock (``BEGIN IMMEDIATE``), so no other writer can
    interleave between the read and the insert -- and because a ``UNIQUE``
    violation on ``event_id`` with a *different* ``dedup_key`` is a producer bug
    that must surface, not a duplicate fact that must be swallowed.
    """

    existing = connection.execute(
        "SELECT event_id FROM event WHERE dedup_key = :dedup_key", {"dedup_key": dedup_key}
    ).fetchone()
    if existing is not None:
        raise _DuplicateFact(str(existing[0]))

    cursor = connection.execute(
        """
        INSERT INTO event (event_id, event_type, subject_kind, subject_id, run_id, payload,
                           producer, producer_epoch, dedup_key, occurred_at_ms, ingested_at_ms)
        VALUES (:event_id, :event_type, :subject_kind, :subject_id, :run_id, :payload,
                :producer, :producer_epoch, :dedup_key, :occurred_at_ms, :ingested_at_ms)
        """,
        {
            "event_id": event_id,
            "event_type": event_type,
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "run_id": run_id,
            "payload": payload,
            "producer": producer,
            "producer_epoch": producer_epoch,
            "dedup_key": dedup_key,
            "occurred_at_ms": occurred_at_ms,
            "ingested_at_ms": ingested_at_ms,
        },
    )
    seq = int(cursor.lastrowid or 0)

    consumptions, messages = _fan_out(
        connection,
        seq=seq,
        event_id=event_id,
        event_type=event_type,
        run_id=run_id,
        payload=payload,
        created_at_ms=ingested_at_ms,
        delivery_payload=delivery_payload,
    )

    # Last, so that a side table which refuses the fact takes the event down
    # with it: the typed row and the spine row are one fact recorded twice, and
    # a spine that carries a ci_observation nobody could insert is a projection
    # that silently disagrees with its source.
    if side_effect is not None:
        side_effect(connection, seq)

    return AppendedEvent(
        seq=seq,
        event_id=event_id,
        duplicate=False,
        consumptions=consumptions,
        messages=messages,
    )


def append_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    event_type: str,
    subject_kind: str,
    subject_id: str,
    dedup_key: str,
    producer: str,
    occurred_at_ms: int,
    ingested_at_ms: int,
    run_id: str | None = None,
    producer_epoch: int | None = None,
    payload: str | None = None,
    side_effect: Callable[[sqlite3.Connection, int], None] | None = None,
    delivery_payload: Callable[[str, str], str] | None = None,
) -> AppendedEvent:
    """Append one event, fan it out, and enqueue its deliveries. One transaction.

    Section 5.4 in order: the event, the subscriber ``SELECT`` inside this same
    transaction, a ``pending`` ``event_consumption`` row per subscriber, an
    ``outbox`` row linked into that consumption for every ``delivery``
    subscriber, and finally *side_effect* for the typed side table (``#64``'s
    ``ci_observation``, ``#65``'s escalation row) with the ``seq`` it must point
    at. Anything that raises anywhere in there leaves no event, no consumption
    and no outbox row --- the all-or-nothing is the point, not the tidiness.

    *occurred_at_ms* is the source clock, as the provider reports the observed
    thing happening; *ingested_at_ms* is ours, and is also what the consumption
    and outbox rows are stamped with. ``time-base-policy.md`` section 2 decides
    which of the two each tolerance is measured against, and conflating them is
    how a provider's clock ends up deciding whether we are late.

    *delivery_payload* renders each delivery's body from ``(consumer_id,
    recipient)``; without it the event's own payload is delivered verbatim. It
    exists because one event legitimately reaches two recipients in two shapes,
    and re-appending the event per recipient would put two rows on the spine for
    one fact.

    A ``dedup_key`` already on the spine returns ``AppendedEvent(seq=None,
    duplicate=True)`` and writes nothing --- no second consumption row for
    anyone. That is an idempotent no-op rather than an error because it is the
    ordinary shape of a producer that re-polls or restarts mid-append.

    :raises EventSpineUsageError: for a malformed argument, before any write.
    :raises sqlite3.IntegrityError: if *event_id* collides while *dedup_key*
        does not, which is a producer reusing an identity for a second fact.
    """

    for field, value in (
        ("event_id", event_id),
        ("event_type", event_type),
        ("subject_kind", subject_kind),
        ("subject_id", subject_id),
        ("dedup_key", dedup_key),
        ("producer", producer),
    ):
        _require_identifier(field, value)
    _require_epoch_ms("occurred_at_ms", occurred_at_ms)
    _require_epoch_ms("ingested_at_ms", ingested_at_ms)
    if producer_epoch is not None:
        _require_positive_epoch("producer_epoch", producer_epoch)
    body = _require_json("payload", payload) if payload is not None else "{}"

    try:
        with transaction(connection):
            return _append_within_transaction(
                connection,
                event_id=event_id,
                event_type=event_type,
                subject_kind=subject_kind,
                subject_id=subject_id,
                dedup_key=dedup_key,
                producer=producer,
                occurred_at_ms=occurred_at_ms,
                ingested_at_ms=ingested_at_ms,
                run_id=run_id,
                producer_epoch=producer_epoch,
                payload=body,
                side_effect=side_effect,
                delivery_payload=delivery_payload,
            )
    except _DuplicateFact as duplicate:
        return AppendedEvent(
            seq=None,
            event_id=duplicate.event_id,
            duplicate=True,
            consumptions=(),
            messages=(),
        )


# --------------------------------------------------------------------------
# registration and subscription
# --------------------------------------------------------------------------


def register_consumer(
    connection: sqlite3.Connection,
    *,
    consumer_id: str,
    kind: str,
    lease_resource: str,
    registered_at_ms: int,
    registered_from_seq: int,
    backfill: bool = False,
) -> None:
    """Register a consumer of the spine, optionally back-filling its history.

    *registered_from_seq* is the watershed: without *backfill* it is a recorded
    statement that this consumer is not responsible for anything at or below
    that sequence, and with it the same number decides what gets back-filled.
    Section 5.4's last bullet is the reason it is a number in a column rather
    than an omission --- "the decision is made once and is visible in the rows,
    never as a gap that someone later has to explain."

    A consumer registered *after* an append never receives that append. Late
    registration does not retroactively fan out, because the fan-out decision
    was taken and committed inside the append's own transaction.

    Back-fill covers events matching a subscription **added in the same
    transaction as this registration**, which is what section 5.4 specifies. In
    practice that is:

    .. code-block:: python

        with transaction(connection):
            register_consumer(connection, ..., registered_from_seq=0, backfill=True)
            subscribe(connection, consumer_id=..., event_type="ci_observed", added_at_ms=t)

    A subscription added in a *later* transaction gets no history, and that is
    the same rule stated from the other side: the decision belongs to one
    transaction, and one only.

    :raises EventSpineUsageError: for a malformed argument.
    :raises sqlite3.IntegrityError: if *consumer_id* is already registered. A
        second registration is not an idempotent retry --- it would silently
        redecide *registered_from_seq* and the back-fill that hangs off it.
    """

    _require_identifier("consumer_id", consumer_id)
    _require_identifier("lease_resource", lease_resource)
    if kind not in ("delivery", "compute"):
        raise EventSpineUsageError(
            f"kind must be 'delivery' or 'compute', got {kind!r}; the column has "
            "a CHECK and the two kinds differ in whether consumption IS a delivery"
        )
    _require_epoch_ms("registered_at_ms", registered_at_ms)
    if not isinstance(registered_from_seq, int) or isinstance(registered_from_seq, bool):
        raise EventSpineUsageError(
            f"registered_from_seq must be an int, got {registered_from_seq!r}"
        )
    if registered_from_seq < 0:
        raise EventSpineUsageError("registered_from_seq must not be negative")

    _forget_stale_backfill_scope(connection)
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO consumer (consumer_id, kind, lease_resource, registered_at_ms,
                                  registered_from_seq)
            VALUES (:consumer_id, :kind, :lease_resource, :registered_at_ms,
                    :registered_from_seq)
            """,
            {
                "consumer_id": consumer_id,
                "kind": kind,
                "lease_resource": lease_resource,
                "registered_at_ms": registered_at_ms,
                "registered_from_seq": registered_from_seq,
            },
        )
        if backfill:
            _REGISTERING_WITH_BACKFILL.setdefault(id(connection), set()).add(consumer_id)
            # Subscriptions cannot pre-exist a consumer (the FK forbids it), so
            # this only matters when the caller has joined an outer transaction
            # and subscribes below; the scope above is what carries the decision
            # across to subscribe() without leaving the transaction.
            _backfill(
                connection,
                consumer_id=consumer_id,
                event_types=tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT event_type FROM consumer_subscription "
                        "WHERE consumer_id = :consumer_id AND removed_at_ms IS NULL",
                        {"consumer_id": consumer_id},
                    ).fetchall()
                ),
                from_seq=registered_from_seq,
                created_at_ms=registered_at_ms,
            )
    _forget_stale_backfill_scope(connection)


def _backfill(
    connection: sqlite3.Connection,
    *,
    consumer_id: str,
    event_types: Iterable[str],
    from_seq: int,
    created_at_ms: int,
) -> None:
    """Insert ``pending`` rows for the history a late registration asked for.

    ``INSERT OR IGNORE`` because the same registration transaction may reach an
    event through two of its subscriptions; the primary key is (consumer_id,
    event_seq) and a consumption row is per event, not per subscription.

    No outbox row is written even for a ``delivery`` consumer. A back-fill is a
    request to *catch up on history*, and history that is materialised as fresh
    deliveries would re-send every past event to a recipient that has, by
    construction, already been told about them by whoever was subscribed at the
    time. The consumption row records the obligation; whether it is discharged
    by a resend or by :func:`mark_skipped` is the consumer's decision to make
    and to leave evidence of.
    """

    for event_type in event_types:
        connection.execute(
            """
            INSERT OR IGNORE INTO event_consumption
                (consumer_id, event_seq, status, attempt_count, created_at_ms)
            SELECT :consumer_id, e.seq, 'pending', 0, :created_at_ms
              FROM event e
             WHERE e.seq > :from_seq AND e.event_type = :event_type
            """,
            {
                "consumer_id": consumer_id,
                "created_at_ms": created_at_ms,
                "from_seq": from_seq,
                "event_type": event_type,
            },
        )


def subscribe(
    connection: sqlite3.Connection,
    *,
    consumer_id: str,
    event_type: str,
    recipient: str | None = None,
    added_at_ms: int,
) -> None:
    """Subscribe *consumer_id* to *event_type*, from the next append onward.

    *recipient* is required for a ``delivery`` consumer and forbidden for a
    ``compute`` one; the schema enforces that with a trigger rather than a
    ``CHECK`` because it is a cross-table invariant, and it refuses **here**
    rather than later inside the append transaction of the next matching event
    --- which would take that event down with it and hand the failure to a party
    who cannot fix it.

    If this call is running inside the transaction that registered *consumer_id*
    with ``backfill=True``, the events already on the spine above the consumer's
    ``registered_from_seq`` are back-filled as ``pending`` in the same
    transaction. Outside that transaction the subscription is forward-only.

    :raises EventSpineUsageError: for a malformed argument.
    :raises sqlite3.IntegrityError: from the recipient/kind trigger, or if this
        consumer already has a row for *event_type*.
    """

    _require_identifier("consumer_id", consumer_id)
    _require_identifier("event_type", event_type)
    if recipient is not None:
        _require_identifier("recipient", recipient)
    _require_epoch_ms("added_at_ms", added_at_ms)

    _forget_stale_backfill_scope(connection)
    backfilling = consumer_id in _REGISTERING_WITH_BACKFILL.get(id(connection), frozenset())
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO consumer_subscription (consumer_id, event_type, recipient, added_at_ms)
            VALUES (:consumer_id, :event_type, :recipient, :added_at_ms)
            """,
            {
                "consumer_id": consumer_id,
                "event_type": event_type,
                "recipient": recipient,
                "added_at_ms": added_at_ms,
            },
        )
        if backfilling:
            row = connection.execute(
                "SELECT registered_from_seq FROM consumer WHERE consumer_id = :consumer_id",
                {"consumer_id": consumer_id},
            ).fetchone()
            _backfill(
                connection,
                consumer_id=consumer_id,
                event_types=(event_type,),
                from_seq=int(row[0]),
                created_at_ms=added_at_ms,
            )
    _forget_stale_backfill_scope(connection)


def unsubscribe(
    connection: sqlite3.Connection,
    *,
    consumer_id: str,
    event_type: str,
    removed_at_ms: int,
) -> None:
    """Stop fanning *event_type* out to *consumer_id*, from the next append on.

    A mark, never a delete: the row is how a later reader explains why this
    consumer has consumption rows for events up to a point and none after it.
    Consumption rows already fanned out are untouched --- they are obligations
    that were taken on, and dropping them on unsubscribe would let a consumer
    make its own backlog disappear.

    :raises EventSpineUsageError: for a malformed argument or a subscription
        that is not there to remove --- a no-op unsubscribe would report success
        for a fan-out that is still happening.
    """

    _require_identifier("consumer_id", consumer_id)
    _require_identifier("event_type", event_type)
    _require_epoch_ms("removed_at_ms", removed_at_ms)

    with transaction(connection):
        cursor = connection.execute(
            """
            UPDATE consumer_subscription
               SET removed_at_ms = :removed_at_ms
             WHERE consumer_id = :consumer_id
               AND event_type = :event_type
               AND removed_at_ms IS NULL
            """,
            {
                "consumer_id": consumer_id,
                "event_type": event_type,
                "removed_at_ms": removed_at_ms,
            },
        )
        if cursor.rowcount == 0:
            raise EventSpineUsageError(
                f"{consumer_id!r} has no live subscription to {event_type!r} to remove"
            )


# --------------------------------------------------------------------------
# settling a consumption -- fenced, and never a silent no-op
# --------------------------------------------------------------------------


def _read_consumption(
    connection: sqlite3.Connection, *, consumer_id: str, event_seq: int
) -> Mapping[str, Any] | None:
    row = connection.execute(
        """
        SELECT consumer_id, event_seq, status, attempt_count, message_id, last_error,
               writer_epoch, created_at_ms, settled_at_ms
          FROM event_consumption
         WHERE consumer_id = :consumer_id AND event_seq = :event_seq
        """,
        {"consumer_id": consumer_id, "event_seq": event_seq},
    ).fetchone()
    if row is None:
        return None
    columns = (
        "consumer_id",
        "event_seq",
        "status",
        "attempt_count",
        "message_id",
        "last_error",
        "writer_epoch",
        "created_at_ms",
        "settled_at_ms",
    )
    return MappingProxyType(dict(zip(columns, row)))


def _settle(
    connection: sqlite3.Connection,
    *,
    sql: str,
    params: Mapping[str, Any],
    consumer_id: str,
    event_seq: int,
    writer_epoch: int,
    what: str,
) -> None:
    """Run one fenced settle statement, or raise :class:`StaleConsumerRefused`.

    The fence is part of the ``UPDATE``'s own predicate, so there is no instant
    between deciding the token is live and using it. A zero ``rowcount`` is the
    refusal; it is never returned as a value, because the caller that forgets to
    inspect a returned ``False`` is exactly how a stale writer's work gets
    merged instead of rejected.
    """

    cursor = connection.execute(sql, dict(params))
    if cursor.rowcount == 0:
        observed = _read_consumption(
            connection, consumer_id=consumer_id, event_seq=event_seq
        )
        raise StaleConsumerRefused(
            f"{what} refused for consumer {consumer_id!r} at event seq {event_seq}: "
            f"epoch {writer_epoch} is not the live epoch of the consumer's lease, "
            "the lease has expired at the caller's clock, or the consumption is "
            f"already settled (observed: {dict(observed) if observed else None})",
            observed=observed,
        )


def mark_consumed(
    connection: sqlite3.Connection,
    *,
    consumer_id: str,
    event_seq: int,
    writer_epoch: int,
    settled_at_ms: int,
) -> None:
    """Settle one consumption as ``consumed``, under the consumer's fence.

    Terminal: the schema's ``event_consumption_settled_is_terminal`` trigger
    refuses to reopen it, and this statement will not match it again either. A
    correction is a new event, not an edit --- the drain evidence is what the
    reconcile pass and the measurement harness are read out of, and evidence
    that can be rewritten is not evidence.

    ``last_error`` is cleared because ``consumed`` and a recorded error cannot
    both be true --- the ``CHECK`` states it as an equality --- and a retry that
    finally lands must not leave the failure that preceded it looking current.
    The durable trace of that failure is ``attempt_count``, which this raises.

    *settled_at_ms* is the caller's clock and doubles as the instant the lease's
    liveness is evaluated at, since there is no other clock in the call.

    :raises StaleConsumerRefused: if the fenced ``UPDATE`` matched no row.
    """

    _require_identifier("consumer_id", consumer_id)
    _require_positive_epoch("writer_epoch", writer_epoch)
    _require_epoch_ms("settled_at_ms", settled_at_ms)

    with transaction(connection):
        _settle(
            connection,
            sql=f"""
            UPDATE event_consumption
               SET status = 'consumed',
                   settled_at_ms = :settled_at_ms,
                   writer_epoch = :writer_epoch,
                   attempt_count = attempt_count + 1,
                   last_error = NULL
             WHERE consumer_id = :consumer_id
               AND event_seq = :event_seq
               AND status IN ('pending', 'failed')
               AND {CONSUMER_FENCE_SQL}
            """,
            params={
                "consumer_id": consumer_id,
                "event_seq": event_seq,
                "writer_epoch": writer_epoch,
                "settled_at_ms": settled_at_ms,
                "now_ms": settled_at_ms,
            },
            consumer_id=consumer_id,
            event_seq=event_seq,
            writer_epoch=writer_epoch,
            what="mark_consumed",
        )


def mark_failed(
    connection: sqlite3.Connection,
    *,
    consumer_id: str,
    event_seq: int,
    writer_epoch: int,
    last_error: str,
    now_ms: int,
) -> None:
    """Record a failed attempt on one consumption. Retryable, **not** terminal.

    ``failed`` is the durable trace of an attempt that did not land, which is
    what distinguishes a stalled consumer from a quiet one; it stays *undrained*
    and the reconcile pass re-attempts it. It is deliberately not a settle:
    ``settled_at_ms`` stays ``NULL`` and the row keeps counting against
    :func:`backlog_depth` and :func:`head_of_line_age_ms`, so a consumer cannot
    make its own backlog disappear by failing.

    :raises StaleConsumerRefused: if the fenced ``UPDATE`` matched no row.
    """

    _require_identifier("consumer_id", consumer_id)
    _require_identifier("last_error", last_error)
    _require_positive_epoch("writer_epoch", writer_epoch)
    _require_epoch_ms("now_ms", now_ms)

    with transaction(connection):
        _settle(
            connection,
            sql=f"""
            UPDATE event_consumption
               SET status = 'failed',
                   last_error = :last_error,
                   writer_epoch = :writer_epoch,
                   attempt_count = attempt_count + 1
             WHERE consumer_id = :consumer_id
               AND event_seq = :event_seq
               AND status IN ('pending', 'failed')
               AND {CONSUMER_FENCE_SQL}
            """,
            params={
                "consumer_id": consumer_id,
                "event_seq": event_seq,
                "writer_epoch": writer_epoch,
                "last_error": last_error,
                "now_ms": now_ms,
            },
            consumer_id=consumer_id,
            event_seq=event_seq,
            writer_epoch=writer_epoch,
            what="mark_failed",
        )


def mark_skipped(
    connection: sqlite3.Connection,
    *,
    consumer_id: str,
    event_seq: int,
    writer_epoch: int,
    reason: str,
    settled_at_ms: int,
    event_id: str,
    ingested_at_ms: int,
) -> AppendedEvent:
    """Settle one consumption as ``skipped`` and append its audit event. Atomic.

    Section 5.3: a skip must append ``consumption_skipped`` **in the same
    transaction** as the settle, because a ``skipped`` row with no recorded
    reason is indistinguishable from a consumer quietly dropping work. The
    reason travels in the appended event's payload and not in ``last_error`` ---
    a skip is not an error, and the column's ``CHECK`` ties ``last_error`` to
    the ``failed`` status precisely so the two cannot be conflated.

    The appended event carries the **original** event's ``subject_kind`` and
    ``subject_id``. The closed ``subject_kind`` ``CHECK`` has no ``consumer``
    member, and inventing one to make a skip its own subject would be a schema
    change smuggled in through an audit record; the skip is a fact *about* the
    original subject, and the consumer is named in the payload and the
    ``dedup_key``.

    *event_id* and *ingested_at_ms* belong to that appended event. Its
    ``dedup_key`` is ``consumption_skipped/<consumer_id>/<event_seq>``, so a
    retried skip cannot put a second audit row on the spine --- though it will
    not get that far, because the settle refuses a consumption that is already
    terminal.

    :raises StaleConsumerRefused: if the fenced ``UPDATE`` matched no row.
        Nothing is appended: the skip and its evidence share one transaction, so
        a skip without the event is unreachable.
    """

    _require_identifier("consumer_id", consumer_id)
    _require_identifier("reason", reason)
    _require_identifier("event_id", event_id)
    _require_positive_epoch("writer_epoch", writer_epoch)
    _require_epoch_ms("settled_at_ms", settled_at_ms)
    _require_epoch_ms("ingested_at_ms", ingested_at_ms)

    try:
        with transaction(connection):
            _settle(
                connection,
                sql=f"""
                UPDATE event_consumption
                   SET status = 'skipped',
                       settled_at_ms = :settled_at_ms,
                       writer_epoch = :writer_epoch,
                       last_error = NULL
                 WHERE consumer_id = :consumer_id
                   AND event_seq = :event_seq
                   AND status IN ('pending', 'failed')
                   AND {CONSUMER_FENCE_SQL}
                """,
                params={
                    "consumer_id": consumer_id,
                    "event_seq": event_seq,
                    "writer_epoch": writer_epoch,
                    "settled_at_ms": settled_at_ms,
                    "now_ms": settled_at_ms,
                },
                consumer_id=consumer_id,
                event_seq=event_seq,
                writer_epoch=writer_epoch,
                what="mark_skipped",
            )
            original = connection.execute(
                "SELECT event_id, subject_kind, subject_id, run_id FROM event WHERE seq = :seq",
                {"seq": event_seq},
            ).fetchone()
            if original is None:  # pragma: no cover - the FK makes this unreachable
                raise EventSpineUsageError(
                    f"event seq {event_seq} does not exist, so a skip of it cannot be recorded"
                )
            return _append_within_transaction(
                connection,
                event_id=event_id,
                event_type="consumption_skipped",
                subject_kind=str(original[1]),
                subject_id=str(original[2]),
                dedup_key=f"consumption_skipped/{consumer_id}/{event_seq}",
                producer=consumer_id,
                occurred_at_ms=settled_at_ms,
                ingested_at_ms=ingested_at_ms,
                run_id=original[3],
                producer_epoch=writer_epoch,
                payload=json.dumps(
                    {
                        "consumer_id": consumer_id,
                        "skipped_event_seq": event_seq,
                        "skipped_event_id": str(original[0]),
                        "reason": reason,
                    },
                    sort_keys=True,
                ),
                side_effect=None,
                delivery_payload=None,
            )
    except _DuplicateFact as duplicate:  # pragma: no cover - the settle refuses first
        return AppendedEvent(
            seq=None,
            event_id=duplicate.event_id,
            duplicate=True,
            consumptions=(),
            messages=(),
        )


# --------------------------------------------------------------------------
# drain -- section 5.5. Every one of these takes a consumer_id, and that is
# the design, not an ergonomic accident.
# --------------------------------------------------------------------------


def undrained(
    connection: sqlite3.Connection, *, consumer_id: str
) -> tuple[Mapping[str, Any], ...]:
    """Everything *consumer_id* still owes, oldest sequence first.

    Undrained by C means ``status IN ('pending','failed')`` for C. There is no
    global ``undrained()`` in this module and there must not be one: the whole
    reason consumption is fanned out per consumer is that a single drained flag
    lets the first consumer to finish hide every other consumer's backlog, which
    is how 134 terminal events sat undelivered for twenty days behind a scan
    that reported nothing wrong.

    Each row carries the event's own fields as well as the consumption's, so a
    caller diagnosing a backlog does not have to join the spine again --- and so
    that the ``ingested_at_ms`` the head-of-line age is measured from is right
    there next to the row it belongs to.
    """

    _require_identifier("consumer_id", consumer_id)
    columns = (
        "consumer_id",
        "event_seq",
        "status",
        "attempt_count",
        "message_id",
        "last_error",
        "created_at_ms",
        "event_id",
        "event_type",
        "subject_kind",
        "subject_id",
        "occurred_at_ms",
        "ingested_at_ms",
    )
    rows = connection.execute(
        """
        SELECT ec.consumer_id, ec.event_seq, ec.status, ec.attempt_count, ec.message_id,
               ec.last_error, ec.created_at_ms,
               e.event_id, e.event_type, e.subject_kind, e.subject_id,
               e.occurred_at_ms, e.ingested_at_ms
          FROM event_consumption ec
          JOIN event e ON e.seq = ec.event_seq
         WHERE ec.consumer_id = :consumer_id
           AND ec.status IN ('pending', 'failed')
         ORDER BY ec.event_seq
        """,
        {"consumer_id": consumer_id},
    ).fetchall()
    return tuple(MappingProxyType(dict(zip(columns, row))) for row in rows)


def backlog_depth(connection: sqlite3.Connection, *, consumer_id: str) -> int:
    """How many events *consumer_id* has not drained. Never a global count."""

    _require_identifier("consumer_id", consumer_id)
    row = connection.execute(
        "SELECT COUNT(*) FROM event_consumption "
        "WHERE consumer_id = :consumer_id AND status IN ('pending', 'failed')",
        {"consumer_id": consumer_id},
    ).fetchone()
    return int(row[0])


def drain_frontier(connection: sqlite3.Connection, *, consumer_id: str) -> int | None:
    """The lowest sequence *consumer_id* still owes, or ``None`` if it owes none.

    The cursor-shaped view of a consumer's position, **derived and never
    stored**. A stored cursor is a second copy of the truth that can disagree
    with the rows, and it cannot express "event 5 failed, event 6 succeeded" ---
    it forces head-of-line blocking on every failure, which is why section 5.3
    chose per-event rows over a cursor in the first place. Deriving it means
    the frontier moves exactly when the rows move and at no other time.
    """

    _require_identifier("consumer_id", consumer_id)
    row = connection.execute(
        "SELECT MIN(event_seq) FROM event_consumption "
        "WHERE consumer_id = :consumer_id AND status IN ('pending', 'failed')",
        {"consumer_id": consumer_id},
    ).fetchone()
    return None if row[0] is None else int(row[0])


def head_of_line_age_ms(
    connection: sqlite3.Connection, *, consumer_id: str, now_ms: int
) -> int | None:
    """How long the oldest thing *consumer_id* owes has been waiting, or ``None``.

    Measured from ``event.ingested_at_ms`` --- our clock, when the row
    committed --- and not from ``occurred_at_ms``, which is the provider's. This
    is a statement about *our* lateness in draining, so a provider's skewed
    clock must not be able to decide it. ``time-base-policy.md`` section 3 is
    where the tolerance this is compared against lives; no number appears here.
    """

    _require_identifier("consumer_id", consumer_id)
    _require_epoch_ms("now_ms", now_ms)
    row = connection.execute(
        """
        SELECT e.ingested_at_ms
          FROM event_consumption ec
          JOIN event e ON e.seq = ec.event_seq
         WHERE ec.consumer_id = :consumer_id
           AND ec.status IN ('pending', 'failed')
         ORDER BY ec.event_seq
         LIMIT 1
        """,
        {"consumer_id": consumer_id},
    ).fetchone()
    return None if row is None else now_ms - int(row[0])


# --------------------------------------------------------------------------
# reconcile -- section 5.6. Two of the passes the table there names: the
# undrained-events row and the orphaned-outbox row. Both are SELECTs. The
# pass that acts on what they name is the caller's, and keeping the detection
# free of writes is what makes it safe to run on any replica at any frequency.
# --------------------------------------------------------------------------


#: The incident class the undrained-events pass ages against
#: (``time-base-policy.md`` section 3.2, seeded in ``0002_policy_seed.sql``).
#: Named here rather than spelled inline at the call so that the class this
#: module alarms under is greppable from the policy row's side too -- a
#: detector reading a class nobody seeded fails as a :class:`PolicyRowMissing`
#: refusal, and the string is how a reader confirms which row that is.
BACKLOG_INCIDENT_CLASS = "consumer_backlog"

#: The incident class the orphaned-outbox pass ages against. It is the one
#: **delivery** tolerance the time base decides (``T`` = 2 min, ``L`` = 5 min):
#: section 3.2 derives it from the gate relay because that is where the stall
#: was first observed, but the condition it measures -- "enqueued and not
#: acked" -- is a property of the ``outbox`` row and of nothing above it, and
#: ``gate_relay`` reaches it only by joining the same column this pass reads.
#: Aging every unfinished message against a second, invented number would put a
#: tolerance in code, which is exactly what ``D-0031`` moves into policy data.
OUTBOX_DELIVERY_INCIDENT_CLASS = "relay_delivery_stall"

#: The statement :func:`orphaned_outbox` executes, hoisted out of the function
#: **so the plan test can EXPLAIN the shipped text**. It lives at module level
#: for one reason: a test that pastes the query into itself and explains the
#: paste asserts a property of the paste. That form was in this suite and it
#: stayed green while the function's own predicate was rewritten into the
#: degraded arithmetic below -- the exact regression the docstring says would
#: turn this pass into a full scan. Nothing else may hold a second copy of this
#: text; the constant is the only copy.
ORPHANED_OUTBOX_SQL = """
        SELECT message_id, recipient, dedup_key, status, retry_count,
               enqueued_at_ms, delivered_at_ms,
               :now_ms - enqueued_at_ms AS age_ms,
               :tolerance_ms,
               :revision_id,
               :incident_class
          FROM outbox
         WHERE status <> 'acked'
           AND enqueued_at_ms < :now_ms - :tolerance_ms
         ORDER BY enqueued_at_ms, message_id
        """

#: The algebraically identical, index-losing form of :data:`ORPHANED_OUTBOX_SQL`
#: -- same rows, ``enqueued_at_ms`` buried inside an expression no b-tree can
#: seek on. It is kept only so the plan test can prove that the degraded form
#: really does lose ``outbox_undelivered``; without that half, "the shipped
#: query uses the index" would also pass on a database where every plan does.
#: **Never execute this in production code.**
DEGRADED_ORPHANED_OUTBOX_SQL = ORPHANED_OUTBOX_SQL.replace(
    "AND enqueued_at_ms < :now_ms - :tolerance_ms",
    "AND :now_ms - enqueued_at_ms > :tolerance_ms",
)


def backlogged_consumers(
    connection: sqlite3.Connection, *, revision_id: int, now_ms: int
) -> tuple[Mapping[str, Any], ...]:
    """Consumers whose head-of-line age exceeds the ``consumer_backlog`` tolerance.

    Section 5.6's undrained-events pass, and it is **per consumer for the same
    reason everything else in this section is**: a global "oldest undrained
    event" figure is the single ``drained_at`` column wearing a different hat --
    it goes quiet the moment *any* consumer drains the head of the spine, while
    the consumer that is actually stuck keeps accumulating. That is
    ``tools/relay_scan.py``'s twenty-day silence, and this function must never
    grow an aggregate that reintroduces it. Every row returned names a
    ``consumer_id``; there is no shape of the result that does not.

    The age is taken at the **drain frontier** (section 5.5) -- ``MIN(event_seq)``
    over that consumer's undrained rows -- and read from *that row's*
    ``event.ingested_at_ms`` rather than from ``MIN(ingested_at_ms)``. The two
    agree only while ingest order matches sequence order, which nothing
    enforces: ``ingested_at_ms`` is the caller's value (no column here has a
    ``DEFAULT``), so a producer catching up on a backlog can commit an older
    instant at a higher sequence. Head-of-line blocking is about the row at the
    *front*, so the front row is the one that must be aged.

    **A retired consumer is not backlogged.** The frontier joins ``consumer``
    and drops rows whose ``retired_at_ms`` is set, for the same reason
    :func:`_subscribers` refuses to fan out to one: the rows a consumer left
    behind when it was retired stay ``pending`` forever, and the remedy section
    5.6 assigns this class -- raise a ``consumer_backlog`` incident, drain the
    consumer -- has nobody left to perform it. Without the join, retiring a
    consumer converts it into a permanent alarm that no action can clear, and a
    class of incident that can never be cleared is how an operator learns to
    stop reading the whole pass. The rows are kept (the fan-out history has to
    stay explicable) and excluded here, exactly as
    ``watcher``/``policy`` exclude their own retired and superseded rows.

    *revision_id* is resolved by the caller -- through
    :func:`~claude_org_runtime.control_plane.policy.effective_revision_id` for a
    detector judging now, or
    :func:`~claude_org_runtime.control_plane.policy.revision_over_period` for a
    report judging a past window. This function will not resolve one for
    itself: ``D-0031``'s corollary is that a ``policy_*`` read without a bound
    revision matches every tolerance ever recorded, and a convenience default
    here would be the same defect one call deeper, where the report and the
    detector could no longer disagree about which instant they are judging.

    The boundary is **strictly exceeds**, matching section 5.6's "exceeds the
    class tolerance" and ``T``'s definition in ``time-base-policy.md`` section
    3.1 as the time the condition may *legitimately persist*. A consumer exactly
    ``T`` old is still inside what it is entitled to.

    :raises ~claude_org_runtime.control_plane.policy.PolicyRowMissing: if
        *revision_id* decides no ``consumer_backlog`` row. A pass that skipped
        itself on unseeded policy would be indistinguishable from a pass that
        found no backlog, which is the failure this whole module is written
        against.
    :raises ~claude_org_runtime.control_plane.policy.NotADuration: if a later
        revision gives the class a ``consecutive_count`` threshold. Refused
        rather than read as milliseconds, because the coercion yields a
        tolerance every consumer crosses immediately.
    """

    _require_positive_epoch("revision_id", revision_id)
    _require_epoch_ms("now_ms", now_ms)
    tolerance_ms = resolve_tolerance_ms(
        connection,
        revision_id=revision_id,
        incident_class=BACKLOG_INCIDENT_CLASS,
        subject=None,
    )

    columns = (
        "consumer_id",
        "drain_frontier",
        "head_of_line_age_ms",
        "ingested_at_ms",
        "backlog_depth",
        "tolerance_ms",
        "revision_id",
        "incident_class",
    )
    rows = connection.execute(
        """
        WITH frontier AS (
            SELECT ec.consumer_id,
                   MIN(ec.event_seq) AS event_seq,
                   COUNT(*) AS backlog_depth
              FROM event_consumption ec
              JOIN consumer c ON c.consumer_id = ec.consumer_id
             WHERE ec.status IN ('pending', 'failed')
               AND c.retired_at_ms IS NULL
             GROUP BY ec.consumer_id)
        SELECT f.consumer_id,
               f.event_seq,
               :now_ms - e.ingested_at_ms AS head_of_line_age_ms,
               e.ingested_at_ms,
               f.backlog_depth,
               :tolerance_ms,
               :revision_id,
               :incident_class
          FROM frontier f
          JOIN event e ON e.seq = f.event_seq
         WHERE :now_ms - e.ingested_at_ms > :tolerance_ms
         ORDER BY head_of_line_age_ms DESC, f.consumer_id
        """,
        {
            "now_ms": now_ms,
            "tolerance_ms": tolerance_ms,
            "revision_id": revision_id,
            "incident_class": BACKLOG_INCIDENT_CLASS,
        },
    ).fetchall()
    return tuple(MappingProxyType(dict(zip(columns, row))) for row in rows)


def orphaned_outbox(
    connection: sqlite3.Connection, *, revision_id: int, now_ms: int
) -> tuple[Mapping[str, Any], ...]:
    """Enqueued messages that are not ``acked`` and are past the delivery tolerance.

    Section 5.6's orphaned-outbox pass. It belongs beside the append that
    *created* these rows: section 5.4 makes the enqueue part of the append
    transaction precisely so the outbox is the only delivery path, and a
    backstop that lived in its own module would read as the second path v1 had.
    (:mod:`~claude_org_runtime.control_plane.outbox` is the S5 spike scaffold,
    marked throwaway by ``D-0026`` and sitting on a different schema; it is not
    where a production-schema pass goes.)

    **This is a ``SELECT`` and re-attempt is the caller's.** Nothing here
    touches ``retry_count``, ``status`` or any other column: section 5.6's "the
    retry count is already durable and monotonic" is a statement about the
    sender's own increment, and a detector that bumped it would inflate the
    evidence an operator reads to decide whether a destination is refusing.
    ``outbox_retry_count_is_monotonic`` in ``0001_initial.sql`` would not catch
    that -- an increment is exactly what it permits.

    ``status <> 'acked'`` and not ``status = 'pending'``: ``delivered`` without
    an ack is the crash-window case this pass exists for -- the send landed and
    the ack did not, or never came back -- and it is the case that goes silent
    if the predicate only names ``pending``.

    **The partial index is usable and the shape of the predicate is what makes
    it so.** ``0001_initial.sql`` carries
    ``CREATE INDEX outbox_undelivered ON outbox(enqueued_at_ms) WHERE status <>
    'acked'``. SQLite may use a partial index only when the query's ``WHERE``
    contains the index's own predicate as a term, so ``status <> 'acked'`` is
    written out verbatim rather than folded into the age arithmetic. And the
    range term is ``enqueued_at_ms < :now_ms - :tolerance_ms`` -- the bare
    indexed **column** on one side -- because the algebraically identical
    ``:now_ms - enqueued_at_ms > :tolerance_ms`` is an expression *over* the
    column, which no b-tree can seek on, and would degrade the pass to a full
    scan of every message ever enqueued (rows are never deleted: see
    ``outbox_rows_are_never_deleted``). The plan is asserted in the tests rather
    than trusted, because both forms return the same rows and only the plan
    tells them apart.

    *revision_id* is the caller's, for the reason given on
    :func:`backlogged_consumers`, and the boundary is likewise strictly
    exceeds -- section 5.6 says "older than the delivery tolerance", and a
    message exactly at ``T`` is not yet older than it.

    :raises ~claude_org_runtime.control_plane.policy.PolicyRowMissing: if
        *revision_id* decides no delivery tolerance.
    """

    _require_positive_epoch("revision_id", revision_id)
    _require_epoch_ms("now_ms", now_ms)
    tolerance_ms = resolve_tolerance_ms(
        connection,
        revision_id=revision_id,
        incident_class=OUTBOX_DELIVERY_INCIDENT_CLASS,
        subject=None,
    )

    columns = (
        "message_id",
        "recipient",
        "dedup_key",
        "status",
        "retry_count",
        "enqueued_at_ms",
        "delivered_at_ms",
        "age_ms",
        "tolerance_ms",
        "revision_id",
        "incident_class",
    )
    rows = connection.execute(
        ORPHANED_OUTBOX_SQL,
        {
            "now_ms": now_ms,
            "tolerance_ms": tolerance_ms,
            "revision_id": revision_id,
            "incident_class": OUTBOX_DELIVERY_INCIDENT_CLASS,
        },
    ).fetchall()
    return tuple(MappingProxyType(dict(zip(columns, row))) for row in rows)
