"""The role driver that binds the fault-runner contract to S6/S7.

.. warning::

   **Throwaway (D-0026).** This module is the single adapter permitted to
   import ``claude_org_runtime.control_plane``; ``test_import_graph.py``
   asserts that no other module in this tree does. It dies with S5-S7. The
   contract, controller, manifest, conformance battery and the cases are the
   durable half and none of them names an S6/S7 symbol.

It is two things in one file, on purpose:

* an **executable module** -- ``python -m tests.fault_injection.spike_driver``
  is the role process the controller spawns (design 2.1: an independent PID,
  an independent SQLite connection, its own lease identity, and a restart
  entrypoint that recovers before it proceeds);
* an **adapter object** (:data:`SPIKE_ADAPTER`) implementing
  ``contract.Adapter``, which is how the controller and the tests reach the
  spike's schema without importing it.

Three contract obligations are worth pointing at directly, because they are the
ones an adapter gets wrong quietly:

**The clock is fully virtual (design 7).** ``now_ms`` comes from :class:`Clock`
and from nowhere else -- not as a base, not as a fallback. Every S6/S7 API takes
``now_ms`` as an argument and the database has no clock of its own, so this
costs nothing and buys the identical-event-trace property the conformance
battery requires. ``time.time()``/``datetime.now()`` do not appear in this
file and a conformance test asserts it by reading the source.

**The barrier hook never raises (design 3).** It writes one line and blocks
reading one line. The kill is a real signal from outside the process; an
exception would unwind the stack, run ``finally`` blocks and close the SQLite
connection in an orderly way, which is exactly the crash a fault-injection
harness must not simulate.

**Every step is resumable by query.** The restart entrypoint re-executes the
same command line with ``--restart-generation N``; there is no warm state. Each
operation therefore asks the database what has already happened before doing
it, which is what "reconstruct its view by query from SQLite alone" (D-0001)
means in a script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from claude_org_runtime.control_plane import destination as destination_module
from claude_org_runtime.control_plane import lease as lease_module
from claude_org_runtime.control_plane import outbox as outbox_module
from claude_org_runtime.control_plane import schema as schema_module
from claude_org_runtime.control_plane.destination import (
    DeliveryReceipt,
    DestinationRefusal,
    KeyedDropbox,
)
from claude_org_runtime.control_plane.handlers import NOTIFY_RECIPIENT, spike_registry
from claude_org_runtime.control_plane.lease import (
    Lease,
    LeaseHeld,
    LeaseNotHeld,
    acquire,
    effect_kind,
    fenced_insert,
    fenced_update,
    protected_write,
    read_lease,
    renew,
    ProtectedWrite,
)
from claude_org_runtime.control_plane.outbox import Outbox
from claude_org_runtime.control_plane.schema import (
    create_control_plane,
    open_control_plane,
)

from tests.fault_injection import contract
from tests.fault_injection.contract import (
    CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    CHECKPOINT_BEFORE_DURABLE_WRITE,
    CMD_CONTINUE,
    CMD_SET_CLOCK_OFFSET,
    ContractViolation,
    EVENT_CHECKPOINT,
    EVENT_CLOCK_OFFSET,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_HELLO,
    EVENT_RECOVERY_COMPLETE,
    EVENT_STEP,
    EVENT_SYNC,
    OPERATION_ACK,
    OPERATION_ATTEMPT,
    OPERATION_BIND,
    OPERATION_ENQUEUE,
    OPERATION_LEASE_ACQUIRE,
    OPERATION_LEASE_RELEASE,
    OPERATION_LEASE_RENEW,
    OPERATION_OBSERVE,
    ROLE_SCRIPTS,
    ROLE_SUPERVISOR,
)

__all__ = [
    "BEHAVIOUR_DROP_DELIVERY",
    "BEHAVIOUR_DUP_ACK",
    "BEHAVIOUR_DUP_DELIVERY",
    "BEHAVIOUR_LOST_ACK",
    "BEHAVIOUR_RECIPIENT_UNAVAILABLE",
    "BEHAVIOUR_RE_ACK",
    "BEHAVIOUR_INCIDENT_REPLAY",
    "BEHAVIOUR_STALE_WRITER",
    "COLLAPSE_INCREMENT_IN_PLACE",
    "COLLAPSE_OPEN_LINKED",
    "COLLAPSE_RULES",
    "DEFAULT_UNAVAILABLE_ATTEMPTS",
    "ObservationUnavailable",
    "classify_observation",
    "write_observation",
    "Clock",
    "DRIVER_MODULE",
    "RESTART_CLOCK_ADVANCE_MS",
    "SPIKE_ADAPTER",
    "SpikeAdapter",
    "STEP_ADVANCE_MS",
    "main",
]

DRIVER_MODULE = "tests.fault_injection.spike_driver"

#: Every script step advances the injected clock by this much. A declared,
#: deterministic increment (design 7) -- never a measured duration.
STEP_ADVANCE_MS = 100

#: How far the injected clock starts ahead per restart generation.
#:
#: Load-bearing, and the reason is easy to miss: a restarted process whose clock
#: began again at ``clock_base_ms`` would be running *behind* the state its
#: predecessor wrote, which is an undeclared backward clock skew injected into
#: every restart -- work enqueued at ``base + 300`` would not even be due yet, so
#: "recover before you proceed" would recover nothing. Time really does pass
#: across a restart. The increment is derived from ``--restart-generation``,
#: which is on the command line, so it stays a script-declared deterministic
#: increment and the trace stays byte-identical across re-runs (design 7). It is
#: far below any case's TTL, so a restart still finds its own lease live.
RESTART_CLOCK_ADVANCE_MS = 1_000

#: Script-shaping behaviours a case may request. They are *script* behaviour,
#: not process faults: the fault kinds that carry them (``drop-delivery``,
#: ``dup-delivery``, ``lost-ack``) inject at the delivery surface rather than at
#: the process, and the barrier still anchors them (design 4.1).
BEHAVIOUR_DROP_DELIVERY = "drop-delivery"
BEHAVIOUR_DUP_DELIVERY = "dup-delivery"
BEHAVIOUR_LOST_ACK = "lost-ack"

# -- I-11 (Issue #16) -------------------------------------------------------
#
#: "Hold the recipient unavailable across several retry attempts." The refusal
#: budget is read from the destination's own attempt log rather than from a
#: counter in this process, so it survives a restart instead of starting again
#: at zero and refusing the first N attempts of *every* generation -- which
#: would mean the message never lands at all.
BEHAVIOUR_RECIPIENT_UNAVAILABLE = "recipient-unavailable"

#: "Duplicate the ack": the same ack is recorded twice while the row is still
#: acked-once, within one generation.
BEHAVIOUR_DUP_ACK = "dup-ack"

#: "Ack an already-acked message": an ack issued against a row that has already
#: reached its terminal state.
BEHAVIOUR_RE_ACK = "re-ack"

#: "Replay a persisted incident packet": every raise after the first is sourced
#: from the row already in SQLite rather than from a fresh observation, which is
#: what a replay is. The packet is in the row and not in anyone's context
#: (D-0003, D-0007), so replaying it means reading it back.
BEHAVIOUR_INCIDENT_REPLAY = "incident-replay"

#: Carry on as a writer that believes it holds the lease and does not.
#:
#: Two things use it, and they are the same injection seen from two sides.
#:
#: The conformance battery needs the *same* writer refused twice, so it can
#: check that two refusal ids do not collide -- no ordinary case does that.
#:
#: ACCEPTANCE.md section 2's single-writer row needs something more important:
#: its observable is that "the state item's history in SQLite is a linear
#: sequence with no interleaving from the rejected writer", and a writer that is
#: turned away at ``acquire`` never attempts a write at all, so that half of the
#: observable is true of every run and could not fail. A racer under this
#: behaviour fabricates the token ``acquire`` refused it and runs its whole
#: script against the same state item -- which is exactly the real hazard, a
#: process that has not noticed it lost its lease. Every write it makes is
#: refused *at the fence* and recorded there, and the history finally has the
#: opportunity to show an interleaving that atomic fencing is what prevents.
BEHAVIOUR_STALE_WRITER = "stale-writer"

BEHAVIOURS = (
    BEHAVIOUR_DROP_DELIVERY,
    BEHAVIOUR_DUP_DELIVERY,
    BEHAVIOUR_LOST_ACK,
    BEHAVIOUR_RECIPIENT_UNAVAILABLE,
    BEHAVIOUR_DUP_ACK,
    BEHAVIOUR_RE_ACK,
    BEHAVIOUR_STALE_WRITER,
    BEHAVIOUR_INCIDENT_REPLAY,
)

#: How many attempts the recipient refuses before it becomes available again.
#: "Several" in ACCEPTANCE.md section 2's outbox row; three is the smallest
#: number for which "monotonically increasing" says more than "incremented".
DEFAULT_UNAVAILABLE_ATTEMPTS = 3

#: The bound on the behaviour-driven retry loop in :func:`op_attempt`. It exists
#: so a destination that refuses forever becomes an attributable case failure
#: rather than a wedged process the barrier watchdog has to reap.
MAX_ATTEMPTS_PER_MESSAGE = 8

#: The detector version stamped on every incident this harness raises. Q-0009
#: (detector-version semantics for replay) is open; this is a constant string so
#: the trace stays byte-identical, and it settles nothing.
DETECTOR_VERSION = "s9-harness-1"

#: The two collapse rules ACCEPTANCE.md section 2 requires the tests to
#: parameterise rather than choose between (Q-0002). The driver implements both
#: and is *told* which to apply; it never picks.
COLLAPSE_INCREMENT_IN_PLACE = "increment-in-place"
COLLAPSE_OPEN_LINKED = "open-linked"

COLLAPSE_RULES = (COLLAPSE_INCREMENT_IN_PLACE, COLLAPSE_OPEN_LINKED)

#: The action kind an escalation would carry. Nothing in the spike composes it;
#: the harness does, precisely so that "no termination or restart recommendation
#: is produced" is an assertion about a row a broken driver *would* write.
ESCALATION_EFFECT = "recommend_restart"

#: The file the observation seam reads. The fault acts here -- on the reader --
#: and never on the classifier or on the assertion.
OBSERVATION_FILE_NAME = "observation.json"


# ---------------------------------------------------------------------------
# the injected clock -- design 7
# ---------------------------------------------------------------------------

@dataclass
class Clock:
    """``now_ms() = base + advance + offset``. No host clock is ever read.

    ``base_ms`` is a fixed constant from the manifest, ``advance_ms`` grows only
    by script-declared increments, and ``offset_ms`` moves only by the
    controller's ``set_clock_offset`` command while the process is blocked at an
    armed barrier. That is the whole model, and it is why two runs of one case
    with one seed produce byte-identical traces on different days.
    """

    base_ms: int
    offset_ms: int = 0
    advance_ms: int = 0

    def now_ms(self) -> int:
        return self.base_ms + self.advance_ms + self.offset_ms

    def advance(self, by_ms: int = STEP_ADVANCE_MS) -> int:
        self.advance_ms += int(by_ms)
        return self.now_ms()

    def set_offset(self, offset_ms: int) -> int:
        self.offset_ms = int(offset_ms)
        return self.now_ms()


# ---------------------------------------------------------------------------
# the two-phase barrier, phase one -- design 3.1
# ---------------------------------------------------------------------------

class Barrier:
    """Phase one of the kill barrier: announce, then block.

    The hook holds no new locks, touches no SQLite state and does no database
    work, so the process freezes mid-window with its transaction exactly as the
    operation script left it. Phase two -- the kill -- is a signal from the
    controller and never anything this class does.
    """

    def __init__(
        self,
        *,
        armed: Sequence[contract.ArmedAnchor],
        emit: Callable[[Mapping[str, Any]], None],
        control: Any,
        clock: Clock,
    ) -> None:
        self._armed = tuple(armed)
        self._emit = emit
        self._control = control
        self._clock = clock
        self._occurrences: dict[tuple[str, str], int] = {}

    def _next_occurrence(self, operation: str, anchor: str) -> int:
        key = (operation, anchor)
        seen = self._occurrences.get(key, 0) + 1
        self._occurrences[key] = seen
        return seen

    def _is_armed(self, operation: str, anchor: str, occurrence: int) -> bool:
        for armed in self._armed:
            if armed.anchor != anchor or armed.occurrence != occurrence:
                continue
            if armed.operation is None or armed.operation == operation:
                return True
        return False

    def hit(self, anchor: str, *, operation: str, kind: str = EVENT_CHECKPOINT) -> None:
        """Pass an anchor. Returns immediately unless this occurrence is armed.

        An unarmed anchor costs one dictionary lookup and no protocol
        round-trip, so a case perturbs the timing of nothing it is not about.
        """

        occurrence = self._next_occurrence(operation, anchor)
        if not self._is_armed(operation, anchor, occurrence):
            return
        self._emit(
            {
                "event": kind,
                "name": anchor,
                "operation": operation,
                "occurrence": occurrence,
                "now_ms": self._clock.now_ms(),
            }
        )
        self._block()

    def _block(self) -> None:
        """Read the control pipe until told to continue.

        A kill case never gets a reply: the blocked read is torn down by the
        SIGKILL itself. EOF means the controller is gone, and a role process
        whose controller has vanished exits rather than spinning -- the
        controller's teardown ladder (design 8.2) is the authority on cleanup,
        and a driver that outlived it has nothing left to report to.
        """

        while True:
            line = self._control.readline()
            if not line:
                os._exit(70)  # EX_SOFTWARE: the controller went away.
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                os._exit(70)
            name = command.get("cmd")
            if name == CMD_CONTINUE:
                return
            if name == CMD_SET_CLOCK_OFFSET:
                now_ms = self._clock.set_offset(int(command["offset_ms"]))
                self._emit(
                    {
                        "event": EVENT_CLOCK_OFFSET,
                        "offset_ms": self._clock.offset_ms,
                        "now_ms": now_ms,
                    }
                )
                continue
            os._exit(70)


# ---------------------------------------------------------------------------
# the destination side
# ---------------------------------------------------------------------------

class _DroppingDropbox:
    """A ``KeyedDropbox`` that refuses a named attempt, then behaves.

    This is the ``drop-delivery`` fault: the delivery is dropped at the
    destination, the outbox row stays pending and due, and the resend is what
    the case asserts. It is deterministic -- the attempt index is counted, not
    timed -- and it is the destination refusing, not an exception injected into
    our own code path.
    """

    def __init__(self, inner: KeyedDropbox, *, root: Path, drop_attempt: int) -> None:
        self._inner = inner
        self._root = Path(root)
        self._drop_attempt = drop_attempt
        self._seen: dict[str, int] = {}
        self.name = inner.name

    def apply(
        self,
        idempotency_key: str,
        payload: str,
        fencing_token: int | None = None,
        fence_scope: str | None = None,
    ) -> DeliveryReceipt:
        seen = self._seen.get(idempotency_key, 0) + 1
        self._seen[idempotency_key] = seen
        if seen == self._drop_attempt:
            # The dropped attempt is recorded at the destination before it is
            # refused. Without it the destination's own log would show a single
            # attempt for the whole case, and "the resend happened" would be
            # unprovable from the counterparty's record -- which is the only
            # record ACCEPTANCE.md section 2 accepts for an external effect.
            self._log_dropped(idempotency_key, payload)
            raise DestinationRefusal(
                f"the harness dropped attempt {seen} for {idempotency_key!r}"
            )
        return self._inner.apply(idempotency_key, payload, fencing_token, fence_scope)

    def _log_dropped(self, idempotency_key: str, payload: str) -> None:
        line = json.dumps(
            {
                "fencing_token": None,
                "idempotency_key": idempotency_key,
                "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            },
            sort_keys=True,
        )
        log = Path(self._root) / destination_module.ATTEMPT_LOG_NAME
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(handle, (line + "\n").encode("utf-8"))
            os.fsync(handle)
        finally:
            os.close(handle)

    def effect_count(self, idempotency_key: str) -> int:
        return self._inner.effect_count(idempotency_key)

    def attempt_count(self, idempotency_key: str) -> int:
        return self._inner.attempt_count(idempotency_key)


class _UnavailableDropbox(_DroppingDropbox):
    """A destination that is unavailable for its first *N* attempts.

    ACCEPTANCE.md section 2's outbox row asks for the recipient to be held
    unavailable "across several retry attempts", with a retry count that is
    monotonically increasing and **survives a restart**. That last word is what
    dictates the shape here: the refusal budget is read from the destination's
    own append-only attempt log, not from a counter in this process. A
    process-local counter would start again at zero in every generation and go
    on refusing the first N attempts forever, so the message would never land
    and the case would be asserting a wedge rather than a resend.

    Reading the counterparty's own record also means the budget is measured in
    the same evidence the case asserts against.
    """

    def __init__(self, inner: KeyedDropbox, *, root: Path, unavailable_attempts: int) -> None:
        super().__init__(inner, root=root, drop_attempt=0)
        self._unavailable_attempts = int(unavailable_attempts)

    def apply(
        self,
        idempotency_key: str,
        payload: str,
        fencing_token: int | None = None,
        fence_scope: str | None = None,
    ) -> DeliveryReceipt:
        if self.attempt_count(idempotency_key) < self._unavailable_attempts:
            self._log_dropped(idempotency_key, payload)
            raise DestinationRefusal(
                f"the recipient is unavailable for {idempotency_key!r}"
            )
        return self._inner.apply(idempotency_key, payload, fencing_token, fence_scope)


def _deliverable(ctx: "Context") -> int:
    """How many of this role's messages this generation may deliver.

    Only ``dup-delivery`` narrows it, and only in the first generation:
    ``ACCEPTANCE.md`` section 2's dedup row asks for a **restart between the
    duplicate arrivals**, so the first copy is delivered and acked before the
    kill-free restart and the duplicate arrives afterwards, into a destination
    that has already seen the key. Delivering both before the restart would make
    the restart a no-op and the recovery assertion vacuous.
    """

    if BEHAVIOUR_DUP_DELIVERY in ctx.behaviours and ctx.restart_generation == 0:
        return 1
    return ctx.messages


def _dropbox_root(workdir: Path, role: str) -> Path:
    """One destination directory per role: its *own* destination (design 2.1)."""

    return Path(workdir) / "destinations" / role


# ---------------------------------------------------------------------------
# the harness refusal ledger -- design 5
# ---------------------------------------------------------------------------
#
# ``ACCEPTANCE.md`` section 2 requires the returning holder's refused write to be
# *recorded, not silently dropped*, and design section 5 holds that record to the
# same standard as any other observable: a SQLite query or a persisted field, and
# explicitly **not** a harness event-trace line -- a trace proves the harness saw
# an exception, not that the refusal is durable.
#
# S6 already records one class of refusal durably (``protected_write`` inserts a
# refused ``action`` row), but only that class: ``LeaseHeld``, ``LeaseNotHeld``
# and ``ClockSkewRefused`` leave no row at all, and S7's ``enqueue`` refusal is
# recorded under a ``kind`` that is not composed by ``effect_kind`` and therefore
# cannot be attributed to a resource by query. So providing the durable record is
# the driver's obligation, as the design says.
#
# **One deviation from design section 5, forced and deliberate.** The design puts
# this ledger "in the same database". It cannot go there: ``open_control_plane``
# verifies a sha256 fingerprint over *every* object in ``sqlite_master``, so a
# harness table added to the control plane makes the next open refuse the file
# outright (D-0026 promises no migration). The ledger is therefore a **sidecar
# SQLite file** beside the control plane. Everything else section 5 asks for is
# unchanged: append-only, harness-owned, written outside the fence because it
# records a failure to write control state, and read back by a named SQL query.
# It adds no table to the S5 spike schema and resolves no ``Q-``.

REFUSAL_LEDGER_NAME = "harness-refusals.sqlite3"

_REFUSAL_DDL = """
CREATE TABLE IF NOT EXISTS harness_refusal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    resource   TEXT    NOT NULL,
    holder     TEXT    NOT NULL,
    epoch      INTEGER NOT NULL,
    operation  TEXT    NOT NULL,
    refusal    TEXT    NOT NULL,
    now_ms     INTEGER NOT NULL
)
"""


def refusal_ledger_path(workdir: Path) -> Path:
    return Path(workdir) / REFUSAL_LEDGER_NAME


def record_refusal(
    ctx: "Context", *, operation: str, refusal: str, epoch: int, now_ms: int
) -> None:
    """Append one refusal. Its own connection, its own file, never fenced."""

    connection = sqlite3.connect(refusal_ledger_path(ctx.workdir))
    try:
        connection.execute(_REFUSAL_DDL)
        connection.execute(
            "INSERT INTO harness_refusal (resource, holder, epoch, operation, "
            "refusal, now_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.resource, ctx.holder, int(epoch), operation, refusal, int(now_ms)),
        )
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# the operation script -- design 2.1
# ---------------------------------------------------------------------------

@dataclass
class Context:
    """Everything one role process needs, and nothing warm across a restart."""

    role: str
    resource: str
    holder: str
    run_id: str
    db_path: Path
    workdir: Path
    case_id: str
    suite_seed: int
    manifest_version: int
    ttl_ms: int
    messages: int
    behaviours: tuple[str, ...]
    restart_generation: int
    clock: Clock
    barrier: Barrier
    emit: Callable[[Mapping[str, Any]], None]
    # -- I-11 case parameters ------------------------------------------------
    #
    # Every one of these arrives on the command line. None of them has a value
    # this module chose: the observation mode and the escalation policy are the
    # case's, and the three incident parameters are the case's precisely
    # because Q-0002 and Q-0003 are open and a driver-side default would settle
    # them by inertia (compare ``resource``/``holder``, which keep Q-0001 open
    # the same way).
    observation_mode: str = contract.OBSERVATION_HEALTHY
    escalate_on: tuple[str, ...] = ()
    incident_dedup_key: str | None = None
    incident_repeats: int = 0
    incident_collapse: str | None = None
    incident_renotify_window_ms: int | None = None
    incident_reconcile_interval_ms: int | None = None
    unavailable_attempts: int = DEFAULT_UNAVAILABLE_ATTEMPTS
    connection: sqlite3.Connection = field(init=False)
    lease: Lease | None = field(default=None, init=False)
    rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(
            contract.case_seed(
                manifest_version=self.manifest_version,
                case_id=self.case_id,
                suite_seed=self.suite_seed,
            )
            ^ _role_salt(self.role)
        )

    # -- message identity, derived and therefore restart-stable -------------

    def message_id(self, index: int) -> str:
        return f"{self.holder}-m{index}"

    def dedup_key(self, index: int) -> str:
        # ``dup-delivery`` is *two messages under one dedup key*: the duplicate
        # arrives as its own row and the destination's idempotency key collapses
        # it, which is the ACCEPTANCE.md section 2 dedup row exactly.
        if BEHAVIOUR_DUP_DELIVERY in self.behaviours:
            return f"{self.holder}-dedup"
        return f"{self.holder}-dedup-{index}"

    def payload(self, index: int) -> str:
        # Payload bytes are the seed's business (design 4.3) and nothing else
        # is: the seed never chooses a checkpoint, a fault or a target.
        token = "".join(self.rng.choice("0123456789abcdef") for _ in range(8))
        if BEHAVIOUR_DUP_DELIVERY in self.behaviours:
            # "Deliver the same message twice" means the *same* message: one
            # dedup key and one payload. A duplicate whose bytes differed would
            # be a payload conflict, which the destination refuses outright --
            # correctly, and it is a different case from the dedup row.
            return json.dumps({"n": 0, "token": "duplicate"}, sort_keys=True)
        return json.dumps({"n": index, "token": token}, sort_keys=True)


def _role_salt(role: str) -> int:
    return {"sup": 0x11, "disp": 0x22, "sec": 0x33}.get(role, 0)


def _attempt_id(ctx: Context, operation: str, repeat: int = 0) -> str:
    """A refusal's ``action_id``: deterministic, and unique per attempt.

    ``protected_write`` passes ``attempt_id`` straight through as the primary
    key of the refusal row, so an id composed only of holder and operation
    collides the moment the same stale writer is refused twice -- and the
    collision surfaces as a raw ``IntegrityError`` from inside the transaction
    *instead of* ``StaleWriterRefused``, losing the refusal record that
    ACCEPTANCE.md section 2 requires to be durable. S7 learned this already and
    randomises its own bare-refusal ids; a harness cannot, because a uuid would
    break the byte-identical-trace property. So the generation and the repeat
    index -- both script-declared and both on the command line or derived from
    it -- carry the uniqueness instead.
    """

    return f"refused-{ctx.holder}-{operation}-g{ctx.restart_generation}-r{repeat}"


def _rows(connection: sqlite3.Connection, sql: str, params: Mapping[str, Any]) -> list[dict]:
    cursor = connection.execute(sql, dict(params))
    try:
        columns = [column[0] for column in cursor.description or ()]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def _open_or_create(ctx: Context) -> sqlite3.Connection:
    """Each role process opens its **own** connection (design 2.1).

    Never inherited across the spawn, never shared between roles: a SIGKILL has
    to take down a connection mid-transaction, which is the crash SQLite's
    journal actually has to recover from.
    """

    return open_control_plane(ctx.db_path)


# -- lease -----------------------------------------------------------------

def op_lease_acquire(ctx: Context) -> Lease | None:
    """Take or resume this role's own lease, or be refused and record it.

    A restarted holder whose lease row is still its own and still live *renews*
    rather than re-acquiring: renewal keeps the epoch, and keeping the epoch is
    what lets the restarted process own the outbox rows its predecessor stamped.
    When the lease has lapsed or moved on, re-acquiring raises the epoch -- and
    then ``Outbox.recover`` re-stamps the orphaned rows, which is the other half
    of the same recovery.

    **Refusal at acquire returns ``None`` rather than raising.** Two of the
    ACCEPTANCE.md section 2 rows need this. A second live claimant on one
    resource is refused here, by ``acquire``'s upsert, and not at any later
    write -- so "two writers race for the same state item ... a stale writer is
    rejected, not merged" is observed at exactly this point. So is the return of
    a holder that was SIGKILLed without releasing: it comes back with no epoch
    in memory, re-runs its script from the top, and meets the claimant that took
    the resource over. ``LeaseHeld`` is persisted nowhere by S6, so the refusal
    ledger is what makes it the durable record section 2 demands rather than an
    exception nobody kept.
    """

    ctx.barrier.hit(CHECKPOINT_BEFORE_DURABLE_WRITE, operation=OPERATION_LEASE_ACQUIRE)
    now_ms = ctx.clock.advance()
    observed = read_lease(ctx.connection, ctx.resource)
    took = "acquired"
    lease: Lease | None = None
    try:
        if (
            observed is not None
            and observed.holder == ctx.holder
            and observed.looks_live_at(now_ms)
        ):
            try:
                lease = renew(ctx.connection, observed, now_ms=now_ms, ttl_ms=ctx.ttl_ms)
                took = "renewed"
            except LeaseNotHeld:
                lease = acquire(
                    ctx.connection,
                    resource=ctx.resource,
                    holder=ctx.holder,
                    now_ms=now_ms,
                    ttl_ms=ctx.ttl_ms,
                )
        else:
            lease = acquire(
                ctx.connection,
                resource=ctx.resource,
                holder=ctx.holder,
                now_ms=now_ms,
                ttl_ms=ctx.ttl_ms,
            )
    except LeaseHeld as error:
        took = f"refused:{type(error).__name__}"
        record_refusal(
            ctx,
            operation=OPERATION_LEASE_ACQUIRE,
            refusal=type(error).__name__,
            # No epoch was granted, and saying so is the honest record: the
            # ledger's epoch column is what this writer *held*, which is
            # nothing.
            epoch=0,
            now_ms=now_ms,
        )
        if BEHAVIOUR_STALE_WRITER in ctx.behaviours:
            # ... and now carry on anyway, holding a token the lease row will
            # reject. Not a way around the refusal: the refusal above is
            # recorded either way. It is how the case reaches the *other* half
            # of the single-writer observable, the one about the write history,
            # which a writer that stops at ``acquire`` can never reach.
            #
            # The epoch is taken from the row that actually exists and moved one
            # past it, which is what a writer that had lost the lease without
            # noticing would present.
            observed_now = read_lease(ctx.connection, ctx.resource)
            lease = Lease(
                resource=ctx.resource,
                holder=ctx.holder,
                epoch=(observed_now.epoch if observed_now is not None else 0) + 1,
                acquired_at_ms=now_ms,
                expires_at_ms=now_ms + ctx.ttl_ms,
            )
            took = f"stale-writer:{type(error).__name__}"
    ctx.lease = lease
    ctx.barrier.hit(
        CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT, operation=OPERATION_LEASE_ACQUIRE
    )
    ctx.emit(
        {
            "event": EVENT_STEP,
            "operation": OPERATION_LEASE_ACQUIRE,
            "outcome": took,
            "epoch": lease.epoch if lease is not None else 0,
            "now_ms": now_ms,
        }
    )
    return lease


def op_lease_renew(ctx: Context) -> None:
    assert ctx.lease is not None
    ctx.barrier.hit(CHECKPOINT_BEFORE_DURABLE_WRITE, operation=OPERATION_LEASE_RENEW)
    now_ms = ctx.clock.advance()
    try:
        ctx.lease = renew(ctx.connection, ctx.lease, now_ms=now_ms, ttl_ms=ctx.ttl_ms)
        outcome = "renewed"
    except (LeaseNotHeld, lease_module.ClockSkewRefused) as error:
        # Losing a renewal is a legitimate observation under a skew or takeover
        # case, not a driver fault: the refusal is the evidence. Only the class
        # name goes on the wire -- refusal texts carry a uuid and would break the
        # identical-trace property (design 6.3) -- and the durable record goes to
        # the ledger, because S6 records no row for either of these.
        outcome = f"refused:{type(error).__name__}"
        record_refusal(
            ctx,
            operation=OPERATION_LEASE_RENEW,
            refusal=type(error).__name__,
            epoch=ctx.lease.epoch if ctx.lease else 0,
            now_ms=now_ms,
        )
    ctx.barrier.hit(CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT, operation=OPERATION_LEASE_RENEW)
    ctx.emit(
        {
            "event": EVENT_STEP,
            "operation": OPERATION_LEASE_RENEW,
            "outcome": outcome,
            "epoch": ctx.lease.epoch if ctx.lease else None,
            "now_ms": now_ms,
        }
    )


def op_lease_release(ctx: Context) -> None:
    assert ctx.lease is not None
    ctx.barrier.hit(CHECKPOINT_BEFORE_DURABLE_WRITE, operation=OPERATION_LEASE_RELEASE)
    now_ms = ctx.clock.advance()
    try:
        ctx.lease = lease_module.release(ctx.connection, ctx.lease, now_ms=now_ms)
        outcome = "released"
    except LeaseNotHeld as error:
        outcome = f"refused:{type(error).__name__}"
        record_refusal(
            ctx,
            operation=OPERATION_LEASE_RELEASE,
            refusal=type(error).__name__,
            epoch=ctx.lease.epoch if ctx.lease else 0,
            now_ms=now_ms,
        )
    ctx.barrier.hit(CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT, operation=OPERATION_LEASE_RELEASE)
    ctx.emit(
        {
            "event": EVENT_STEP,
            "operation": OPERATION_LEASE_RELEASE,
            "outcome": outcome,
            "now_ms": now_ms,
        }
    )


# -- the Supervisor's identity binding --------------------------------------

def op_bind(ctx: Context) -> None:
    """Bind an identity to this role's run -- the Supervisor's write-set.

    Resumable by query: the schema's ``session_one_active_binding_per_run``
    index permits exactly one live session per run, so a restarted Supervisor
    that re-inserted would hit an ``IntegrityError`` instead of recovering. It
    asks first, which is what D-0001 requires of every restart.
    """

    assert ctx.lease is not None
    session_id = f"{ctx.holder}-session"
    existing = _rows(
        ctx.connection,
        "SELECT session_id FROM session WHERE run_id = :run_id "
        "AND released_at_ms IS NULL",
        {"run_id": ctx.run_id},
    )
    if existing:
        ctx.emit(
            {
                "event": EVENT_STEP,
                "operation": OPERATION_BIND,
                "outcome": "adopted",
                "now_ms": ctx.clock.now_ms(),
            }
        )
        return

    ctx.barrier.hit(CHECKPOINT_BEFORE_DURABLE_WRITE, operation=OPERATION_BIND)
    now_ms = ctx.clock.advance()
    statement = fenced_insert(
        "session",
        columns=(
            "session_id",
            "run_id",
            "provider",
            "observation",
            "provider_state",
            "bound_at_ms",
        ),
        values=(
            ":session_id",
            ":run_id",
            ":provider",
            ":observation",
            ":provider_state",
            ":bound_at_ms",
        ),
        # ``session`` genuinely has no ``writer_epoch`` column; the fence is
        # still a clause of the write itself.
        stamps_writer_epoch=False,
    )
    write = ProtectedWrite(
        kind=effect_kind(ctx.resource, "bind_session"),
        idempotency_key=f"bind_session:{session_id}",
        statement=statement,
        exactly_once_mechanism="transactional_with_record",
        params={
            "session_id": session_id,
            "run_id": ctx.run_id,
            "provider": "harness",
            "observation": "observed",
            "provider_state": "running",
            "bound_at_ms": now_ms,
        },
        run_id=ctx.run_id,
    )
    try:
        protected_write(
            ctx.connection,
            ctx.lease,
            write,
            now_ms=now_ms,
            # A deterministic refusal id: the module's default is a uuid4, and a
            # uuid in the evidence is a re-run that cannot be compared. It also
            # has to be unique *per attempt* -- see :func:`_attempt_id`.
            attempt_id=_attempt_id(ctx, OPERATION_BIND),
        )
        outcome = "bound"
    except lease_module.StaleWriterRefused as error:
        outcome = f"refused:{type(error).__name__}"
        record_refusal(
            ctx,
            operation=OPERATION_BIND,
            refusal=type(error).__name__,
            epoch=ctx.lease.epoch,
            now_ms=now_ms,
        )
    ctx.barrier.hit(CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT, operation=OPERATION_BIND)
    ctx.emit(
        {
            "event": EVENT_STEP,
            "operation": OPERATION_BIND,
            "outcome": outcome,
            "now_ms": now_ms,
        }
    )


# -- the observation seam and the incident packet ----------------------------
#
# ACCEPTANCE.md section 2's last row asks for the observation path to "fail or
# return nothing while the worker is genuinely healthy", classified
# ``OBSERVATION_UNAVAILABLE`` and never as an anomaly, with
# ``NO_ACTIVITY_EVIDENCE`` likewise not an anomaly (D-0006, AC-3/AC-4).
#
# The shape below is chosen so the case can actually FAIL. The fault acts on the
# **reader** -- a file the driver reads through -- and the classifier maps the
# reader's outcome onto a fact state. If the reader collapsed a read failure into
# an empty result (the exact defect D-0006 exists to police) the two modes would
# produce the same fact state and the case would go red. A design in which the
# same step both chose the fact state and asserted it could only fail if it
# contradicted itself, which is not a test of anything.


class ObservationUnavailable(RuntimeError):
    """The observation path failed. Not an anomaly -- a missing observation."""


def observation_path(workdir: Path, role: str) -> Path:
    return workdir / "observations" / role / OBSERVATION_FILE_NAME


def write_observation(workdir: Path, role: str, *, mode: str) -> None:
    """Prepare the seam the case's observation mode asks for.

    ``unreadable`` deliberately leaves *no* file: the read raises. ``silent``
    writes a well-formed observation carrying no activity -- readable, and
    empty, which is a different fact about the world from "we could not look".
    """

    path = observation_path(workdir, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == contract.OBSERVATION_UNREADABLE:
        if path.exists():
            path.unlink()
        return
    activity = [] if mode == contract.OBSERVATION_SILENT else [{"kind": "tool_use"}]
    path.write_text(json.dumps({"activity": activity}, sort_keys=True), encoding="utf-8")


def read_observation(ctx: Context) -> list:
    """Read the worker's activity, or fail to.

    Raising and returning nothing are kept apart on purpose: this function is
    the seam D-0006 is about, and a reader that swallowed the exception into an
    empty list would make an outage indistinguishable from a quiet worker.
    """

    path = observation_path(ctx.workdir, ctx.role)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ObservationUnavailable(str(path)) from error
    return list(json.loads(raw).get("activity", ()))


def classify_observation(ctx: Context) -> str:
    """The reader's outcome, named. Nothing here is a verdict.

    D-0005 fixes the names and D-0006 fixes one relation between two of them;
    per-state semantics are Q-0012 and stay open, so this function maps *what
    the read did* onto a name and stops there. It never decides what the name
    means, and no other function maps a name onto an action.
    """

    try:
        activity = read_observation(ctx)
    except ObservationUnavailable:
        return contract.FACT_OBSERVATION_UNAVAILABLE
    return contract.FACT_ACTIVE_EVIDENCE if activity else contract.FACT_NO_ACTIVITY_EVIDENCE


def raise_incident(
    ctx: Context,
    *,
    fact_state: str,
    dedup_key: str,
    repeat: int,
    now_ms: int,
    lease: Lease | None = None,
) -> tuple[str, str]:
    """Persist one incident packet, collapsing per the case's declared rule.

    Returns ``(incident_id, outcome)``. Both Q-0002 rules are implemented and
    the caller is *told* which to apply -- the schema deliberately permits both
    (``dedup_key`` is indexed but not unique, ``related_incident_id`` is a plain
    nullable self-reference) and this driver may not choose between them any
    more than the schema did.
    """

    assert ctx.lease is not None
    lease = lease if lease is not None else ctx.lease
    open_rows = _rows(
        ctx.connection,
        "SELECT incident_id, retry_count, created_at_ms FROM incident "
        "WHERE dedup_key = :dedup_key AND resolved_at_ms IS NULL "
        "ORDER BY created_at_ms, incident_id",
        {"dedup_key": dedup_key},
    )
    window_ms = ctx.incident_renotify_window_ms
    within_window = bool(open_rows) and (
        window_ms is None or (now_ms - int(open_rows[0]["created_at_ms"])) <= window_ms
    )

    if within_window and ctx.incident_collapse == COLLAPSE_INCREMENT_IN_PLACE:
        root = open_rows[0]
        statement = fenced_update(
            "incident",
            set_clause="retry_count = retry_count + 1, updated_at_ms = :updated_at_ms",
            where="incident_id = :incident_id AND resolved_at_ms IS NULL",
            # ``incident`` has no ``writer_epoch`` column, so the fence is a
            # clause of the write without stamping one.
            stamps_writer_epoch=False,
        )
        params = {"incident_id": root["incident_id"], "updated_at_ms": now_ms}
        incident_id = str(root["incident_id"])
        outcome = "collapsed"
    else:
        incident_id = f"{dedup_key}-i{repeat}"
        related = str(open_rows[0]["incident_id"]) if within_window else None
        statement = fenced_insert(
            "incident",
            columns=(
                "incident_id",
                "run_id",
                "session_id",
                "fact_state",
                "detector_version",
                "dedup_key",
                "retry_count",
                "related_incident_id",
                "created_at_ms",
                "updated_at_ms",
            ),
            values=(
                ":incident_id",
                ":run_id",
                ":session_id",
                ":fact_state",
                ":detector_version",
                ":dedup_key",
                "0",
                ":related_incident_id",
                ":created_at_ms",
                ":updated_at_ms",
            ),
            stamps_writer_epoch=False,
        )
        params = {
            "incident_id": incident_id,
            "run_id": ctx.run_id,
            # Only the Supervisor's script binds a session, and the foreign key
            # is enforced, so any other role's incident carries none.
            # Foreign keys are enforced, and the binding is not guaranteed to
            # exist: only the Supervisor's script binds at all, and even there
            # the bind can have been refused by a fence. So the row is looked
            # up rather than assumed.
            "session_id": _bound_session_id(ctx),
            "fact_state": fact_state,
            "detector_version": DETECTOR_VERSION,
            "dedup_key": dedup_key,
            "related_incident_id": related,
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
        outcome = "linked" if related else "opened"

    write = ProtectedWrite(
        kind=effect_kind(ctx.resource, "raise_incident"),
        idempotency_key=f"raise_incident:{incident_id}:{repeat}",
        statement=statement,
        exactly_once_mechanism="transactional_with_record",
        params=params,
        run_id=ctx.run_id,
        # Deliberately NOT ``incident_id=incident_id``: on the refusal path that
        # would insert an ``action`` row referencing an incident this write did
        # not manage to create, which is a foreign-key violation in exactly the
        # case where the refusal record matters most.
    )
    try:
        protected_write(
            ctx.connection,
            lease,
            write,
            now_ms=now_ms,
            attempt_id=_attempt_id(ctx, "raise_incident", repeat),
        )
    except lease_module.StaleWriterRefused as error:
        record_refusal(
            ctx,
            operation=OPERATION_OBSERVE,
            refusal=type(error).__name__,
            epoch=lease.epoch,
            now_ms=now_ms,
        )
        outcome = f"refused:{type(error).__name__}"
    return incident_id, outcome


def escalate(ctx: Context, *, fact_state: str, incident_id: str, now_ms: int) -> str:
    """Record a termination/restart recommendation -- or refuse to.

    This is where D-0006 is *enforced* rather than merely hoped for. The
    escalation policy is case data: the manifest names which fact states this
    case would escalate on, and the driver refuses the two D-0006 settles are
    not anomalies even when it is asked, recording that refusal durably. That is
    what makes "no termination or restart recommendation is produced from it" an
    assertion about a row a broken driver would have written, rather than a
    count of rows nothing in the tree can write.

    Nothing here reads a fact state's *meaning*: the policy arrives from
    outside, so Q-0012 stays open.
    """

    assert ctx.lease is not None
    if fact_state not in ctx.escalate_on:
        return "not-escalated"
    if fact_state in contract.ESCALATION_REFUSED_FACT_STATES:
        record_refusal(
            ctx,
            operation=OPERATION_OBSERVE,
            refusal="EscalationRefusedNotAnAnomaly",
            epoch=ctx.lease.epoch,
            now_ms=now_ms,
        )
        return "escalation-refused"
    # The recommendation is an ``action`` row, which is what the schema calls a
    # side-effect record -- and it has to be written by a *fenced* insert,
    # because ``protected_write`` only synthesises an action row on the refusal
    # path. A successful protected write leaves no action row behind, so an
    # escalation recorded any other way would be invisible to the query that is
    # supposed to catch it.
    statement = fenced_insert(
        "action",
        columns=(
            "action_id",
            "run_id",
            "kind",
            "idempotency_key",
            "exactly_once_mechanism",
            "status",
            # ``action`` really does carry a ``writer_epoch``, so the fence
            # stamps one. Omitting the column while leaving the builder's
            # default in place raises ``UnfencedStatement`` before the row is
            # ever written -- which would make this whole path unreachable, and
            # a "no recommendation was produced" assertion means nothing if a
            # recommendation could not have been produced either way.
            "writer_epoch",
            "created_at_ms",
            "applied_at_ms",
        ),
        values=(
            ":action_id",
            ":run_id",
            ":kind",
            ":idempotency_key",
            ":exactly_once_mechanism",
            "'applied'",
            ":fence_epoch",
            ":created_at_ms",
            ":applied_at_ms",
        ),
    )
    escalation_id = f"{incident_id}-escalation"
    kind = effect_kind(ctx.resource, ESCALATION_EFFECT)
    write = ProtectedWrite(
        kind=kind,
        idempotency_key=f"{ESCALATION_EFFECT}:{escalation_id}",
        statement=statement,
        # D-0004: an action with a real side effect is not the AI's to apply.
        # A restart recommendation is exactly that, so it names the human gate.
        exactly_once_mechanism="human_gate",
        params={
            "action_id": escalation_id,
            "run_id": ctx.run_id,
            "kind": kind,
            "idempotency_key": f"{ESCALATION_EFFECT}:{escalation_id}",
            "exactly_once_mechanism": "human_gate",
            "created_at_ms": now_ms,
            "applied_at_ms": now_ms,
        },
        run_id=ctx.run_id,
    )
    protected_write(
        ctx.connection,
        ctx.lease,
        write,
        now_ms=now_ms,
        attempt_id=_attempt_id(ctx, ESCALATION_EFFECT),
    )
    return "escalated"


def _bound_session_id(ctx: Context) -> str | None:
    """This role's live session binding, or ``None`` if it has none."""

    rows = _rows(
        ctx.connection,
        "SELECT session_id FROM session WHERE run_id = :run_id "
        "AND released_at_ms IS NULL",
        {"run_id": ctx.run_id},
    )
    return str(rows[0]["session_id"]) if rows else None


def op_observe(ctx: Context) -> None:
    """Read the worker, name what the read found, and persist the packet.

    The repeats are the ACCEPTANCE.md section 2 dedup row's "raise the same
    incident condition repeatedly within a window"; the window and the collapse
    rule are the case's, never this module's.
    """

    assert ctx.lease is not None
    dedup_key = ctx.incident_dedup_key or f"{ctx.holder}-observation"
    repeats = max(1, ctx.incident_repeats)

    # Resumable by query, like every other step (D-0001). A restarted process
    # re-runs its script from the top, and re-observing would either collide on
    # the incident's primary key or increment a retry count that no repeat
    # earned -- so an observation already on record is adopted rather than
    # taken again. The seam is also left as the predecessor found it: a restart
    # must not repair the observation path on its way past.
    if ctx.restart_generation > 0 and _rows(
        ctx.connection,
        "SELECT incident_id FROM incident WHERE dedup_key = :dedup_key",
        {"dedup_key": dedup_key},
    ):
        ctx.emit(
            {
                "event": EVENT_STEP,
                "operation": OPERATION_OBSERVE,
                "outcome": "adopted",
                "now_ms": ctx.clock.now_ms(),
            }
        )
        return

    fact_state = classify_observation(ctx)
    # The stale-writer injection: a token one epoch off the row, so every
    # protected write below is fenced out. Two repeats, because one refusal
    # cannot collide with anything -- the defect this exists to expose is a
    # refusal id that repeats.
    stale = BEHAVIOUR_STALE_WRITER in ctx.behaviours
    lease = (
        replace(ctx.lease, epoch=ctx.lease.epoch + 1) if stale else None
    )
    if stale:
        repeats = max(repeats, 2)
    replay = BEHAVIOUR_INCIDENT_REPLAY in ctx.behaviours
    outcomes: list[str] = []
    incident_id = ""
    for repeat in range(repeats):
        ctx.barrier.hit(CHECKPOINT_BEFORE_DURABLE_WRITE, operation=OPERATION_OBSERVE)
        now_ms = ctx.clock.advance()
        raised = fact_state
        if replay and repeat:
            # The replay: this raise is not a fresh observation at all, it is
            # the persisted packet read back and submitted again. Whether the
            # replay is collapsed or opens a linked incident is the case's
            # declared rule -- the same rule a repeat follows -- which is the
            # point: a replayed packet must not be a way around dedup.
            persisted = _rows(
                ctx.connection,
                "SELECT fact_state FROM incident WHERE dedup_key = :dedup_key "
                "ORDER BY created_at_ms, incident_id LIMIT 1",
                {"dedup_key": dedup_key},
            )
            if persisted:
                raised = str(persisted[0]["fact_state"])
        incident_id, outcome = raise_incident(
            ctx,
            fact_state=raised,
            dedup_key=dedup_key,
            repeat=repeat,
            now_ms=now_ms,
            lease=lease,
        )
        outcomes.append(outcome)
        ctx.barrier.hit(CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT, operation=OPERATION_OBSERVE)
    escalation = escalate(
        ctx, fact_state=fact_state, incident_id=incident_id, now_ms=ctx.clock.now_ms()
    )
    ctx.emit(
        {
            "event": EVENT_STEP,
            "operation": OPERATION_OBSERVE,
            "outcome": ",".join(outcomes),
            "fact_state": fact_state,
            "escalation": escalation,
            "now_ms": ctx.clock.now_ms(),
        }
    )
    ctx.barrier.hit(contract.SYNC_OBSERVED, operation=OPERATION_OBSERVE, kind=EVENT_SYNC)


# -- the outbox surface ------------------------------------------------------

def _outbox(ctx: Context) -> Outbox:
    root = _dropbox_root(ctx.workdir, ctx.role)
    dropbox: Any = KeyedDropbox(root, name=f"{ctx.role}-dropbox")
    if BEHAVIOUR_DROP_DELIVERY in ctx.behaviours and ctx.restart_generation == 0:
        # Only the first generation drops: a restart's job is to drive the
        # unfinished work to resolution, and a destination that keeps refusing
        # would be testing the harness's patience rather than the resend.
        dropbox = _DroppingDropbox(dropbox, root=root, drop_attempt=1)
    elif BEHAVIOUR_RECIPIENT_UNAVAILABLE in ctx.behaviours:
        # Deliberately *not* gated on the generation: this budget is durable, so
        # it keeps counting across the restart and stops refusing on its own.
        dropbox = _UnavailableDropbox(
            dropbox, root=root, unavailable_attempts=ctx.unavailable_attempts
        )
    return Outbox(
        ctx.connection,
        resource=ctx.resource,
        holder=ctx.holder,
        registry=spike_registry(dropbox),
        checkpoint=lambda name: ctx.barrier.hit(name, operation=OPERATION_ATTEMPT),
    )


def op_enqueue(ctx: Context, outbox: Outbox) -> None:
    assert ctx.lease is not None
    for index in range(ctx.messages):
        message_id = ctx.message_id(index)
        payload = ctx.payload(index)
        known = _rows(
            ctx.connection,
            "SELECT message_id FROM outbox WHERE message_id = :message_id",
            {"message_id": message_id},
        )
        if known:
            ctx.emit(
                {
                    "event": EVENT_STEP,
                    "operation": OPERATION_ENQUEUE,
                    "outcome": "already-enqueued",
                    "message_id": message_id,
                    "now_ms": ctx.clock.now_ms(),
                }
            )
            continue
        ctx.barrier.hit(CHECKPOINT_BEFORE_DURABLE_WRITE, operation=OPERATION_ENQUEUE)
        now_ms = ctx.clock.advance()
        try:
            outbox.enqueue(
                message_id=message_id,
                recipient=NOTIFY_RECIPIENT,
                payload=payload,
                dedup_key=ctx.dedup_key(index),
                now_ms=now_ms,
                epoch=ctx.lease.epoch,
                run_id=ctx.run_id,
            )
            outcome = "enqueued"
        except outbox_module.StaleWriterRefused as error:
            outcome = f"refused:{type(error).__name__}"
            record_refusal(
                ctx,
                operation=OPERATION_ENQUEUE,
                refusal=type(error).__name__,
                epoch=ctx.lease.epoch,
                now_ms=now_ms,
            )
        ctx.barrier.hit(CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT, operation=OPERATION_ENQUEUE)
        ctx.emit(
            {
                "event": EVENT_STEP,
                "operation": OPERATION_ENQUEUE,
                "outcome": outcome,
                "message_id": message_id,
                "now_ms": now_ms,
            }
        )


def op_attempt(ctx: Context, outbox: Outbox) -> None:
    """The record -> effect -> result path: where all four windows live.

    Scoped to **this role's own rows**, by the message ids this role derives
    from its own holder identity. That scoping is load-bearing rather than
    tidy: ``Outbox.due()`` returns every unacked row in the database, not the
    rows of the outbox object's own ``(resource, holder)``, and the fence it
    validates is ``writer_epoch = :epoch`` against *this* writer's live lease --
    so with every role sitting at epoch 1 (which is the normal case, since each
    holds a different resource) one role's delivery loop will happily deliver
    another role's messages into its own destination. Disjoint write-sets are
    what makes a combination case a cross-role interleaving rather than three
    processes doing each other's work (design 2.1 item 5), so the driver scopes
    what the API does not.
    """

    assert ctx.lease is not None
    due = {message.message_id: message for message in outbox.due(ctx.clock.now_ms())}
    for index in range(_deliverable(ctx)):
        message_id = ctx.message_id(index)
        message = due.get(message_id)
        if message is None or message.status == "acked":
            continue
        # One attempt per message normally. A case that holds the recipient
        # unavailable needs *several*, and they have to happen here: a resend
        # driven only by restart generations would give at most two attempts,
        # and "monotonically increasing" wants more than one increment to be
        # meaningful. The loop is bounded so a destination that refuses forever
        # becomes an attributable failure and not a wedge (design 8.2), and it
        # only ever runs more than once for a case that asked for it -- so no
        # other case's ``attempt`` occurrence indices move.
        attempts = (
            MAX_ATTEMPTS_PER_MESSAGE
            if BEHAVIOUR_RECIPIENT_UNAVAILABLE in ctx.behaviours
            else 1
        )
        for _ in range(attempts):
            now_ms = ctx.clock.advance()
            try:
                result = outbox.attempt(message_id, now_ms=now_ms, epoch=ctx.lease.epoch)
                outcome = "delivered"
                deduplicated = result.deduplicated
            except DestinationRefusal as error:
                outcome = f"refused:{type(error).__name__}"
                deduplicated = False
            except outbox_module.StaleWriterRefused as error:
                outcome = f"refused:{type(error).__name__}"
                deduplicated = False
                record_refusal(
                    ctx,
                    operation=OPERATION_ATTEMPT,
                    refusal=type(error).__name__,
                    epoch=ctx.lease.epoch,
                    now_ms=now_ms,
                )
            ctx.emit(
                {
                    "event": EVENT_STEP,
                    "operation": OPERATION_ATTEMPT,
                    "outcome": outcome,
                    "message_id": message_id,
                    "deduplicated": deduplicated,
                    "now_ms": now_ms,
                }
            )
            if outcome == "delivered":
                break


def _ack_repeats(ctx: Context) -> int:
    """How many times each message is acked in this generation.

    Once by default. It used to be twice unconditionally, as standing evidence
    that acks are idempotent -- but ACCEPTANCE.md section 2's Ack row asks for
    "duplicate the ack" and "ack an already-acked message" as *injections*, and
    an injection every case performs anyway is one no case can fail on. So the
    repeat is behaviour-driven now: the cases that name the injection get it,
    and the baseline cases ack once, which is what lets a regression in one of
    the two shapes actually turn a case red.
    """

    if BEHAVIOUR_DUP_ACK in ctx.behaviours or BEHAVIOUR_RE_ACK in ctx.behaviours:
        return 2
    return 1


def op_ack(ctx: Context, outbox: Outbox) -> None:
    """Record the acks for this role's own messages."""

    if BEHAVIOUR_LOST_ACK in ctx.behaviours and ctx.restart_generation == 0:
        ctx.emit(
            {
                "event": EVENT_STEP,
                "operation": OPERATION_ACK,
                "outcome": "lost",
                "now_ms": ctx.clock.now_ms(),
            }
        )
        return
    for index in range(ctx.messages):
        message_id = ctx.message_id(index)
        rows = _rows(
            ctx.connection,
            "SELECT status FROM outbox WHERE message_id = :message_id",
            {"message_id": message_id},
        )
        if not rows or rows[0]["status"] == "pending":
            continue
        if BEHAVIOUR_RE_ACK in ctx.behaviours and rows[0]["status"] != "acked":
            # "Ack an already-acked message" means exactly that: drive the row
            # to its terminal state first, then ack it again below. Without
            # this the second ack would be a duplicate of a non-terminal ack,
            # which is the *other* injection.
            outbox.record_ack(message_id, now_ms=ctx.clock.advance())
        for _ in range(_ack_repeats(ctx)):
            ctx.barrier.hit(CHECKPOINT_BEFORE_DURABLE_WRITE, operation=OPERATION_ACK)
            now_ms = ctx.clock.advance()
            outcome = outbox.record_ack(message_id, now_ms=now_ms)
            if not outcome.recorded:
                # An ack against a row that is already terminal changes nothing
                # -- which is the invariant, and which is also why it leaves no
                # trace of its own anywhere in the control plane. That silence
                # is a problem for the two cases whose whole injection is the
                # *second* ack: with no record, a case asserting "exactly one
                # acked state" passes identically whether the duplicate was
                # issued or never happened at all.
                #
                # So the ignored ack goes in the harness ledger, which exists
                # for exactly this -- the classes S6/S7 persist nowhere. It is a
                # persisted, query-answerable row, which is the standard
                # ACCEPTANCE.md section 2 sets, and it makes the ack-multiplicity
                # cases fail if the multiplicity ever stops happening.
                record_refusal(
                    ctx,
                    operation=OPERATION_ACK,
                    refusal="AckAlreadyRecorded",
                    epoch=ctx.lease.epoch if ctx.lease is not None else 0,
                    now_ms=now_ms,
                )
            ctx.barrier.hit(
                CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT, operation=OPERATION_ACK
            )
            ctx.emit(
                {
                    "event": EVENT_STEP,
                    "operation": OPERATION_ACK,
                    "outcome": "recorded" if outcome.recorded else "already-acked",
                    "message_id": message_id,
                    "now_ms": now_ms,
                }
            )


_OPERATIONS: Mapping[str, Any] = {
    OPERATION_LEASE_ACQUIRE: op_lease_acquire,
    OPERATION_LEASE_RENEW: op_lease_renew,
    OPERATION_LEASE_RELEASE: op_lease_release,
    OPERATION_BIND: op_bind,
    OPERATION_OBSERVE: op_observe,
}


# ---------------------------------------------------------------------------
# recovery -- design 2.1, item 4 of the role-process contract
# ---------------------------------------------------------------------------

def recover(ctx: Context, outbox: Outbox) -> None:
    """Recover before proceeding. The command line and the file are the input.

    Reconstruct by query, re-establish the lease (already done by
    ``op_lease_acquire``), adopt the rows the dead generation left unowned, and
    drive them to resolution. Only then does the operation script continue.
    """

    assert ctx.lease is not None
    now_ms = ctx.clock.advance()
    report = outbox.recover(now_ms=now_ms, epoch=ctx.lease.epoch)
    ctx.emit(
        {
            "event": EVENT_STEP,
            "operation": "recover",
            "adopted": sorted(report.adopted),
            "still_unowned": sorted(report.still_unowned),
            "now_ms": now_ms,
        }
    )
    op_attempt(ctx, outbox)
    op_ack(ctx, outbox)
    ctx.emit(
        {
            "event": EVENT_RECOVERY_COMPLETE,
            "generation": ctx.restart_generation,
            "now_ms": ctx.clock.now_ms(),
        }
    )


def run_script(ctx: Context) -> None:
    """Run this role's operation script (design 2.1)."""

    steps = ROLE_SCRIPTS[ctx.role]
    outbox: Outbox | None = None

    for step in steps:
        if step == OPERATION_LEASE_ACQUIRE:
            if op_lease_acquire(ctx) is None:
                # Refused at acquire. The script is *over*, and it ended
                # correctly: this writer was rejected rather than merged, which
                # is the whole of what the case is asserting. Carrying on would
                # mean writing without a lease, which is the defect.
                if ctx.restart_generation > 0:
                    # A restart's contract is "recover before you proceed", and
                    # this restart did: it reconstructed its view from SQLite
                    # alone, found the resource held by someone else, and
                    # declined to write. That is recovery concluding correctly,
                    # not recovery failing to happen -- so the event is emitted,
                    # and the controller is not left waiting on a process that
                    # has already done everything it may do.
                    ctx.emit(
                        {
                            "event": EVENT_RECOVERY_COMPLETE,
                            "generation": ctx.restart_generation,
                            "adopted": [],
                            "outcome": "refused",
                            "now_ms": ctx.clock.now_ms(),
                        }
                    )
                break
            ctx.barrier.hit(
                contract.SYNC_LEASE_ACQUIRED,
                operation=OPERATION_LEASE_ACQUIRE,
                kind=EVENT_SYNC,
            )
            outbox = _outbox(ctx)
            if ctx.restart_generation > 0:
                recover(ctx, outbox)
            continue
        if outbox is None:  # pragma: no cover - every script acquires first
            raise ContractViolation("a role script acquires its lease first")
        if step in _OPERATIONS:
            _OPERATIONS[step](ctx)
        elif step == OPERATION_ENQUEUE:
            op_enqueue(ctx, outbox)
        elif step == OPERATION_ATTEMPT:
            op_attempt(ctx, outbox)
        elif step == OPERATION_ACK:
            op_ack(ctx, outbox)
        else:  # pragma: no cover - ROLE_SCRIPTS is closed
            raise ContractViolation(f"unknown script step {step!r}")

    ctx.barrier.hit(
        contract.SYNC_SCRIPT_COMPLETE, operation=OPERATION_ACK, kind=EVENT_SYNC
    )


# ---------------------------------------------------------------------------
# the adapter object -- contract.Adapter
# ---------------------------------------------------------------------------

class _DropboxObserver:
    """The destination's own record, read from outside the killed process.

    ``KeyedDropbox`` is file-backed and its effect files are published with
    ``os.link``, so the record survives a SIGKILL of the writer and the
    controller can read it directly. That is what design 6.2 requires of the
    destination observer: the counterparty's evidence, never a re-derivation
    from our own rows.
    """

    def __init__(self, root: Path, name: str) -> None:
        self._dropbox = KeyedDropbox(root, name=name)

    def effect_count(self, idempotency_key: str) -> int:
        return self._dropbox.effect_count(idempotency_key)

    def attempt_count(self, idempotency_key: str) -> int:
        return self._dropbox.attempt_count(idempotency_key)

    def effects(self) -> Sequence[str]:
        return self._dropbox.effects()

    def unwedge(self) -> None:
        """Remove a lock file a SIGKILLed writer left behind.

        ``KeyedDropbox`` serialises its critical section with an ``O_EXCL`` lock
        file and nothing reaps it; a process killed inside that section wedges
        the destination for every later attempt. Reaping it is the controller's
        job precisely because the controller is the one that fired the signal.
        """

        lock = Path(self._dropbox._root) / destination_module.LOCK_NAME
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


#: Named SQL over the spike schema. The names are the contract's and the
#: assertions are written against them; this mapping is the throwaway half.
_INVARIANT_QUERIES: Mapping[str, str] = {
    # No outbox row is left in a state with no owner after recovery
    # (ACCEPTANCE.md section 2, outbox resend row). This is S7's own query.
    contract.INVARIANT_NO_UNOWNED_OUTBOX: outbox_module.UNOWNED_OUTBOX_QUERY,
    # Retry count is durable across restarts and never goes backwards; the
    # schema's own trigger forbids a decrease, so the query reports the values
    # and the test asserts the floor it expects.
    contract.INVARIANT_RETRY_COUNT_DURABLE: """
        SELECT message_id, status, retry_count, writer_epoch
          FROM outbox
         WHERE message_id LIKE :holder_prefix
         ORDER BY message_id
    """,
    # Exactly one acked state per message identity, regardless of ack
    # multiplicity (ACCEPTANCE.md section 2, ack row).
    contract.INVARIANT_SINGLE_ACKED_STATE: """
        SELECT dedup_key,
               COUNT(*)                                    AS rows_total,
               SUM(CASE WHEN status = 'acked' THEN 1 ELSE 0 END) AS acked_rows
          FROM outbox
         WHERE message_id LIKE :holder_prefix
         GROUP BY dedup_key
         ORDER BY dedup_key
    """,
    # The applied-write history for one resource, in the database's own
    # insertion order -- never in the caller's skewed clock order. A non-empty
    # epoch regression here is the interleaving ACCEPTANCE.md section 2 forbids.
    # The applied-write history for one role's own write scope, in the
    # database's own insertion order -- never in the caller's skewed clock
    # order. A non-empty epoch regression here is the interleaving
    # ACCEPTANCE.md section 2 forbids.
    #
    # Scoped by ``run_id`` and not by resource, because ``action`` has no
    # resource column (docs/lease-fencing.md records the limit) and S6's
    # workaround -- encoding the resource in ``action.kind`` via
    # ``effect_kind`` -- only reaches the rows S6 itself writes. S7's delivery
    # rows carry the handler's bare ``kind`` ("notify"), so a resource-suffix
    # filter would silently match nothing and this invariant would be vacuous.
    # Every role has its own run, so ``run_id`` is per-resource in practice.
    contract.INVARIANT_LINEAR_WRITER_HISTORY: """
        SELECT rowid AS write_seq, action_id, kind, status, writer_epoch,
               refusal_reason, created_at_ms, applied_at_ms
          FROM action
         WHERE run_id = :scope
         ORDER BY write_seq
    """,
    # The refusal of a stale writer, durable and query-answerable. This is a
    # SQL query over a persisted row, not a harness event-trace line: the trace
    # would only prove the harness saw an exception (design 5).
    # ``holder LIKE :holder || '%'`` and not ``holder = :holder``: a claimant or
    # a racer is this holder plus a suffix, and its refusal is the one several
    # of the ACCEPTANCE.md section 2 rows are actually about -- the second
    # writer that was rejected rather than merged. Scoping to the exact holder
    # would make precisely those refusals invisible and report them as "never
    # recorded". The refusal belongs to the resource's timeline, which is what
    # the resource predicate already pins, not to one holder identity.
    contract.INVARIANT_RECORDED_REFUSALS: """
        SELECT seq, resource, holder, epoch, operation, refusal, now_ms
          FROM harness_refusal
         WHERE resource = :resource
           AND holder LIKE :holder || '%'
         ORDER BY seq
    """,
    # Nothing is left half-recorded once recovery has run.
    contract.INVARIANT_NO_PENDING_ACTION: """
        SELECT action_id, kind, idempotency_key, writer_epoch, created_at_ms
          FROM action
         WHERE run_id = :scope
           AND status = 'pending'
         ORDER BY rowid
    """,
    # One live holder per resource at the observation instant. The spike schema
    # keeps one mutable lease row per resource and no history table, so this is
    # the final-state half only -- the timeline property is asserted through
    # linear-writer-history and recorded-refusals instead (design 5).
    contract.INVARIANT_LEASE_SINGLE_HOLDER: """
        SELECT resource, holder, epoch, acquired_at_ms, expires_at_ms
          FROM lease
         WHERE expires_at_ms > :now_ms
         ORDER BY resource
    """,
    # -- I-11 (Issue #16) --------------------------------------------------
    #
    # Every incident row in the scope. The assertion groups by dedup key and
    # checks whichever collapse rule the case declared -- so neither Q-0002
    # rule appears in this SQL, which is the same reason the schema indexes
    # ``dedup_key`` without making it unique. A query that counted rows per key
    # would have chosen the increment-in-place rule by arithmetic.
    contract.INVARIANT_INCIDENT_COLLAPSE: """
        SELECT incident_id, dedup_key, fact_state, detector_version,
               retry_count, related_incident_id, created_at_ms, updated_at_ms,
               resolved_at_ms
          FROM incident
         WHERE run_id = :scope
         ORDER BY created_at_ms, incident_id
    """,
    # "Work resumes from unresolved incidents" (gate item 4, D-0001) is one
    # query, and the schema says so in a comment on the index this uses.
    contract.INVARIANT_UNRESOLVED_INCIDENTS: """
        SELECT incident_id, dedup_key, fact_state, retry_count, created_at_ms
          FROM incident
         WHERE run_id = :scope
           AND resolved_at_ms IS NULL
         ORDER BY created_at_ms, incident_id
    """,
    # What the observation path was classified as. Every incident row in the
    # scope is one, because an escalation is an ``action`` row and not an
    # incident -- the recommendation and the fact it was drawn from are
    # different records on purpose.
    contract.INVARIANT_OBSERVATION_CLASSIFIED: """
        SELECT incident_id, fact_state, detector_version, dedup_key,
               created_at_ms
          FROM incident
         WHERE run_id = :scope
         ORDER BY created_at_ms, incident_id
    """,
    # The termination/restart recommendations produced in this scope. A COUNT,
    # so the query always returns exactly one row and "none were produced" is a
    # pass rather than an empty result the assertion would have to guess about.
    # A driver that escalated on a D-0006 state moves this number, which is what
    # makes the assertion falsifiable.
    contract.INVARIANT_NO_ANOMALY_ESCALATION: """
        SELECT COUNT(*) AS escalations
          FROM action
         WHERE run_id = :scope
           AND kind LIKE 'recommend_restart@%'
           AND status <> 'refused'
    """,
}


class SpikeAdapter:
    """``contract.Adapter`` over S6/S7. Throwaway with them (design 6)."""

    driver_module = DRIVER_MODULE
    name = "spike"

    def bootstrap(self, db_path: Any, *, roles: Sequence[str], now_ms: int) -> None:
        """Create the control plane and the run rows the scripts presuppose."""

        connection = create_control_plane(Path(db_path))
        try:
            for role in roles:
                connection.execute(
                    "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id_of(role), "running", now_ms, now_ms),
                )
            connection.commit()
        finally:
            connection.close()

    def role_arguments(
        self, role: str, *, case: Mapping[str, Any], workdir: Any
    ) -> Sequence[str]:
        behaviours = tuple(case.get("behaviours", ()))
        arguments = [
            "--resource",
            resource_of(role),
            "--holder",
            holder_of(role),
            "--run-id",
            run_id_of(role),
            "--workdir",
            str(workdir),
            "--ttl-ms",
            str(case["ttl_ms"]),
            "--messages",
            str(case.get("messages", 1)),
            "--manifest-version",
            str(case["manifest_version"]),
        ]
        for behaviour in behaviours:
            arguments.extend(["--behaviour", behaviour])

        # -- I-11 case parameters ------------------------------------------
        #
        # Forwarded verbatim, and only when the case declared them: a case that
        # says nothing gets the driver's inert defaults, which is what keeps the
        # 35 S9 seed cases running exactly as they did.
        observation = case.get("observation")
        if observation:
            arguments.extend(["--observation-mode", observation["mode"]])
            for fact_state in observation.get("escalate_on", ()):
                arguments.extend(["--escalate-on", fact_state])

        incident = case.get("incident_params")
        if incident:
            # ``dedup_key`` is the case's, never composed here. Q-0002 asks
            # what composes an incident dedup key; a driver-side formula would
            # answer it by inertia, exactly as a role-to-resource table would
            # have answered Q-0001.
            if incident.get("dedup_key") is not None:
                arguments.extend(["--incident-dedup-key", str(incident["dedup_key"])])
            if incident.get("repeats"):
                arguments.extend(["--incident-repeats", str(incident["repeats"])])
            if incident.get("collapse") is not None:
                arguments.extend(["--incident-collapse", str(incident["collapse"])])
            if incident.get("renotify_window_ms") is not None:
                arguments.extend(
                    [
                        "--incident-renotify-window-ms",
                        str(incident["renotify_window_ms"]),
                    ]
                )
            if incident.get("reconcile_interval_ms") is not None:
                arguments.extend(
                    [
                        "--incident-reconcile-interval-ms",
                        str(incident["reconcile_interval_ms"]),
                    ]
                )

        if case.get("unavailable_attempts"):
            arguments.extend(
                ["--unavailable-attempts", str(case["unavailable_attempts"])]
            )
        return tuple(arguments)

    def observer(self, workdir: Any, role: str) -> _DropboxObserver:
        return _DropboxObserver(_dropbox_root(Path(workdir), role), f"{role}-dropbox")

    def invariant_queries(self) -> Mapping[str, str]:
        return dict(_INVARIANT_QUERIES)

    # -- helpers the cases use to name rows without importing the spike -----

    def resource_of(self, role: str) -> str:
        return resource_of(role)

    def holder_of(self, role: str) -> str:
        return holder_of(role)

    def run_id_of(self, role: str) -> str:
        return run_id_of(role)

    def effect_keys(
        self, role: str, case: Mapping[str, Any], *, holder_suffix: str | None = None
    ) -> Sequence[str]:
        """The destination keys ``role``'s script produced under ``case``.

        The durable tests count effects per key without knowing how a key is
        spelled; ``dup-delivery`` is exactly the case where two messages share
        one key, and one key is what "duplicate delivery causes exactly one
        effect" is counted over.
        """

        holder = holder_of(role)
        if holder_suffix:
            holder = f"{holder}-{holder_suffix}"
        if BEHAVIOUR_DUP_DELIVERY in tuple(case.get("behaviours", ())):
            dedup_keys = [f"{holder}-dedup"]
        else:
            dedup_keys = [
                f"{holder}-dedup-{index}" for index in range(int(case.get("messages", 1)))
            ]
        return tuple(f"{NOTIFY_RECIPIENT}:notify:{key}" for key in dedup_keys)

    def query_parameters(self, role: str, *, now_ms: int) -> Mapping[str, Any]:
        """Bind the contract's invariant parameters to this schema's spelling."""

        return {
            "resource": resource_of(role),
            "holder": holder_of(role),
            # ``-m%`` and not ``-%``: a claimant's holder is this holder plus a
            # suffix, and a looser pattern would sweep the claimant's rows into
            # assertions scoped to this role.
            "holder_prefix": f"{holder_of(role)}-m%",
            "scope": run_id_of(role),
            "now_ms": int(now_ms),
        }

    def store_path(self, name: str, *, control_plane: Any, workdir: Any) -> Path:
        """The refusal ledger is a sidecar; everything else is the control plane."""

        if name == contract.INVARIANT_RECORDED_REFUSALS:
            return refusal_ledger_path(Path(workdir))
        return Path(control_plane)

    def checkpoint_vocabulary(self) -> Sequence[str]:
        """S7's own constants, for the battery's equality assertion (design 6.2)."""

        return tuple(outbox_module.CHECKPOINTS)

    def open_store(self, db_path: Any) -> sqlite3.Connection:
        return open_control_plane(Path(db_path))


#: Resource names are **per-case data, not a role table** -- ``Q-0001`` (which
#: component may hold which resource) stays open and this harness does not
#: answer it by inertia. These are the spike adapter's defaults for its own
#: three scripts and nothing more.
def resource_of(role: str) -> str:
    return f"harness-{role}"


def holder_of(role: str) -> str:
    return f"holder-{role}"


def run_id_of(role: str) -> str:
    return f"run-{role}"


SPIKE_ADAPTER = SpikeAdapter()


# ---------------------------------------------------------------------------
# the executable module
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=DRIVER_MODULE,
        description=(
            "S9 role driver: one role process over the S6/S7 spike surface. "
            "Spawned by the fault-injection controller; not useful by hand."
        ),
    )
    parser.add_argument("--role", required=True, choices=list(contract.ROLES))
    parser.add_argument("--db", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--suite-seed", type=int, required=True)
    parser.add_argument(
        "--armed",
        default="",
        help="comma-separated armed anchors, each 'operation@anchor:occurrence'",
    )
    parser.add_argument("--clock-base-ms", type=int, required=True)
    parser.add_argument("--clock-offset-ms", type=int, default=0)
    parser.add_argument("--restart-generation", type=int, default=0)
    parser.add_argument("--control-fd", type=int, default=0)
    parser.add_argument("--event-fd", type=int, default=1)
    # adapter-specific
    parser.add_argument("--resource", required=True)
    parser.add_argument("--holder", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--ttl-ms", type=int, default=30_000)
    parser.add_argument("--messages", type=int, default=1)
    parser.add_argument("--manifest-version", type=int, default=1)
    parser.add_argument("--behaviour", action="append", default=[], choices=list(BEHAVIOURS))
    # -- I-11 case parameters (Issue #16) ---------------------------------
    #
    # The observation seam and the incident parameters. Every default here is
    # the *inert* one -- healthy observation, no escalation policy, no repeats,
    # no collapse rule -- so a case that says nothing gets the behaviour the 35
    # S9 seed cases already had. The values that matter are the case's.
    parser.add_argument(
        "--observation-mode",
        default=contract.OBSERVATION_HEALTHY,
        choices=list(contract.OBSERVATION_MODES),
    )
    parser.add_argument(
        "--escalate-on",
        action="append",
        default=[],
        choices=list(contract.FACT_STATES),
        help="fact states this case's escalation policy would escalate on",
    )
    parser.add_argument("--incident-dedup-key", default=None)
    parser.add_argument("--incident-repeats", type=int, default=0)
    parser.add_argument(
        "--incident-collapse", default=None, choices=list(COLLAPSE_RULES)
    )
    parser.add_argument("--incident-renotify-window-ms", type=int, default=None)
    parser.add_argument("--incident-reconcile-interval-ms", type=int, default=None)
    parser.add_argument(
        "--unavailable-attempts", type=int, default=DEFAULT_UNAVAILABLE_ATTEMPTS
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(argv) if argv is not None else None)

    event_fd = os.dup(arguments.event_fd)
    if arguments.event_fd == 1:
        # Nothing but the protocol may reach the event pipe. A stray ``print``
        # in any imported module would otherwise corrupt the stream, so stdout
        # is pointed at stderr for the rest of the process.
        os.dup2(2, 1)
    events = os.fdopen(event_fd, "w", encoding="utf-8", newline="\n")
    control = os.fdopen(arguments.control_fd, "r", encoding="utf-8")

    def emit(message: Mapping[str, Any]) -> None:
        events.write(json.dumps(message, sort_keys=True) + "\n")
        events.flush()

    emit(
        {
            "event": EVENT_HELLO,
            "protocol_version": contract.PROTOCOL_VERSION,
            "contract_version": contract.FAULT_RUNNER_CONTRACT_VERSION,
            "role": arguments.role,
            "case_id": arguments.case_id,
            "restart_generation": arguments.restart_generation,
            "adapter": SpikeAdapter.name,
        }
    )

    armed = tuple(
        contract.ArmedAnchor.parse(item)
        for item in arguments.armed.split(",")
        if item.strip()
    )
    clock = Clock(
        base_ms=arguments.clock_base_ms
        + arguments.restart_generation * RESTART_CLOCK_ADVANCE_MS,
        offset_ms=arguments.clock_offset_ms,
    )
    barrier = Barrier(armed=armed, emit=emit, control=control, clock=clock)

    ctx = Context(
        role=arguments.role,
        resource=arguments.resource,
        holder=arguments.holder,
        run_id=arguments.run_id,
        db_path=Path(arguments.db),
        workdir=Path(arguments.workdir),
        case_id=arguments.case_id,
        suite_seed=arguments.suite_seed,
        manifest_version=arguments.manifest_version,
        ttl_ms=arguments.ttl_ms,
        messages=arguments.messages,
        behaviours=tuple(arguments.behaviour),
        restart_generation=arguments.restart_generation,
        clock=clock,
        barrier=barrier,
        emit=emit,
        observation_mode=arguments.observation_mode,
        escalate_on=tuple(arguments.escalate_on),
        incident_dedup_key=arguments.incident_dedup_key,
        incident_repeats=arguments.incident_repeats,
        incident_collapse=arguments.incident_collapse,
        incident_renotify_window_ms=arguments.incident_renotify_window_ms,
        incident_reconcile_interval_ms=arguments.incident_reconcile_interval_ms,
        unavailable_attempts=arguments.unavailable_attempts,
    )
    # The seam is prepared before the script runs and only in the first
    # generation: a restart must find the world as its predecessor left it, not
    # a freshly repaired observation path.
    if arguments.restart_generation == 0:
        write_observation(
            Path(arguments.workdir), arguments.role, mode=arguments.observation_mode
        )
    ctx.connection = _open_or_create(ctx)
    try:
        run_script(ctx)
        emit({"event": EVENT_DONE, "now_ms": clock.now_ms()})
        return 0
    except BaseException as error:  # noqa: BLE001 - the driver reports, never hides
        emit({"event": EVENT_ERROR, "type": type(error).__name__})
        import traceback

        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        try:
            ctx.connection.close()
        except Exception:  # pragma: no cover - closing a dead connection
            pass


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
