"""S7 -- the outbox: resend, ack, dedup, and the declared exactly-once mechanism.

**These tests are the durable half of Issue ``#14`` (D-0026).** The outbox they
exercise is throwaway; the questions are not. They are written so that whatever
replaces :mod:`claude_org_runtime.control_plane.outbox` still has to answer the
same ones, which is why the assertions are about **records** -- our rows, and the
destination's own ledger -- rather than about the shape of any API.

One rule runs through the exactly-once cases and is worth stating before the
first of them, because it is the criterion easiest to satisfy by accident.
``ACCEPTANCE.md`` section 2:

    *A case that asserts exactly-once for an external effect using only our own
    rows does not pass.*

So every exactly-once assertion below reads
:meth:`~claude_org_runtime.control_plane.destination.KeyedDropbox.effect_count`
-- the **destination's** count of effects it actually applied -- and the
strongest of them
(:func:`test_the_exactly_once_evidence_outlives_our_database`) deletes the
control-plane database first, so that no row of ours can be what makes it pass.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

import claude_org_runtime
from claude_org_runtime.control_plane.destination import (
    DestinationRefusal,
    KeyedDropbox,
)
from claude_org_runtime.control_plane.handlers import (
    HUMAN_GATED_RECIPIENT,
    NOTIFY_RECIPIENT,
    HumanGatedHandler,
    NotifyDestinationHandler,
    spike_registry,
)
from claude_org_runtime.control_plane.outbox import (
    CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD,
    CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    CHECKPOINT_BEFORE_DURABLE_WRITE,
    CHECKPOINT_DELIVERED_BEFORE_ACK,
    CHECKPOINTS,
    EXACTLY_ONCE_MECHANISMS,
    UNOWNED_OUTBOX_QUERY,
    ActionHandler,
    HandlerRegistry,
    HandlerRejected,
    HumanGateRequired,
    Outbox,
    StaleWriterRefused,
)
from claude_org_runtime.control_plane.schema import (
    create_control_plane,
    load_schema_sql,
    open_control_plane,
    reconstruct,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
RESOURCE = "outbox-of-run-1"
HOLDER = "writer-a"
EPOCH = 1
TTL_MS = 30_000


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "control-plane.sqlite3"


@pytest.fixture
def cp(db_path: Path):
    connection = create_control_plane(db_path)
    try:
        connection.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
            " VALUES ('run-1', 'running', ?, ?)",
            (T0, T0),
        )
        connection.execute(
            "INSERT INTO lease (resource, holder, epoch, acquired_at_ms, expires_at_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (RESOURCE, HOLDER, EPOCH, T0, T0 + TTL_MS),
        )
        connection.commit()
        yield connection
    finally:
        connection.close()


@pytest.fixture
def dropbox(tmp_path: Path) -> KeyedDropbox:
    """The counterparty. A directory outside the database, on purpose."""

    return KeyedDropbox(tmp_path / "destination", name="spike-dropbox")


class _Kills:
    """A checkpoint that raises once, at a named point.

    S9 (Issue ``#15``) builds the deterministic harness. This is the minimum S7
    needs to prove its own windows are real: a kill is an exception out of the
    named point, and the assertion is about what the database and the
    destination hold afterwards.
    """

    class Killed(Exception):
        pass

    def __init__(self, at: str | None = None) -> None:
        self.at = at
        self.seen: list[str] = []

    def __call__(self, name: str) -> None:
        self.seen.append(name)
        if name == self.at:
            self.at = None  # one kill, so a retry can get past it
            raise self.Killed(name)


def make_outbox(cp, dropbox, *, checkpoint=None, registry=None, holder=HOLDER):
    return Outbox(
        cp,
        resource=RESOURCE,
        holder=holder,
        registry=registry if registry is not None else spike_registry(dropbox),
        **({"checkpoint": checkpoint} if checkpoint is not None else {}),
    )


def enqueue(outbox, *, message_id="msg-1", dedup_key="dk-1", payload='{"body":"hello"}',
            recipient=NOTIFY_RECIPIENT, at=T0):
    return outbox.enqueue(
        message_id=message_id,
        recipient=recipient,
        payload=payload,
        dedup_key=dedup_key,
        now_ms=at,
        epoch=EPOCH,
        run_id="run-1",
    )


def actions(cp, **where):
    clause = " AND ".join(f"{k} = :{k}" for k in where) or "1"
    cursor = cp.execute(f"SELECT * FROM action WHERE {clause} ORDER BY action_id", where)
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# --------------------------------------------------------------------------
# criterion 1 -- the handler names its mechanism, and a test asserts the name
#
# "so a later handler cannot be added without one" is the operative half. It is
# asserted twice: over the registry, which cannot admit an undeclared handler,
# and over every handler class in the package, which catches one that bypassed
# the registry.
# --------------------------------------------------------------------------


def test_every_registered_handler_names_its_exactly_once_mechanism(dropbox):
    registry = spike_registry(dropbox)
    assert registry.handlers(), "the spike registry must contain at least one handler"
    for handler in registry.handlers():
        assert handler.exactly_once_mechanism in EXACTLY_ONCE_MECHANISMS, (
            f"{type(handler).__name__} does not name one of the mechanisms "
            "ACCEPTANCE.md section 2 requires"
        )


def test_every_handler_class_in_the_package_names_one_too(dropbox):
    """The same criterion, reached without going through the registry.

    A handler that is never registered still ships, and the next author will
    copy whichever one they find. ``ActionHandler`` itself is the abstract base
    and is excluded -- its empty declaration is what makes a subclass that
    forgets fail rather than inherit an answer.
    """

    # Restricted to classes that actually ship. Handlers defined inside the
    # tests below are registered as subclasses the moment their test runs, and
    # some of them are deliberately undeclared -- picking those up would make
    # this assertion depend on execution order and then fail on its own fixtures.
    subclasses = [
        cls
        for cls in _all_subclasses(ActionHandler)
        if cls.__module__.startswith("claude_org_runtime.")
    ]
    assert subclasses, "there is no handler to check, which is itself a failure"
    for cls in subclasses:
        assert cls.exactly_once_mechanism in EXACTLY_ONCE_MECHANISMS, (
            f"{cls.__name__} ships without naming an exactly-once mechanism"
        )


def _all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


def test_the_mechanism_names_are_exactly_the_ddls(cp):
    """The constant and the schema's CHECK cannot drift apart.

    Two enumerations of the same clause is one more than is safe, so they are
    pinned to each other: a mechanism added to the DDL without being added here
    would be registrable-but-unwritable, and the reverse would be
    writable-but-unregistrable.
    """

    ddl = load_schema_sql()
    for mechanism in EXACTLY_ONCE_MECHANISMS:
        assert f"'{mechanism}'" in ddl

    (stored,) = cp.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'action'"
    ).fetchone()
    # sqlite_master keeps the DDL verbatim, comments included, and the comment on
    # each branch of this CHECK explains the mechanism it names.
    executable = "\n".join(re.sub(r"--.*$", "", line) for line in stored.splitlines())
    enumerated = executable.split("exactly_once_mechanism IN (")[1].split(")")[0]
    assert set(re.findall(r"'([^']+)'", enumerated)) == set(EXACTLY_ONCE_MECHANISMS)


def test_a_handler_that_names_no_mechanism_is_refused_registration():
    class Undeclared(ActionHandler):
        recipient = "somewhere"
        action_kind = "something"
        # exactly_once_mechanism deliberately not set

    with pytest.raises(HandlerRejected, match="exactly_once_mechanism"):
        HandlerRegistry().register(Undeclared())


def test_a_handler_that_invents_a_mechanism_is_refused_registration():
    class Inventive(ActionHandler):
        recipient = "somewhere"
        action_kind = "something"
        exactly_once_mechanism = "best_effort"

    with pytest.raises(HandlerRejected, match="best_effort"):
        HandlerRegistry().register(Inventive())


def test_the_declared_mechanism_reaches_the_durable_record(cp, dropbox):
    """The declaration is not decoration: it is written to the action row.

    Item 4's evidence is an idempotency record, and a record that did not say
    *how* it is made exactly-once would be claiming a guarantee without naming
    what holds it.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outcome = outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)

    (row,) = actions(cp, action_id=outcome.action_id)
    assert row["exactly_once_mechanism"] == "destination_idempotency_key"
    assert outcome.exactly_once_mechanism == row["exactly_once_mechanism"]


def test_the_chosen_handler_is_not_a_human_gate_case(dropbox):
    """Issue #14: *if the chosen handler turns out to be such a case, say so and
    pick a different one*.

    The handler that carries the spike's delivery declares a real mechanism, and
    it has a counterparty implementing it. The human-gate branch exists as a
    declaration (see below) rather than as the delivery path.
    """

    handler = spike_registry(dropbox).for_recipient(NOTIFY_RECIPIENT)
    assert handler.exactly_once_mechanism == "destination_idempotency_key"
    assert isinstance(handler, NotifyDestinationHandler)
    assert handler.destination is dropbox


def test_declaring_a_destination_mechanism_requires_a_destination():
    with pytest.raises(TypeError, match="Destination"):
        NotifyDestinationHandler(object())  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# criterion 2 -- ack is idempotent; a lost ack resends, never loses
# --------------------------------------------------------------------------


def test_a_lost_ack_causes_a_resend_and_the_effect_count_stays_one(cp, dropbox):
    """The headline ack case, end to end.

    The ack never arrives, so the message stays due and is delivered again --
    twice more, to make the point that the resend is unbounded rather than
    lucky. Our row shows the resends; the destination shows one effect.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    key = f"notify:{message.dedup_key}"

    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)
    assert [m.message_id for m in outbox.due(T0 + 20)] == ["msg-1"], (
        "a delivered message with no ack must stay due -- that is the resend"
    )

    outbox.attempt(message.message_id, now_ms=T0 + 20, epoch=EPOCH)
    outbox.attempt(message.message_id, now_ms=T0 + 30, epoch=EPOCH)

    assert dropbox.attempt_count(key) == 3, "the destination was offered the effect three times"
    assert dropbox.effect_count(key) == 1, "and applied it once"
    assert outbox.load(message.message_id).retry_count == 3

    outcome = outbox.record_ack(message.message_id, now_ms=T0 + 40)
    assert outcome.recorded is True
    assert outbox.due(T0 + 50) == (), "an acked message is not due"


def test_a_duplicate_ack_changes_nothing(cp, dropbox):
    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)

    first = outbox.record_ack(message.message_id, now_ms=T0 + 20)
    before = outbox.load(message.message_id)

    for later in (T0 + 21, T0 + 22, T0 + 999):
        repeat = outbox.record_ack(message.message_id, now_ms=later)
        assert repeat.recorded is False, "only the first ack records anything"
        assert repeat.acked_at_ms == first.acked_at_ms

    assert outbox.load(message.message_id) == before, "the row is byte-identical afterwards"


def test_a_late_ack_after_a_restart_changes_nothing(cp, db_path, dropbox):
    """The ack arrives after the sender has died and come back.

    The connection is closed and reopened between the ack and its duplicate, so
    nothing in memory can be what makes the second one a no-op.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)
    outbox.record_ack(message.message_id, now_ms=T0 + 20)
    cp.close()

    restarted = open_control_plane(db_path)
    try:
        after = make_outbox(restarted, dropbox)
        late = after.record_ack(message.message_id, now_ms=T0 + 5_000)
        assert late.recorded is False
        assert late.acked_at_ms == T0 + 20
        assert after.load(message.message_id).status == "acked"
    finally:
        restarted.close()


def test_the_message_shows_exactly_one_acked_state_however_many_acks_arrive(cp, dropbox):
    """*Message identity in SQLite shows exactly one acked state regardless of
    ack multiplicity* -- asserted as a count over the table, not over a return
    value, because the return value is this module's and the row is the gate's.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)
    for at in range(T0 + 20, T0 + 30):
        outbox.record_ack(message.message_id, now_ms=at)

    (count,) = cp.execute(
        "SELECT COUNT(*) FROM outbox WHERE message_id = ? AND status = 'acked'"
        "   AND acked_at_ms IS NOT NULL",
        (message.message_id,),
    ).fetchone()
    assert count == 1


def test_an_ack_for_an_undelivered_message_is_refused(cp, dropbox):
    """An ack with no delivery behind it is evidence of a lost record.

    Accepting it would move the row to 'acked' without a delivery instant, which
    S5's CHECK forbids anyway -- but refusing it here says *why*, rather than
    surfacing an IntegrityError from three layers down.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    with pytest.raises(ValueError, match="not been delivered"):
        outbox.record_ack(message.message_id, now_ms=T0 + 10)


def test_an_ack_under_backward_clock_skew_is_kept_and_the_clamp_is_reported(cp, dropbox):
    """ACCEPTANCE.md section 2 skews the clock backwards on purpose.

    S5's ``acked_at_ms >= delivered_at_ms`` CHECK would refuse the row, and
    losing a real ack to a clock skew is the worse failure. The lifecycle order
    is preserved and the disagreement is **reported** rather than applied
    silently -- a caller that cares can see that its clock ran behind.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 1_000, epoch=EPOCH)

    skewed = outbox.record_ack(message.message_id, now_ms=T0 + 500)
    assert skewed.recorded is True
    assert skewed.clock_clamped is True, "the clamp must not be silent"
    assert skewed.acked_at_ms == T0 + 1_000
    assert outbox.load(message.message_id).status == "acked"


def test_an_ack_is_recorded_even_after_the_writers_lease_moved_on(cp, dropbox):
    """The ack is deliberately unfenced, and that is a decision worth pinning.

    An ack is the recipient reporting what it already did. Refusing to record it
    because our own lease moved on would turn a delivered message back into an
    undelivered one and resend an effect that is already present -- the fence
    protects writes that *drive* effects, and this drives none.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)

    cp.execute(
        "UPDATE lease SET holder = 'writer-b', epoch = 2 WHERE resource = ?", (RESOURCE,)
    )
    cp.commit()

    assert outbox.record_ack(message.message_id, now_ms=T0 + 20).recorded is True


# --------------------------------------------------------------------------
# criterion 3 -- retry count is monotonic across a process restart
# --------------------------------------------------------------------------


def test_the_retry_count_counts_attempts_not_successes(cp, dropbox):
    """*Hold the recipient unavailable across several retry attempts.*

    Attempts that by construction never succeed still have to be counted, which
    is why the increment is committed before the effect is attempted rather than
    after it succeeds.
    """

    class Unavailable(ActionHandler):
        recipient = NOTIFY_RECIPIENT
        action_kind = "notify"
        exactly_once_mechanism = "destination_idempotency_key"

        def apply(self, message, idempotency_key):
            raise DestinationRefusal("the recipient is unavailable")

    registry = HandlerRegistry()
    registry.register(Unavailable())
    outbox = make_outbox(cp, dropbox, registry=registry)
    message = enqueue(outbox)

    for attempt in range(1, 4):
        with pytest.raises(DestinationRefusal):
            outbox.attempt(message.message_id, now_ms=T0 + attempt, epoch=EPOCH)
        assert outbox.load(message.message_id).retry_count == attempt

    assert outbox.load(message.message_id).status == "pending", "nothing was delivered"
    assert [m.message_id for m in outbox.due(T0 + 100)] == ["msg-1"], "and it is still due"


def test_the_retry_count_is_monotonic_across_a_process_restart(cp, db_path, dropbox, tmp_path):
    """Monotonic *across a process restart* -- so the restart is a real one.

    A second connection in this process would still share module state. The
    claim is that the count lives in the database, so the reading process is a
    fresh interpreter that was never told anything.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)
    outbox.attempt(message.message_id, now_ms=T0 + 20, epoch=EPOCH)
    before = outbox.load(message.message_id).retry_count
    assert before == 2
    cp.close()

    src = Path(claude_org_runtime.__file__).resolve().parent.parent
    program = (
        "import json, sys\n"
        "from claude_org_runtime.control_plane import open_control_plane\n"
        "from claude_org_runtime.control_plane.destination import KeyedDropbox\n"
        "from claude_org_runtime.control_plane.handlers import spike_registry\n"
        "from claude_org_runtime.control_plane.outbox import Outbox\n"
        "connection = open_control_plane(sys.argv[1])\n"
        "outbox = Outbox(connection, resource=sys.argv[3], holder=sys.argv[4],\n"
        "                registry=spike_registry(KeyedDropbox(sys.argv[2])))\n"
        "before = outbox.load('msg-1').retry_count\n"
        "outbox.attempt('msg-1', now_ms=int(sys.argv[5]), epoch=1)\n"
        "print(json.dumps({'before': before, 'after': outbox.load('msg-1').retry_count}))\n"
    )
    # Inherit the environment and prepend src, rather than handing the child a
    # hand-built one: on Windows an interpreter started without SystemRoot and
    # without the PATH its DLLs are found on never reaches main().
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        os.pathsep.join([str(src), env["PYTHONPATH"]]) if env.get("PYTHONPATH") else str(src)
    )
    result = subprocess.run(
        [
            sys.executable, "-c", program, str(db_path), str(tmp_path / "destination"),
            RESOURCE, HOLDER, str(T0 + 30),
        ],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (
        f"the restarted sender exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    seen = json.loads(result.stdout)
    assert seen["before"] == before, "the restarted process inherited the count from SQLite alone"
    assert seen["after"] == before + 1, "and continued it upwards rather than restarting it"


def test_the_retry_count_cannot_be_walked_backwards(cp, dropbox):
    """S5's trigger, asserted from S7's side.

    The schema tests own this rule; it is re-asserted here because the outbox is
    what would violate it, and a later rewrite of this module must not be able
    to drop the guarantee by writing its own UPDATE.
    """

    import sqlite3

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)
    with pytest.raises(sqlite3.IntegrityError, match="retry_count"):
        cp.execute("UPDATE outbox SET retry_count = 0 WHERE message_id = ?", ("msg-1",))


# --------------------------------------------------------------------------
# criterion 4 -- no outbox row remains in a state with no owner after recovery
# --------------------------------------------------------------------------


def test_a_row_is_owned_from_the_instant_it_is_enqueued(cp, dropbox):
    outbox = make_outbox(cp, dropbox)
    enqueue(outbox)
    assert outbox.unowned(T0) == (), (
        "an enqueue that left the row unowned would satisfy the forbidden state "
        "the moment it committed"
    )


def test_rows_orphaned_by_a_dead_epoch_are_adopted_by_recovery(cp, dropbox):
    """The crash case: the epoch that owned the rows died with its holder.

    A new holder takes the lease at a higher epoch, and recovery re-stamps the
    orphans so that the criterion's query comes back empty.
    """

    outbox = make_outbox(cp, dropbox)
    enqueue(outbox, message_id="msg-1", dedup_key="dk-1")
    enqueue(outbox, message_id="msg-2", dedup_key="dk-2")

    later = T0 + TTL_MS + 1  # epoch 1's lease has expired
    assert sorted(outbox.unowned(later)) == ["msg-1", "msg-2"]

    cp.execute(
        "UPDATE lease SET holder = ?, epoch = 2, expires_at_ms = ? WHERE resource = ?",
        ("writer-b", later + TTL_MS, RESOURCE),
    )
    cp.commit()

    successor = make_outbox(cp, dropbox, holder="writer-b")
    report = successor.recover(now_ms=later, epoch=2)

    assert sorted(report.adopted) == ["msg-1", "msg-2"]
    assert report.still_unowned == ()
    assert successor.unowned(later) == ()


def test_recovery_adopts_nothing_when_the_recovering_holders_lease_is_not_live(cp, dropbox):
    """A recovering process without a live lease must not claim the orphans.

    Adopting them would be exactly the stale writer the fence exists to reject,
    arriving through the recovery path. Recovery reports the rows as still
    unowned rather than reporting success.
    """

    outbox = make_outbox(cp, dropbox)
    enqueue(outbox)

    later = T0 + TTL_MS + 1
    impostor = make_outbox(cp, dropbox, holder="writer-b")
    report = impostor.recover(now_ms=later, epoch=99)

    assert report.adopted == ()
    assert report.still_unowned == ("msg-1",)
    assert outbox.load("msg-1").writer_epoch == EPOCH, "the row kept its epoch"


def test_an_acked_row_is_never_unowned(cp, dropbox):
    """Ownership is about rows that still need someone to advance them."""

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)
    outbox.record_ack(message.message_id, now_ms=T0 + 20)

    assert outbox.unowned(T0 + TTL_MS + 10_000) == ()


def test_the_ownership_criterion_is_a_query_anyone_can_run(cp, dropbox):
    """D-0001: the answer is readable from SQLite without this module.

    ``UNOWNED_OUTBOX_QUERY`` is exported as SQL for the same reason S5 keeps its
    reconstruction reads as data -- an operator with a database recovered from a
    crash can run it by hand.
    """

    outbox = make_outbox(cp, dropbox)
    enqueue(outbox)
    later = T0 + TTL_MS + 1

    rows = cp.execute(UNOWNED_OUTBOX_QUERY, {"resource": RESOURCE, "now_ms": later}).fetchall()
    assert [row[0] for row in rows] == ["msg-1"]
    assert [row[0] for row in rows] == list(outbox.unowned(later))


def test_a_reconstructed_process_sees_every_unfinished_row(cp, dropbox):
    """S5's reconstruction and S7's ownership answer the same question together.

    *No outbox row remains in a state with no owner after recovery* is only
    meaningful if recovery can see every unfinished row in the first place.
    """

    outbox = make_outbox(cp, dropbox)
    enqueue(outbox, message_id="msg-1", dedup_key="dk-1")
    enqueue(outbox, message_id="msg-2", dedup_key="dk-2")
    outbox.attempt("msg-2", now_ms=T0 + 10, epoch=EPOCH)
    outbox.record_ack("msg-2", now_ms=T0 + 20)

    state = reconstruct(cp, now_ms=T0 + 30)
    assert [row["message_id"] for row in state.unfinished_outbox] == ["msg-1"]


# --------------------------------------------------------------------------
# criterion 5 -- duplicate delivery causes exactly one effect
# criterion 6 -- and the evidence for an external effect is the destination's
#
# ACCEPTANCE.md section 2: *a case that asserts exactly-once for an external
# effect using only our own rows does not pass.* Every assertion in this section
# reads the destination's ledger.
# --------------------------------------------------------------------------


def test_duplicate_delivery_causes_exactly_one_effect(cp, dropbox):
    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    key = f"notify:{message.dedup_key}"

    first = outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)
    second = outbox.attempt(message.message_id, now_ms=T0 + 20, epoch=EPOCH)

    assert first.deduplicated is False, "the first attempt applied the effect"
    assert second.deduplicated is True, "and the destination refused the second"
    assert dropbox.attempt_count(key) == 2
    assert dropbox.effect_count(key) == 1
    assert dropbox.effects() == (key,)


def test_one_effect_record_per_dedup_key(cp, dropbox):
    """*One effect record per delivery dedup key* -- our half of the evidence."""

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    for at in (T0 + 10, T0 + 20, T0 + 30):
        outbox.attempt(message.message_id, now_ms=at, epoch=EPOCH)

    applied = actions(cp, idempotency_key=f"notify:{message.dedup_key}", status="applied")
    assert len(applied) == 1
    assert applied[0]["applied_at_ms"] == T0 + 10, "the first apply is the one on record"


def test_a_re_enqueue_of_the_same_dedup_key_still_causes_one_effect(cp, dropbox):
    """The case S5 left ``outbox.dedup_key`` non-unique for.

    A sender killed after committing an outbox row may not know it committed and
    may legitimately enqueue the same work again under a new message id. Two
    rows, two deliveries, one effect -- because exactly-once is a property of
    the effect and not of the row.
    """

    outbox = make_outbox(cp, dropbox)
    enqueue(outbox, message_id="msg-1", dedup_key="shared")
    enqueue(outbox, message_id="msg-1-again", dedup_key="shared")

    outbox.attempt("msg-1", now_ms=T0 + 10, epoch=EPOCH)
    second = outbox.attempt("msg-1-again", now_ms=T0 + 20, epoch=EPOCH)

    assert second.deduplicated is True
    assert dropbox.effect_count("notify:shared") == 1
    assert len(actions(cp, idempotency_key="notify:shared", status="applied")) == 1
    assert outbox.load("msg-1").status == "delivered"
    assert outbox.load("msg-1-again").status == "delivered", (
        "the second row is delivered too -- its effect is present, which is what "
        "delivered means"
    )


def test_a_kill_after_the_effect_and_before_its_record_replays_to_one_effect(cp, dropbox):
    """The injection point that proves idempotency rather than luck.

    The process dies after the destination applied the effect and before the
    result was recorded. By construction our rows cannot tell that apart from an
    effect that never started -- which is precisely why the handler's declared
    mechanism, and not a query, is what makes the replay safe.
    """

    kills = _Kills(at=CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD)
    outbox = make_outbox(cp, dropbox, checkpoint=kills)
    message = enqueue(outbox)
    key = f"notify:{message.dedup_key}"

    with pytest.raises(_Kills.Killed):
        outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)

    # The ambiguous window, described exactly: the effect happened, our record
    # does not say so, and nothing in SQLite can distinguish this from the
    # effect never having started.
    assert dropbox.effect_count(key) == 1
    assert actions(cp, idempotency_key=key)[0]["status"] == "pending"

    outbox.attempt(message.message_id, now_ms=T0 + 20, epoch=EPOCH)

    assert dropbox.attempt_count(key) == 2, "the replay was offered to the destination"
    assert dropbox.effect_count(key) == 1, "which refused it -- one effect, still"
    assert actions(cp, idempotency_key=key)[0]["status"] == "applied"
    assert outbox.load(message.message_id).status == "delivered"


def test_a_kill_after_the_record_and_before_the_effect_loses_nothing(cp, dropbox):
    """The middle injection point. The intent is durable; the effect is not yet."""

    kills = _Kills(at=CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT)
    outbox = make_outbox(cp, dropbox, checkpoint=kills)
    message = enqueue(outbox)
    key = f"notify:{message.dedup_key}"

    with pytest.raises(_Kills.Killed):
        outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)

    assert dropbox.effect_count(key) == 0, "no effect happened"
    assert actions(cp, idempotency_key=key)[0]["status"] == "pending"
    assert [m.message_id for m in outbox.due(T0 + 15)] == [message.message_id]

    outbox.attempt(message.message_id, now_ms=T0 + 20, epoch=EPOCH)
    assert dropbox.effect_count(key) == 1


def test_a_kill_before_the_durable_write_loses_nothing(cp, dropbox):
    """The first injection point. Nothing has been attempted, and the row is due."""

    kills = _Kills(at=CHECKPOINT_BEFORE_DURABLE_WRITE)
    outbox = make_outbox(cp, dropbox, checkpoint=kills)
    message = enqueue(outbox)

    with pytest.raises(_Kills.Killed):
        outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)

    assert outbox.load(message.message_id).retry_count == 0
    assert outbox.load(message.message_id).status == "pending"
    assert [m.message_id for m in outbox.due(T0 + 15)] == [message.message_id]


def test_a_kill_after_delivery_and_before_the_ack_resends_to_one_effect(cp, dropbox):
    """The outbox row's own window: delivered, never acked, sender dies."""

    kills = _Kills(at=CHECKPOINT_DELIVERED_BEFORE_ACK)
    outbox = make_outbox(cp, dropbox, checkpoint=kills)
    message = enqueue(outbox)
    key = f"notify:{message.dedup_key}"

    with pytest.raises(_Kills.Killed):
        outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)

    assert outbox.load(message.message_id).status == "delivered"
    assert [m.message_id for m in outbox.due(T0 + 15)] == [message.message_id]

    outbox.attempt(message.message_id, now_ms=T0 + 20, epoch=EPOCH)
    outbox.record_ack(message.message_id, now_ms=T0 + 30)
    assert dropbox.effect_count(key) == 1


def test_every_named_checkpoint_is_actually_reached(cp, dropbox):
    """A window no harness can stop inside is one nobody can prove anything about.

    S9 (Issue ``#15``) binds to these names, so S7 owes it the guarantee that
    each one is on the path rather than merely declared.
    """

    kills = _Kills()
    outbox = make_outbox(cp, dropbox, checkpoint=kills)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)

    assert kills.seen == list(CHECKPOINTS)


def test_the_exactly_once_evidence_outlives_our_database(cp, db_path, dropbox):
    """The strongest form of the criterion, and the reason this suite has a
    separate destination at all.

    ``ACCEPTANCE.md`` section 2 rejects a case that asserts exactly-once for an
    external effect *using only our own rows*. So the control-plane database is
    **deleted** and the question is put to the destination, which is the party
    that would have carried a duplicate effect had one happened. Nothing we
    wrote can be what makes this pass.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    key = f"notify:{message.dedup_key}"
    for at in (T0 + 10, T0 + 20, T0 + 30, T0 + 40):
        outbox.attempt(message.message_id, now_ms=at, epoch=EPOCH)
    cp.close()

    db_path.unlink()
    assert not db_path.exists()

    assert dropbox.attempt_count(key) == 4, "four deliveries were offered"
    assert dropbox.effect_count(key) == 1, "the destination applied exactly one"
    assert dropbox.effects() == (key,)
    assert json.loads(dropbox.payload_of(key) or "{}") == {"body": "hello"}


def test_the_destination_refuses_a_key_that_is_already_bound_to_another_payload(cp, dropbox):
    """A dedup-key collision must not pass as an exactly-once success.

    An idempotency key names an effect. The same key carrying different content
    is a collision, and applying nothing while reporting success would hide it
    behind the very guarantee it breaks.
    """

    outbox = make_outbox(cp, dropbox)
    enqueue(outbox, message_id="msg-1", dedup_key="shared", payload='{"body":"one"}')
    outbox.attempt("msg-1", now_ms=T0 + 10, epoch=EPOCH)

    enqueue(outbox, message_id="msg-2", dedup_key="shared", payload='{"body":"two"}')
    with pytest.raises(DestinationRefusal, match="different payload"):
        outbox.attempt("msg-2", now_ms=T0 + 20, epoch=EPOCH)

    assert dropbox.effect_count("notify:shared") == 1
    assert json.loads(dropbox.payload_of("notify:shared") or "{}") == {"body": "one"}
    assert outbox.load("msg-2").status == "pending", "and msg-2 was not recorded delivered"


def test_a_destination_record_left_incomplete_is_finished_not_deduplicated(tmp_path):
    """The destination's own crash window, which is not ours to hide.

    A destination that reserved a key and died before completing its record must
    not turn the next attempt away as a duplicate: the effect never completed,
    and deduplicating against a promise nobody kept is how a message is lost
    while every record looks healthy.
    """

    dropbox = KeyedDropbox(tmp_path / "destination")
    dropbox.apply("k", "payload")
    (record,) = sorted((tmp_path / "destination").glob("*.effect.json"))
    record.write_text(record.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")

    assert dropbox.effect_count("k") == 0, "an unterminated record is not an effect"
    receipt = dropbox.apply("k", "payload")
    assert receipt.deduplicated is False, "the next attempt finished it"
    assert dropbox.effect_count("k") == 1


def test_a_destination_refuses_an_empty_idempotency_key(tmp_path):
    """Every effect deduplicating against every other is the failure that looks
    most like success."""

    with pytest.raises(DestinationRefusal, match="may not be empty"):
        KeyedDropbox(tmp_path / "destination").apply("", "payload")


# --------------------------------------------------------------------------
# the fence -- a stale writer is rejected, not merged, and the rejection is
# itself durable (ACCEPTANCE.md section 2)
# --------------------------------------------------------------------------


def test_a_stale_writer_is_refused_and_the_refusal_is_recorded(cp, dropbox):
    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)

    cp.execute(
        "UPDATE lease SET holder = 'writer-b', epoch = 2 WHERE resource = ?", (RESOURCE,)
    )
    cp.commit()

    with pytest.raises(StaleWriterRefused):
        outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)

    refusals = actions(cp, status="refused")
    assert len(refusals) == 1, "the rejection is durable, not silently dropped"
    assert "not a live lease" in refusals[0]["refusal_reason"]
    assert refusals[0]["writer_epoch"] == EPOCH
    assert outbox.load(message.message_id).retry_count == 0, "and no write landed"


def test_a_writer_that_keeps_returning_is_refused_every_time(cp, dropbox):
    """A refused row is excluded from ``action_one_effect_per_key`` on purpose.

    A first refusal standing in for the rest would lose the fact that the stale
    writer kept coming back, which is the thing triage would want to see.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    cp.execute("UPDATE lease SET holder = 'writer-b', epoch = 2 WHERE resource = ?", (RESOURCE,))
    cp.commit()

    for at in (T0 + 10, T0 + 20, T0 + 30):
        with pytest.raises(StaleWriterRefused):
            outbox.attempt(message.message_id, now_ms=at, epoch=EPOCH)

    assert len(actions(cp, status="refused")) == 3


def test_an_expired_lease_refuses_the_write_even_though_the_epoch_matches(cp, dropbox):
    """Expiry discovery alone is insufficient; the epoch is validated *in* the write.

    The epoch is still 1 and the holder is still writer-a -- only the clock has
    moved past the lease's expiry. A check-then-write would have passed the
    check.
    """

    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)

    with pytest.raises(StaleWriterRefused):
        outbox.attempt(message.message_id, now_ms=T0 + TTL_MS + 1, epoch=EPOCH)
    assert len(actions(cp, status="refused")) == 1


def test_the_fence_is_one_statement_and_not_a_check_then_write(cp, dropbox):
    """The property the race depends on, asserted against the SQL itself.

    A test that only exercises behaviour cannot tell a fenced UPDATE from a
    SELECT followed by an UPDATE that happens not to have raced yet.
    """

    from claude_org_runtime.control_plane import outbox as module

    fence = module._FENCE_PREDICATE
    assert "EXISTS (SELECT 1" in fence and "expires_at_ms > :now_ms" in fence
    assert "writer_epoch = :epoch" in fence


def test_an_outbox_writer_must_name_its_lease_resource_and_holder(cp, dropbox):
    """No defaults. Which component may write which state item is ``Q-0001``."""

    for resource, holder in (("", HOLDER), (RESOURCE, "")):
        with pytest.raises(ValueError, match="names the lease resource and holder"):
            Outbox(cp, resource=resource, holder=holder, registry=spike_registry(dropbox))


# --------------------------------------------------------------------------
# the third branch -- neither mechanism is achievable, so a human gate (D-0004)
# --------------------------------------------------------------------------


def test_a_human_gated_action_is_recorded_and_never_applied(cp, dropbox):
    """Issue #14: the gap is **explicit**, and *do not paper over it*.

    The outbox records the action and stops. It does not attempt the effect, and
    it does not invent an automatic recovery path for one it cannot make
    exactly-once.
    """

    outbox = make_outbox(cp, dropbox)
    enqueue(outbox, message_id="msg-gated", dedup_key="dk-gated",
            recipient=HUMAN_GATED_RECIPIENT)

    with pytest.raises(HumanGateRequired, match="human_gate"):
        outbox.attempt("msg-gated", now_ms=T0 + 10, epoch=EPOCH)

    (row,) = actions(cp, idempotency_key="human_gated:dk-gated")
    assert row["status"] == "pending", "recorded, and waiting for a human"
    assert row["exactly_once_mechanism"] == "human_gate"
    assert dropbox.effects() == (), "and no effect was attempted"


def test_a_human_gated_action_stays_pending_however_often_it_is_offered(cp, dropbox):
    outbox = make_outbox(cp, dropbox)
    enqueue(outbox, message_id="msg-gated", dedup_key="dk-gated",
            recipient=HUMAN_GATED_RECIPIENT)

    for at in (T0 + 10, T0 + 20, T0 + 30):
        with pytest.raises(HumanGateRequired):
            outbox.attempt("msg-gated", now_ms=at, epoch=EPOCH)

    assert [row["status"] for row in actions(cp, kind="human_gated")] == ["pending"]
    assert outbox.load("msg-gated").status == "pending"


def test_the_human_gated_handler_refuses_to_be_applied_directly(dropbox):
    """Belt and braces: even called by hand, it does not perform an effect."""

    with pytest.raises(AssertionError, match="never applied automatically"):
        HumanGatedHandler().apply(None, "k")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# registry hygiene
# --------------------------------------------------------------------------


def test_an_unknown_recipient_has_no_handler_and_says_so(cp, dropbox):
    outbox = make_outbox(cp, dropbox)
    enqueue(outbox, message_id="msg-x", dedup_key="dk-x", recipient="nobody")
    with pytest.raises(HandlerRejected, match="nobody"):
        outbox.attempt("msg-x", now_ms=T0 + 10, epoch=EPOCH)


def test_two_handlers_cannot_claim_the_same_recipient(dropbox):
    registry = spike_registry(dropbox)
    with pytest.raises(HandlerRejected, match="already has a handler"):
        registry.register(NotifyDestinationHandler(dropbox))


def test_two_handlers_do_not_collide_on_a_shared_dedup_key(cp, dropbox):
    """``action.idempotency_key`` is unique across the whole table.

    Two handlers deriving keys from the same dedup key without namespacing would
    have one silently deduplicate against the other's effect -- an effect that
    never happens, reported as exactly-once.
    """

    notify = NotifyDestinationHandler(dropbox)
    gated = HumanGatedHandler()
    message = enqueue(make_outbox(cp, dropbox), dedup_key="shared")
    assert notify.idempotency_key(message) != gated.idempotency_key(message)


def test_an_acked_message_is_not_resent(cp, dropbox):
    outbox = make_outbox(cp, dropbox)
    message = enqueue(outbox)
    outbox.attempt(message.message_id, now_ms=T0 + 10, epoch=EPOCH)
    outbox.record_ack(message.message_id, now_ms=T0 + 20)

    with pytest.raises(ValueError, match="already acked"):
        outbox.attempt(message.message_id, now_ms=T0 + 30, epoch=EPOCH)


def test_no_retry_interval_or_window_appears_in_this_layer():
    """``Q-0003`` has to settle tolerable detection latency first.

    S5 kept every such number out of the schema. S7 sits directly on it and is
    the obvious place for a backoff to appear by convenience, so the absence is
    asserted rather than trusted.
    """

    from claude_org_runtime.control_plane import outbox as module

    # Comments and docstrings are stripped, because the prose deliberately
    # discusses the very things the code must not contain -- scanning the raw
    # text would fail on the explanation of why the thing is absent. (S5's
    # ``executable_ddl`` helper does the same for the DDL, for the same reason.)
    executable = _executable_source(Path(module.__file__))
    for forbidden in ("backoff", "visibility_timeout", "retry_after", "sleep("):
        assert forbidden not in executable.lower(), (
            f"{forbidden!r} is a retry policy, and Q-0003 has not settled one"
        )


def _executable_source(path: Path) -> str:
    """*path*'s source with comments and string literals removed."""

    kept = []
    with tokenize.open(path) as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    return "\n".join(kept)
