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
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass, field
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
    ROLE_SCRIPTS,
    ROLE_SUPERVISOR,
)

__all__ = [
    "BEHAVIOUR_DROP_DELIVERY",
    "BEHAVIOUR_DUP_DELIVERY",
    "BEHAVIOUR_LOST_ACK",
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

BEHAVIOURS = (
    BEHAVIOUR_DROP_DELIVERY,
    BEHAVIOUR_DUP_DELIVERY,
    BEHAVIOUR_LOST_ACK,
)


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

    def __init__(self, inner: KeyedDropbox, *, drop_attempt: int) -> None:
        self._inner = inner
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
            raise DestinationRefusal(
                f"the harness dropped attempt {seen} for {idempotency_key!r}"
            )
        return self._inner.apply(idempotency_key, payload, fencing_token, fence_scope)

    def effect_count(self, idempotency_key: str) -> int:
        return self._inner.effect_count(idempotency_key)

    def attempt_count(self, idempotency_key: str) -> int:
        return self._inner.attempt_count(idempotency_key)


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

def op_lease_acquire(ctx: Context) -> Lease:
    """Take or resume this role's own lease.

    A restarted holder whose lease row is still its own and still live *renews*
    rather than re-acquiring: renewal keeps the epoch, and keeping the epoch is
    what lets the restarted process own the outbox rows its predecessor stamped.
    When the lease has lapsed or moved on, re-acquiring raises the epoch -- and
    then ``Outbox.recover`` re-stamps the orphaned rows, which is the other half
    of the same recovery.
    """

    ctx.barrier.hit(CHECKPOINT_BEFORE_DURABLE_WRITE, operation=OPERATION_LEASE_ACQUIRE)
    now_ms = ctx.clock.advance()
    observed = read_lease(ctx.connection, ctx.resource)
    took = "acquired"
    if observed is not None and observed.holder == ctx.holder and observed.looks_live_at(now_ms):
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
    ctx.lease = lease
    ctx.barrier.hit(
        CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT, operation=OPERATION_LEASE_ACQUIRE
    )
    ctx.emit(
        {
            "event": EVENT_STEP,
            "operation": OPERATION_LEASE_ACQUIRE,
            "outcome": took,
            "epoch": lease.epoch,
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
            # uuid in the evidence is a re-run that cannot be compared.
            attempt_id=f"refused-{ctx.holder}-bind",
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


# -- the outbox surface ------------------------------------------------------

def _outbox(ctx: Context) -> Outbox:
    root = _dropbox_root(ctx.workdir, ctx.role)
    dropbox: Any = KeyedDropbox(root, name=f"{ctx.role}-dropbox")
    if BEHAVIOUR_DROP_DELIVERY in ctx.behaviours and ctx.restart_generation == 0:
        # Only the first generation drops: a restart's job is to drive the
        # unfinished work to resolution, and a destination that keeps refusing
        # would be testing the harness's patience rather than the resend.
        dropbox = _DroppingDropbox(dropbox, drop_attempt=1)
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
    """The record -> effect -> result path: where all four windows live."""

    assert ctx.lease is not None
    for message in outbox.due(ctx.clock.now_ms()):
        if message.status == "acked":
            continue
        now_ms = ctx.clock.advance()
        try:
            result = outbox.attempt(
                message.message_id, now_ms=now_ms, epoch=ctx.lease.epoch
            )
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
                "message_id": message.message_id,
                "deduplicated": deduplicated,
                "now_ms": now_ms,
            }
        )


def op_ack(ctx: Context, outbox: Outbox) -> None:
    """Record the acks. Twice per message, deliberately: acks are idempotent."""

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
        for _ in range(2):
            ctx.barrier.hit(CHECKPOINT_BEFORE_DURABLE_WRITE, operation=OPERATION_ACK)
            now_ms = ctx.clock.advance()
            outcome = outbox.record_ack(message_id, now_ms=now_ms)
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
            op_lease_acquire(ctx)
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
    contract.INVARIANT_LINEAR_WRITER_HISTORY: lease_module.WRITE_HISTORY_QUERY,
    # The refusal of a stale writer, durable and query-answerable. This is a
    # SQL query over a persisted row, not a harness event-trace line: the trace
    # would only prove the harness saw an exception (design 5).
    contract.INVARIANT_RECORDED_REFUSALS: """
        SELECT seq, resource, holder, epoch, operation, refusal, now_ms
          FROM harness_refusal
         WHERE resource = :resource
           AND holder = :holder
         ORDER BY seq
    """,
    # Nothing is left half-recorded once recovery has run.
    contract.INVARIANT_NO_PENDING_ACTION: """
        SELECT action_id, kind, idempotency_key, writer_epoch, created_at_ms
          FROM action
         WHERE status = 'pending'
           AND (:resource IS NULL
                OR substr(kind, -(length(:resource) + 1)) = '@' || :resource)
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
            "kind": None,
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
