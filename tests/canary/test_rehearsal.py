"""The item 10 rehearsal, end to end -- Issue #23's acceptance criteria as one
scenario.

A REHEARSAL AGAINST A SYNTHETIC COUNTERPARTY (D-0022), and the file says so
because the *output* of this scenario is the rehearsal's evidence. It is not
a discharge: item 10 is discharged at the canary itself, with live v1 as the
counterparty, under numeric criteria Q-0005 leaves open -- none appear below,
and none of the timestamps or counts here is a go/no-go threshold.

The scenario is the canary shape (D-0013) played against the stand-in, with
the review-required minimum of three runs, so that "one worker at a time on
Interlock" and "exactly one new run routed to Interlock in total" are separate
assertions:

    run-v1-before  starts under the baseline decision   -> synthetic_v1
    run-canary     starts under the canary decision     -> interlock
    run-v1-after   starts after the rehearsed rollback  -> synthetic_v1

with run-v1-before finishing on the synthetic side mid-canary (v1-started
runs finish on v1), a writer audit over both stores before and after the
rollback, and the rollback itself asserted to have changed only the routing
decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_org_runtime.canary import (
    INTERLOCK,
    REHEARSAL_MARKING,
    SYNTHETIC_V1,
    RunStartRoutingPoint,
    SyntheticV1RunStore,
    compare_across_rollback,
    create_routing_ledger,
    snapshot_stores,
    writer_audit,
)
from claude_org_runtime.control_plane.schema import create_control_plane

T0 = 1_700_000_000_000


@pytest.fixture
def stores(tmp_path: Path):
    ledger = create_routing_ledger(tmp_path / "routing-ledger.sqlite3")
    interlock = create_control_plane(tmp_path / "control-plane.sqlite3")
    synthetic = SyntheticV1RunStore.create(tmp_path / "synthetic-v1-runs.jsonl")
    try:
        yield ledger, interlock, synthetic
    finally:
        interlock.close()
        ledger.close()


def start_run(routing, interlock, synthetic, run_id: str, at: int) -> str:
    """The rehearsal's run-start path: consult the routing point first, then
    take the answer to the owning system's own start -- the routing point
    itself starts nothing and knows neither store."""

    routed = routing.route_run_start(run_id, now_ms=at)
    if routed.owning_system == INTERLOCK:
        with interlock:
            interlock.execute(
                "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) "
                "VALUES (?, 'running', ?, ?)",
                (run_id, at, at),
            )
    else:
        synthetic.start_run(run_id, now_ms=at)
    return routed.owning_system


def test_the_rehearsed_canary_and_rollback(stores):
    ledger, interlock, synthetic = stores
    routing = RunStartRoutingPoint(ledger)

    # Baseline: new runs belong to the (synthetic) old system, as a recorded
    # decision rather than an assumption.
    routing.route_new_runs_to(SYNTHETIC_V1, now_ms=T0, reason="baseline: v1 stand-in owns new runs")
    assert start_run(routing, interlock, synthetic, "run-v1-before", at=T0 + 1) == SYNTHETIC_V1

    # The canary decision, and under it exactly one new run.
    routing.route_new_runs_to(INTERLOCK, now_ms=T0 + 2, reason="canary: one worker on interlock")
    assert start_run(routing, interlock, synthetic, "run-canary", at=T0 + 3) == INTERLOCK

    # A v1-started run finishes on v1, mid-canary, owner untouched.
    synthetic.finish_run("run-v1-before", now_ms=T0 + 4)
    assert routing.routed_run("run-v1-before").owning_system == SYNTHETIC_V1

    # Writer audit over both stores: no record written by both systems.
    mid = writer_audit(ledger, interlock, synthetic)
    assert mid.dual_written == ()
    assert mid.clean

    # The rehearsed rollback: one routing decision, nothing else. The stores
    # are canonically byte-identical across it except for the routing rows.
    before = snapshot_stores(ledger, interlock, synthetic)
    rollback = routing.route_new_runs_to(
        SYNTHETIC_V1, now_ms=T0 + 5, reason="rollback: routing decision only"
    )
    after = snapshot_stores(ledger, interlock, synthetic)
    comparison = compare_across_rollback(before, after)
    assert comparison.only_the_routing_decision_changed
    assert comparison.appended_decisions == (
        (rollback.decision_seq, SYNTHETIC_V1, T0 + 5, "rollback: routing decision only"),
    )

    # Subsequent new runs go back to the stand-in.
    assert start_run(routing, interlock, synthetic, "run-v1-after", at=T0 + 6) == SYNTHETIC_V1

    # Exactly one new run was routed to Interlock, in total, and no run
    # changed owner at any point in the scenario.
    owners = dict(ledger.execute("SELECT run_id, owning_system FROM run_owner"))
    assert owners == {
        "run-v1-before": SYNTHETIC_V1,
        "run-canary": INTERLOCK,
        "run-v1-after": SYNTHETIC_V1,
    }
    assert sum(1 for system in owners.values() if system == INTERLOCK) == 1

    # The Interlock-started run is still running on Interlock. What a real
    # rollback does with such runs is Q-0005's open question; the rehearsal
    # shows only that the rollback itself did not touch it.
    final = writer_audit(ledger, interlock, synthetic)
    assert final.clean
    assert final.interlock_written == ("run-canary",)
    assert final.synthetic_v1_written == ("run-v1-after", "run-v1-before")

    # The output is labelled: a rehearsal against a synthetic counterparty,
    # naming the canary as its discharge point.
    for output in (mid, final, comparison):
        assert output.label == REHEARSAL_MARKING
    assert "SYNTHETIC COUNTERPARTY" in REHEARSAL_MARKING
    assert "NOT A DISCHARGE" in REHEARSAL_MARKING
    assert "AT THE CANARY ITSELF" in REHEARSAL_MARKING
    assert "Q-0005 REMAINS OPEN" in REHEARSAL_MARKING
