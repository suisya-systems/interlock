"""G4 -- the gate ledger: staged escalation, ack-gated relays, and a terminal taxonomy.

A gate is a halt that requires a decision from outside the deterministic layer
(``docs/production-schema.md`` section 9.1), made durable as an entity with a
rationale, options, a deadline and an outcome. ``#65`` gives it the escalation
form -- worker to Secretary to human and back to the worker -- and ``#64`` the
merge-approval form; both use this module.

Four things here are load-bearing rather than stylistic.

**The stage is a projection, and the history is the truth.** ``gate.stage`` /
``gate.stage_seq`` name a row of ``gate_transition``; the schema's
``gate_stage_matches_its_transition`` trigger refuses any projection that names
a transition which does not exist, belongs to another gate, or landed on
another stage. So every function below writes the transition first and points
the projection at it second, inside one transaction. Nothing in this module
ever writes ``gate.stage`` without having just written the row it names.

**The edges are data, not control flow.** :data:`ADMISSIBLE` is section 9.3's
transition table transcribed as a tuple of :class:`Edge`. The document says the
edges are enforced in application code inside the appending transaction because
a SQLite trigger can express their shape but not the ack precondition, which is
a join. Written as ``if``/``elif`` the claim "every other edge is inadmissible"
would be checkable only by reading every branch; written as a table it is
checkable by reading one constant, and the suite reads the same constant to
enumerate what must be refused.

**A relay stage advances on the ack, never on the send** (section 9.5). The gap
between a durable write and an external effect is the one ``ACCEPTANCE.md``
section 2 says SQLite alone cannot close: advancing before the send loses the
relay to a kill and the gate looks presented when nobody saw it; advancing after
the send as its own write re-sends on recovery and the human sees the question
twice. So :func:`enqueue_relay` writes an outbox row and a ``gate_relay`` row in
one transaction, the delivery worker delivers, the ack is set once by the
outbox's own trigger, and only then does :func:`advance_on_ack` move the stage.
:func:`gates_needing_advance` is the recovery for a kill in the last window --
an acked relay whose advance never landed -- and it is a completion, not an
incident.

**There is no backwards edge.** A question that needs re-asking after being
answered is a *new* gate linked by ``superseded_by``, not a rewind, because
``gate.stage_entered_at_ms`` is the aging basis :func:`relay_gaps` reads and a
rewind would reset it -- turning an old unanswered question into a young one at
exactly the moment somebody noticed it was old.

**Every ``policy_*`` read binds one revision** (``D-0031``). Policy rows are
versioned and never updated in place, so a join that omits ``revision_id``
matches every historical tolerance and emits one incident per revision ever
recorded, some of them alarming on a tolerance retired months ago. Both
detectors below pick the effective revision once, in a scalar subquery, and join
only its rows.

**This module supplies the detectors; the driver is not in this branch.** Every
reader below -- :func:`relay_gaps`, :func:`stalled_relays`,
:func:`gates_needing_advance`, :func:`gates_past_deadline` -- and the writer
:func:`sweep_subject_gone` have zero callers in ``src/``, because no
reconcile-pass module exists here yet (``policy.budget_violations`` and
``policy.gate_stage_owner`` are in the same position). That is a scope boundary
worth stating rather than leaving to be discovered, because ``D-0032`` says a
``relay_gap`` incident *names the ball holder* and no row returned by
:func:`relay_gaps` carries one. The missing piece is not the owner lookup --
``policy.gate_stage_owner`` resolves ``(revision_id, gate_type, stage)`` to
``ball_holder`` today -- it is the pass that would join a detector row to it and
raise the incident. Deriving the owner inside the detector instead would put the
revision binding in two places and make the detector's shape depend on what the
incident wants to print, so it stays where ``D-0032`` puts it: the detector
returns the aged gate and its ``(gate_type, stage)``, and the caller that raises
the incident resolves the owner against the revision effective at that instant.

Time is the caller's everywhere: no function here reads a clock, and no column
this module writes has a SQL default. ``occurred_at_ms`` is when the actor acted
(a human's own moment) and ``recorded_at_ms`` is when we made it durable; the
stage's aging basis is ``recorded_at_ms``, because section 2 of
``docs/time-base-policy.md`` evaluates every tolerance against our clock only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .events import append_event
from .txn import transaction

__all__ = [
    "ADMISSIBLE",
    "CLOSE_OUTCOME_STAGES",
    "GATE_OUTCOMES",
    "GATE_STAGES",
    "GATE_TYPES",
    "RELAYED_STAGES",
    "TERMINAL_RUN_STATUSES",
    "TRANSITION_KINDS",
    "WRITER",
    "AnswerBodyRequired",
    "CorrectionTargetRefused",
    "Edge",
    "GateClosedRefused",
    "GateRefusal",
    "InadmissibleTransitionRefused",
    "RelayNotAckedRefused",
    "UnknownGateRefused",
    "advance_on_ack",
    "close_gate",
    "enqueue_relay",
    "gates_needing_advance",
    "gates_past_deadline",
    "open_gate",
    "record_correction",
    "record_resend",
    "relay_gaps",
    "stalled_relays",
    "sweep_subject_gone",
]

#: The stages of section 9.2, in the order the gate walks them.
GATE_STAGES: tuple[str, ...] = ("received", "presented", "answered", "forwarded")

#: Section 9.4's terminal taxonomy. ``forwarded`` alone was the draft's only
#: terminus, which leaves a cancelled run, a withdrawn question, an expired
#: deadline, an unanswerable question and a superseded question as permanently
#: open rows that either alarm forever or are silently ignored.
GATE_OUTCOMES: tuple[str, ...] = (
    "answered_and_forwarded",
    "withdrawn",
    "subject_gone",
    "expired",
    "unanswerable",
    "superseded",
)

TRANSITION_KINDS: tuple[str, ...] = ("open", "advance", "resend", "correction", "close")

GATE_TYPES: tuple[str, ...] = (
    "worker_escalation",
    "merge_approval",
    "plan_approval",
    "risk_approval",
)

#: The two stages reached through the outbox, and therefore the two whose
#: advance is ack-gated. ``answered`` is not one of them: a human answer arrives
#: from outside and its durability is the ``body`` on the advance itself.
RELAYED_STAGES: tuple[str, ...] = ("presented", "forwarded")

#: The G1 adjudication, restated where the sweep reads it: which terminal status
#: a run reached is a fact, and ``run_status_is_forward_only`` refuses to leave
#: any of them.
TERMINAL_RUN_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")

#: The writer of every row below is Dispatcher Core even when the *actor* is a
#: human: admissibility is a deterministic check and ``D-0008`` puts
#: deterministic evaluation in Core's row. A human answering a question is an
#: actor, not a writer to SQLite -- which is why ``gate_transition`` carries
#: ``actor_kind`` and the event carries ``producer``, and they differ on
#: purpose.
WRITER = "dispatcher_core"


@dataclass(frozen=True)
class Edge:
    """One row of the section 9.3 transition table.

    *precondition* is prose on purpose: the machine-checkable half of each
    precondition is implemented by the function that writes the edge (the ack
    join for a relayed advance, the non-null ``body`` for ``answered``), and
    duplicating it here as a predicate would create a second place for it to be
    true. What this field carries is the sentence a reader needs to know which
    function to look in.
    """

    from_stage: str | None
    to_stage: str
    kind: str
    actor_kinds: frozenset[str]
    precondition: str


_ANY_ACTOR = frozenset(("worker", "secretary", "human", "dispatcher_core", "system"))

#: Section 9.3's table, verbatim. **Every edge not listed here is inadmissible**
#: -- notably every backwards edge, and every advance that skips a stage.
#:
#: ``resend`` and ``correction`` are enumerated for all four stages rather than
#: written as a wildcard, because "any open stage" includes ``forwarded``: a
#: gate that has been forwarded is still open until its ``close``, and a
#: correction to the answer it carried must remain recordable in that window.
#:
#: ``close`` is **not** enumerated the same way, and the difference is the whole
#: point of transcribing the actor column instead of defaulting it. Section 9.3
#: spends two rows on the close: ``received``/``presented``/``answered`` close
#: with actor "varies", because the section 9.4 taxonomy decides which outcome
#: and each outcome has its own actor (a ``withdrawn`` is the worker's, an
#: ``expired`` the reconcile pass's, an ``unanswerable`` the human's); but
#: ``forwarded -> forwarded`` closes with actor ``system`` alone, because that
#: close is the consequence of the forward relay's ack and nobody *decides* it.
#: Widening it to any actor would let a worker close its own gate as
#: ``answered_and_forwarded`` at the one stage where the ack is the only
#: evidence the forward happened -- which is a gate that reports the answer
#: delivered on the say-so of the party that was supposed to receive it.
ADMISSIBLE: tuple[Edge, ...] = (
    Edge(None, "received", "open", frozenset(("worker", "system")),
         "an escalation event exists on the spine (gate.origin_event_seq)"),
    Edge("received", "presented", "advance", frozenset(("secretary",)),
         "the presented relay's outbox row is acked (section 9.5)"),
    Edge("presented", "answered", "advance", frozenset(("human",)),
         "a human answer is durable; body non-null"),
    Edge("answered", "forwarded", "advance", frozenset(("secretary",)),
         "the forwarded relay's outbox row is acked (section 9.5)"),
    *(Edge(stage, stage, "resend", _ANY_ACTOR, "a relay attempt was repeated")
      for stage in GATE_STAGES),
    *(Edge(stage, stage, "correction", _ANY_ACTOR,
           "supersedes_seq names an earlier transition of this gate")
      for stage in GATE_STAGES),
    *(Edge(stage, stage, "close", _ANY_ACTOR, "see the section 9.4 taxonomy")
      for stage in ("received", "presented", "answered")),
    Edge("forwarded", "forwarded", "close", frozenset(("system",)),
         "the forward relay is acked; see the section 9.4 taxonomy"),
)

#: Section 9.4's taxonomy, read the other way round: which stages each outcome
#: is reachable from. The ``close`` rows of :data:`ADMISSIBLE` say a close may
#: happen at any stage; this says *which* close.
#:
#: ``subject_gone`` and ``superseded`` list ``forwarded`` because section 9.4
#: gives them "any open stage" and a forwarded gate is open until it closes;
#: the section 9.3 row naming only ``answered_and_forwarded`` out of
#: ``forwarded`` is the *ordinary* path, not an exhaustive one. What that row
#: *is* exhaustive about is the actor: every close out of ``forwarded``,
#: whichever of the three outcomes it carries, is ``system``'s, so both of
#: these are written by the reconcile pass and never by a party to the gate.
CLOSE_OUTCOME_STAGES: Mapping[str, frozenset[str]] = {
    "answered_and_forwarded": frozenset(("forwarded",)),
    "withdrawn": frozenset(("received", "presented", "answered")),
    "subject_gone": frozenset(GATE_STAGES),
    "expired": frozenset(("presented", "answered")),
    "unanswerable": frozenset(("presented",)),
    "superseded": frozenset(GATE_STAGES),
}


class GateRefusal(Exception):
    """A gate write that was refused, with the reason it was refused for.

    Every refusal below is typed rather than a false return, because the two
    outcomes a caller must distinguish -- "this already happened, carry on" and
    "this may not happen" -- are exactly the two a bare ``bool`` collapses. The
    idempotent no-op *is* a ``False`` return; everything else raises.
    """


class UnknownGateRefused(GateRefusal):
    """The gate_id names no row. A missing gate is never created implicitly."""


class GateClosedRefused(GateRefusal):
    """The gate is closed, and a closed gate keeps its outcome (section 9.2)."""


class InadmissibleTransitionRefused(GateRefusal):
    """The edge is not in :data:`ADMISSIBLE`. Includes every rewind."""


class RelayNotAckedRefused(GateRefusal):
    """A relayed stage was advanced without its outbox row being acked.

    This is the refusal that makes section 9.5 a property rather than a
    convention: with the advance permitted on the send, a kill in the crash
    window either loses the relay or duplicates the question, and no ordering
    of the two operations fixes it.
    """


class AnswerBodyRequired(GateRefusal):
    """The advance to ``answered`` carried no body.

    The verbatim answer is the whole point of the stage -- section 9.3 records
    it on the advance row precisely so it is never paraphrased and never
    overwritten -- so an advance without one is refused rather than stored as a
    stage change with the answer lost.
    """


class CorrectionTargetRefused(GateRefusal):
    """``supersedes_seq`` does not name an earlier transition of this gate."""


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def open_gate(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    gate_type: str,
    subject_kind: str,
    subject_id: str,
    rationale: str,
    origin_event_seq: int,
    created_at_ms: int,
    actor_kind: str,
    actor_id: str,
    options: Sequence[str] = (),
    deadline_at_ms: int | None = None,
    run_id: str | None = None,
) -> int:
    """Open a gate at ``received`` and return the seq of its opening transition.

    One transaction over three statements, in this order because the schema
    admits no other: the gate row is inserted with a **null** ``stage_seq`` and
    no outcome (``gate_opens_without_a_projection`` refuses anything else --
    creation is the one moment the projection cannot be validated, because
    ``gate_transition`` has a foreign key back to ``gate``); the ``open``
    transition is inserted; and the projection is then pointed at it through
    the UPDATE path, where ``gate_stage_matches_its_transition`` governs.

    *origin_event_seq* is the escalation event already on the spine -- section
    9.3's precondition for the ``open`` edge. This function does **not** append
    it: the party that observed the escalation appends the event, and opening a
    gate for an event nobody appended would make the gate its own evidence.

    :raises InadmissibleTransitionRefused: if *actor_kind* may not open a gate.
    """

    _require_actor(None, "received", "open", actor_kind)
    payload = json.dumps(list(options))
    with transaction(connection) as conn:
        conn.execute(
            """
            INSERT INTO gate (gate_id, gate_type, run_id, subject_kind, subject_id,
                              origin_event_seq, rationale, options, deadline_at_ms,
                              stage, stage_seq, stage_entered_at_ms, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', NULL, ?, ?)
            """,
            (gate_id, gate_type, run_id, subject_kind, subject_id, origin_event_seq,
             rationale, payload, deadline_at_ms, created_at_ms, created_at_ms),
        )
        seq = _insert_transition(
            conn,
            gate_id=gate_id,
            transition_kind="open",
            from_stage=None,
            to_stage="received",
            actor_kind=actor_kind,
            actor_id=actor_id,
            occurred_at_ms=created_at_ms,
            recorded_at_ms=created_at_ms,
        )
        conn.execute(
            "UPDATE gate SET stage = 'received', stage_seq = ? WHERE gate_id = ?",
            (seq, gate_id),
        )
    return seq


def enqueue_relay(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    to_stage: str,
    recipient: str,
    payload: str,
    message_id: str,
    enqueued_at_ms: int,
) -> str:
    """Enqueue the relay for *to_stage*, idempotently, and return the message in force.

    One transaction over the ``gate_relay`` row and the ``outbox`` row at
    ``pending`` with ``dedup_key = 'gate/<gate_id>/<to_stage>'``.

    The ``(gate_id, to_stage)`` primary key is what makes the *enqueue* itself
    idempotent, and it is why this returns a message id rather than ``None``: a
    Secretary that was killed after the commit and re-enqueues on recovery
    collides here and gets back the id already in force, so its retries
    accumulate on one outbox row (``retry_count``, durable and monotonic)
    instead of producing a second message a human would see twice. Deliberately
    *not* done by making ``outbox.dedup_key`` unique -- that column is
    non-unique on purpose and gate relays get their own identity table rather
    than a shared table's semantics changed under every other caller.

    The existing row is read, not inserted-and-caught: the transaction holds the
    write lock from its first statement (``BEGIN IMMEDIATE``), so a read that
    finds nothing cannot be overtaken between the read and the insert.

    :raises UnknownGateRefused: if *gate_id* names no gate.
    :raises GateClosedRefused: a closed gate is not relayed to; its outcome is
        already recorded and nothing is waiting on the message.
    :raises ValueError: if *to_stage* is not a relayed stage.
    """

    if to_stage not in RELAYED_STAGES:
        raise ValueError(
            f"only {RELAYED_STAGES} are relayed stages; {to_stage!r} is not one"
        )
    with transaction(connection) as conn:
        gate = _load_gate(conn, gate_id)
        if gate["closed_at_ms"] is not None:
            raise GateClosedRefused(
                f"gate {gate_id} closed as {gate['outcome']!r}; it is not relayed to"
            )
        existing = conn.execute(
            "SELECT message_id FROM gate_relay WHERE gate_id = ? AND to_stage = ?",
            (gate_id, to_stage),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        conn.execute(
            """
            INSERT INTO outbox (message_id, run_id, recipient, payload, dedup_key,
                                status, enqueued_at_ms)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (message_id, gate["run_id"], recipient, payload,
             f"gate/{gate_id}/{to_stage}", enqueued_at_ms),
        )
        conn.execute(
            """
            INSERT INTO gate_relay (gate_id, to_stage, message_id, enqueued_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (gate_id, to_stage, message_id, enqueued_at_ms),
        )
    return message_id


def advance_on_ack(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    to_stage: str,
    actor_kind: str,
    actor_id: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    writer_epoch: int | None = None,
    body: str | None = None,
) -> bool:
    """Advance the gate to *to_stage*; return whether this call was the one that moved it.

    Step 4 of section 9.5. For a relayed stage the advance is refused unless the
    relay's outbox row is ``acked`` -- the ack is the durable evidence that the
    external effect happened, and it is set once by the outbox's own trigger, so
    a duplicate or late ack changes nothing here either.

    Idempotent by design and not by accident: the reconcile pass calls this as a
    *recovery* for a kill between the ack and the advance
    (:func:`gates_needing_advance`), so a second call on a gate already at
    *to_stage* returns ``False`` rather than raising. Everything else raises,
    because "already done" and "not allowed" are the two things a caller must be
    able to tell apart.

    ``stage_entered_at_ms`` is set from *recorded_at_ms*, not *occurred_at_ms*:
    it is the aging basis :func:`relay_gaps` reads, and rule 1 of
    ``docs/time-base-policy.md`` section 2 evaluates every tolerance against our
    clock only. The actor's own moment survives on the transition row.

    :raises UnknownGateRefused: if *gate_id* names no gate.
    :raises GateClosedRefused: if the gate is closed.
    :raises InadmissibleTransitionRefused: for any edge outside
        :data:`ADMISSIBLE`, which is every rewind and every skipped stage.
    :raises RelayNotAckedRefused: if the relay for *to_stage* is missing or
        unacked.
    :raises AnswerBodyRequired: if *to_stage* is ``answered`` and *body* is None.
    """

    with transaction(connection) as conn:
        gate = _load_gate(conn, gate_id)
        if gate["stage"] == to_stage:
            return False
        if gate["closed_at_ms"] is not None:
            raise GateClosedRefused(
                f"gate {gate_id} closed as {gate['outcome']!r}; open a new gate instead"
            )
        from_stage = str(gate["stage"])
        _require_actor(from_stage, to_stage, "advance", actor_kind)
        if to_stage == "answered" and body is None:
            raise AnswerBodyRequired(
                f"the advance of gate {gate_id} to 'answered' carries the verbatim "
                "answer; an advance without one loses the thing the stage is for"
            )
        message_id = None
        if to_stage in RELAYED_STAGES:
            message_id = _acked_relay_message(conn, gate_id=gate_id, to_stage=to_stage)
        seq = _insert_transition(
            conn,
            gate_id=gate_id,
            transition_kind="advance",
            from_stage=from_stage,
            to_stage=to_stage,
            actor_kind=actor_kind,
            actor_id=actor_id,
            occurred_at_ms=occurred_at_ms,
            recorded_at_ms=recorded_at_ms,
            writer_epoch=writer_epoch,
            message_id=message_id,
            body=body,
        )
        conn.execute(
            """
            UPDATE gate
               SET stage = ?, stage_seq = ?, stage_entered_at_ms = ?
             WHERE gate_id = ?
            """,
            (to_stage, seq, recorded_at_ms, gate_id),
        )
    return True


def record_resend(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    actor_kind: str,
    actor_id: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    message_id: str | None = None,
    writer_epoch: int | None = None,
) -> int:
    """Record that the current stage's relay was attempted again; return the seq.

    A resend does **not** move the stage, and this function has no *to_stage*
    parameter for that reason: it reads the stage the gate is already at and
    writes ``from_stage = to_stage``. The stage moves on the ack alone.

    It does not touch ``outbox.retry_count`` either. That counter belongs to the
    delivery worker, which is the thing making delivery attempts; incrementing
    it here as well would count one attempt twice and make the durable retry
    count -- the number ``ACCEPTANCE.md`` section 2 asks a query to be able to
    show -- disagree with the deliveries that actually happened.
    """

    with transaction(connection) as conn:
        gate = _load_gate(conn, gate_id)
        if gate["closed_at_ms"] is not None:
            raise GateClosedRefused(f"gate {gate_id} is closed; nothing is resent")
        stage = str(gate["stage"])
        _require_actor(stage, stage, "resend", actor_kind)
        return _insert_transition(
            conn,
            gate_id=gate_id,
            transition_kind="resend",
            from_stage=stage,
            to_stage=stage,
            actor_kind=actor_kind,
            actor_id=actor_id,
            occurred_at_ms=occurred_at_ms,
            recorded_at_ms=recorded_at_ms,
            writer_epoch=writer_epoch,
            message_id=message_id,
        )


def record_correction(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    supersedes_seq: int,
    body: str,
    actor_kind: str,
    actor_id: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    writer_epoch: int | None = None,
) -> int:
    """Correct an earlier transition's body with a new row; return its seq.

    Both texts survive, and that is the point: ``gate_transition`` is immutable
    by trigger, so a corrected answer is a *second* row naming the first in
    ``supersedes_seq`` rather than an UPDATE that would leave no trace of what
    the human first said. A reader that wants the current answer takes the
    latest row of the chain; a reader auditing what was acted on at the time
    takes the row that was current then. An overwrite serves only the first
    reader and silently misleads the second.

    :raises CorrectionTargetRefused: if *supersedes_seq* is not an earlier
        transition of this same gate. A correction pointing at another gate's
        history would attach one human's words to another's question.
    """

    with transaction(connection) as conn:
        gate = _load_gate(conn, gate_id)
        if gate["closed_at_ms"] is not None:
            raise GateClosedRefused(f"gate {gate_id} is closed; its history is settled")
        stage = str(gate["stage"])
        _require_actor(stage, stage, "correction", actor_kind)
        target = conn.execute(
            "SELECT gate_id FROM gate_transition WHERE seq = ?", (supersedes_seq,)
        ).fetchone()
        if target is None or str(target[0]) != gate_id:
            raise CorrectionTargetRefused(
                f"transition {supersedes_seq} is not a transition of gate {gate_id}"
            )
        return _insert_transition(
            conn,
            gate_id=gate_id,
            transition_kind="correction",
            from_stage=stage,
            to_stage=stage,
            actor_kind=actor_kind,
            actor_id=actor_id,
            occurred_at_ms=occurred_at_ms,
            recorded_at_ms=recorded_at_ms,
            writer_epoch=writer_epoch,
            body=body,
            supersedes_seq=supersedes_seq,
        )


def close_gate(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    outcome: str,
    actor_kind: str,
    actor_id: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    superseded_by: str | None = None,
    writer_epoch: int | None = None,
    body: str | None = None,
) -> bool:
    """Close the gate with one of the six section 9.4 outcomes.

    Returns ``True`` when this call closed it and ``False`` when it was already
    closed with the same outcome -- the reconcile sweep re-runs, and a second
    pass over a gate it closed last time is not an error.

    The close is written **inside an event append**, through
    :func:`~.events.append_event`'s ``side_effect``, so the closure and the
    event that announces it commit together. Section 9.4 requires this for
    ``expired`` in so many words -- "expiry is recorded as an event so the
    decision's absence is itself visible" -- and the same argument covers every
    other outcome: a gate that stops being open with nothing on the spine is a
    decision that disappeared. ``expired`` gets the event type ``gate_expired``
    because the absence of a decision is a different fact from a decision being
    reached; every other outcome is a ``gate_closed``.

    The event's ``dedup_key`` is ``'gate_closed/<gate_id>'`` for every outcome:
    a gate closes once, so one identity per gate is the strongest statement of
    that and makes a re-run of the sweep an idempotent no-op on the spine as
    well as in the table.

    **Closure does not neutralise an undelivered relay, and cannot today.** The
    close writes the ``gate`` row and the spine event; a ``gate_relay`` whose
    ``outbox`` row is still ``pending`` or ``delivered`` is left exactly as it
    was. ``outbox.status`` runs ``pending -> delivered -> acked`` and
    ``outbox_status_is_forward_only`` (``0001_initial.sql``) admits only steps
    along that ladder, so the schema has no status meaning "this message is no
    longer wanted" for a close to retire the row into -- adding one is a DDL
    change to the settled section 5 vocabulary and a decision this function may
    not make on its own. Two things follow for a caller, and both are real:

    * a delivery worker reading the outbox is still instructed to send the
      question, or forward the answer, for a gate that is already ``withdrawn``,
      ``expired`` or ``subject_gone``. Any such worker must therefore re-check
      ``gate.closed_at_ms`` at send time; the outbox row alone is not authority
      that the message is still wanted. No component in this branch does that
      check, because the delivery driver does not exist here yet.
    * :func:`stalled_relays` keeps reporting that relay for as long as the row
      exists -- section 9.6 writes its query with no ``closed_at_ms`` predicate
      -- so a closed gate can alarm forever through its relay, which is the
      failure the section 9.4 ``subject_gone`` outcome exists to end, displaced
      one table over.

    ``test_closing_a_gate_does_not_retire_its_undelivered_relay`` pins both
    halves so the hole is visible in the suite and not only in this paragraph.

    :raises GateClosedRefused: if the gate is closed with a *different* outcome.
    :raises InadmissibleTransitionRefused: if *outcome* is not reachable from
        the stage the gate is at (:data:`CLOSE_OUTCOME_STAGES`).
    :raises ValueError: if ``superseded_by`` does not accompany exactly the
        ``superseded`` outcome, which the schema also enforces.
    """

    if outcome not in GATE_OUTCOMES:
        raise ValueError(f"{outcome!r} is not one of the section 9.4 outcomes")
    if (outcome == "superseded") != (superseded_by is not None):
        raise ValueError(
            "outcome 'superseded' carries superseded_by and no other outcome does"
        )
    gate = _load_gate(connection, gate_id)
    if gate["closed_at_ms"] is not None:
        if gate["outcome"] == outcome:
            return False
        raise GateClosedRefused(
            f"gate {gate_id} is already closed as {gate['outcome']!r}; "
            f"it does not become {outcome!r}"
        )

    def side_effect(conn: sqlite3.Connection, seq: int) -> None:
        _close_in_transaction(
            conn,
            gate_id=gate_id,
            outcome=outcome,
            actor_kind=actor_kind,
            actor_id=actor_id,
            occurred_at_ms=occurred_at_ms,
            recorded_at_ms=recorded_at_ms,
            superseded_by=superseded_by,
            writer_epoch=writer_epoch,
            body=body,
        )

    event_type = "gate_expired" if outcome == "expired" else "gate_closed"
    appended = append_event(
        connection,
        event_id=f"gate_closed/{gate_id}",
        event_type=event_type,
        subject_kind="gate",
        subject_id=gate_id,
        dedup_key=f"gate_closed/{gate_id}",
        producer=WRITER,
        occurred_at_ms=occurred_at_ms,
        ingested_at_ms=recorded_at_ms,
        run_id=gate["run_id"],
        payload=json.dumps(
            {"gate_id": gate_id, "gate_type": gate["gate_type"],
             "stage": gate["stage"], "outcome": outcome}
        ),
        side_effect=side_effect,
    )
    return not appended.duplicate


def sweep_subject_gone(
    connection: sqlite3.Connection,
    *,
    now_ms: int,
    actor_id: str = "reconcile",
) -> tuple[str, ...]:
    """Close every open gate whose subject run reached a terminal status.

    Section 9.4 says ``subject_gone`` "needs a mechanism, not just a name": the
    outcome exists so that a gate whose worker is gone stops being an open row
    that alarms forever, and without this sweep it would be an enumeration
    member nothing ever writes -- the permanent-open-row problem with extra
    vocabulary. Terminal is the G1 set :data:`TERMINAL_RUN_STATUSES`, which
    ``run_status_is_forward_only`` makes an absorbing state, so a gate closed
    here can never be wrong later.

    A gate's subject run is ``gate.run_id`` or, for a gate whose subject *is* a
    run, ``subject_id`` -- both are checked, because ``run_id`` is nullable and
    a gate that names its run only as the subject is the same situation.

    Each gate is closed in its **own** transaction rather than the sweep being
    one: :func:`close_gate` appends an event per closure, one transaction each,
    and a kill part way through leaves the gates already closed closed and the
    rest for the next pass. Batching them would buy atomicity nobody needs and
    lose the partial progress that makes the pass restartable.
    """

    rows = connection.execute(
        f"""
        SELECT g.gate_id
          FROM gate g
          JOIN run r
            ON r.run_id = g.run_id
            OR (g.subject_kind = 'run' AND r.run_id = g.subject_id)
         WHERE g.closed_at_ms IS NULL
           AND r.status IN ({_placeholders(TERMINAL_RUN_STATUSES)})
         GROUP BY g.gate_id
         ORDER BY g.gate_id
        """,
        TERMINAL_RUN_STATUSES,
    ).fetchall()
    closed: list[str] = []
    for row in rows:
        gate_id = str(row[0])
        if close_gate(
            connection,
            gate_id=gate_id,
            outcome="subject_gone",
            actor_kind="system",
            actor_id=actor_id,
            occurred_at_ms=now_ms,
            recorded_at_ms=now_ms,
        ):
            closed.append(gate_id)
    return tuple(closed)


# --------------------------------------------------------------------------
# reading -- the reconcile pass's three gate queries (sections 5.6, 9.6)
# --------------------------------------------------------------------------


def gates_needing_advance(connection: sqlite3.Connection) -> tuple[Mapping[str, Any], ...]:
    """Acked relays whose advance never landed -- the section 9.5 kill-point-4 recovery.

    This is a *completion*, not an incident: the ack is durable, the human has
    seen the question or the worker has the answer, and only our own write is
    missing. The caller feeds each row to :func:`advance_on_ack`, which is
    guarded by the same admissibility check as any other advance and returns
    ``False`` if a concurrent pass got there first.
    """

    rows = connection.execute(
        """
        SELECT r.gate_id, r.to_stage, r.message_id, o.acked_at_ms, g.stage
          FROM gate_relay r
          JOIN outbox o ON o.message_id = r.message_id
          JOIN gate   g ON g.gate_id = r.gate_id
         WHERE o.status = 'acked'
           AND g.closed_at_ms IS NULL
           AND NOT EXISTS (SELECT 1 FROM gate_transition t
                            WHERE t.gate_id = r.gate_id
                              AND t.transition_kind = 'advance'
                              AND t.to_stage = r.to_stage)
         ORDER BY r.gate_id, r.to_stage
        """
    ).fetchall()
    return tuple(_as_mapping(row, ("gate_id", "to_stage", "message_id",
                                   "acked_at_ms", "stage")) for row in rows)


def relay_gaps(
    connection: sqlite3.Connection, *, now_ms: int
) -> tuple[Mapping[str, Any], ...]:
    """Open gates aged past their stage's tolerance -- section 9.6, verbatim.

    Two properties of the query are the design and not an implementation
    detail. It binds **one** policy revision, chosen once in the ``effective``
    CTE: policy rows are versioned and never updated in place, so a join
    without a ``revision_id`` predicate matches every historical tolerance and
    emits one row per revision ever recorded (``D-0031``). And ``presented``
    opts out through ``tolerance_ms IS NULL`` in the data rather than through a
    branch here -- "a slow human is not a gap" is a fact about the stage, so it
    lives where the other facts about the stage live, and a stage that later
    acquires a tolerance needs no code change to start being aged.

    **Known hole, stated rather than silently carried.** The inner join means a
    gate whose ``gate_type`` has *no* tolerance rows at all is never aged, in
    any stage, and no refusal marks that. ``0002_policy_seed.sql`` deliberately
    seeds no rows for ``plan_approval`` or ``risk_approval`` --
    ``time-base-policy.md`` decides no numbers for them and seeding invented
    ones in a migration is exactly what ``D-0031`` forbids -- so a gate of
    either type is unpoliced today and reads, from this query, exactly like a
    gate that is not late. This is the difference between the two shapes of
    "undecided": :func:`~.policy.gate_stage_tolerance` raises
    :class:`~.policy.PolicyRowMissing` rather than let an undecided gate type be
    silently unpoliced, and the watcher side has ``watcher_scope_uncovered`` as
    a named incident class for the same situation; the detector here has
    neither, and there is no ``gate_type_unpoliced`` counterpart to raise. It is
    a design-level hole and not a transcription error: the section 9.6 query is
    written this way in the design itself, an inner join with no coverage check,
    so closing it means deciding a new incident class, not fixing this function.
    ``test_an_unpoliced_gate_type_is_silently_never_aged`` pins the current
    behaviour so the hole is visible in the suite and not only in this
    paragraph.

    The row this returns names no owner; see the module docstring for why that
    is the caller's join and which caller is missing.
    """

    rows = connection.execute(
        """
        WITH effective AS (
            SELECT revision_id FROM policy_revision
             WHERE effective_at_ms <= :now_ms
             ORDER BY effective_at_ms DESC, revision_id DESC
             LIMIT 1)
        SELECT g.gate_id, g.gate_type, g.stage, g.stage_entered_at_ms,
               :now_ms - g.stage_entered_at_ms AS age_ms
          FROM gate g
          JOIN policy_gate_stage_tolerance p
            ON p.gate_type = g.gate_type AND p.stage = g.stage
           AND p.revision_id = (SELECT revision_id FROM effective)
         WHERE g.closed_at_ms IS NULL
           AND p.tolerance_ms IS NOT NULL
           AND :now_ms - g.stage_entered_at_ms > p.tolerance_ms
         ORDER BY g.gate_id
        """,
        {"now_ms": now_ms},
    ).fetchall()
    return tuple(_as_mapping(row, ("gate_id", "gate_type", "stage",
                                   "stage_entered_at_ms", "age_ms")) for row in rows)


def stalled_relays(
    connection: sqlite3.Connection, *, now_ms: int, tolerance_ms: int
) -> tuple[Mapping[str, Any], ...]:
    """Relays enqueued and never acked -- a delivery stall, not a stage stall.

    Section 9.6 keeps this separate from :func:`relay_gaps` because the two have
    different remedies and different owners: a stage stall means whoever holds
    the ball has not acted, while this means the message never got there. They
    are only distinguishable *because* the advance is ack-gated -- if the stage
    moved on the send, an undelivered relay would look like a gate that had
    progressed normally, and the fault would surface later as a human who never
    answered a question they never received.

    **Known hole, stated rather than silently carried.** Section 9.6 writes this
    query with no ``closed_at_ms`` predicate and it is transcribed as written,
    so a relay enqueued and never acked on a gate that has since been closed is
    reported here forever: closing the gate cannot retire the outbox row (see
    :func:`close_gate`), and nothing else ever will. Excluding closed gates here
    would silence the report but not stop a delivery worker sending the message,
    so it is half a fix and a design decision either way, not a transcription
    fix. ``test_closing_a_gate_does_not_retire_its_undelivered_relay`` pins it.
    """

    rows = connection.execute(
        """
        SELECT r.gate_id, r.to_stage, o.retry_count,
               :now_ms - r.enqueued_at_ms AS age_ms
          FROM gate_relay r
          JOIN outbox o ON o.message_id = r.message_id
         WHERE o.status <> 'acked'
           AND :now_ms - r.enqueued_at_ms > :tolerance_ms
         ORDER BY r.gate_id, r.to_stage
        """,
        {"now_ms": now_ms, "tolerance_ms": tolerance_ms},
    ).fetchall()
    return tuple(_as_mapping(row, ("gate_id", "to_stage", "retry_count", "age_ms"))
                 for row in rows)


def gates_past_deadline(
    connection: sqlite3.Connection, *, now_ms: int
) -> tuple[Mapping[str, Any], ...]:
    """Open gates whose own ``deadline_at_ms`` has passed -- candidates for ``expired``.

    Section 9.2 separates the business deadline from a relay tolerance, and this
    is the reader of the former: a deadline is owned by whoever set it and its
    consequence is an outcome on the gate, while a tolerance is a property of a
    stage and its consequence is a ``relay_gap`` incident. It is what governs
    the ``presented -> answered`` leg, which has no tolerance at all.

    The window is half-open ``[created_at_ms, deadline_at_ms)``
    (``docs/time-base-policy.md`` section 2), so a gate is past its deadline at
    ``now_ms == deadline_at_ms`` and not a millisecond later.

    This **names candidates and pronounces no verdict** (``D-0008``): section
    9.4 makes expiry conditional on "the gate's policy says expire", and no such
    policy is decided in ``time-base-policy.md``, so inventing one here would be
    deciding policy in code. The caller closes.
    """

    rows = connection.execute(
        """
        SELECT gate_id, gate_type, stage, deadline_at_ms,
               :now_ms - deadline_at_ms AS overdue_ms
          FROM gate
         WHERE closed_at_ms IS NULL
           AND deadline_at_ms IS NOT NULL
           AND deadline_at_ms <= :now_ms
         ORDER BY gate_id
        """,
        {"now_ms": now_ms},
    ).fetchall()
    return tuple(_as_mapping(row, ("gate_id", "gate_type", "stage",
                                   "deadline_at_ms", "overdue_ms")) for row in rows)


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _require_actor(
    from_stage: str | None, to_stage: str, kind: str, actor_kind: str
) -> None:
    """Refuse any (from, to, kind) not in :data:`ADMISSIBLE`, and any wrong actor.

    The two refusals are one function because they are one table row: an edge
    whose actor does not match is as absent from section 9.3 as an edge that
    was never written down, and reporting them differently would suggest the
    caller could fix the second by retrying.
    """

    for edge in ADMISSIBLE:
        if edge.from_stage == from_stage and edge.to_stage == to_stage and edge.kind == kind:
            if actor_kind in edge.actor_kinds:
                return
            raise InadmissibleTransitionRefused(
                f"{from_stage} -> {to_stage} ({kind}) is a {sorted(edge.actor_kinds)} "
                f"edge; {actor_kind!r} may not take it"
            )
    raise InadmissibleTransitionRefused(
        f"{from_stage} -> {to_stage} ({kind}) is not an admissible edge; "
        "there is no backwards edge -- re-ask as a new gate linked by superseded_by"
    )


def _load_gate(connection: sqlite3.Connection, gate_id: str) -> Mapping[str, Any]:
    row = connection.execute(
        """
        SELECT gate_id, gate_type, run_id, stage, stage_seq, stage_entered_at_ms,
               outcome, closed_at_ms, deadline_at_ms, created_at_ms
          FROM gate WHERE gate_id = ?
        """,
        (gate_id,),
    ).fetchone()
    if row is None:
        raise UnknownGateRefused(f"no gate {gate_id!r}")
    return _as_mapping(row, ("gate_id", "gate_type", "run_id", "stage", "stage_seq",
                             "stage_entered_at_ms", "outcome", "closed_at_ms",
                             "deadline_at_ms", "created_at_ms"))


def _acked_relay_message(
    connection: sqlite3.Connection, *, gate_id: str, to_stage: str
) -> str:
    row = connection.execute(
        """
        SELECT r.message_id, o.status
          FROM gate_relay r JOIN outbox o ON o.message_id = r.message_id
         WHERE r.gate_id = ? AND r.to_stage = ?
        """,
        (gate_id, to_stage),
    ).fetchone()
    if row is None:
        raise RelayNotAckedRefused(
            f"gate {gate_id} has no relay for {to_stage!r}; the stage follows the ack"
        )
    if str(row[1]) != "acked":
        raise RelayNotAckedRefused(
            f"the {to_stage!r} relay of gate {gate_id} is {row[1]!r}, not 'acked'; "
            "advancing on the send is what loses a relay or duplicates a question"
        )
    return str(row[0])


def _insert_transition(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    transition_kind: str,
    from_stage: str | None,
    to_stage: str,
    actor_kind: str,
    actor_id: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    writer_epoch: int | None = None,
    message_id: str | None = None,
    body: str | None = None,
    supersedes_seq: int | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO gate_transition (gate_id, transition_kind, from_stage, to_stage,
                                     actor_kind, actor_id, writer_epoch, message_id,
                                     body, supersedes_seq, occurred_at_ms, recorded_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (gate_id, transition_kind, from_stage, to_stage, actor_kind, actor_id,
         writer_epoch, message_id, body, supersedes_seq, occurred_at_ms, recorded_at_ms),
    )
    return int(cursor.lastrowid)


def _close_in_transaction(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    outcome: str,
    actor_kind: str,
    actor_id: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    superseded_by: str | None,
    writer_epoch: int | None,
    body: str | None,
) -> None:
    """The close itself, re-validated inside the append transaction.

    The caller's pre-check answered "is this already done?"; this answers "is it
    still allowed?" against rows read under the write lock, so a gate closed by
    somebody else between the two raises here and takes the event down with it
    rather than committing an announcement of a closure that did not happen.
    """

    gate = _load_gate(connection, gate_id)
    if gate["closed_at_ms"] is not None:
        raise GateClosedRefused(
            f"gate {gate_id} was closed as {gate['outcome']!r} while this close ran"
        )
    stage = str(gate["stage"])
    _require_actor(stage, stage, "close", actor_kind)
    if stage not in CLOSE_OUTCOME_STAGES[outcome]:
        raise InadmissibleTransitionRefused(
            f"outcome {outcome!r} is reached from "
            f"{sorted(CLOSE_OUTCOME_STAGES[outcome])}, not from {stage!r}"
        )
    _insert_transition(
        connection,
        gate_id=gate_id,
        transition_kind="close",
        from_stage=stage,
        to_stage=stage,
        actor_kind=actor_kind,
        actor_id=actor_id,
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        writer_epoch=writer_epoch,
        body=body,
    )
    # stage / stage_seq are deliberately untouched: a close is not a stage
    # change, and the stage a gate was closed at is part of what the taxonomy
    # means (an 'expired' at 'answered' is a different failure from one at
    # 'presented').
    connection.execute(
        """
        UPDATE gate SET outcome = ?, closed_at_ms = ?, superseded_by = ?
         WHERE gate_id = ?
        """,
        (outcome, recorded_at_ms, superseded_by, gate_id),
    )


def _as_mapping(row: Sequence[Any], names: Sequence[str]) -> Mapping[str, Any]:
    return dict(zip(names, row))


def _placeholders(values: Iterable[Any]) -> str:
    return ", ".join("?" for _ in values)
