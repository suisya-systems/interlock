"""The writer audit and the rollback comparison.

Durable half (D-0026). The audit's construction is what these tests hold on
to: attribution is physical presence in a store, the enumeration reads the
stores themselves rather than the ledger's opinion of them, and the
byte-identity claim is over a canonical serialisation whose stability is
itself asserted -- a comparison that must be forgiven false alarms is not
evidence, and one that cannot see a real difference is worse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_org_runtime.canary.audit import (
    canonical_sqlite_bytes,
    compare_across_rollback,
    snapshot_stores,
    sqlite_run_ids,
    writer_audit,
)
from claude_org_runtime.canary.ledger import INTERLOCK, SYNTHETIC_V1, create_routing_ledger
from claude_org_runtime.canary.marking import REHEARSAL_MARKING
from claude_org_runtime.canary.routing import RunStartRoutingPoint
from claude_org_runtime.canary.synthetic_v1 import SyntheticV1RunStore
from claude_org_runtime.control_plane.schema import create_control_plane

T0 = 1_700_000_000_000


@pytest.fixture
def ledger(tmp_path: Path):
    connection = create_routing_ledger(tmp_path / "routing-ledger.sqlite3")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def interlock(tmp_path: Path):
    connection = create_control_plane(tmp_path / "control-plane.sqlite3")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def synthetic(tmp_path: Path) -> SyntheticV1RunStore:
    return SyntheticV1RunStore.create(tmp_path / "synthetic-v1-runs.jsonl")


@pytest.fixture
def routing(ledger) -> RunStartRoutingPoint:
    return RunStartRoutingPoint(ledger)


def start_on_interlock(interlock, run_id: str, at: int = T0) -> None:
    with interlock:
        interlock.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?)",
            (run_id, "running", at, at),
        )


# --------------------------------------------------------------------------
# the writer audit
# --------------------------------------------------------------------------


def test_a_clean_split_audits_clean_and_the_report_is_labelled(
    routing, ledger, interlock, synthetic
):
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline")
    routing.route_run_start("run-a", now_ms=T0 + 1)
    synthetic.start_run("run-a", now_ms=T0 + 1)
    routing.route_new_runs_to(INTERLOCK, now_ms=T0 + 2, reason="canary")
    routing.route_run_start("run-b", now_ms=T0 + 3)
    start_on_interlock(interlock, "run-b", at=T0 + 3)

    report = writer_audit(ledger, interlock, synthetic)
    assert report.dual_written == ()
    assert report.unledgered == ()
    assert report.misrouted == ()
    assert report.clean
    assert report.interlock_written == ("run-b",)
    assert report.synthetic_v1_written == ("run-a",)
    assert report.label == REHEARSAL_MARKING


def test_a_record_written_by_both_systems_is_named(routing, ledger, interlock, synthetic):
    routing.route_new_runs_to(INTERLOCK, now_ms=T0, reason="canary")
    routing.route_run_start("run-dual", now_ms=T0 + 1)
    start_on_interlock(interlock, "run-dual")
    synthetic.start_run("run-dual", now_ms=T0 + 2)  # the dual write

    report = writer_audit(ledger, interlock, synthetic)
    assert report.dual_written == ("run-dual",)
    assert not report.clean
    # The synthetic-side copy also contradicts the ledger, and the report
    # says so rather than folding it into the dual-write count.
    assert (SYNTHETIC_V1, "run-dual") in report.misrouted


def test_a_write_that_bypassed_the_routing_point_is_named(ledger, interlock, synthetic):
    start_on_interlock(interlock, "run-rogue")
    report = writer_audit(ledger, interlock, synthetic)
    assert report.unledgered == ((INTERLOCK, "run-rogue"),)
    assert not report.clean


def test_a_run_in_the_wrong_store_is_named(routing, ledger, interlock, synthetic):
    routing.route_new_runs_to(INTERLOCK, now_ms=T0, reason="canary")
    routing.route_run_start("run-b", now_ms=T0 + 1)
    synthetic.start_run("run-b", now_ms=T0 + 2)  # ledger says interlock

    report = writer_audit(ledger, interlock, synthetic)
    assert report.misrouted == ((SYNTHETIC_V1, "run-b"),)
    assert not report.clean


def test_the_audit_reads_the_stores_not_the_ledger(routing, ledger, interlock, synthetic):
    # A ledger row with no store record is not a write; the audit does not
    # invent one from the ledger's opinion.
    routing.route_new_runs_to(INTERLOCK, now_ms=T0, reason="canary")
    routing.route_run_start("run-ledger-only", now_ms=T0 + 1)
    report = writer_audit(ledger, interlock, synthetic)
    assert report.interlock_written == ()
    assert report.synthetic_v1_written == ()
    assert report.clean


# --------------------------------------------------------------------------
# canonical serialisation: stable where it must be, sensitive where it must be
# --------------------------------------------------------------------------


def test_insertion_order_does_not_move_the_canonical_bytes(tmp_path):
    a = create_control_plane(tmp_path / "a.sqlite3")
    b = create_control_plane(tmp_path / "b.sqlite3")
    try:
        for connection, order in ((a, ("run-1", "run-2")), (b, ("run-2", "run-1"))):
            with connection:
                for run_id in order:
                    connection.execute(
                        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) "
                        "VALUES (?, 'running', ?, ?)",
                        (run_id, T0, T0),
                    )
        assert canonical_sqlite_bytes(a) == canonical_sqlite_bytes(b)
    finally:
        a.close()
        b.close()


def test_a_single_changed_value_moves_the_canonical_bytes(interlock):
    start_on_interlock(interlock, "run-1")
    before = canonical_sqlite_bytes(interlock)
    with interlock:
        interlock.execute(
            "UPDATE run SET status = 'done', updated_at_ms = ? WHERE run_id = 'run-1'",
            (T0 + 1,),
        )
    assert canonical_sqlite_bytes(interlock) != before


def test_exclusion_excludes_exactly_the_named_table(ledger, routing):
    routing.route_new_runs_to(INTERLOCK, now_ms=T0, reason="canary")
    with_routing = canonical_sqlite_bytes(ledger)
    without = canonical_sqlite_bytes(ledger, exclude_tables=("routing_decision",))
    assert with_routing != without
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0 + 1, reason="rollback")
    assert canonical_sqlite_bytes(ledger, exclude_tables=("routing_decision",)) == without


def test_a_schema_only_mutation_moves_the_canonical_bytes(ledger, routing, interlock, synthetic):
    # A rollback that created or dropped an EMPTY table -- or touched only
    # an index or trigger -- writes no row; the canonical stream must see
    # it anyway, or "byte-identical" would be blind to exactly the class of
    # store surgery a migration starts with.
    routing.route_new_runs_to(INTERLOCK, now_ms=T0, reason="canary")
    before = snapshot_stores(ledger, interlock, synthetic)
    with interlock:
        interlock.execute("CREATE TABLE migration_scaffold (x TEXT)")  # empty, rowless
    after = snapshot_stores(ledger, interlock, synthetic)
    comparison = compare_across_rollback(before, after)
    assert not comparison.interlock_identical
    assert not comparison.only_the_routing_decision_changed


def test_a_blob_value_is_canonicalised_not_crashed_on(interlock):
    # S5's outbox payload carries no typeof CHECK, so a store can legally
    # hold bytes -- and the store the canonicaliser most needs to see, one
    # with an unexpected write in it, must not be the one it cannot
    # serialise.
    start_on_interlock(interlock, "run-1")
    with interlock:
        interlock.execute(
            "INSERT INTO outbox (message_id, run_id, recipient, payload, dedup_key, "
            "status, enqueued_at_ms) VALUES ('msg-1', 'run-1', 'peer', ?, 'dk-1', "
            "'pending', ?)",
            (b"\x00\x01\xff", T0),
        )
    first = canonical_sqlite_bytes(interlock)
    assert first == canonical_sqlite_bytes(interlock)
    assert b"$blob_sha256" in first


def test_the_enumeration_reads_every_run_keyed_table(tmp_path):
    # "Enumeration is capture" has to survive a run that exists only in a
    # child table: a foreign writer with foreign_keys off can leave one, and
    # an audit that read only `run` would call that store unwritten.
    import sqlite3 as sqlite3_module

    scratch = sqlite3_module.connect(tmp_path / "scratch.sqlite3")
    try:
        scratch.execute("CREATE TABLE run (run_id TEXT PRIMARY KEY)")
        scratch.execute("CREATE TABLE outbox (message_id TEXT, run_id TEXT)")
        scratch.execute("CREATE TABLE unrelated (note TEXT)")
        scratch.execute("INSERT INTO run VALUES ('run-parent')")
        scratch.execute("INSERT INTO outbox VALUES ('msg-1', 'run-orphan')")
        scratch.execute("INSERT INTO outbox VALUES ('msg-2', NULL)")
        scratch.execute("INSERT INTO unrelated VALUES ('no run key here')")
        assert sqlite_run_ids(scratch) == ("run-orphan", "run-parent")
    finally:
        scratch.close()


# --------------------------------------------------------------------------
# the rollback comparison
# --------------------------------------------------------------------------


def test_a_rollback_alone_changes_only_the_routing_decision(
    routing, ledger, interlock, synthetic
):
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline")
    routing.route_run_start("run-a", now_ms=T0 + 1)
    synthetic.start_run("run-a", now_ms=T0 + 1)
    routing.route_new_runs_to(INTERLOCK, now_ms=T0 + 2, reason="canary")
    routing.route_run_start("run-b", now_ms=T0 + 3)
    start_on_interlock(interlock, "run-b", at=T0 + 3)

    before = snapshot_stores(ledger, interlock, synthetic)
    rollback = routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0 + 4, reason="rollback")
    after = snapshot_stores(ledger, interlock, synthetic)

    comparison = compare_across_rollback(before, after)
    assert comparison.only_the_routing_decision_changed
    assert comparison.appended_decisions == (
        (rollback.decision_seq, SYNTHETIC_V1, T0 + 4, "rollback"),
    )
    assert comparison.label == REHEARSAL_MARKING


def test_a_store_write_during_the_window_is_seen(routing, ledger, interlock, synthetic):
    routing.route_new_runs_to(INTERLOCK, now_ms=T0, reason="canary")
    before = snapshot_stores(ledger, interlock, synthetic)
    start_on_interlock(interlock, "run-smuggled")  # a migration would look like this
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0 + 1, reason="rollback")
    after = snapshot_stores(ledger, interlock, synthetic)

    comparison = compare_across_rollback(before, after)
    assert not comparison.interlock_identical
    assert not comparison.only_the_routing_decision_changed


def test_a_ledger_write_during_the_window_is_seen(routing, ledger, interlock, synthetic):
    routing.route_new_runs_to(INTERLOCK, now_ms=T0, reason="canary")
    before = snapshot_stores(ledger, interlock, synthetic)
    routing.route_run_start("run-late", now_ms=T0 + 1)  # an in-flight state conversion
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0 + 2, reason="rollback")
    after = snapshot_stores(ledger, interlock, synthetic)

    comparison = compare_across_rollback(before, after)
    assert not comparison.run_ledger_identical
    assert not comparison.only_the_routing_decision_changed


def test_the_predicate_answers_touched_nothing_else_not_rollback_happened(
    routing, ledger, interlock, synthetic
):
    # Two identical snapshots satisfy the predicate vacuously, by design:
    # it answers "did anything beyond routing change?", and "did a rollback
    # actually happen?" is appended_decisions' question. A caller asserting
    # a rollback must ask both -- as the rehearsal test does.
    routing.route_new_runs_to(INTERLOCK, now_ms=T0, reason="canary")
    snapshot = snapshot_stores(ledger, interlock, synthetic)
    comparison = compare_across_rollback(snapshot, snapshot)
    assert comparison.only_the_routing_decision_changed
    assert comparison.appended_decisions == ()


def test_a_rewritten_history_is_not_an_append(routing, ledger, interlock, synthetic):
    # Snapshots taken around a history that shrank (or changed under its
    # prefix) must not read as a clean rollback. The ledger's own triggers
    # forbid this; the comparison must still be able to say it, because the
    # comparison, not the trigger, is what the rehearsal's evidence cites.
    routing.route_new_runs_to(INTERLOCK, now_ms=T0, reason="canary")
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0 + 1, reason="rollback")
    after = snapshot_stores(ledger, interlock, synthetic)
    longer_history = snapshot_stores(ledger, interlock, synthetic)
    trimmed = after.__class__(
        interlock_digest=after.interlock_digest,
        synthetic_v1_digest=after.synthetic_v1_digest,
        run_ledger_digest=after.run_ledger_digest,
        routing_decision_rows=after.routing_decision_rows[:1],
    )
    comparison = compare_across_rollback(longer_history, trimmed)
    assert not comparison.decisions_appended_only
    assert comparison.appended_decisions == ()
    assert not comparison.only_the_routing_decision_changed
