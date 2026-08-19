"""S6 -- the lease, and the fencing token validated atomically with each write.

The tests are the durable half of this issue (D-0026): the implementation they
exercise may be thrown away, and they are written so that whatever replaces it
still has to answer the same questions. The six acceptance criteria of Issue
``#13`` are the section headings below, in order.

Two rules run through the whole file:

**No case may lean on a provider refusing a duplicate.** Under C2 the provider's
own ``already in use`` refusal has a measured admission window (U27) and the
``--resume`` path excludes nothing at all (U32). No case here involves a
provider, and :func:`test_no_dependency_edge_on_the_session_provider` asserts
that structurally rather than describing it.

**No case proves an invariant from the absence of a symptom.** Every assertion
lands on a durable record -- a row in SQLite, or the external destination's own
effect record where the effect is external, as ``ACCEPTANCE.md`` section 2
requires.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import lease as s6
from claude_org_runtime.control_plane.lease import (
    DESTINATIONS,
    FENCE_SQL,
    ClockSkewRefused,
    Destination,
    DestinationRejectedStaleToken,
    EpochGuardedDestination,
    FencedStatement,
    Lease,
    LeaseHeld,
    LeaseNotHeld,
    LeaseUsageError,
    PROTECTED_TABLES,
    ProtectedWrite,
    ProtectedWriteMissed,
    StaleWriterRefused,
    UnfencedStatement,
    acquire,
    applied_epoch_regressions,
    authority_timeline,
    claimed_timeline,
    effect_kind,
    resource_of_kind,
    epoch_regressions,
    fenced_insert,
    fenced_update,
    overlapping_claims,
    protected_write,
    read_lease,
    release,
    renew,
    write_history,
)
from claude_org_runtime.control_plane.schema import create_control_plane, open_control_plane

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
TTL = 30_000
RESOURCE = "run/r1"
DOC = Path(__file__).resolve().parents[2] / "docs" / "lease-fencing.md"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "control-plane.sqlite3"


@pytest.fixture
def cp(db_path: Path):
    connection = create_control_plane(db_path)
    connection.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?)",
        ("r1", "running", T0, T0),
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------
# helpers -- the smallest protected write there is
#
# The protected table is `action`: it carries writer_epoch, it is where a
# refusal is recorded, and its one-effect-per-key index makes a second landing
# visible as a row rather than as a suspicion.
# --------------------------------------------------------------------------

EFFECT_KIND = effect_kind(RESOURCE, "deliver_task")

APPLY_EFFECT = fenced_insert(
    "action",
    columns=[
        "action_id",
        "run_id",
        "kind",
        "idempotency_key",
        "exactly_once_mechanism",
        "status",
        "applied_at_ms",
        "writer_epoch",
        "created_at_ms",
    ],
    values=[
        ":action_id",
        "'r1'",
        ":kind",
        ":idempotency_key",
        ":mechanism",
        "'applied'",
        ":now_ms",
        ":fence_epoch",
        ":now_ms",
    ],
)


def effect(
    action_id: str,
    *,
    key: str | None = None,
    now_ms: int,
    kind: str = EFFECT_KIND,
    mechanism: str = "transactional_with_record",
) -> ProtectedWrite:
    idempotency_key = key or action_id
    return ProtectedWrite(
        kind=kind,
        idempotency_key=idempotency_key,
        statement=APPLY_EFFECT,
        exactly_once_mechanism=mechanism,
        run_id="r1",
        params={
            "action_id": action_id,
            "kind": kind,
            "idempotency_key": idempotency_key,
            "mechanism": mechanism,
            "now_ms": now_ms,
        },
    )


def action_rows(connection: sqlite3.Connection) -> list[dict]:
    cursor = connection.execute(
        "SELECT action_id, status, refusal_reason, writer_epoch, created_at_ms "
        "FROM action ORDER BY created_at_ms, action_id"
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ==========================================================================
# Criterion 1 -- a protected write carrying a stale token is refused, and the
# refusal is recorded rather than silently dropped.
# ==========================================================================


def test_a_live_token_writes(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    assert protected_write(cp, lease, effect("a1", now_ms=T0 + 1), now_ms=T0 + 1) == 1

    (row,) = action_rows(cp)
    assert row["status"] == "applied"
    # The epoch the row was written under is what the single-writer property is
    # read back out of afterwards; a fenced write that left it NULL would be
    # unprovable later, so protected_write refuses that statement outright.
    assert row["writer_epoch"] == lease.epoch


def test_a_superseded_token_is_refused_and_the_refusal_is_recorded(cp):
    stale = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    # The holder is killed without releasing. The lease expires on its own, and a
    # second claimant takes it -- which is what raises the epoch.
    live = acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)

    with pytest.raises(StaleWriterRefused) as refused:
        protected_write(
            cp,
            stale,
            effect("a-stale", now_ms=T0 + TTL + 2),
            now_ms=T0 + TTL + 2,
            attempt_id="refusal-1",
        )

    assert refused.value.action_id == "refusal-1"
    assert refused.value.observed == live

    (row,) = action_rows(cp)
    assert row["action_id"] == "refusal-1"
    assert row["status"] == "refused"
    # The reason names who was refused, which token they presented, and what the
    # lease actually was -- a refusal recorded as a bare flag is a refusal
    # nobody can act on.
    assert "stale fencing token" in row["refusal_reason"]
    assert "'alpha'" in row["refusal_reason"] and "'beta'" in row["refusal_reason"]
    assert row["writer_epoch"] == stale.epoch == 1
    # The effect itself did not happen. Asserted against the rows, not against
    # the absence of a visible duplicate.
    assert not cp.execute("SELECT 1 FROM action WHERE status = 'applied'").fetchall()


def test_an_expired_token_is_refused_even_with_nobody_else_holding_it(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    with pytest.raises(StaleWriterRefused):
        protected_write(cp, lease, effect("a1", now_ms=T0 + TTL + 1), now_ms=T0 + TTL + 1)

    (row,) = action_rows(cp)
    assert row["status"] == "refused"
    # Nobody took the lease over, so the row still names the expired holder --
    # the refusal is the expiry's, not a handover's.
    assert "holder='alpha'" in row["refusal_reason"]


def test_the_returning_paused_holder_is_refused_repeatedly_and_recorded_each_time(cp):
    """The SIGSTOP case from ``ACCEPTANCE.md`` section 2's lease row.

    A paused process is modelled by a holder that simply does not act -- no
    signal is portable to the Windows jobs, and pausing is not what the property
    depends on. What matters is that the lease expired while it was away, a
    second claimant took it, and the returning holder's writes are refused
    *every* time rather than once.
    """

    paused = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)

    for attempt in range(3):
        with pytest.raises(StaleWriterRefused):
            protected_write(
                cp,
                paused,
                effect(f"a-return-{attempt}", key="same-effect", now_ms=T0 + TTL + 10 + attempt),
                now_ms=T0 + TTL + 10 + attempt,
                attempt_id=f"refusal-{attempt}",
            )

    rows = action_rows(cp)
    # Three refusals under one idempotency key. The schema's
    # action_one_effect_per_key index excludes refused rows precisely so that a
    # writer which keeps coming back is recorded every time, without any of
    # those records becoming the thing that admits a second effect.
    assert [row["status"] for row in rows] == ["refused"] * 3
    assert len({row["action_id"] for row in rows}) == 3


def test_the_recorded_refusal_survives_the_process_that_recorded_it(cp, db_path):
    stale = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)
    with pytest.raises(StaleWriterRefused):
        protected_write(
            cp, stale, effect("a1", now_ms=T0 + TTL + 2), now_ms=T0 + TTL + 2, attempt_id="refusal-1"
        )
    cp.close()

    reopened = open_control_plane(db_path)
    try:
        (row,) = action_rows(reopened)
        assert row["action_id"] == "refusal-1" and row["status"] == "refused"
        # And the schema keeps it that way: a refused action that could be moved
        # back to 'pending' is a rejection erased by the same statement that
        # makes the attempt executable again.
        with pytest.raises(sqlite3.IntegrityError):
            reopened.execute("UPDATE action SET status = 'pending' WHERE action_id = 'refusal-1'")
    finally:
        reopened.close()


def test_a_refusal_is_recorded_even_when_the_lease_row_is_gone_entirely(cp):
    """A token for a resource that was never taken is stale, not a crash."""

    invented = Lease(
        resource="run/never-taken",
        holder="alpha",
        epoch=7,
        acquired_at_ms=T0,
        expires_at_ms=T0 + TTL,
    )

    with pytest.raises(StaleWriterRefused) as refused:
        protected_write(
            cp,
            invented,
            effect("a1", now_ms=T0 + 1, kind=effect_kind("run/never-taken", "deliver_task")),
            now_ms=T0 + 1,
        )

    assert refused.value.observed is None
    (row,) = action_rows(cp)
    assert row["status"] == "refused" and "absent" in row["refusal_reason"]


# ==========================================================================
# Criterion 2 -- validation is atomic with the write in a single transaction,
# and the check-then-write shape would have admitted a writer the atomic shape
# refuses.
# ==========================================================================


def _check_then_write(cp, lease: Lease, action_id: str, *, now_ms: int, interleave) -> int:
    """The shape ``ACCEPTANCE.md`` section 2 names as insufficient.

    Written out here, in the tests, rather than offered by the module: the check
    reads the lease, *something happens*, and the write goes ahead on the
    strength of what the check saw. The interleaving is a callback so the race
    is deterministic and reproducible rather than a sleep and a hope.
    """

    observed = read_lease(cp, lease.resource)
    if observed is None or observed.epoch != lease.epoch or not observed.looks_live_at(now_ms):
        return 0  # the check refuses

    interleave()  # the window: the lease expires, or is taken over, right here

    cursor = cp.execute(
        """
        INSERT INTO action (action_id, run_id, kind, idempotency_key,
                            exactly_once_mechanism, status, applied_at_ms,
                            writer_epoch, created_at_ms)
        VALUES (:action_id, 'r1', :kind, :action_id, 'transactional_with_record',
                'applied', :now_ms, :epoch, :now_ms)
        """,
        {"action_id": action_id, "kind": EFFECT_KIND, "now_ms": now_ms, "epoch": lease.epoch},
    )
    cp.commit()
    return cursor.rowcount


def test_check_then_write_admits_the_writer_the_atomic_shape_refuses(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    def takeover():
        acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)

    # The check passes at T0 + 1; the lease is taken over inside the window; the
    # write lands anyway, carrying an epoch that is no longer anybody's.
    assert _check_then_write(cp, lease, "admitted", now_ms=T0 + 1, interleave=takeover) == 1
    admitted = action_rows(cp)
    assert admitted[0]["status"] == "applied" and admitted[0]["writer_epoch"] == 1
    assert read_lease(cp, RESOURCE).epoch == 2

    # The atomic shape, offered the same stale token afterwards, refuses it --
    # there is no instant between the validation and the write for the lease to
    # move in, because they are one statement.
    with pytest.raises(StaleWriterRefused):
        protected_write(cp, lease, effect("atomic", now_ms=T0 + TTL + 2), now_ms=T0 + TTL + 2)

    statuses = [row["status"] for row in action_rows(cp)]
    assert statuses == ["applied", "refused"]


def test_the_fence_lives_in_the_database_not_in_the_lease_object(cp, db_path):
    """A second connection's takeover invalidates the first one's token.

    If the validation were a property of the in-process :class:`Lease`, this
    would pass: nothing on ``cp`` ever saw the handover. It fails because the
    fence is evaluated by SQLite, in the write, against the row.
    """

    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    other = open_control_plane(db_path)
    try:
        acquire(other, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)
    finally:
        other.close()

    with pytest.raises(StaleWriterRefused):
        protected_write(cp, lease, effect("a1", now_ms=T0 + TTL + 2), now_ms=T0 + TTL + 2)


def test_an_unfenced_statement_cannot_be_run_through_this_module():
    with pytest.raises(UnfencedStatement) as refused:
        ProtectedWrite(
            kind=EFFECT_KIND,
            idempotency_key="k",
            statement="UPDATE action SET status = 'applied' WHERE action_id = :a",
            exactly_once_mechanism="transactional_with_record",
        )
    assert "fenced_update" in str(refused.value)


def test_a_statement_that_mentions_the_fence_without_obeying_it_is_refused(cp):
    """The shape a substring check would have waved through.

    This statement carries ``FENCE_SQL`` verbatim -- in a ``SET`` expression,
    where it decides a *value* rather than whether the row changes. Under a
    stale token it still updates its row and still reports a positive rowcount:
    a protected write that silently is not one. Only the builders can issue a
    statement, so this cannot be handed to :func:`protected_write` at all.
    """

    smuggled = (
        "UPDATE action\n"
        f"   SET writer_epoch = CASE WHEN {FENCE_SQL} THEN :fence_epoch ELSE 0 END\n"
        " WHERE action_id = :action_id"
    )
    with pytest.raises(UnfencedStatement):
        ProtectedWrite(
            kind=EFFECT_KIND,
            idempotency_key="k",
            statement=smuggled,
            exactly_once_mechanism="transactional_with_record",
        )
    # ...and it cannot be laundered into one either.
    with pytest.raises(UnfencedStatement):
        FencedStatement(smuggled)


def test_a_fenced_statement_that_forgets_the_epoch_is_refused():
    """The history is only readable if every fenced write stamps its epoch."""

    with pytest.raises(UnfencedStatement):
        fenced_update("action", set_clause="status = 'applied'", where="action_id = :a")
    # Mentioning the column is not stamping it: a predicate that names
    # writer_epoch, or a constant assigned to it, leaves a row whose epoch means
    # nothing, and write_history() would then be reading a number it cannot trust.
    with pytest.raises(UnfencedStatement):
        fenced_update("action", set_clause="writer_epoch = 1", where="action_id = :a")
    with pytest.raises(UnfencedStatement):
        fenced_insert("action", columns=["action_id", "writer_epoch"], values=[":a", "1"])
    # ...and the opt-out is explicit, for a target that genuinely has no such column.
    fenced_update(
        "run", set_clause="status = 'done'", where="run_id = :r", stamps_writer_epoch=False
    )


def test_a_fragment_cannot_hide_its_structure_inside_a_string_literal(cp):
    """The parenthesis inside `'('` is text; the one that closes the wrapper is not.

    This predicate balances character for character while genuinely closing the
    parentheses the fence is ANDed onto and putting a true `OR` branch in front
    of it. The literals are taken out before the structural scan, by SQLite's
    own quoting rules, so what is scanned is all and only structure.
    """

    with pytest.raises(UnfencedStatement):
        fenced_update(
            "action",
            set_clause="writer_epoch = :fence_epoch",
            where="'(' = '(') OR (1 = 1 AND ')' = ')'",
        )
    with pytest.raises(UnfencedStatement):  # a comment hidden the same way
        fenced_update(
            "action", set_clause="writer_epoch = :fence_epoch", where="x = 'a' AND y = 'b'--'"
        )
    # ...and an honest literal containing those characters is still accepted.
    fenced_update(
        "outbox",
        set_clause="recipient = 'a(b', writer_epoch = :fence_epoch",
        where="payload = '--'",
    )


def test_the_table_is_chosen_from_a_closed_set_not_composed(cp):
    """A table name carrying its own SQL can comment the fence away entirely.

    ``action (x) SELECT 1 WHERE 1 /*`` leaves SQLite reading an unterminated
    block comment to end of input, so the builder's columns, values and fence
    are never part of the statement at all. A name is not a fragment, so it is
    picked from the closed set rather than validated as text.
    """

    assert set(PROTECTED_TABLES) == {"run", "session", "lease", "outbox", "incident", "action"}
    with pytest.raises(UnfencedStatement):
        fenced_insert(
            "action (action_id) SELECT 'x' WHERE 1 /*",
            columns=["action_id", "writer_epoch"],
            values=[":a", ":fence_epoch"],
        )
    with pytest.raises(UnfencedStatement):
        fenced_update("sqlite_master", set_clause="writer_epoch = :fence_epoch", where="1 = 1")


def test_a_protected_write_cannot_rewrite_an_applied_rows_attribution(cp):
    """Finished evidence is added to, never replaced.

    Two rules, and both are the builder's rather than the caller's memory: the
    columns a row is attributed by cannot be assigned at all, and an update to
    `action` carries `applied_at_ms IS NULL` so it cannot land on a row that is
    already in the history and restamp its epoch under a later lease.
    """

    for column in ("kind", "idempotency_key", "action_id"):
        with pytest.raises(UnfencedStatement):
            fenced_update(
                "action",
                set_clause=f"{column} = :x, writer_epoch = :fence_epoch",
                where="action_id = :a",
            )

    alpha = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    protected_write(cp, alpha, effect("a1", now_ms=T0 + 1), now_ms=T0 + 1)
    beta = acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)

    restamp = ProtectedWrite(
        kind=EFFECT_KIND,
        idempotency_key="a1",
        statement=fenced_update(
            "action", set_clause="writer_epoch = :fence_epoch", where="action_id = :action_id"
        ),
        exactly_once_mechanism="transactional_with_record",
        params={"action_id": "a1"},
    )
    # beta holds a perfectly live token -- and still cannot touch alpha's row.
    with pytest.raises(ProtectedWriteMissed):
        protected_write(cp, beta, restamp, now_ms=T0 + TTL + 2)

    (row,) = write_history(cp, resource=RESOURCE)
    assert row["writer_epoch"] == alpha.epoch


def test_a_caller_cannot_rebind_the_fences_own_parameters():
    with pytest.raises(LeaseUsageError):
        ProtectedWrite(
            kind=EFFECT_KIND,
            idempotency_key="k",
            statement=APPLY_EFFECT,
            exactly_once_mechanism="transactional_with_record",
            params={"fence_epoch": 99, "fence_now_ms": 0},
        )


def test_a_handler_that_names_no_exactly_once_mechanism_is_refused():
    with pytest.raises(LeaseUsageError) as refused:
        ProtectedWrite(
            kind=EFFECT_KIND,
            idempotency_key="k",
            statement=APPLY_EFFECT,
            exactly_once_mechanism="probably_fine",
        )
    assert "human gate" in str(refused.value)


def test_a_write_that_misses_its_own_where_is_not_recorded_as_a_stale_writer(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    protected_write(cp, lease, effect("a1", now_ms=T0 + 1), now_ms=T0 + 1)

    missing = ProtectedWrite(
        kind=EFFECT_KIND,
        idempotency_key="a1",
        statement=fenced_update(
            "action",
            set_clause="status = 'applied', writer_epoch = :fence_epoch",
            where="action_id = :action_id AND status = 'pending'",
        ),
        exactly_once_mechanism="transactional_with_record",
        params={"action_id": "no-such-row"},
    )
    with pytest.raises(ProtectedWriteMissed):
        protected_write(cp, lease, missing, now_ms=T0 + 2)

    # One row, and it is the applied one. A refusal written here would be a
    # rejection that never happened, in the evidence gate item 5 is read from.
    assert [row["status"] for row in action_rows(cp)] == ["applied"]


def test_a_lease_operation_refuses_to_run_inside_somebody_elses_transaction(cp):
    cp.execute("BEGIN")
    cp.execute("UPDATE run SET status = 'paused' WHERE run_id = 'r1'")
    try:
        with pytest.raises(LeaseUsageError) as refused:
            acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
        assert "transaction" in str(refused.value)
    finally:
        cp.rollback()


def test_the_refusal_and_the_refused_attempt_commit_together(cp, db_path):
    """The refusal is durable before the caller is told about it."""

    stale = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)
    with pytest.raises(StaleWriterRefused):
        protected_write(
            cp, stale, effect("a1", now_ms=T0 + TTL + 2), now_ms=T0 + TTL + 2, attempt_id="refusal-1"
        )
    assert not cp.in_transaction  # committed, not left open for someone to roll back

    witness = open_control_plane(db_path)
    try:
        assert witness.execute(
            "SELECT status FROM action WHERE action_id = 'refusal-1'"
        ).fetchone() == ("refused",)
    finally:
        witness.close()


# ==========================================================================
# Criterion 3 -- at most one live holder per leased resource at any instant,
# shown over a timeline of lease rows rather than at sampled points.
# ==========================================================================


def test_a_second_claimant_is_refused_while_the_lease_is_live(cp):
    first = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    for offset in (0, 1, TTL - 1):
        with pytest.raises(LeaseHeld):
            acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + offset, ttl_ms=TTL)

    # The refused claimant changed nothing: not the holder, not the epoch, not
    # the expiry it would have extended.
    assert read_lease(cp, RESOURCE) == first


def test_re_acquiring_after_expiry_raises_the_epoch_even_for_the_same_holder(cp):
    first = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    again = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0 + TTL + 1, ttl_ms=TTL)

    assert again.epoch == first.epoch + 1
    # The old token dies with the old epoch. If re-acquiring preserved it, the
    # writes the holder had in flight under the old one would validate again.
    with pytest.raises(StaleWriterRefused):
        protected_write(cp, first, effect("a1", now_ms=T0 + TTL + 2), now_ms=T0 + TTL + 2)
    assert protected_write(cp, again, effect("a2", now_ms=T0 + TTL + 3), now_ms=T0 + TTL + 3) == 1


def test_the_timeline_of_lease_rows_has_one_authority_per_instant(cp):
    observations = []
    holders = ["alpha", "beta", "gamma", "alpha"]
    now = T0
    for holder in holders:
        lease = acquire(cp, resource=RESOURCE, holder=holder, now_ms=now, ttl_ms=TTL)
        observations.append(lease)
        observations.append(renew(cp, lease, now_ms=now + 1, ttl_ms=TTL))
        now += TTL + 1

    timeline = authority_timeline(observations)

    assert [authority.epoch for authority in timeline] == [1, 2, 3, 4]
    assert [authority.holder for authority in timeline] == holders
    # Half-open and contiguous: an epoch's authority ends exactly where its
    # successor's begins, so no instant is covered twice and none is unowned
    # between them. This is checked over the whole timeline, not sampled.
    for earlier, later in zip(timeline, timeline[1:]):
        assert earlier.until_ms == later.from_ms
    assert not epoch_regressions(timeline)
    # And the wall-clock windows the rows themselves claim are disjoint too:
    # a takeover is stamped at or after the previous expiry, so no recorded
    # instant has two holders. Checked over every pair, not at sample points.
    assert not overlapping_claims(claimed_timeline(observations))
    # A renewal restates an epoch rather than opening one; four acquisitions and
    # four renewals are four authorities.
    assert len(timeline) == 4


def test_the_write_history_shows_no_interleaving_from_the_rejected_writer(cp):
    """The durable half: read back out of SQLite alone, after the fact (D-0001)."""

    alpha = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    protected_write(cp, alpha, effect("a1", now_ms=T0 + 1), now_ms=T0 + 1)
    beta = acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)
    protected_write(cp, beta, effect("b1", now_ms=T0 + TTL + 2), now_ms=T0 + TTL + 2)
    with pytest.raises(StaleWriterRefused):  # alpha comes back
        protected_write(cp, alpha, effect("a2", now_ms=T0 + TTL + 3), now_ms=T0 + TTL + 3)
    protected_write(cp, beta, effect("b2", now_ms=T0 + TTL + 4), now_ms=T0 + TTL + 4)

    history = write_history(cp, resource=RESOURCE)

    assert [row["status"] for row in history] == ["applied", "applied", "refused", "applied"]
    # The applied rows are a linear sequence in epoch order with nothing from
    # the rejected writer between them; the refused row is present as the record
    # that it was kept out.
    assert not applied_epoch_regressions(history)
    assert [row["writer_epoch"] for row in history if row["status"] == "applied"] == [1, 2, 2]
    assert [row["writer_epoch"] for row in history if row["status"] == "refused"] == [1]


def test_two_resources_epochs_are_not_compared_with_each_other(cp):
    """Epochs belong to a resource, and the spike rows cannot say which.

    ``action`` has no resource column (``Q-0001``), so a history that mixes
    kinds is several independent sequences shuffled together: a valid epoch 2
    for one resource followed by a valid epoch 1 for another would read as a
    violation, and a real interleaving would hide in the same noise. The check
    refuses rather than answering a question the rows cannot support, and
    :func:`effect_kind` is how a kind carries its resource.
    """

    first = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)
    promoted = acquire(cp, resource=RESOURCE, holder="gamma", now_ms=T0 + 2 * TTL + 2, ttl_ms=TTL)
    other = acquire(cp, resource="run/r2", holder="alpha", now_ms=T0 + 2 * TTL + 2, ttl_ms=TTL)
    other_kind = effect_kind("run/r2", "deliver_task")

    protected_write(cp, promoted, effect("a1", now_ms=T0 + 2 * TTL + 3), now_ms=T0 + 2 * TTL + 3)
    protected_write(
        cp,
        other,
        effect("b1", now_ms=T0 + 2 * TTL + 4, kind=other_kind),
        now_ms=T0 + 2 * TTL + 4,
    )

    assert first.epoch < promoted.epoch and other.epoch == 1  # epoch 3 then epoch 1
    with pytest.raises(LeaseUsageError) as refused:
        applied_epoch_regressions(write_history(cp))
    assert "one leased resource at a time" in str(refused.value)

    # Scoped to one resource, each history is a clean sequence of its own.
    assert not applied_epoch_regressions(write_history(cp, resource=RESOURCE))
    assert not applied_epoch_regressions(write_history(cp, resource="run/r2"))
    assert resource_of_kind(other_kind) == "run/r2"


def test_every_effect_under_one_lease_stays_in_one_history(cp):
    """Two effect kinds, one lease: they share an epoch sequence and a history.

    Filtering by exact kind would split them, and a writer whose stale epoch
    landed under a *different* effect than the one before it would fall through
    the gap between the two halves.
    """

    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    other_effect = effect_kind(RESOURCE, "update_status")
    protected_write(cp, lease, effect("a1", now_ms=T0 + 1), now_ms=T0 + 1)
    protected_write(cp, lease, effect("a2", now_ms=T0 + 2, kind=other_effect), now_ms=T0 + 2)

    history = write_history(cp, resource=RESOURCE)

    assert [row["kind"] for row in history] == [EFFECT_KIND, other_effect]
    assert not applied_epoch_regressions(history)
    # Narrowing to one effect is still available; it is just not the scope the
    # single-writer property is about.
    assert len(write_history(cp, resource=RESOURCE, kind=EFFECT_KIND)) == 1


def test_the_history_is_ordered_by_the_database_not_by_the_callers_clock(cp):
    """Under skew a later write can carry an earlier timestamp.

    Ordering the evidence by `created_at_ms` would then invent a regression that
    never happened, so the query orders by the rows' own insertion order and
    exposes it as ``write_seq``.
    """

    alpha = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    protected_write(cp, alpha, effect("a1", now_ms=T0 + 10_000), now_ms=T0 + 1)
    beta = acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)
    # beta's clock lags alpha's: its write is later, and stamped earlier.
    protected_write(cp, beta, effect("b1", now_ms=T0 + 1), now_ms=T0 + TTL + 2)

    history = write_history(cp, resource=RESOURCE)

    assert [row["action_id"] for row in history] == ["a1", "b1"]
    assert [row["write_seq"] for row in history] == sorted(row["write_seq"] for row in history)
    assert history[1]["created_at_ms"] < history[0]["created_at_ms"]
    assert not applied_epoch_regressions(history)


def test_a_protected_write_to_another_table_is_stamped_on_its_own_row(cp):
    """The scope of `write_history()`, pinned rather than left implied.

    It reads `action`, which is the exactly-once effect record. A fenced write to
    `outbox` carries its epoch on the outbox row, where the same shape of query
    reads it; nothing synthesises an action row for it, because manufacturing an
    effect record for a write that is not an effect would corrupt the evidence
    gate item 4 is read out of.
    """

    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    enqueue = ProtectedWrite(
        kind=EFFECT_KIND,
        idempotency_key="m1",
        statement=fenced_insert(
            "outbox",
            columns=[
                "message_id",
                "run_id",
                "recipient",
                "payload",
                "dedup_key",
                "status",
                "writer_epoch",
                "enqueued_at_ms",
            ],
            values=[
                "'m1'",
                "'r1'",
                "'secretary'",
                "'{}'",
                "'d1'",
                "'pending'",
                ":fence_epoch",
                ":now_ms",
            ],
        ),
        exactly_once_mechanism="transactional_with_record",
        params={"now_ms": T0 + 1},
    )

    assert protected_write(cp, lease, enqueue, now_ms=T0 + 1) == 1

    assert cp.execute("SELECT writer_epoch FROM outbox WHERE message_id = 'm1'").fetchone() == (
        lease.epoch,
    )
    assert write_history(cp, resource=RESOURCE) == ()
    # Refusals are the exception: a refused write has no row of its own to be
    # stamped on, so it is recorded in `action` whatever table it was aimed at.
    acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)
    with pytest.raises(StaleWriterRefused):
        protected_write(cp, lease, enqueue, now_ms=T0 + TTL + 2)
    assert [row["status"] for row in write_history(cp, resource=RESOURCE)] == ["refused"]


def test_write_history_is_answerable_by_query_after_the_process_is_gone(cp, db_path):
    alpha = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    protected_write(cp, alpha, effect("a1", now_ms=T0 + 1), now_ms=T0 + 1)
    cp.close()

    reopened = open_control_plane(db_path)
    try:
        history = write_history(reopened, resource=RESOURCE)
        assert [row["writer_epoch"] for row in history] == [1]
        # The lease row itself is durable too, epoch included -- the recovering
        # process is not told which epoch was live, it reads it.
        assert read_lease(reopened, RESOURCE).epoch == 1
    finally:
        reopened.close()


def test_a_released_lease_is_expired_and_never_deleted(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    released = release(cp, lease, now_ms=T0 + 10)

    assert released.expires_at_ms == T0 + 10
    assert not released.looks_live_at(T0 + 10)
    # The row survives, carrying its epoch. A deleted row would let the next
    # acquisition restart at epoch 1 and hand a returning stale holder a token
    # that validates.
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute("DELETE FROM lease WHERE resource = ?", (RESOURCE,))
    cp.rollback()  # the refused DELETE left sqlite3's implicit transaction open
    assert acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + 11, ttl_ms=TTL).epoch == 2
    # And the released holder's token is dead the moment it is released.
    with pytest.raises(StaleWriterRefused):
        protected_write(cp, lease, effect("a1", now_ms=T0 + 12), now_ms=T0 + 12)


def test_a_superseded_holder_can_neither_renew_nor_release(cp):
    alpha = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    beta = acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)

    with pytest.raises(LeaseNotHeld):
        renew(cp, alpha, now_ms=T0 + TTL + 2, ttl_ms=TTL)
    with pytest.raises(LeaseNotHeld):
        release(cp, alpha, now_ms=T0 + TTL + 2)

    # Neither refusal touched the live lease -- a release by a former holder
    # that expired the current one would hand the resource to a third claimant.
    assert read_lease(cp, RESOURCE) == beta


def test_an_expired_lease_cannot_be_renewed_back_to_life(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    with pytest.raises(LeaseNotHeld):
        renew(cp, lease, now_ms=T0 + TTL + 1, ttl_ms=TTL)

    # Re-acquiring is the way back, and re-acquiring raises the epoch -- so a
    # holder that was away cannot return under the token it left with.
    assert acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0 + TTL + 2, ttl_ms=TTL).epoch == 2


def test_a_renewal_keeps_the_epoch_and_the_token_keeps_working(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    renewed = renew(cp, lease, now_ms=T0 + TTL - 1, ttl_ms=TTL)

    assert renewed.epoch == lease.epoch
    assert renewed.expires_at_ms == T0 + 2 * TTL - 1
    # The token the holder is already writing under stays valid across its own
    # renewal; bumping the epoch here would invalidate its own writes in flight.
    assert protected_write(cp, lease, effect("a1", now_ms=T0 + TTL), now_ms=T0 + TTL) == 1


# ==========================================================================
# Criterion 4 -- clock skew forward and backward across the expiry boundary is
# handled and tested.
# ==========================================================================


def test_a_fast_clock_takes_the_lease_over_and_the_fence_still_excludes(cp):
    """The case where the wall clock genuinely does *not* provide exclusion.

    Alpha holds until ``T0 + TTL`` by its own clock. Beta's clock runs an hour
    fast, so it sees the lease as long expired and takes it over at an instant
    alpha still believes it holds. In *true* time the two holders overlap; the
    exclusion holds anyway, because it was never the clock's.
    """

    skew = 3_600_000
    alpha = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    beta = acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + skew, ttl_ms=TTL)

    # Alpha, on its own unskewed clock, still believes it holds the lease...
    assert alpha.looks_live_at(T0 + 1)
    # ...and its write is refused all the same, by the epoch and not by the time.
    with pytest.raises(StaleWriterRefused) as refused:
        protected_write(cp, alpha, effect("a1", now_ms=T0 + 1), now_ms=T0 + 1)
    assert refused.value.observed == beta

    # The rows themselves cannot show the overlap, and that is the point worth
    # writing down: beta stamped its acquisition in its own skewed frame, so the
    # recorded windows are disjoint while the true ones are not. A timeline of
    # lease rows is only as truthful as the clocks that wrote it -- which is
    # exactly why a protected write validates the epoch and not the expiry.
    assert not overlapping_claims(claimed_timeline([alpha, beta]))
    true_time = [
        Lease(RESOURCE, "alpha", 1, T0, T0 + TTL),
        Lease(RESOURCE, "beta", 2, T0 + 10, T0 + 10 + TTL),  # the same events, one clock
    ]
    overlaps = overlapping_claims(claimed_timeline(true_time))
    assert {claim.holder for pair in overlaps for claim in pair} == {"alpha", "beta"}

    # Authority is ordered by epoch, so it does not overlap in either frame.
    timeline = authority_timeline([alpha, beta])
    assert [authority.epoch for authority in timeline] == [1, 2]
    assert not epoch_regressions(timeline)


def test_a_clock_that_jumps_back_does_not_resurrect_a_superseded_token(cp):
    """Backward skew across the boundary, from the loser's side."""

    alpha = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)

    # Alpha's clock jumps back to before its own expiry, so by every check it
    # could make locally, its lease is live again.
    assert alpha.looks_live_at(T0 - TTL)
    with pytest.raises(StaleWriterRefused):
        protected_write(cp, alpha, effect("a1", now_ms=T0 - TTL), now_ms=T0 - TTL)
    (row,) = action_rows(cp)
    assert row["status"] == "refused"


def test_a_slow_clock_declines_to_take_a_lease_it_sees_as_live(cp):
    """Backward skew from the claimant's side: the safe direction.

    Acquisition requires the existing lease to have expired at the *claimant's*
    clock, so a slow clock sees a lease as more live than it is and refuses to
    take it over. It stalls; it does not admit a second writer.
    """

    acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    with pytest.raises(LeaseHeld):
        acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 - 3_600_000, ttl_ms=TTL)

    assert read_lease(cp, RESOURCE).holder == "alpha"


def test_a_backward_skewed_renewal_shortens_rather_than_extends(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    renewed = renew(cp, lease, now_ms=T0 + 1, ttl_ms=TTL // 3)

    assert renewed.expires_at_ms < lease.expires_at_ms
    # Ending its own authority earlier is safe; the resource becomes takeable
    # sooner, and the takeover raises the epoch as always.
    assert acquire(
        cp, resource=RESOURCE, holder="beta", now_ms=renewed.expires_at_ms, ttl_ms=TTL
    ).epoch == 2


def test_a_renewal_skewed_behind_its_own_acquisition_is_refused_not_crashed(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    with pytest.raises(ClockSkewRefused):
        renew(cp, lease, now_ms=T0 - 10_000, ttl_ms=1_000)

    # The lease is untouched -- in particular it was not left half-written by a
    # CHECK violation surfacing from inside what the caller thought was a renewal.
    assert read_lease(cp, RESOURCE) == lease


def test_releasing_with_a_clock_behind_the_acquisition_stays_legal(cp):
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    released = release(cp, lease, now_ms=T0 - 10_000)

    # The row's own CHECK requires expires_at_ms > acquired_at_ms, so the
    # release clamps to acquired_at_ms + 1 rather than failing. The window it
    # leaves is one millisecond wide and errs towards withholding the resource.
    assert released.expires_at_ms == T0 + 1
    assert not released.looks_live_at(T0 + 1)


def test_releasing_late_never_pushes_an_expiry_forward(cp):
    """Giving a lease up may not be the thing that extends it.

    The lease expired at ``T0 + TTL`` and nobody took it. Releasing it an hour
    later must not move the expiry to the hour mark: that would make the
    releasing holder's own token read live again over the interval it had
    already lost, and would withhold the resource from a claimant whose clock
    falls inside it.
    """

    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    released = release(cp, lease, now_ms=T0 + 3_600_000)

    assert released.expires_at_ms == lease.expires_at_ms
    # The token stayed dead throughout the interval a forward-moved expiry would
    # have revived it over.
    for now in (T0 + TTL + 1, T0 + TTL + 1000, T0 + 3_600_000 - 1):
        with pytest.raises(StaleWriterRefused):
            protected_write(cp, lease, effect(f"a-{now}", now_ms=now), now_ms=now)
    # And the resource was takeable at every one of those instants.
    assert acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL).epoch == 2


def test_the_database_never_supplies_a_clock_of_its_own(cp):
    """Every timestamp is the caller's -- there is no DEFAULT to inherit."""

    columns = cp.execute("PRAGMA table_info(lease)").fetchall()
    defaults = {row[1]: row[4] for row in columns}
    assert defaults["acquired_at_ms"] is None and defaults["expires_at_ms"] is None
    # And the module never reaches for one either: no wall-clock call anywhere.
    source = Path(s6.__file__).read_text(encoding="utf-8")
    for forbidden in ("time.time", "datetime.now", "CURRENT_TIMESTAMP", "strftime('now'"):
        assert forbidden not in source


# ==========================================================================
# Criterion 5 -- where an external destination can enforce a stale token, it
# does; where it cannot, that is written down rather than assumed away.
# ==========================================================================


def test_an_enforcing_destination_rejects_the_stale_token_from_its_own_record(cp):
    """Proven against the destination's record, not ours (``ACCEPTANCE.md`` §2)."""

    destination = EpochGuardedDestination()
    alpha = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)
    assert destination.apply(
        resource=RESOURCE, holder="alpha", epoch=alpha.epoch, effect_key="e1", payload="first"
    )
    beta = acquire(cp, resource=RESOURCE, holder="beta", now_ms=T0 + TTL + 1, ttl_ms=TTL)
    destination.apply(
        resource=RESOURCE, holder="beta", epoch=beta.epoch, effect_key="e2", payload="second"
    )

    with pytest.raises(DestinationRejectedStaleToken):
        destination.apply(
            resource=RESOURCE, holder="alpha", epoch=alpha.epoch, effect_key="e3", payload="stale"
        )

    assert destination.highest_epoch(RESOURCE) == beta.epoch
    assert destination.rejected == [(RESOURCE, "alpha", alpha.epoch)]
    assert destination.effect_count("e3") == 0


def test_an_enforcing_destination_absorbs_a_duplicate_under_a_live_token(cp):
    """Fencing and idempotency are separate properties, and both are its own."""

    destination = EpochGuardedDestination()
    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    assert destination.apply(
        resource=RESOURCE, holder="alpha", epoch=lease.epoch, effect_key="e1", payload="x"
    )
    assert not destination.apply(
        resource=RESOURCE, holder="alpha", epoch=lease.epoch, effect_key="e1", payload="x"
    )
    assert destination.effect_count("e1") == 1


def test_a_destination_that_cannot_enforce_must_record_its_residual():
    with pytest.raises(LeaseUsageError) as refused:
        Destination(name="silent", enforces_stale_token=False, note="cannot", residual=None)
    assert "written down rather than assumed away" in str(refused.value)

    with pytest.raises(LeaseUsageError):
        Destination(name="enforcing", enforces_stale_token=True, note="does", residual="but also")


def test_every_registered_destination_is_written_down_in_the_doc():
    """The register and ``docs/lease-fencing.md`` say the same thing.

    A residual that drifts out of the code is a residual nobody is holding any
    more, and a register entry with no written-down counterpart is the
    assumed-away gap section 2 rules out. Neither can happen silently while this
    passes.
    """

    text = DOC.read_text(encoding="utf-8")
    rows = {
        match.group("name"): match.group("verdict").strip()
        for match in re.finditer(
            r"^\|\s*`(?P<name>[a-z_]+)`\s*\|\s*(?P<verdict>yes|no)\s*\|", text, re.MULTILINE
        )
    }

    assert rows, "the destination register table is missing from the doc"
    assert set(rows) == set(DESTINATIONS)
    for name, destination in DESTINATIONS.items():
        assert rows[name] == ("yes" if destination.enforces_stale_token else "no")
        if not destination.enforces_stale_token:
            assert destination.residual and destination.residual.strip()


def test_the_provider_is_registered_as_unable_to_enforce():
    """The one entry the fence search makes non-negotiable.

    U27 measured an admission window in which two writers both exited 0 and both
    wrote; U32 found no exclusion at all on the ``--resume`` path. A register
    that let the provider count as enforcing would put back exactly the
    assumption ``investigation/pre-spawn-fence-search.md`` §5.3 removed.
    """

    provider = DESTINATIONS["session_provider_child_process"]
    assert provider.enforces_stale_token is False
    assert "U27" in provider.note and "U32" in provider.note
    assert "human gate" in provider.residual or "D-0004" in provider.residual


# ==========================================================================
# Criterion 6 -- no test may lean on the provider refusing a duplicate. Every
# case above must pass with the provider's refusal assumed absent.
# ==========================================================================


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_dependency_edge_on_the_session_provider():
    """Asserted structurally, so it fails the build rather than a review.

    Neither the implementation nor this suite may reach a provider. There is
    consequently no case here whose outcome could depend on a provider refusing
    a duplicate -- the property is proven by there being no such call to make,
    not by each test declaring that it did not rely on one.
    """

    for module in (Path(s6.__file__), Path(__file__)):
        for imported in _imported_modules(module):
            assert "session" not in imported.split("."), f"{module.name} imports {imported}"
            assert "provider" not in imported.split("."), f"{module.name} imports {imported}"


def test_no_dataclass_default_is_one_a_supported_python_would_reject(cp):
    """The rule Python 3.11 applies, checked on whatever version is running.

    3.11's dataclasses refuse any default whose type is unhashable -- a
    ``MappingProxyType({})`` among them -- while 3.10 and 3.12 accept it, so a
    module that imports fine here can fail to import at all on one row of the
    support matrix. The project supports 3.10 through 3.12; this reproduces the
    strictest of their rules rather than waiting for CI to find it on one third
    of the jobs.
    """

    for name, value in vars(s6).items():
        if not (isinstance(value, type) and dataclasses.is_dataclass(value)):
            continue
        for declared in dataclasses.fields(value):
            if declared.default is dataclasses.MISSING:
                continue
            assert type(declared.default).__hash__ is not None, (
                f"{name}.{declared.name} defaults to a "
                f"{type(declared.default).__name__}, which Python 3.11 rejects as a "
                "mutable default; use field(default_factory=...)"
            )


def test_the_only_exclusion_is_the_lease_and_the_module_says_so():
    """The premise is in the module, where a later reader will meet it.

    ``I-08``'s ``c2_revision`` adds this criterion so that nobody later reads
    "the provider refuses duplicates" as a reason to soften the issue. A comment
    is the wrong place for that only if nothing checks it is still there.
    """

    source = Path(s6.__file__).read_text(encoding="utf-8")
    assert "U27" in source and "U32" in source
    assert "pre-spawn-fence-search.md" in source


# ==========================================================================
# The fence itself, as a shape
# ==========================================================================


def test_fenced_statements_carry_the_fence_verbatim():
    update = fenced_update(
        "outbox",
        set_clause="status = 'delivered', writer_epoch = :fence_epoch",
        where="message_id = :m",
    )
    insert = fenced_insert("action", columns=["action_id", "writer_epoch"], values=[":a", ":fence_epoch"])

    assert FENCE_SQL in update and FENCE_SQL in insert
    # The fence is one constant, not a template rebuilt at each call site: a
    # fence assembled by string surgery is one that can be assembled slightly
    # wrong, and the failure is invisible in the row that results.
    assert update.count("EXISTS (SELECT 1 FROM lease") == 1
    assert insert.count("EXISTS (SELECT 1 FROM lease") == 1


def test_the_fence_matches_the_whole_token_not_just_the_resource(cp):
    """Resource, holder and epoch all have to match, and it has to be live."""

    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    for index, wrong in enumerate(
        (
            Lease("run/other", lease.holder, lease.epoch, lease.acquired_at_ms, lease.expires_at_ms),
            Lease(lease.resource, "beta", lease.epoch, lease.acquired_at_ms, lease.expires_at_ms),
            Lease(lease.resource, lease.holder, 2, lease.acquired_at_ms, lease.expires_at_ms),
        )
    ):
        with pytest.raises(StaleWriterRefused):
            protected_write(
                cp,
                wrong,
                effect(
                    f"a-{index}",
                    now_ms=T0 + 1,
                    kind=effect_kind(wrong.resource, "deliver_task"),
                ),
                now_ms=T0 + 1,
            )

    assert protected_write(cp, lease, effect("good", now_ms=T0 + 2), now_ms=T0 + 2) == 1


def test_a_kind_may_not_name_a_resource_the_token_is_not_for(cp):
    """A kind is how a row records which lease allocated its epoch.

    If the two could disagree, one kind would accumulate epochs from several
    leases and the history read back under it would be two unrelated sequences
    with nothing left to tell them apart.
    """

    lease = acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=TTL)

    with pytest.raises(LeaseUsageError):
        protected_write(
            cp,
            lease,
            effect("a1", now_ms=T0 + 1, kind=effect_kind("run/elsewhere", "deliver_task")),
            now_ms=T0 + 1,
        )
    with pytest.raises(LeaseUsageError):
        protected_write(cp, lease, effect("a2", now_ms=T0 + 1, kind="uncomposed"), now_ms=T0 + 1)
    assert not action_rows(cp)


def test_acquire_refuses_a_lease_that_expires_when_it_starts(cp):
    with pytest.raises(LeaseUsageError):
        acquire(cp, resource=RESOURCE, holder="alpha", now_ms=T0, ttl_ms=0)
    assert read_lease(cp, RESOURCE) is None
