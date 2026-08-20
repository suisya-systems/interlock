"""The fault-runner contract: what every role driver must satisfy.

Design section 6.2. This module is the **durable** side of the seam. It owns
the vocabulary -- checkpoint names, operation names, protocol messages, fault
kinds, invariant observable names, the driver CLI -- and it knows nothing about
S5/S6/S7. When the spike implementation is discarded (D-0026) the next adapter
is written against this file unchanged.

Two version numbers travel with every run:

``FAULT_RUNNER_CONTRACT_VERSION``
    Bumped by any change to the checkpoint vocabulary, the protocol messages or
    the driver CLI. Controller and driver refuse a mismatch at the handshake.

``PROTOCOL_VERSION``
    The wire format of the two-phase barrier itself (design section 3.1). It is
    carried in the spawn handshake and is part of the contract version above.

Nothing here imports ``claude_org_runtime`` -- deliberately, and asserted by
``test_import_graph.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

__all__ = [
    "ARMABLE_ANCHORS",
    "CHECKPOINTS",
    "CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD",
    "CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT",
    "CHECKPOINT_APPLICABILITY",
    "CHECKPOINT_BEFORE_DURABLE_WRITE",
    "CHECKPOINT_DELIVERED_BEFORE_ACK",
    "CLOCK_GUARD_MS",
    "ContractViolation",
    "DestinationObserver",
    "EVENT_CHECKPOINT",
    "EVENT_CLOCK_OFFSET",
    "EVENT_DONE",
    "EVENT_ERROR",
    "EVENT_HELLO",
    "EVENT_RECOVERY_COMPLETE",
    "EVENT_STEP",
    "EVENT_SYNC",
    "ESCALATION_REFUSED_FACT_STATES",
    "FACT_STATES",
    "FACT_ACTIVE_EVIDENCE",
    "FACT_EXPLICIT_BLOCK",
    "FACT_KNOWN_WAIT",
    "FACT_NO_ACTIVITY_EVIDENCE",
    "FACT_OBSERVATION_UNAVAILABLE",
    "FACT_TERMINAL",
    "OBSERVATION_FACT_STATES",
    "FAULT_KINDS",
    "FAULT_RUNNER_CONTRACT_VERSION",
    "INVARIANT_INCIDENT_COLLAPSE",
    "INVARIANT_NAMES",
    "INVARIANT_NO_ANOMALY_ESCALATION",
    "INVARIANT_OBSERVATION_CLASSIFIED",
    "INVARIANT_UNRESOLVED_INCIDENTS",
    "KILL_FAULTS",
    "TAKEOVER_FAULTS",
    "LANES",
    "LANE_LINUX",
    "LANE_PORTABLE",
    "OBSERVATION_MODES",
    "OBSERVATION_HEALTHY",
    "OBSERVATION_SILENT",
    "OBSERVATION_UNREADABLE",
    "OPERATIONS",
    "OPERATION_ACK",
    "OPERATION_ATTEMPT",
    "OPERATION_BIND",
    "OPERATION_ENQUEUE",
    "OPERATION_LEASE_ACQUIRE",
    "OPERATION_LEASE_RELEASE",
    "OPERATION_LEASE_RENEW",
    "OPERATION_OBSERVE",
    "PROTOCOL_VERSION",
    "ROLES",
    "ROLE_DISPATCHER",
    "ROLE_SECRETARY",
    "ROLE_SUPERVISOR",
    "ROLE_SCRIPTS",
    "RoleDriver",
    "SYNC_POINTS",
    "SYNC_LEASE_ACQUIRED",
    "SYNC_OBSERVED",
    "SYNC_SCRIPT_COMPLETE",
    "CMD_CONTINUE",
    "CMD_SET_CLOCK_OFFSET",
    "case_seed",
    "driver_cli_arguments",
    "resolve_skew_ms",
]


#: Bumped by any change to the checkpoint vocabulary, the protocol messages or
#: the driver CLI below. A failure report always carries it (design 4.4).
FAULT_RUNNER_CONTRACT_VERSION = 2

#: The wire format of the two-phase barrier (design 3.1).
PROTOCOL_VERSION = 1


class ContractViolation(AssertionError):
    """A driver, a case or a controller broke the contract itself.

    Deliberately an ``AssertionError`` subclass: a contract violation is a
    harness fault, and the design is explicit that a harness fault must be
    attributable as such rather than reported as a component failure.
    """


# ---------------------------------------------------------------------------
# checkpoint vocabulary -- design 6.2
# ---------------------------------------------------------------------------
#
# These four names are the contract's, not S7's. Today they are textually equal
# to ``claude_org_runtime.control_plane.outbox``'s constants and the spike
# adapter's conformance battery asserts that equality (design 2.2); when S7 is
# discarded the names here survive and the next adapter maps its internals onto
# them.

CHECKPOINT_BEFORE_DURABLE_WRITE = "before_durable_write"
CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT = "after_record_before_effect"
CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD = "after_effect_before_record"
CHECKPOINT_DELIVERED_BEFORE_ACK = "delivered_before_ack"

#: The three ``ACCEPTANCE.md`` section 2 mid-flight points plus the fourth the
#: outbox rows add, in the order a delivery passes them.
CHECKPOINTS = (
    CHECKPOINT_BEFORE_DURABLE_WRITE,
    CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD,
    CHECKPOINT_DELIVERED_BEFORE_ACK,
)


# ---------------------------------------------------------------------------
# roles and operations -- design 2.1
# ---------------------------------------------------------------------------

ROLE_SUPERVISOR = "sup"
ROLE_DISPATCHER = "disp"
ROLE_SECRETARY = "sec"

#: Ordered so that a ``targets`` set has one canonical spelling.
ROLES = (ROLE_SUPERVISOR, ROLE_DISPATCHER, ROLE_SECRETARY)

OPERATION_LEASE_ACQUIRE = "lease-acquire"
OPERATION_LEASE_RENEW = "lease-renew"
OPERATION_LEASE_RELEASE = "lease-release"
OPERATION_BIND = "bind"
OPERATION_ENQUEUE = "enqueue"
OPERATION_ATTEMPT = "attempt"
OPERATION_ACK = "ack"
#: The watcher's read of a worker, and the classification it produces (D-0005,
#: D-0006). It is one durable write with no external effect, exactly like
#: ``bind``: the observation is read through a seam the fault can break, and the
#: fact state it yields is written to the ``incident`` table.
OPERATION_OBSERVE = "observe"

OPERATIONS = (
    OPERATION_LEASE_ACQUIRE,
    OPERATION_LEASE_RENEW,
    OPERATION_LEASE_RELEASE,
    OPERATION_BIND,
    OPERATION_ENQUEUE,
    OPERATION_ATTEMPT,
    OPERATION_ACK,
    OPERATION_OBSERVE,
)

#: Which checkpoint windows each operation physically has (design 3.1).
#:
#: The two mid-call windows exist only on a record -> effect -> result path, so
#: only ``attempt`` carries all four. Every other operation is a single durable
#: write with nothing external after it, and exposes the window immediately
#: before that write commits and the window immediately after it committed --
#: named ``after_record_before_effect`` because that is what it is: the record
#: is durable and no effect follows.
#:
#: Manifest validation refuses a case arming a window its operation does not
#: have, so an unreachable barrier is a collection-time error and never a CI
#: timeout.
CHECKPOINT_APPLICABILITY: Mapping[str, tuple[str, ...]] = {
    OPERATION_LEASE_ACQUIRE: (
        CHECKPOINT_BEFORE_DURABLE_WRITE,
        CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    ),
    OPERATION_LEASE_RENEW: (
        CHECKPOINT_BEFORE_DURABLE_WRITE,
        CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    ),
    OPERATION_LEASE_RELEASE: (
        CHECKPOINT_BEFORE_DURABLE_WRITE,
        CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    ),
    OPERATION_BIND: (
        CHECKPOINT_BEFORE_DURABLE_WRITE,
        CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    ),
    OPERATION_ENQUEUE: (
        CHECKPOINT_BEFORE_DURABLE_WRITE,
        CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    ),
    OPERATION_ATTEMPT: CHECKPOINTS,
    OPERATION_ACK: (
        CHECKPOINT_BEFORE_DURABLE_WRITE,
        CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    ),
    OPERATION_OBSERVE: (
        CHECKPOINT_BEFORE_DURABLE_WRITE,
        CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    ),
}


# ---------------------------------------------------------------------------
# sync points -- design 3.1
# ---------------------------------------------------------------------------
#
# Barrier-capable like a checkpoint, but marking script progress rather than a
# durable-write window. They exist so a fault can be anchored to a known state
# when no write window is the right anchor; the SIGSTOP cases require one.

SYNC_LEASE_ACQUIRED = "lease-acquired"
SYNC_SCRIPT_COMPLETE = "script-complete"
#: The observation has been read and classified but nothing downstream has acted
#: on it yet -- the anchor an escalation-policy fault needs.
SYNC_OBSERVED = "observed"

SYNC_POINTS = (SYNC_LEASE_ACQUIRED, SYNC_SCRIPT_COMPLETE, SYNC_OBSERVED)

#: Everything a case may arm. Design 4.1: *every* fault is anchored; there is
#: no unanchored kind.
ARMABLE_ANCHORS = CHECKPOINTS + SYNC_POINTS


#: The operation script each role runs (design 2.1). The three are deliberately
#: different shapes over different tables, rows and leases, so a combination
#: case exercises a cross-role interleaving a single renamed process could not
#: produce. Supervisor and Secretary each carry one ``attempt``-driven action
#: precisely so that all four mandated windows are reachable for every role --
#: the two mid-call windows exist nowhere else.
ROLE_SCRIPTS: Mapping[str, tuple[str, ...]] = {
    ROLE_SUPERVISOR: (
        OPERATION_LEASE_ACQUIRE,
        OPERATION_BIND,
        # The Supervisor binds the session, so the Supervisor is the role that
        # observes it. Only this script carries the step; disp and sec are
        # unchanged, which is what keeps the observation row a Supervisor
        # concern rather than a property of every role.
        OPERATION_OBSERVE,
        OPERATION_LEASE_RENEW,
        OPERATION_ENQUEUE,
        OPERATION_ATTEMPT,
        OPERATION_ACK,
    ),
    # The delivery loop: hold the writer lease across the whole run, renewing
    # rather than releasing, and take rows through record -> effect -> result.
    ROLE_DISPATCHER: (
        OPERATION_LEASE_ACQUIRE,
        OPERATION_LEASE_RENEW,
        OPERATION_ENQUEUE,
        OPERATION_ATTEMPT,
        OPERATION_ACK,
    ),
    # The intake/ack side: enqueue, deliver, ack, and then hand the resource
    # back. The release is not decoration -- it is the step neither other script
    # performs, so the Secretary's write-set ends on a lease-row mutation the
    # Dispatcher never makes and the two scripts are not one function under two
    # names (design 2.1 item 5).
    ROLE_SECRETARY: (
        OPERATION_LEASE_ACQUIRE,
        OPERATION_ENQUEUE,
        OPERATION_ATTEMPT,
        OPERATION_ACK,
        OPERATION_LEASE_RELEASE,
    ),
}


# ---------------------------------------------------------------------------
# fault kinds and lanes -- design 4.1, 5, 8.1
# ---------------------------------------------------------------------------

FAULT_KINDS = (
    # -- S9's seed set (Issue #15) ----------------------------------------
    "sigkill",
    "sigstop-expire",
    "clock-fwd",
    "clock-back",
    "drop-delivery",
    "dup-delivery",
    "lost-ack",
    "staggered-sigkill",
    # -- I-11: the rest of the ACCEPTANCE.md section 2 matrix (Issue #16) --
    #
    # Each name below is one injection the section 2 table asks for by name,
    # and nothing else. They are grouped by the row they discharge so a reader
    # can check the table against this tuple without opening the manifest.
    #
    # Lease row: "kill the lease holder without release".
    "sigkill-expire",
    # Outbox-resend row: "hold the recipient unavailable across several retry
    # attempts".
    "recipient-unavailable",
    # Ack row: "duplicate the ack", "deliver the ack after the sender has
    # restarted", "ack an already-acked message".
    "dup-ack",
    "late-ack",
    "re-ack",
    # Dedup row: "raise the same incident condition repeatedly within a
    # window", "replay a persisted incident packet".
    "incident-repeat",
    "incident-replay",
    # Single-writer row: "two writers race for the same state item", "a write
    # is attempted concurrently from a resumed process and its replacement".
    "writer-race",
    "resumed-writer-race",
    # Observation-outage row: "make the observation path fail or return nothing
    # while the worker is genuinely healthy".
    "observation-outage",
)

#: The kill-shaped faults. Three separate places used to spell this membership
#: out as a literal tuple -- execute_case's dispatch, the at_kill window-landing
#: gate and the kill-shaped branch of the invariant assertions -- and two of
#: them fail *silently* when a new kill-shaped kind is not added to all three.
#: Naming the set once is what stops a new fault kind from quietly asserting
#: nothing.
#: The faults in which a second claimant *takes the resource over* -- the
#: incumbent is gone or fenced out and the claimant's epoch is the one that
#: wins. They are the cases where the destination-side statement is "the
#: superseded holder reached the destination zero times".
#:
#: ``writer-race`` is deliberately not one of them: there the incumbent is
#: alive and holds a live lease, so the *racer* is the one refused. Reading the
#: two shapes the same way would assert that the winner produced nothing.
TAKEOVER_FAULTS = (
    "sigstop-expire",
    "clock-fwd",
    "sigkill-expire",
    "resumed-writer-race",
)

KILL_FAULTS = (
    "sigkill",
    "staggered-sigkill",
    "sigkill-expire",
    "recipient-unavailable",
    "late-ack",
    "resumed-writer-race",
)

# ---------------------------------------------------------------------------
# the watcher's fact state and the observation seam -- D-0005, D-0006
# ---------------------------------------------------------------------------
#
# D-0005 fixes the *names* and D-0006 fixes one relation between two of them.
# Neither fixes any per-state semantics or detection predicate -- that is
# Q-0012, and it stays open. So the contract carries the closed set as a
# vocabulary to validate against, and carries **no** mapping from a fact state
# to a verdict. The only rule encoded here is the one D-0006 actually decides.

FACT_ACTIVE_EVIDENCE = "ACTIVE_EVIDENCE"
FACT_KNOWN_WAIT = "KNOWN_WAIT"
FACT_EXPLICIT_BLOCK = "EXPLICIT_BLOCK"
FACT_NO_ACTIVITY_EVIDENCE = "NO_ACTIVITY_EVIDENCE"
FACT_OBSERVATION_UNAVAILABLE = "OBSERVATION_UNAVAILABLE"
FACT_TERMINAL = "TERMINAL"

#: The closed set (D-0005). A seventh state is a ``D-`` entry, not a code
#: change, so this tuple is a vocabulary check and never a place to add one.
FACT_STATES = (
    FACT_ACTIVE_EVIDENCE,
    FACT_KNOWN_WAIT,
    FACT_EXPLICIT_BLOCK,
    FACT_NO_ACTIVITY_EVIDENCE,
    FACT_OBSERVATION_UNAVAILABLE,
    FACT_TERMINAL,
)

#: The two states D-0006 settles are **not** anomalies.
#:
#: A case declares an escalation policy -- which fact states it would escalate
#: on -- as ordinary case data, and the observation cases deliberately name the
#: very state their injection produces. The driver must then **refuse** to
#: escalate and record that refusal. Asking for the escalation is the point: it
#: is what makes "no termination or restart recommendation is produced from it"
#: an assertion about a row a broken driver would have written, rather than a
#: count over rows nothing in the tree can write. A case that never asked would
#: pass whether or not the rule held.
#:
#: Nothing here says what either state *means*; Q-0012 stays open.
ESCALATION_REFUSED_FACT_STATES = (
    FACT_OBSERVATION_UNAVAILABLE,
    FACT_NO_ACTIVITY_EVIDENCE,
)

#: What the observation seam is made to do. The fault acts on the *reader*, not
#: on the classifier and not on the assertion: ``unreadable`` makes the read
#: raise, ``silent`` makes it return a well-formed observation carrying no
#: activity, and ``healthy`` is the control. Collapsing the first two into one
#: outcome is precisely the defect D-0006 exists to police, so they are distinct
#: modes producing distinct fact states.
OBSERVATION_HEALTHY = "healthy"
OBSERVATION_SILENT = "silent"
OBSERVATION_UNREADABLE = "unreadable"

OBSERVATION_MODES = (
    OBSERVATION_HEALTHY,
    OBSERVATION_SILENT,
    OBSERVATION_UNREADABLE,
)

#: The fact state each observation mode must yield. Read the mapping in the
#: direction it is written: it says what the *reader's outcome* is called, not
#: what it means. An outage reads as ``OBSERVATION_UNAVAILABLE`` and a silent
#: but readable worker reads as ``NO_ACTIVITY_EVIDENCE``; asserting a
#: disjunction of the two would pass exactly the confusion D-0006 forbids.
OBSERVATION_FACT_STATES: Mapping[str, str] = {
    OBSERVATION_HEALTHY: FACT_ACTIVE_EVIDENCE,
    OBSERVATION_SILENT: FACT_NO_ACTIVITY_EVIDENCE,
    OBSERVATION_UNREADABLE: FACT_OBSERVATION_UNAVAILABLE,
}


LANE_LINUX = "linux"
LANE_PORTABLE = "portable"
LANES = (LANE_LINUX, LANE_PORTABLE)

#: Barrier modes (design 5).
BARRIER_ALIGNED = "aligned"
BARRIER_STAGGERED = "staggered"
BARRIER_MODES = (BARRIER_ALIGNED, BARRIER_STAGGERED)


# ---------------------------------------------------------------------------
# protocol messages -- design 3.1
# ---------------------------------------------------------------------------
#
# Line-oriented JSON, one object per line, over two inherited pipes: the
# controller writes commands to the driver's control pipe and reads events from
# its event pipe. The driver's stderr is never part of the protocol -- it is a
# diagnostic channel the controller captures to a file and attaches to a failed
# case.

EVENT_HELLO = "hello"
EVENT_CHECKPOINT = "checkpoint"
EVENT_SYNC = "sync"
EVENT_STEP = "step"
EVENT_CLOCK_OFFSET = "clock_offset"
EVENT_RECOVERY_COMPLETE = "recovery_complete"
EVENT_DONE = "done"
EVENT_ERROR = "error"

EVENTS = (
    EVENT_HELLO,
    EVENT_CHECKPOINT,
    EVENT_SYNC,
    EVENT_STEP,
    EVENT_CLOCK_OFFSET,
    EVENT_RECOVERY_COMPLETE,
    EVENT_DONE,
    EVENT_ERROR,
)

CMD_CONTINUE = "continue"
CMD_SET_CLOCK_OFFSET = "set_clock_offset"

COMMANDS = (CMD_CONTINUE, CMD_SET_CLOCK_OFFSET)


# ---------------------------------------------------------------------------
# the driver CLI -- design 6.2
# ---------------------------------------------------------------------------

def driver_cli_arguments() -> tuple[str, ...]:
    """The long options every role driver must accept.

    Kept as data so the conformance battery can assert an adapter's parser
    against the contract rather than against a prose list. A driver may add
    options; it may not drop one of these or change its meaning.
    """

    return (
        "--role",
        "--db",
        "--case-id",
        "--suite-seed",
        "--armed",
        "--clock-base-ms",
        "--clock-offset-ms",
        "--restart-generation",
        "--control-fd",
        "--event-fd",
        # I-11: the observation seam (D-0006) and the incident parameters the
        # ACCEPTANCE.md section 2 dedup row requires to be parameterised rather
        # than hard-coded (Q-0002, Q-0003). A driver that cannot be told which
        # collapse rule to apply would be answering an open question by
        # inertia, which is the one thing the matrix may not do.
        "--observation-mode",
        "--escalate-on",
        "--incident-dedup-key",
        "--incident-repeats",
        "--incident-collapse",
        "--incident-renotify-window-ms",
        "--incident-reconcile-interval-ms",
        "--unavailable-attempts",
    )


# ---------------------------------------------------------------------------
# seeds -- design 4.3
# ---------------------------------------------------------------------------

def case_seed(*, manifest_version: int, case_id: str, suite_seed: int) -> int:
    """The per-case seed: ``sha256(manifest_version || case_id || suite_seed)``.

    Order-independent and platform-independent by construction. Adding a case
    does not shift any other case's stream, and Python's hash randomisation is
    irrelevant because no ``hash()`` is involved.

    The seed's authority is payload and schedule only (design 4.3): it never
    chooses the checkpoint, the fault, the target set or the kill order -- those
    are the case's identity and they live in the manifest.
    """

    material = f"{int(manifest_version)}\x00{case_id}\x00{int(suite_seed)}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


# ---------------------------------------------------------------------------
# clock model -- design 7
# ---------------------------------------------------------------------------

#: The single named constant the boundary-relative skew magnitudes are built
#: from. Forward skew is ``ttl_ms + CLOCK_GUARD_MS`` (guaranteed to cross
#: ``expires_at_ms`` from inside the lease); backward skew is
#: ``-(elapsed + CLOCK_GUARD_MS)`` (guaranteed to land before
#: ``acquired_at_ms``).
CLOCK_GUARD_MS = 1_000


def resolve_skew_ms(direction: str, *, ttl_ms: int, elapsed_ms: int) -> int:
    """Resolve a symbolic clock programme to milliseconds (design 7).

    Symbolic in the manifest, resolved at run time from the case's ``ttl_ms``,
    and recorded in the reproduction line -- so a failure replays exactly while
    the manifest stays meaningful when a case's TTL changes.
    """

    if direction == "forward":
        return int(ttl_ms) + CLOCK_GUARD_MS
    if direction == "backward":
        return -(int(elapsed_ms) + CLOCK_GUARD_MS)
    raise ContractViolation(
        f"unknown clock direction {direction!r}; the manifest carries "
        "'forward' or 'backward', never a raw millisecond count"
    )


# ---------------------------------------------------------------------------
# invariant observables -- design 6.2
# ---------------------------------------------------------------------------
#
# Two kinds, both named and both required. The durable tests assert through
# these names; the adapter maps them to the schema of the day, so when the
# spike schema is thrown away the queries are re-bound and the assertions are
# not rewritten.

#: Named SQL queries over the control-plane store. The adapter supplies the SQL
#: (see ``spike_driver.invariant_queries``); the names are the contract's.
INVARIANT_NO_UNOWNED_OUTBOX = "no-unowned-outbox"
INVARIANT_RETRY_COUNT_DURABLE = "retry-count-durable"
INVARIANT_SINGLE_ACKED_STATE = "single-acked-state"
INVARIANT_LINEAR_WRITER_HISTORY = "linear-writer-history"
INVARIANT_RECORDED_REFUSALS = "recorded-refusals"
INVARIANT_NO_PENDING_ACTION = "no-pending-action"
INVARIANT_LEASE_SINGLE_HOLDER = "lease-single-holder"

# -- I-11 additions (Issue #16) ---------------------------------------------
#
# Every one of these is scoped by ``scope`` alone. That is deliberate: an
# invariant needing a per-case bind (a dedup key, say) would have to widen
# ``Adapter.query_parameters``, which the conformance battery binds blindly for
# every name in :data:`SQL_INVARIANTS` on every PR job. Returning the whole
# scope and letting the assertion group the rows keeps the plumbing unchanged.

#: Every incident row in the scope, so the assertion can group by dedup key and
#: check whichever collapse rule the case declared. Neither Q-0002 rule is
#: expressed in the SQL -- that is the point.
INVARIANT_INCIDENT_COLLAPSE = "incident-collapse"

#: The incidents still open. Gate item 4 asks that "work resumes from
#: unresolved incidents", and the spike schema indexes exactly this question
#: (``incident_unresolved``, commented with that sentence). It is also fed into
#: the controller's recoverable-state set, so an incident open at the kill
#: counts as something the restart had to recover.
INVARIANT_UNRESOLVED_INCIDENTS = "unresolved-incidents"

#: What the observation path was classified as. Asserted against the closed
#: D-0005 set and, per injection, against exactly one member of it.
INVARIANT_OBSERVATION_CLASSIFIED = "observation-classified"

#: The termination/restart recommendations produced in the scope. Always
#: returns exactly one row (a count), so "none were produced" is a pass rather
#: than an empty result -- and a driver that escalated on a D-0006 state would
#: move the count, which is what makes the assertion falsifiable.
INVARIANT_NO_ANOMALY_ESCALATION = "no-anomaly-escalation"

#: Destination-side observables. ``ACCEPTANCE.md`` section 2 is explicit that
#: SQLite alone cannot prove exactly-once for an external effect, so every case
#: that kills inside or after an effect window **must** name one of these --
#: manifest validation enforces it.
INVARIANT_ONE_EFFECT_PER_KEY = "one-effect-per-key"
INVARIANT_DELIVERED_IMPLIES_EFFECT = "delivered-implies-effect"

SQL_INVARIANTS = (
    INVARIANT_NO_UNOWNED_OUTBOX,
    INVARIANT_RETRY_COUNT_DURABLE,
    INVARIANT_SINGLE_ACKED_STATE,
    INVARIANT_LINEAR_WRITER_HISTORY,
    INVARIANT_RECORDED_REFUSALS,
    INVARIANT_NO_PENDING_ACTION,
    INVARIANT_LEASE_SINGLE_HOLDER,
    INVARIANT_INCIDENT_COLLAPSE,
    INVARIANT_UNRESOLVED_INCIDENTS,
    INVARIANT_OBSERVATION_CLASSIFIED,
    INVARIANT_NO_ANOMALY_ESCALATION,
)

DESTINATION_INVARIANTS = (
    INVARIANT_ONE_EFFECT_PER_KEY,
    INVARIANT_DELIVERED_IMPLIES_EFFECT,
)

INVARIANT_NAMES = SQL_INVARIANTS + DESTINATION_INVARIANTS

#: The named parameters each SQL invariant binds. The contract fixes the names
#: so a durable test can supply them without knowing the schema behind them; the
#: adapter's SQL must use exactly these and no others, and the conformance
#: battery checks that it does.
#:
#: ``scope`` is deliberately not ``resource``. The spike schema's effect table
#: has no resource column at all -- a known limit recorded in
#: ``docs/lease-fencing.md`` -- so an adapter must be free to scope a history
#: query by whatever its schema actually carries. Naming the parameter after the
#: *question* ("this role's own write scope") rather than after one schema's
#: answer is what keeps the durable assertion re-bindable.
INVARIANT_PARAMETERS: Mapping[str, tuple[str, ...]] = {
    INVARIANT_NO_UNOWNED_OUTBOX: ("resource", "now_ms"),
    INVARIANT_RETRY_COUNT_DURABLE: ("holder_prefix",),
    INVARIANT_SINGLE_ACKED_STATE: ("holder_prefix",),
    INVARIANT_LINEAR_WRITER_HISTORY: ("scope",),
    INVARIANT_RECORDED_REFUSALS: ("resource", "holder"),
    INVARIANT_NO_PENDING_ACTION: ("scope",),
    INVARIANT_LEASE_SINGLE_HOLDER: ("now_ms",),
    INVARIANT_INCIDENT_COLLAPSE: ("scope",),
    INVARIANT_UNRESOLVED_INCIDENTS: ("scope",),
    INVARIANT_OBSERVATION_CLASSIFIED: ("scope",),
    INVARIANT_NO_ANOMALY_ESCALATION: ("scope",),
}

#: The checkpoints after which an external effect may already have happened.
#: A case anchored at one of these must name a destination assertion.
EFFECT_BEARING_CHECKPOINTS = (
    CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD,
    CHECKPOINT_DELIVERED_BEFORE_ACK,
)


@runtime_checkable
class DestinationObserver(Protocol):
    """The destination-side evidence interface (design 6.2).

    It mirrors S7's ``Destination`` protocol deliberately, but the harness's
    implementation must be **durable across the role kill and out-of-process
    relative to the killed role**: the controller reads the destination's own
    store after the kill, so the evidence is the counterparty's record and never
    a re-derivation from our control-plane rows.
    """

    def effect_count(self, idempotency_key: str) -> int:
        ...

    def attempt_count(self, idempotency_key: str) -> int:
        ...

    def unwedge(self) -> None:
        """Release any exclusion the killed writer left behind.

        A destination that serialises its own critical section can be left
        holding that exclusion forever by a SIGKILL landing inside it, and the
        process that fired the signal is the only one that knows it happened.
        A destination with no such section implements this as a no-op.
        """


class RoleDriver(Protocol):
    """What the controller requires of an adapter, as a callable module.

    A driver is an executable module: ``python -m <driver> <cli arguments>``.
    The contract on it is:

    1. it accepts every option in :func:`driver_cli_arguments`;
    2. it emits :data:`EVENT_HELLO` first, carrying ``protocol_version`` and
       ``contract_version``, and refuses a mismatch;
    3. it reaches every checkpoint in :data:`CHECKPOINT_APPLICABILITY` for the
       operations its role script performs, and blocks at an armed one until a
       command arrives;
    4. it recovers before it proceeds on a restart, and emits
       :data:`EVENT_RECOVERY_COMPLETE` when it has;
    5. it sources ``now_ms`` freshly from its injected clock at every API call
       and never reads the host wall clock.

    The battery in ``conformance.py`` asserts all five against any adapter.
    """

    def main(self, argv: Sequence[str]) -> int:
        ...


@dataclass(frozen=True)
class ArmedAnchor:
    """One armed barrier: an anchor name and which occurrence of it to hold at.

    A loop passes the same point repeatedly, so the occurrence index is part of
    the arming, not an afterthought (design 3.1). Occurrences are 1-based.
    """

    anchor: str
    occurrence: int = 1
    operation: str | None = None

    def __post_init__(self) -> None:
        if self.anchor not in ARMABLE_ANCHORS:
            raise ContractViolation(
                f"{self.anchor!r} is not an armable anchor; the contract's "
                f"anchors are {ARMABLE_ANCHORS}"
            )
        if self.occurrence < 1:
            raise ContractViolation("occurrence indices are 1-based")
        if self.operation is not None and self.operation not in OPERATIONS:
            raise ContractViolation(f"{self.operation!r} is not a contract operation")

    def wire(self) -> str:
        """The ``--armed`` spelling: ``anchor:occurrence`` or ``op@anchor:occ``."""

        if self.operation is None:
            return f"{self.anchor}:{self.occurrence}"
        return f"{self.operation}@{self.anchor}:{self.occurrence}"

    @classmethod
    def parse(cls, text: str) -> "ArmedAnchor":
        operation: str | None = None
        body = text
        if "@" in body:
            operation, body = body.split("@", 1)
        anchor, _, occurrence = body.partition(":")
        return cls(
            anchor=anchor,
            occurrence=int(occurrence) if occurrence else 1,
            operation=operation,
        )


@dataclass(frozen=True)
class Handshake:
    """The spawn handshake, as the controller checks it."""

    protocol_version: int
    contract_version: int
    role: str
    case_id: str
    restart_generation: int
    extras: Mapping[str, Any] = field(default_factory=dict)

    def check(
        self,
        *,
        expect_role: str | None = None,
        expect_case_id: str | None = None,
        expect_generation: int | None = None,
    ) -> None:
        if expect_role is not None and self.role != expect_role:
            raise ContractViolation(
                f"the driver answered as {self.role!r}, but was spawned as "
                f"{expect_role!r}: every later event is correlated by the slot, "
                "so the harness would drive one role and report another"
            )
        if expect_case_id is not None and self.case_id != expect_case_id:
            raise ContractViolation(
                f"the driver answered for case {self.case_id!r}, but was "
                f"spawned for {expect_case_id!r}"
            )
        if expect_generation is not None and self.restart_generation != expect_generation:
            raise ContractViolation(
                f"the driver answered as restart generation "
                f"{self.restart_generation}, but was spawned as "
                f"{expect_generation}: a generation 0 reported as a restart is a "
                "recovery that never happened"
            )
        if self.protocol_version != PROTOCOL_VERSION:
            raise ContractViolation(
                f"driver speaks protocol {self.protocol_version}, controller "
                f"speaks {PROTOCOL_VERSION}"
            )
        if self.contract_version != FAULT_RUNNER_CONTRACT_VERSION:
            raise ContractViolation(
                f"driver targets fault-runner contract "
                f"{self.contract_version}, controller is "
                f"{FAULT_RUNNER_CONTRACT_VERSION}"
            )
        if self.role not in ROLES:
            raise ContractViolation(f"{self.role!r} is not a contract role")


class Adapter(Protocol):
    """The seam itself: what binds the durable harness to one implementation.

    The controller and the tests hold an ``Adapter``; they never import an
    implementation module. Today the only adapter is ``spike_driver`` over
    S6/S7; when the real Supervisor / Dispatcher Core / Secretary exist
    (I-12/I-14), adapters over their entrypoints implement this same protocol
    and neither the controller, the manifest, the invariant names nor the tests
    change (design 2.2, 6.2).
    """

    #: Dotted module path the controller spawns with ``-m``.
    driver_module: str

    def bootstrap(self, db_path: Any, *, roles: Sequence[str], now_ms: int) -> None:
        """Create the store and whatever rows a role script presupposes."""

    def role_arguments(self, role: str, *, case: Mapping[str, Any], workdir: Any) -> Sequence[str]:
        """Extra CLI arguments this adapter's driver needs for ``role``."""

    def observer(self, workdir: Any, role: str) -> DestinationObserver:
        """The out-of-process destination record for ``role``."""

    def invariant_queries(self) -> Mapping[str, str]:
        """Map every name in :data:`SQL_INVARIANTS` to SQL for today's schema."""

    def store_path(self, name: str, *, control_plane: Any, workdir: Any) -> Any:
        """Which store a named invariant is read from.

        Usually the control-plane database. It is a method rather than a
        constant because an adapter may keep a harness-scoped record beside the
        control plane rather than inside it -- see the note on
        ``recorded-refusals`` in ``spike_driver``.
        """

    def query_parameters(self, role: str, *, now_ms: int) -> Mapping[str, Any]:
        """Bind :data:`INVARIANT_PARAMETERS` for one role's own rows.

        The durable tests scope an assertion to a role without knowing what a
        resource, a holder or an action kind is spelled like in the schema of
        the day -- that spelling is the adapter's, and only the adapter's.
        """
