"""The event spine's contract: one append transaction, and a per-consumer drain.

These tests are for ``docs/production-schema.md`` sections 5.1-5.6 and
``D-0030``, and they exist because every property below is one that a plausible
implementation satisfies on the happy path and loses on the failure path --
which is the shape of failure this design was written against. So the assertions
are deliberately about what is *absent* after something went wrong:

* an append whose side table refuses leaves **no** event, **no** consumption and
  **no** outbox row. Asserting only on ``event`` would pass for an
  implementation that leaked an orphan outbox row, and an event with no delivery
  record -- or a delivery with no event -- is v1's push-vs-poll duplication
  coming back;
* a re-append of a known ``dedup_key`` creates no second consumption row for
  anybody, not merely no second event;
* every drain quantity is per consumer, and one consumer draining does not move
  another's numbers. That is the twenty-day failure of ``tools/relay_scan.py``
  written as a regression test, and it is named as one;
* the two section 5.6 reconcile passes age against the tolerance of the
  revision the **caller bound**, which is asserted with two revisions on
  record because a read that forgot the predicate still returns rows;
* a skip that the fence refuses appends no ``consumption_skipped`` event, and a
  skip that succeeds always appends exactly one. A ``skipped`` row with no
  recorded reason is indistinguishable from a consumer quietly dropping work,
  so "unreachable" has to be asserted rather than intended.

Every timestamp comes from :data:`T0` and arithmetic on it. No test reads a
clock: the schema gives no timestamp column a ``DEFAULT`` precisely so that
clock skew across an expiry boundary is expressible, and a suite whose
expectations move with the wall clock cannot assert a boundary at all.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane.events import (
    DEGRADED_ORPHANED_OUTBOX_SQL,
    EVENT_TYPES,
    ORPHANED_OUTBOX_SQL,
    AppendedEvent,
    EventSpineUsageError,
    StaleConsumerRefused,
    append_event,
    backlog_depth,
    backlogged_consumers,
    drain_frontier,
    head_of_line_age_ms,
    mark_consumed,
    mark_failed,
    mark_skipped,
    orphaned_outbox,
    register_consumer,
    subscribe,
    undrained,
    unsubscribe,
)
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.control_plane.policy import (
    NotADuration,
    PolicyRowMissing,
    effective_revision_id,
)
from claude_org_runtime.control_plane.txn import (
    TransactionUsageError,
    in_autocommit,
    transaction,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

TTL = 60_000  # a lease window long enough that no test crosses it by accident


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "production.sqlite3"


@pytest.fixture
def cp(db_path: Path):
    connection = create_production_control_plane(db_path, now_ms=T0)
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------
# helpers -- the smallest legal row of each kind
# --------------------------------------------------------------------------


def add_run(cp, run_id: str = "run-1", at: int = T0) -> str:
    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?)",
        (run_id, "running", at, at),
    )
    return run_id


def grant_lease(cp, resource: str, *, holder: str = "worker-1", epoch: int = 1,
                at: int = T0, ttl_ms: int = TTL) -> int:
    """Put a live lease on *resource*. The settle fence validates against this."""

    cp.execute(
        "INSERT INTO lease (resource, holder, epoch, acquired_at_ms, expires_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (resource, holder, epoch, at, at + ttl_ms),
    )
    return epoch


def add_consumer(cp, consumer_id: str, *, kind: str = "compute",
                 event_type: str | None = "ci_observed", recipient: str | None = None,
                 at: int = T0, registered_from_seq: int = 0) -> str:
    """Register a consumer, give it a live lease, and subscribe it.

    The lease resource is derived from the consumer id so that each consumer in
    a test fences against its own epoch -- two consumers sharing one lease would
    make the stale-epoch tests pass for the wrong reason.
    """

    register_consumer(
        cp,
        consumer_id=consumer_id,
        kind=kind,
        lease_resource=f"consumer:{consumer_id}",
        registered_at_ms=at,
        registered_from_seq=registered_from_seq,
    )
    grant_lease(cp, f"consumer:{consumer_id}", holder=consumer_id, at=at)
    if event_type is not None:
        subscribe(
            cp,
            consumer_id=consumer_id,
            event_type=event_type,
            recipient=recipient,
            added_at_ms=at,
        )
    return consumer_id


def append(cp, *, event_id: str = "evt-1", event_type: str = "ci_observed",
           at: int = T0, **kwargs) -> AppendedEvent:
    payload = {
        "subject_kind": "run",
        "subject_id": "run-1",
        "dedup_key": f"dk/{event_id}",
        "producer": "gh-watcher",
    }
    payload.update(kwargs)
    return append_event(
        cp,
        event_id=event_id,
        event_type=event_type,
        occurred_at_ms=at,
        ingested_at_ms=at,
        **payload,
    )


def rows(cp, sql: str, *params) -> list[tuple]:
    return list(cp.execute(sql, params).fetchall())


def explain(cp, sql: str, params: dict) -> str:
    """The query plan SQLite chose for *sql*, flattened for substring assertions."""

    return " ".join(
        str(row) for row in cp.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    )


def rows_of(cp, sql: str, params: dict) -> list[tuple]:
    return list(cp.execute(sql, params).fetchall())


def executed_outbox_sql(cp) -> str:
    """The outbox statement :func:`orphaned_outbox` really ran, from the driver.

    Traced through ``set_trace_callback`` rather than pasted into the test or
    read out of the source: the plan assertions below are only worth anything if
    they are made against the text sqlite was actually handed. The trace hands
    back the statement with its parameters already expanded, which EXPLAIN QUERY
    PLAN accepts unchanged.
    """

    seen: list[str] = []
    cp.set_trace_callback(seen.append)
    try:
        orphaned_outbox(cp, revision_id=seeded_revision(cp), now_ms=T0 + DELIVERY_T + 1)
    finally:
        cp.set_trace_callback(None)
    outbox_statements = [sql for sql in seen if "FROM outbox" in sql]
    assert len(outbox_statements) == 1, outbox_statements
    return outbox_statements[0]


def count(cp, table: str) -> int:
    return int(cp.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --------------------------------------------------------------------------
# the shared transaction helper
# --------------------------------------------------------------------------


def test_transaction_refuses_a_connection_the_driver_would_commit_for_itself(db_path):
    connection = sqlite3.connect(db_path)
    try:
        with pytest.raises(TransactionUsageError):
            with transaction(connection):
                pass
    finally:
        connection.close()


def test_in_autocommit_makes_such_a_connection_usable(db_path):
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE t (a INTEGER)")
        connection.commit()
        with transaction(in_autocommit(connection)):
            connection.execute("INSERT INTO t VALUES (1)")
        assert rows(connection, "SELECT a FROM t") == [(1,)]
    finally:
        connection.close()


def test_transaction_rolls_the_whole_block_back_on_any_exception(cp):
    add_run(cp)
    with pytest.raises(RuntimeError):
        with transaction(cp):
            append_event(
                cp,
                event_id="evt-1",
                event_type="ci_observed",
                subject_kind="run",
                subject_id="run-1",
                dedup_key="dk/1",
                producer="gh-watcher",
                occurred_at_ms=T0,
                ingested_at_ms=T0,
            )
            raise RuntimeError("the caller's own failure, after a nested append")
    assert count(cp, "event") == 0


def test_a_nested_transaction_joins_the_outer_one_instead_of_committing_early(cp):
    add_run(cp)
    with pytest.raises(RuntimeError):
        with transaction(cp):
            # append_event opens a transaction of its own; joining is what lets
            # a composed operation stay one all-or-nothing unit.
            append(cp)
            assert count(cp, "event") == 1
            raise RuntimeError("abandon the composed operation")
    assert count(cp, "event") == 0


# --------------------------------------------------------------------------
# the append transaction -- section 5.4
# --------------------------------------------------------------------------


def test_append_fans_out_one_pending_consumption_row_per_subscribed_consumer(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    add_consumer(cp, "completion")

    appended = append(cp)

    assert appended.duplicate is False
    assert appended.seq == 1
    assert appended.consumptions == ("completion", "secretary")
    assert rows(
        cp, "SELECT consumer_id, status, created_at_ms FROM event_consumption ORDER BY consumer_id"
    ) == [("completion", "pending", T0), ("secretary", "pending", T0)]


def test_a_delivery_subscriber_gets_an_outbox_row_in_the_same_transaction_and_a_compute_one_does_not(cp):
    add_run(cp)
    add_consumer(cp, "relay", kind="delivery", recipient="secretary-pane")
    add_consumer(cp, "completion", kind="compute")

    appended = append(cp, payload=json.dumps({"verdict": "success"}))

    assert appended.messages == ("event/evt-1/relay",)
    assert rows(
        cp, "SELECT message_id, recipient, dedup_key, status, payload FROM outbox"
    ) == [
        (
            "event/evt-1/relay",
            "secretary-pane",
            "event/evt-1/relay",
            "pending",
            '{"verdict": "success"}',
        )
    ]
    assert rows(
        cp, "SELECT consumer_id, message_id FROM event_consumption ORDER BY consumer_id"
    ) == [("completion", None), ("relay", "event/evt-1/relay")]


def test_delivery_payload_renders_per_recipient_without_a_second_row_on_the_spine(cp):
    add_run(cp)
    add_consumer(cp, "relay-a", kind="delivery", recipient="pane-a")
    add_consumer(cp, "relay-b", kind="delivery", recipient="pane-b")

    append(
        cp,
        delivery_payload=lambda consumer_id, recipient: json.dumps(
            {"to": recipient, "by": consumer_id}
        ),
    )

    assert count(cp, "event") == 1
    assert rows(cp, "SELECT recipient, payload FROM outbox ORDER BY recipient") == [
        ("pane-a", '{"to": "pane-a", "by": "relay-a"}'),
        ("pane-b", '{"to": "pane-b", "by": "relay-b"}'),
    ]


def test_the_side_effect_receives_the_seq_the_event_was_assigned(cp):
    add_run(cp)
    seen: list[int] = []
    appended = append(cp, side_effect=lambda connection, seq: seen.append(seq))
    assert seen == [appended.seq]


def test_a_failing_side_effect_leaves_no_event_no_consumption_and_no_outbox_row(cp):
    add_run(cp)
    add_consumer(cp, "relay", kind="delivery", recipient="secretary-pane")

    def refuse(connection: sqlite3.Connection, seq: int) -> None:
        raise RuntimeError("the typed side table refused this fact")

    with pytest.raises(RuntimeError):
        append(cp, side_effect=refuse)

    # All three, not just the event: an orphan outbox row is a delivery with no
    # fact behind it, which is the same failure from the other direction.
    assert count(cp, "event") == 0
    assert count(cp, "event_consumption") == 0
    assert count(cp, "outbox") == 0


def test_a_re_append_of_the_same_dedup_key_is_an_idempotent_no_op(cp):
    add_run(cp)
    add_consumer(cp, "secretary")

    first = append(cp, event_id="evt-1")
    again = append(cp, event_id="evt-1-retry", dedup_key="dk/evt-1")

    assert again.duplicate is True
    assert again.seq is None
    # The id of the event that actually holds the fact, not the one refused.
    assert again.event_id == first.event_id
    assert again.consumptions == ()
    assert count(cp, "event") == 1
    assert count(cp, "event_consumption") == 1


def test_a_re_append_creates_no_second_consumption_row_for_a_consumer_added_between_the_two(cp):
    add_run(cp)
    append(cp, event_id="evt-1")
    add_consumer(cp, "late")

    again = append(cp, event_id="evt-1", dedup_key="dk/evt-1")

    assert again.duplicate is True
    assert backlog_depth(cp, consumer_id="late") == 0


def test_reusing_an_event_id_for_a_different_fact_is_an_error_not_a_duplicate(cp):
    add_run(cp)
    append(cp, event_id="evt-1", dedup_key="dk/a")
    with pytest.raises(sqlite3.IntegrityError):
        append(cp, event_id="evt-1", dedup_key="dk/b")


def test_a_consumer_registered_after_the_append_receives_nothing_for_it(cp):
    add_run(cp)
    append(cp)
    add_consumer(cp, "late")

    assert backlog_depth(cp, consumer_id="late") == 0
    assert drain_frontier(cp, consumer_id="late") is None


def test_a_removed_subscription_and_a_retired_consumer_are_not_subscribers(cp):
    add_run(cp)
    add_consumer(cp, "unsubscribed")
    add_consumer(cp, "retired")
    unsubscribe(
        cp, consumer_id="unsubscribed", event_type="ci_observed", removed_at_ms=T0 + 1
    )
    cp.execute(
        "UPDATE consumer SET retired_at_ms = ? WHERE consumer_id = 'retired'", (T0 + 1,)
    )

    appended = append(cp, at=T0 + 2)

    assert appended.consumptions == ()
    assert count(cp, "event_consumption") == 0


def test_unsubscribing_something_that_is_not_subscribed_is_refused_not_a_no_op(cp):
    add_consumer(cp, "secretary")
    with pytest.raises(EventSpineUsageError):
        unsubscribe(
            cp, consumer_id="secretary", event_type="pr_merged", removed_at_ms=T0 + 1
        )


def test_a_backfilling_registration_gets_the_history_it_asked_for_and_none_of_what_it_did_not(cp):
    add_run(cp)
    append(cp, event_id="evt-1", event_type="ci_observed")          # seq 1
    append(cp, event_id="evt-2", event_type="ci_observed")          # seq 2
    append(cp, event_id="evt-3", event_type="pr_merged")            # seq 3

    with transaction(cp):
        register_consumer(
            cp,
            consumer_id="catch-up",
            kind="compute",
            lease_resource="consumer:catch-up",
            registered_at_ms=T0 + 5,
            registered_from_seq=1,
            backfill=True,
        )
        subscribe(
            cp, consumer_id="catch-up", event_type="ci_observed", added_at_ms=T0 + 5
        )

    # seq 2 only: seq 1 is at or below the watershed it registered from, and
    # seq 3 is an event type it never subscribed to.
    assert [row["event_seq"] for row in undrained(cp, consumer_id="catch-up")] == [2]


def test_a_registration_without_backfill_gets_no_history_at_all(cp):
    add_run(cp)
    append(cp, event_id="evt-1")
    add_consumer(cp, "forward-only", registered_from_seq=0)
    assert backlog_depth(cp, consumer_id="forward-only") == 0


def test_a_subscription_added_in_a_later_transaction_does_not_backfill(cp):
    add_run(cp)
    append(cp, event_id="evt-1")
    register_consumer(
        cp,
        consumer_id="late-subscriber",
        kind="compute",
        lease_resource="consumer:late-subscriber",
        registered_at_ms=T0 + 1,
        registered_from_seq=0,
        backfill=True,
    )
    subscribe(
        cp, consumer_id="late-subscriber", event_type="ci_observed", added_at_ms=T0 + 2
    )
    assert backlog_depth(cp, consumer_id="late-subscriber") == 0


def test_registering_the_same_consumer_twice_is_refused(cp):
    add_consumer(cp, "secretary")
    with pytest.raises(sqlite3.IntegrityError):
        register_consumer(
            cp,
            consumer_id="secretary",
            kind="compute",
            lease_resource="consumer:secretary",
            registered_at_ms=T0,
            registered_from_seq=0,
        )


def test_a_delivery_subscription_without_a_recipient_is_refused_at_registration(cp):
    register_consumer(
        cp,
        consumer_id="relay",
        kind="delivery",
        lease_resource="consumer:relay",
        registered_at_ms=T0,
        registered_from_seq=0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        subscribe(cp, consumer_id="relay", event_type="ci_observed", added_at_ms=T0)


# --------------------------------------------------------------------------
# settling -- fenced, typed, and never silent
# --------------------------------------------------------------------------


def test_mark_consumed_settles_the_row_and_raises_the_attempt_count(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp)

    mark_consumed(
        cp, consumer_id="secretary", event_seq=1, writer_epoch=1, settled_at_ms=T0 + 10
    )

    assert rows(
        cp,
        "SELECT status, attempt_count, writer_epoch, settled_at_ms FROM event_consumption",
    ) == [("consumed", 1, 1, T0 + 10)]
    assert backlog_depth(cp, consumer_id="secretary") == 0


def test_a_settle_by_a_stale_consumer_epoch_is_refused_and_the_refusal_is_typed(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp)
    # Someone else took the consumer's lease over, which raises the epoch; the
    # old token now matches nothing.
    cp.execute(
        "UPDATE lease SET holder = 'usurper', epoch = 2 WHERE resource = 'consumer:secretary'"
    )

    with pytest.raises(StaleConsumerRefused) as refusal:
        mark_consumed(
            cp,
            consumer_id="secretary",
            event_seq=1,
            writer_epoch=1,
            settled_at_ms=T0 + 10,
        )

    # The refusal is durable in the only sense that matters here: the row it was
    # refused against is untouched and still counts as backlog.
    assert refusal.value.observed is not None
    assert refusal.value.observed["status"] == "pending"
    assert backlog_depth(cp, consumer_id="secretary") == 1


def test_a_settle_against_an_expired_lease_is_refused(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp)
    with pytest.raises(StaleConsumerRefused):
        mark_consumed(
            cp,
            consumer_id="secretary",
            event_seq=1,
            writer_epoch=1,
            settled_at_ms=T0 + TTL + 1,
        )


def test_a_settled_consumption_is_not_settled_twice(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp)
    mark_consumed(
        cp, consumer_id="secretary", event_seq=1, writer_epoch=1, settled_at_ms=T0 + 10
    )
    with pytest.raises(StaleConsumerRefused):
        mark_consumed(
            cp,
            consumer_id="secretary",
            event_seq=1,
            writer_epoch=1,
            settled_at_ms=T0 + 20,
        )


def test_mark_failed_leaves_the_consumption_undrained_and_the_error_readable(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp)

    mark_failed(
        cp,
        consumer_id="secretary",
        event_seq=1,
        writer_epoch=1,
        last_error="pane not found",
        now_ms=T0 + 10,
    )

    assert backlog_depth(cp, consumer_id="secretary") == 1
    (row,) = undrained(cp, consumer_id="secretary")
    assert (row["status"], row["last_error"], row["attempt_count"]) == (
        "failed",
        "pane not found",
        1,
    )


def test_a_retry_that_lands_after_a_failure_clears_the_error_and_keeps_the_attempts(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp)
    mark_failed(
        cp,
        consumer_id="secretary",
        event_seq=1,
        writer_epoch=1,
        last_error="pane not found",
        now_ms=T0 + 10,
    )
    mark_consumed(
        cp, consumer_id="secretary", event_seq=1, writer_epoch=1, settled_at_ms=T0 + 20
    )
    assert rows(cp, "SELECT status, last_error, attempt_count FROM event_consumption") == [
        ("consumed", None, 2)
    ]


# --------------------------------------------------------------------------
# skipping -- section 5.3's evidence requirement
# --------------------------------------------------------------------------


def test_mark_skipped_appends_a_consumption_skipped_event_in_the_same_transaction(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", subject_kind="run", subject_id="run-1")

    appended = mark_skipped(
        cp,
        consumer_id="secretary",
        event_seq=1,
        writer_epoch=1,
        reason="not this consumer's repository",
        settled_at_ms=T0 + 10,
        event_id="evt-skip-1",
        ingested_at_ms=T0 + 10,
    )

    assert appended.duplicate is False
    (event_type, subject_kind, subject_id, dedup_key, producer, payload) = rows(
        cp,
        "SELECT event_type, subject_kind, subject_id, dedup_key, producer, payload "
        "FROM event WHERE seq = ?",
        appended.seq,
    )[0]
    assert event_type == "consumption_skipped"
    # The ORIGINAL subject: the closed subject_kind CHECK has no 'consumer'
    # member, and inventing one for an audit record would be a schema change.
    assert (subject_kind, subject_id) == ("run", "run-1")
    assert dedup_key == "consumption_skipped/secretary/1"
    assert producer == "secretary"
    assert json.loads(payload)["reason"] == "not this consumer's repository"
    assert rows(cp, "SELECT status, last_error FROM event_consumption WHERE event_seq = 1") == [
        ("skipped", None)
    ]


def test_a_skip_refused_by_the_fence_appends_no_consumption_skipped_event(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp)
    cp.execute("UPDATE lease SET holder = 'usurper', epoch = 2 WHERE resource = 'consumer:secretary'")

    with pytest.raises(StaleConsumerRefused):
        mark_skipped(
            cp,
            consumer_id="secretary",
            event_seq=1,
            writer_epoch=1,
            reason="not applicable",
            settled_at_ms=T0 + 10,
            event_id="evt-skip-1",
            ingested_at_ms=T0 + 10,
        )

    # One event on the spine: the original. A skip with no audit event is
    # unreachable because the settle and the append share one transaction.
    assert count(cp, "event") == 1
    assert rows(cp, "SELECT status FROM event_consumption") == [("pending",)]


def test_every_skipped_consumption_has_a_consumption_skipped_event_naming_it(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    add_consumer(cp, "completion")
    append(cp)
    mark_skipped(
        cp,
        consumer_id="secretary",
        event_seq=1,
        writer_epoch=1,
        reason="not applicable",
        settled_at_ms=T0 + 10,
        event_id="evt-skip-1",
        ingested_at_ms=T0 + 10,
    )

    skipped = rows(
        cp, "SELECT consumer_id, event_seq FROM event_consumption WHERE status = 'skipped'"
    )
    audited = rows(
        cp, "SELECT dedup_key FROM event WHERE event_type = 'consumption_skipped'"
    )
    assert [f"consumption_skipped/{c}/{s}" for c, s in skipped] == [key for (key,) in audited]


# --------------------------------------------------------------------------
# drain -- section 5.5, per consumer and never global
# --------------------------------------------------------------------------


def test_one_consumer_draining_does_not_move_another_consumers_numbers_the_relay_scan_regression(cp):
    """The twenty-day failure, as a regression test.

    ``tools/relay_scan.py`` let 134 terminal events sit undelivered for twenty
    days behind a scan that reported nothing wrong. A single ``drained_at``
    column reaches the same outcome by a different route: the first consumer to
    finish marks the row drained and every other consumer's backlog becomes
    invisible. So the property is asserted directly -- ``secretary`` drains
    everything, and ``completion``'s depth, frontier and head-of-line age are
    all exactly what they were before it did.
    """

    add_run(cp)
    add_consumer(cp, "secretary")
    add_consumer(cp, "completion")
    append(cp, event_id="evt-1", at=T0)
    append(cp, event_id="evt-2", at=T0 + 1_000)

    before = (
        backlog_depth(cp, consumer_id="completion"),
        drain_frontier(cp, consumer_id="completion"),
        head_of_line_age_ms(cp, consumer_id="completion", now_ms=T0 + 5_000),
    )
    assert before == (2, 1, 5_000)

    for seq in (1, 2):
        mark_consumed(
            cp,
            consumer_id="secretary",
            event_seq=seq,
            writer_epoch=1,
            settled_at_ms=T0 + 2_000,
        )

    assert backlog_depth(cp, consumer_id="secretary") == 0
    assert drain_frontier(cp, consumer_id="secretary") is None
    assert head_of_line_age_ms(cp, consumer_id="secretary", now_ms=T0 + 5_000) is None
    assert (
        backlog_depth(cp, consumer_id="completion"),
        drain_frontier(cp, consumer_id="completion"),
        head_of_line_age_ms(cp, consumer_id="completion", now_ms=T0 + 5_000),
    ) == before


def test_drain_frontier_is_derived_from_the_rows_and_never_stored(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", at=T0)
    append(cp, event_id="evt-2", at=T0 + 1_000)

    assert drain_frontier(cp, consumer_id="secretary") == 1
    mark_consumed(
        cp, consumer_id="secretary", event_seq=1, writer_epoch=1, settled_at_ms=T0 + 10
    )
    assert drain_frontier(cp, consumer_id="secretary") == 2

    # Nothing was written to make the frontier move: no column anywhere holds
    # it, so it cannot drift out of agreement with the consumption rows.
    columns = {
        row[1]
        for table in ("consumer", "event_consumption", "event")
        for row in cp.execute(f"PRAGMA table_info({table})")
    }
    assert not [name for name in columns if "frontier" in name or "cursor" in name]


def test_a_failed_row_still_counts_as_backlog_and_holds_the_frontier(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", at=T0)
    append(cp, event_id="evt-2", at=T0 + 1_000)
    mark_failed(
        cp,
        consumer_id="secretary",
        event_seq=1,
        writer_epoch=1,
        last_error="destination refused",
        now_ms=T0 + 10,
    )
    mark_consumed(
        cp, consumer_id="secretary", event_seq=2, writer_epoch=1, settled_at_ms=T0 + 2_000
    )

    # A cursor could not express this; per-event rows can.
    assert backlog_depth(cp, consumer_id="secretary") == 1
    assert drain_frontier(cp, consumer_id="secretary") == 1


def test_head_of_line_age_is_measured_from_our_ingest_clock_not_the_providers(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append_event(
        cp,
        event_id="evt-1",
        event_type="ci_observed",
        subject_kind="run",
        subject_id="run-1",
        dedup_key="dk/1",
        producer="gh-watcher",
        occurred_at_ms=T0 - 900_000,  # the provider's clock, far behind ours
        ingested_at_ms=T0,
    )
    assert head_of_line_age_ms(cp, consumer_id="secretary", now_ms=T0 + 30_000) == 30_000


def test_undrained_is_per_consumer_and_carries_the_event_it_is_about(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1")

    (row,) = undrained(cp, consumer_id="secretary")
    assert row["event_id"] == "evt-1"
    assert row["event_type"] == "ci_observed"
    assert row["ingested_at_ms"] == T0
    assert undrained(cp, consumer_id="nobody") == ()


# --------------------------------------------------------------------------
# the module's own vocabulary
# --------------------------------------------------------------------------


def test_the_event_type_vocabulary_is_the_modules_own_and_not_a_schema_constraint(cp):
    add_run(cp)
    assert "consumption_skipped" in EVENT_TYPES
    # The DDL leaves event_type open text on purpose: a closed CHECK would make
    # every new producer a schema change.
    appended = append(cp, event_type="something.this.module.never.emits")
    assert appended.duplicate is False


def test_a_malformed_argument_is_refused_before_anything_is_written(cp):
    add_run(cp)
    with pytest.raises(EventSpineUsageError):
        append(cp, at="not-a-timestamp")
    with pytest.raises(EventSpineUsageError):
        append(cp, payload="not json")
    assert count(cp, "event") == 0


# --------------------------------------------------------------------------
# section 5.6 -- the two reconcile passes
#
# Both bind a revision the caller resolved, so both are proved with **two
# revisions on record**: a read that forgot the predicate still returns rows,
# and only a second revision carrying a different tolerance can tell a bound
# read from an unbound one. Each boundary is asserted on the exact millisecond
# the design names, because "exceeds" and "at or exceeds" differ by one row and
# only at that instant.
# --------------------------------------------------------------------------


def add_revision(cp, *, note: str, effective_at_ms: int, **thresholds: tuple) -> int:
    """A later policy revision, carrying new tolerances for the named classes.

    Everything but the tolerance is carried from the seed: what these tests vary
    is ``T``, and varying ``L`` as well would let a budget CHECK, rather than the
    binding, decide whether the row inserts.
    """

    revision_id = cp.execute(
        "INSERT INTO policy_revision (note, decided_by, effective_at_ms) VALUES (?, ?, ?)",
        (note, "test", effective_at_ms),
    ).lastrowid
    for incident_class, (threshold_kind, threshold_value) in thresholds.items():
        cp.execute(
            """
            INSERT INTO policy_detection_latency
                (revision_id, incident_class, threshold_kind, threshold_value,
                 reconcile_period_ms, budget_ms, budget_kind)
            VALUES (?, ?, ?, ?, 120000, 900000, 'absolute_ms')
            """,
            (revision_id, incident_class, threshold_kind, threshold_value),
        )
    return int(revision_id)


BACKLOG_T = 300_000  # consumer_backlog, seeded: T = 5 min (time-base-policy.md 3.2)
DELIVERY_T = 120_000  # relay_delivery_stall, seeded: T = 2 min


def seeded_revision(cp) -> int:
    return effective_revision_id(cp, now_ms=T0)


# -- the undrained-events pass ---------------------------------------------


def test_a_consumer_inside_the_backlog_tolerance_is_not_named(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", at=T0)

    revision = seeded_revision(cp)
    # On the bound, and one millisecond short of it: T is what the work is
    # ENTITLED to (time-base-policy.md 3.1), so neither instant is abnormal.
    assert backlogged_consumers(cp, revision_id=revision, now_ms=T0 + BACKLOG_T - 1) == ()
    assert backlogged_consumers(cp, revision_id=revision, now_ms=T0 + BACKLOG_T) == ()

    named = backlogged_consumers(cp, revision_id=revision, now_ms=T0 + BACKLOG_T + 1)
    assert [row["consumer_id"] for row in named] == ["secretary"]
    assert named[0]["head_of_line_age_ms"] == BACKLOG_T + 1
    assert named[0]["tolerance_ms"] == BACKLOG_T
    assert named[0]["revision_id"] == revision
    assert named[0]["incident_class"] == "consumer_backlog"


def test_the_backlog_threshold_follows_the_revision_the_caller_bound(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", at=T0)

    seed = seeded_revision(cp)
    tighter = add_revision(
        cp,
        note="tighter consumer backlog",
        effective_at_ms=T0 + 1_000,
        consumer_backlog=("absolute_ms", 60_000),
    )
    now = T0 + 90_000  # inside the seed's 5 min, past the later revision's 1 min

    # The same rows, the same instant, two answers -- which is the whole reason
    # the revision is an argument and not something this query picks for itself.
    assert backlogged_consumers(cp, revision_id=seed, now_ms=now) == ()
    assert [row["consumer_id"] for row in backlogged_consumers(
        cp, revision_id=tighter, now_ms=now)] == ["secretary"]
    # And a read that had forgotten the predicate would match BOTH rows and
    # report the consumer under either binding; this asserts it reports one
    # tolerance, the bound one.
    assert backlogged_consumers(cp, revision_id=tighter, now_ms=now)[0]["tolerance_ms"] == 60_000


def test_the_pass_never_resolves_a_revision_for_itself(cp):
    # A default would be D-0031's corollary reintroduced one call deeper, where
    # a report and a detector could no longer disagree about which instant they
    # are judging. The shape is asserted so a later "convenience" has to delete
    # a test that says why it must not.
    parameters = inspect.signature(backlogged_consumers).parameters
    assert parameters["revision_id"].default is inspect.Parameter.empty
    assert parameters["now_ms"].default is inspect.Parameter.empty
    assert inspect.signature(orphaned_outbox).parameters.keys() == parameters.keys()


def test_a_revision_that_decides_no_backlog_tolerance_refuses_rather_than_passing(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", at=T0)
    silent = add_revision(cp, note="decides nothing", effective_at_ms=T0 + 1_000)

    # An empty tuple here would be indistinguishable from "no consumer is
    # backlogged", which is the twenty-day failure with a policy table in it.
    with pytest.raises(PolicyRowMissing):
        backlogged_consumers(cp, revision_id=silent, now_ms=T0 + 10 * BACKLOG_T)
    with pytest.raises(PolicyRowMissing):
        orphaned_outbox(cp, revision_id=silent, now_ms=T0 + 10 * BACKLOG_T)


def test_a_count_threshold_is_refused_and_not_read_as_milliseconds(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", at=T0)
    counted = add_revision(
        cp,
        note="backlog as a count",
        effective_at_ms=T0 + 1_000,
        consumer_backlog=("consecutive_count", 5),
    )
    # Read as 5 ms, every consumer alive would be reported as backlogged.
    with pytest.raises(NotADuration):
        backlogged_consumers(cp, revision_id=counted, now_ms=T0 + 10)


def test_the_pass_is_per_consumer_and_one_drain_does_not_hide_another(cp):
    # relay_scan.py's twenty-day silence as a regression test: with a global
    # oldest-undrained figure, 'brisk' draining the head of the spine would
    # empty this result while 'stuck' was still stuck.
    add_run(cp)
    add_consumer(cp, "stuck")
    add_consumer(cp, "brisk")
    append(cp, event_id="evt-1", at=T0)
    mark_consumed(
        cp, consumer_id="brisk", event_seq=1, writer_epoch=1, settled_at_ms=T0 + 10
    )

    now = T0 + BACKLOG_T + 1
    named = backlogged_consumers(cp, revision_id=seeded_revision(cp), now_ms=now)
    assert [row["consumer_id"] for row in named] == ["stuck"]

    # There is no global shape to fall back on: every row names a consumer.
    assert all("consumer_id" in row for row in named)
    assert named[0]["backlog_depth"] == 1


def test_the_age_is_taken_at_the_frontier_row_and_not_at_the_oldest_ingest(cp):
    # ingested_at_ms is the caller's value (no column has a DEFAULT), so a
    # producer catching up can commit an OLDER instant at a HIGHER sequence.
    # Head-of-line blocking is about the row at the front, so seq 1 is the row
    # that must be aged -- MIN(ingested_at_ms) would age seq 2 instead and
    # report a backlog five minutes before there is one.
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", at=T0 + 400_000)
    append(cp, event_id="evt-2", at=T0)

    revision = seeded_revision(cp)
    now = T0 + 400_000 + BACKLOG_T  # the frontier is exactly on its bound
    assert backlogged_consumers(cp, revision_id=revision, now_ms=now) == ()

    named = backlogged_consumers(cp, revision_id=revision, now_ms=now + 1)
    assert named[0]["drain_frontier"] == 1
    assert named[0]["ingested_at_ms"] == T0 + 400_000
    assert named[0]["head_of_line_age_ms"] == BACKLOG_T + 1
    assert named[0]["backlog_depth"] == 2


def test_a_failed_row_keeps_a_consumer_in_the_pass_and_a_settle_removes_it(cp):
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", at=T0)
    mark_failed(
        cp, consumer_id="secretary", event_seq=1, writer_epoch=1,
        last_error="destination refused", now_ms=T0 + 10,
    )

    revision = seeded_revision(cp)
    now = T0 + BACKLOG_T + 1
    # 'failed' is undrained (section 5.5): a consumer cannot make its own
    # backlog disappear by failing, and the attempt does not reset the age.
    assert [row["consumer_id"] for row in backlogged_consumers(
        cp, revision_id=revision, now_ms=now)] == ["secretary"]

    # Settled inside the lease window the consumer actually holds (the fence
    # validates expiry at the settle's own clock), and the pass goes quiet at
    # the same later instant it was reporting a moment ago.
    mark_consumed(
        cp, consumer_id="secretary", event_seq=1, writer_epoch=1, settled_at_ms=T0 + 30_000
    )
    assert backlogged_consumers(cp, revision_id=revision, now_ms=now) == ()


def test_a_skipped_consumption_does_not_count_as_backlog(cp):
    # Why 'skipped' exists at all: an inapplicable subscription that stayed
    # 'pending' would age into a consumer_backlog incident forever.
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp, event_id="evt-1", at=T0)
    mark_skipped(
        cp, consumer_id="secretary", event_seq=1, writer_epoch=1,
        reason="not applicable to this repo", settled_at_ms=T0 + 10,
        event_id="evt-skip", ingested_at_ms=T0 + 10,
    )
    assert backlogged_consumers(
        cp, revision_id=seeded_revision(cp), now_ms=T0 + 100 * BACKLOG_T
    ) == ()


# -- the orphaned-outbox pass ----------------------------------------------


def a_delivery(cp, consumer_id: str = "secretary", *, event_id: str = "evt-1",
               at: int = T0) -> str:
    """Append an event with a ``delivery`` subscriber, returning its message id.

    The outbox row is written by the append transaction itself (section 5.4), so
    this pass is aged over exactly the rows the spine enqueued rather than over
    a fixture's idea of one.
    """

    add_consumer(cp, consumer_id, kind="delivery", recipient="secretary-inbox", at=at)
    appended = append(cp, event_id=event_id, at=at)
    return appended.messages[0]


def test_an_unacked_message_is_orphaned_strictly_past_the_delivery_tolerance(cp):
    add_run(cp)
    message_id = a_delivery(cp)
    revision = seeded_revision(cp)

    assert orphaned_outbox(cp, revision_id=revision, now_ms=T0 + DELIVERY_T) == ()
    orphaned = orphaned_outbox(cp, revision_id=revision, now_ms=T0 + DELIVERY_T + 1)
    assert [row["message_id"] for row in orphaned] == [message_id]
    assert orphaned[0]["age_ms"] == DELIVERY_T + 1
    assert orphaned[0]["tolerance_ms"] == DELIVERY_T
    assert orphaned[0]["revision_id"] == revision
    assert orphaned[0]["status"] == "pending"


def test_the_delivery_threshold_follows_the_revision_the_caller_bound(cp):
    add_run(cp)
    a_delivery(cp)
    seed = seeded_revision(cp)
    tighter = add_revision(
        cp,
        note="tighter delivery stall",
        effective_at_ms=T0 + 1_000,
        relay_delivery_stall=("absolute_ms", 30_000),
    )
    now = T0 + 60_000  # inside the seed's 2 min, past the later revision's 30 s

    assert orphaned_outbox(cp, revision_id=seed, now_ms=now) == ()
    assert len(orphaned_outbox(cp, revision_id=tighter, now_ms=now)) == 1


def test_a_delivered_but_unacked_message_is_the_case_the_pass_exists_for(cp):
    add_run(cp)
    message_id = a_delivery(cp)
    cp.execute(
        "UPDATE outbox SET status = 'delivered', delivered_at_ms = ? WHERE message_id = ?",
        (T0 + 1_000, message_id),
    )
    now = T0 + DELIVERY_T + 1

    # status = 'pending' would go quiet here, and this is precisely the crash
    # window: the send landed, the ack did not come back.
    orphaned = orphaned_outbox(cp, revision_id=seeded_revision(cp), now_ms=now)
    assert [(row["message_id"], row["status"]) for row in orphaned] == [
        (message_id, "delivered")
    ]

    cp.execute(
        "UPDATE outbox SET status = 'acked', acked_at_ms = ? WHERE message_id = ?",
        (now, message_id),
    )
    assert orphaned_outbox(cp, revision_id=seeded_revision(cp), now_ms=now) == ()


def test_the_pass_mutates_nothing_at_all(cp):
    # Section 5.6 re-attempts; this function only NAMES. A detector that bumped
    # retry_count would inflate the evidence an operator reads to decide whether
    # a destination is refusing -- and outbox_retry_count_is_monotonic would not
    # catch it, because an increment is exactly what that trigger permits.
    add_run(cp)
    a_delivery(cp)
    before = rows(cp, "SELECT * FROM outbox")
    orphaned_outbox(cp, revision_id=seeded_revision(cp), now_ms=T0 + 100 * DELIVERY_T)
    assert rows(cp, "SELECT * FROM outbox") == before


def test_the_orphan_query_uses_the_partial_index_written_to_serve_it(cp):
    # 0001_initial.sql: CREATE INDEX outbox_undelivered ON outbox(enqueued_at_ms)
    # WHERE status <> 'acked'. Both the indexable predicate form and the
    # arithmetic one return the same rows, so only the PLAN distinguishes them
    # -- and outbox rows are never deleted, so a scan grows without bound.
    #
    # This EXPLAINs the constant the FUNCTION executes, not a copy pasted here.
    # The pasted form was in this test and it stayed green while the shipped
    # predicate was rewritten into the degraded arithmetic below, which is the
    # whole regression the assertion claims to catch.
    params = {"now_ms": T0, "tolerance_ms": DELIVERY_T, "revision_id": 1,
              "incident_class": "relay_delivery_stall"}
    # The plan of the statement the FUNCTION ran, captured from the driver.
    plan = explain(cp, executed_outbox_sql(cp), {})
    assert "SEARCH" in plan
    assert "outbox_undelivered" in plan
    assert "SCAN" not in plan

    # The algebraically identical form does not: the column is inside an
    # expression, which no b-tree can seek on. Asserting this half is what makes
    # the half above mean something -- without it, a database where every plan
    # says SEARCH would also pass.
    assert DEGRADED_ORPHANED_OUTBOX_SQL != ORPHANED_OUTBOX_SQL
    degraded = explain(cp, DEGRADED_ORPHANED_OUTBOX_SQL, params)
    # SQLite still names outbox_undelivered here -- it reads the partial index
    # as a narrower covering table -- but the verb is SCAN, not SEARCH: every
    # unacked row ever enqueued is visited and the age is evaluated per row.
    # So the assertion is on the verb, never on the index name.
    assert "SEARCH" not in degraded
    assert "SCAN" in degraded


def test_the_two_forms_the_plan_test_separates_return_the_same_rows(cp):
    # If they disagreed on rows, the plan comparison would be comparing two
    # different questions and the index claim would be vacuous.
    add_run(cp)
    a_delivery(cp)
    params = {"now_ms": T0 + DELIVERY_T + 1, "tolerance_ms": DELIVERY_T,
              "revision_id": 1, "incident_class": "relay_delivery_stall"}
    assert (rows_of(cp, ORPHANED_OUTBOX_SQL, params)
            == rows_of(cp, DEGRADED_ORPHANED_OUTBOX_SQL, params)
            != [])


def test_the_two_passes_answer_about_different_rows_of_the_same_append(cp):
    # One append writes both records (section 5.4), and the two backstops age
    # them against different tolerances: an ack that never came back is not the
    # same fault as a consumer that never drained, and each has its own class.
    add_run(cp)
    message_id = a_delivery(cp)
    revision = seeded_revision(cp)

    mid = T0 + DELIVERY_T + 1  # past the delivery T, inside the backlog T
    assert [row["message_id"] for row in orphaned_outbox(
        cp, revision_id=revision, now_ms=mid)] == [message_id]
    assert backlogged_consumers(cp, revision_id=revision, now_ms=mid) == ()

    late = T0 + BACKLOG_T + 1
    assert [row["consumer_id"] for row in backlogged_consumers(
        cp, revision_id=revision, now_ms=late)] == ["secretary"]


def test_a_retired_consumer_is_not_backlogged_however_long_its_rows_sit(cp):
    # The rows a consumer left behind when it was retired stay pending forever.
    # Section 5.6's remedy for this class -- raise consumer_backlog, drain the
    # consumer -- has nobody left to perform it, so reporting one is an alarm no
    # action can clear. _subscribers already refuses to fan out to a retired
    # consumer for exactly this reason; the detector has to agree with it.
    add_run(cp)
    add_consumer(cp, "secretary")
    append(cp)
    revision = seeded_revision(cp)
    late = T0 + BACKLOG_T + 1

    assert [row["consumer_id"] for row in backlogged_consumers(
        cp, revision_id=revision, now_ms=late)] == ["secretary"]

    cp.execute(
        "UPDATE consumer SET retired_at_ms = ? WHERE consumer_id = 'secretary'",
        (T0 + 1,),
    )

    # The pending rows are still there -- retirement is not a drain, and the
    # fan-out history has to stay explicable.
    assert backlog_depth(cp, consumer_id="secretary") == 1
    for now in (late, T0 + 1_000 * BACKLOG_T):
        assert backlogged_consumers(cp, revision_id=revision, now_ms=now) == ()


def test_retiring_one_consumer_does_not_hide_another_that_is_still_stuck(cp):
    # The exclusion must be per consumer. A filter that dropped the whole
    # frontier CTE on any retirement would be the twenty-day silence again.
    add_run(cp)
    add_consumer(cp, "gone")
    add_consumer(cp, "stuck")
    append(cp)
    cp.execute(
        "UPDATE consumer SET retired_at_ms = ? WHERE consumer_id = 'gone'", (T0 + 1,)
    )

    assert [row["consumer_id"] for row in backlogged_consumers(
        cp, revision_id=seeded_revision(cp), now_ms=T0 + BACKLOG_T + 1)] == ["stuck"]


def test_a_malformed_reconcile_argument_is_refused_before_policy_is_read(cp):
    with pytest.raises(EventSpineUsageError):
        backlogged_consumers(cp, revision_id=0, now_ms=T0)
    with pytest.raises(EventSpineUsageError):
        backlogged_consumers(cp, revision_id=seeded_revision(cp), now_ms="soon")
    with pytest.raises(EventSpineUsageError):
        orphaned_outbox(cp, revision_id=-1, now_ms=T0)
