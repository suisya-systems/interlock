"""The run-start routing point's contract.

Durable half (D-0026): whatever routes runs at the real canary still has to
refuse to assume an owner nobody decided, keep a started run's owner across
every later policy flip, and make rollback a single appended decision.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.canary.ledger import INTERLOCK, SYNTHETIC_V1, create_routing_ledger
from claude_org_runtime.canary.routing import (
    NoRoutingDecision,
    OwnerChangeRefused,
    RunStartRoutingPoint,
    UnknownOwningSystem,
    UnroutedRun,
)

T0 = 1_700_000_000_000


@pytest.fixture
def ledger(tmp_path: Path):
    connection = create_routing_ledger(tmp_path / "routing-ledger.sqlite3")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def routing(ledger) -> RunStartRoutingPoint:
    return RunStartRoutingPoint(ledger)


def test_no_owner_is_assumed_before_a_decision_exists(routing):
    with pytest.raises(NoRoutingDecision):
        routing.current_decision()
    with pytest.raises(NoRoutingDecision):
        routing.route_run_start("run-1", now_ms=T0)


def test_the_baseline_is_itself_a_recorded_decision(routing):
    decision = routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline")
    assert decision.decision_seq == 1
    assert routing.current_decision() == decision


def test_the_vocabulary_is_closed_at_the_api_too(routing):
    with pytest.raises(UnknownOwningSystem):
        routing.route_new_runs_to("v1", now_ms=T0, reason="the live system is not in this rehearsal")


def test_a_run_is_routed_under_the_newest_decision(routing):
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline")
    routing.route_new_runs_to(INTERLOCK, now_ms=T0 + 1, reason="canary")
    routed = routing.route_run_start("run-1", now_ms=T0 + 2)
    assert routed.owning_system == INTERLOCK
    assert routed.decision_seq == 2


def test_a_policy_flip_does_not_move_a_started_run(routing):
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline")
    routed = routing.route_run_start("run-1", now_ms=T0 + 1)
    routing.route_new_runs_to(INTERLOCK, now_ms=T0 + 2, reason="canary")
    assert routing.routed_run("run-1") == routed


def test_re_routing_to_the_same_owner_is_an_idempotent_no_op(routing):
    # A router that crashed between the ledger write and the system-specific
    # start may retry; the retry must not become a second row or a new time.
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline")
    first = routing.route_run_start("run-1", now_ms=T0 + 1)
    again = routing.route_run_start("run-1", now_ms=T0 + 99)
    assert again == first


def test_re_routing_under_a_flipped_policy_is_an_owner_change_and_refused(routing):
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline")
    routing.route_run_start("run-1", now_ms=T0 + 1)
    routing.route_new_runs_to(INTERLOCK, now_ms=T0 + 2, reason="canary")
    with pytest.raises(OwnerChangeRefused, match="mid-flight"):
        routing.route_run_start("run-1", now_ms=T0 + 3)
    # The refusal left the ledger row exactly as it was.
    assert routing.routed_run("run-1").owning_system == SYNTHETIC_V1


def test_a_non_ownership_integrity_failure_passes_through_as_itself(routing, ledger):
    # An empty run_id fails the DDL CHECK, which is not an ownership
    # question: it must surface as the database's own refusal -- not as an
    # idempotent retry, not as an owner change -- and write nothing.
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline")
    with pytest.raises(sqlite3.IntegrityError):
        routing.route_run_start("", now_ms=T0 + 1)
    assert ledger.execute("SELECT COUNT(*) FROM run_owner").fetchone() == (0,)


def test_a_run_never_routed_reads_as_such(routing):
    with pytest.raises(UnroutedRun):
        routing.routed_run("run-never")


def test_rollback_is_route_new_runs_to_and_nothing_else(routing, ledger):
    # The rollback's whole footprint: one appended routing_decision row.
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline")
    routing.route_new_runs_to(INTERLOCK, now_ms=T0 + 1, reason="canary")
    routing.route_run_start("run-1", now_ms=T0 + 2)

    rows_before = ledger.execute("SELECT * FROM run_owner").fetchall()
    rollback = routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0 + 3, reason="rollback")
    assert rollback.decision_seq == 3
    assert ledger.execute("SELECT * FROM run_owner").fetchall() == rows_before

    # And a run starting after the rollback falls under it.
    assert routing.route_run_start("run-2", now_ms=T0 + 4).owning_system == SYNTHETIC_V1
