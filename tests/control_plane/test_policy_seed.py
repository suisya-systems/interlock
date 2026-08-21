"""The seeded time base, value for value against ``docs/time-base-policy.md``.

Every number in ``0002_policy_seed.sql`` is a *decision* -- `D-0031` for the
detection budgets and the reconcile period, `D-0032` for gate ownership -- and
the whole point of holding them as policy rows rather than as constants is that
changing one is a deliberate, versioned act. That property is only real if a
silent drift fails something, so this file transcribes sections 3.2, 3.3, 4 and
6.1 of the design document into tables and compares the seeded rows against them
exactly. A migration that quietly relaxed a tolerance would otherwise be
indistinguishable from one that fixed a typo.

The tables below are therefore duplication **on purpose**. They are not derived
from the SQL, they are read from the design document, and the test is the
comparison between two independently written copies of the same decision. A
helper that generated the expectations from the migration would assert only that
SQLite can read back what it stored.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane.migrator import create_production_control_plane

T0 = 1_700_000_000_000

MINUTE_MS = 60_000

#: docs/time-base-policy.md section 3.3: "The reconcile period P is 120 seconds."
RECONCILE_PERIOD_MS = 120 * 1000

#: Section 3.2's table, transcribed: incident class -> (threshold_kind,
#: T, L, budget_kind). The three relative classes carry a multiple or a count
#: in T rather than a duration, and ``lease_orphan`` carries a multiple in L
#: too -- the adjudicated ``budget_kind`` column (section 3.2, the lease_orphan
#: row's "2 x lease TTL") is what lets it say so instead of precomputing some
#: assumed TTL into milliseconds.
DETECTION_LATENCY = {
    "relay_gap": ("absolute_ms", 3 * MINUTE_MS, 5 * MINUTE_MS, "absolute_ms"),
    "relay_delivery_stall": ("absolute_ms", 2 * MINUTE_MS, 5 * MINUTE_MS, "absolute_ms"),
    "ci_outcome_undrained": ("absolute_ms", 3 * MINUTE_MS, 5 * MINUTE_MS, "absolute_ms"),
    "consumer_backlog": ("absolute_ms", 5 * MINUTE_MS, 10 * MINUTE_MS, "absolute_ms"),
    "watcher_silence": ("scope_interval_multiple", 3, 10 * MINUTE_MS, "absolute_ms"),
    "watcher_error_streak": ("consecutive_count", 5, 10 * MINUTE_MS, "absolute_ms"),
    "watcher_scope_uncovered": ("absolute_ms", 0, 10 * MINUTE_MS, "absolute_ms"),
    "session_no_evidence": ("absolute_ms", 10 * MINUTE_MS, 15 * MINUTE_MS, "absolute_ms"),
    "observation_unavailable": ("absolute_ms", 5 * MINUTE_MS, 10 * MINUTE_MS, "absolute_ms"),
    # L = 2 x the lease's own TTL, so both sides of this row are multiples and
    # the DDL's T + P <= L CHECK deliberately does not reach it; the
    # policy_budget_violation pass asserts the inequality per subject instead.
    "lease_orphan": ("lease_ttl_multiple", 1, 2, "lease_ttl_multiple"),
}

#: Section 4. ``None`` is not "unset": it is how "never a gap" is expressed, so
#: that the relay-gap detector has no special case for the human stage.
GATE_STAGE_TOLERANCE = {
    ("worker_escalation", "received"): 3 * MINUTE_MS,
    ("worker_escalation", "presented"): None,
    ("worker_escalation", "answered"): 2 * MINUTE_MS,
    ("merge_approval", "received"): 3 * MINUTE_MS,
    ("merge_approval", "presented"): None,
    ("merge_approval", "answered"): 2 * MINUTE_MS,
}

#: Section 6.1: ball_holder is a function of (gate_type, stage) and is who a
#: relay_gap incident names; standing_owner is a function of gate_type alone.
#: worker_escalation stands with the Secretary (D-0016, the single human
#: window); merge_approval stands with the human, whose decision it is.
GATE_STAGE_OWNER = {
    ("worker_escalation", "received"): ("secretary", "secretary"),
    ("worker_escalation", "presented"): ("human", "secretary"),
    ("worker_escalation", "answered"): ("secretary", "secretary"),
    ("merge_approval", "received"): ("secretary", "human"),
    ("merge_approval", "presented"): ("human", "human"),
    ("merge_approval", "answered"): ("secretary", "human"),
}


@pytest.fixture
def cp(tmp_path: Path):
    connection = create_production_control_plane(tmp_path / "production.sqlite3", now_ms=T0)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def revision_id(cp) -> int:
    rows = cp.execute("SELECT revision_id FROM policy_revision").fetchall()
    assert len(rows) == 1, "the initial seed is exactly one revision"
    return rows[0]["revision_id"]


# --------------------------------------------------------------------------
# the revision itself
# --------------------------------------------------------------------------


def test_the_seed_is_one_revision_carrying_the_decisions_that_set_it(cp):
    rows = cp.execute("SELECT * FROM policy_revision").fetchall()
    assert len(rows) == 1
    row = rows[0]
    # D-0031 decided the budgets and the period, D-0032 the ownership; the
    # column exists so a report can say which decision it was judged under.
    assert "D-0031" in row["decided_by"]
    assert "D-0032" in row["decided_by"]
    assert row["note"].strip() != ""


def test_the_first_revision_is_effective_from_zero_rather_than_from_a_wall_clock(cp):
    # A migration that read a clock would produce a different effective_at_ms
    # on every database it was applied to, and the checksum discipline would
    # then be pinning bytes whose EFFECT still differed. Zero is also the
    # honest value: a detector binding "the revision effective at :now_ms"
    # finds this one for every :now_ms there is.
    assert cp.execute("SELECT effective_at_ms FROM policy_revision").fetchone()[0] == 0


def test_every_seeded_row_hangs_off_that_one_revision(cp, revision_id):
    for table in (
        "policy_detection_latency",
        "policy_gate_stage_tolerance",
        "policy_gate_stage_owner",
    ):
        others = cp.execute(
            f"SELECT COUNT(*) FROM {table} WHERE revision_id <> ?", (revision_id,)
        ).fetchone()[0]
        assert others == 0, table


# --------------------------------------------------------------------------
# section 3.2 -- the classes
# --------------------------------------------------------------------------


def test_exactly_the_classes_of_section_3_2_are_seeded(cp):
    seeded = {row["incident_class"] for row in cp.execute("SELECT incident_class FROM policy_detection_latency")}
    # Both directions: a missing class is a condition nothing ages, and an
    # extra one is a tolerance no document decided.
    assert seeded == set(DETECTION_LATENCY)


@pytest.mark.parametrize("incident_class", sorted(DETECTION_LATENCY))
def test_each_detection_latency_row_matches_the_document(cp, incident_class):
    expected_kind, expected_t, expected_l, expected_budget_kind = DETECTION_LATENCY[incident_class]
    row = cp.execute(
        "SELECT * FROM policy_detection_latency WHERE incident_class = ?", (incident_class,)
    ).fetchone()
    assert row is not None
    assert row["threshold_kind"] == expected_kind
    assert row["threshold_value"] == expected_t
    assert row["budget_ms"] == expected_l
    assert row["budget_kind"] == expected_budget_kind


def test_every_class_is_evaluated_on_the_base_reconcile_period(cp):
    # Section 3.3 permits a class with a large L - T to run on a multiple of P,
    # and none is moved here: a coarser period is a cost optimisation, there is
    # no measured pass cost yet to justify one, and choosing one anyway would be
    # deciding policy inside a migration.
    periods = {
        row["incident_class"]: row["reconcile_period_ms"]
        for row in cp.execute("SELECT incident_class, reconcile_period_ms FROM policy_detection_latency")
    }
    assert set(periods.values()) == {RECONCILE_PERIOD_MS}


# --------------------------------------------------------------------------
# section 3.3 -- P is a consequence of the budgets, not a choice
# --------------------------------------------------------------------------


def test_the_absolute_classes_satisfy_t_plus_p_le_l(cp):
    # The derivation rule of section 3.1, checked against the seeded rows
    # rather than against the DDL's CHECK -- the CHECK proves no row can be
    # inserted that violates it, this proves the rows that WERE inserted are
    # the ones the document derived.
    for row in cp.execute("SELECT * FROM policy_detection_latency"):
        if row["threshold_kind"] != "absolute_ms" or row["budget_kind"] != "absolute_ms":
            continue
        assert row["threshold_value"] + row["reconcile_period_ms"] <= row["budget_ms"], row["incident_class"]


def test_the_reconcile_period_is_the_largest_the_tightest_budget_admits(cp):
    # P = min(L - T) over the absolute classes. If a future revision tightened
    # a budget without moving P, this fails -- which is the point: section 3.3
    # calls the period a consequence of the budgets rather than a choice, and a
    # consequence that no longer follows is a broken derivation.
    slack = [
        row["budget_ms"] - row["threshold_value"]
        for row in cp.execute("SELECT * FROM policy_detection_latency")
        if row["threshold_kind"] == "absolute_ms" and row["budget_kind"] == "absolute_ms"
    ]
    assert min(slack) == RECONCILE_PERIOD_MS


def test_the_binding_classes_are_the_ones_the_document_names(cp):
    # Section 3.3's L - T table: relay_gap and ci_outcome_undrained sit at
    # 2 min and are what P was derived FROM; they are the constraint, not a
    # near miss that a later edit may quietly widen away from.
    slack = {
        row["incident_class"]: row["budget_ms"] - row["threshold_value"]
        for row in cp.execute("SELECT * FROM policy_detection_latency")
        if row["threshold_kind"] == "absolute_ms" and row["budget_kind"] == "absolute_ms"
    }
    assert slack["relay_gap"] == 2 * MINUTE_MS
    assert slack["ci_outcome_undrained"] == 2 * MINUTE_MS
    assert slack["relay_delivery_stall"] == 3 * MINUTE_MS
    assert slack["watcher_scope_uncovered"] == 10 * MINUTE_MS
    binding = {"relay_gap", "ci_outcome_undrained"}
    for incident_class, value in slack.items():
        if incident_class not in binding:
            assert value >= 3 * MINUTE_MS, incident_class


def test_the_relative_classes_are_left_relative(cp):
    # Precomputing a relative threshold into milliseconds bakes one scope's
    # interval, or one lease's TTL, into a row every other subject also reads.
    kinds = {
        row["incident_class"]: (row["threshold_kind"], row["budget_kind"])
        for row in cp.execute("SELECT * FROM policy_detection_latency")
    }
    assert kinds["watcher_silence"][0] == "scope_interval_multiple"
    assert kinds["watcher_error_streak"][0] == "consecutive_count"
    assert kinds["lease_orphan"] == ("lease_ttl_multiple", "lease_ttl_multiple")
    # And exactly one class has a relative BUDGET, which is why budget_kind
    # defaults to 'absolute_ms': every other row reads as it did before the
    # column existed.
    relative_budgets = [name for name, (_, budget) in kinds.items() if budget != "absolute_ms"]
    assert relative_budgets == ["lease_orphan"]


def test_the_watcher_silence_budget_bounds_a_scopes_interval(cp):
    # Section 3.3 spells this consequence out: with L = 10 min, P = 120 s and
    # T = 3 x the scope's own interval, a scope registered slower than about
    # 160 s cannot be served inside its budget and is reported rather than
    # silently under-served. The arithmetic is asserted so the three numbers
    # cannot drift apart.
    row = cp.execute(
        "SELECT * FROM policy_detection_latency WHERE incident_class = 'watcher_silence'"
    ).fetchone()
    bound_ms = (row["budget_ms"] - row["reconcile_period_ms"]) / row["threshold_value"]
    assert 159_000 <= bound_ms <= 161_000


# --------------------------------------------------------------------------
# section 4 -- gate stage tolerances
# --------------------------------------------------------------------------


def test_exactly_the_stage_tolerances_of_section_4_are_seeded(cp):
    seeded = {
        (row["gate_type"], row["stage"]): row["tolerance_ms"]
        for row in cp.execute("SELECT * FROM policy_gate_stage_tolerance")
    }
    assert seeded == GATE_STAGE_TOLERANCE


def test_the_human_stage_is_untimed_because_a_slow_human_is_not_a_gap(cp):
    # NULL is the mechanism, not a gap in the seed: the detector joins this
    # table and the row simply does not match. Expressing it as a very large
    # tolerance instead would make "never" a number someone could shrink.
    for gate_type in ("worker_escalation", "merge_approval"):
        row = cp.execute(
            "SELECT tolerance_ms FROM policy_gate_stage_tolerance "
            "WHERE gate_type = ? AND stage = 'presented'",
            (gate_type,),
        ).fetchone()
        assert row is not None, f"{gate_type} must opt out by value, not by absence"
        assert row["tolerance_ms"] is None


def test_the_answered_leg_carries_the_tightest_tolerance_in_the_system(cp):
    # This is the leg v1 actually dropped, and work is blocked on it: the
    # answer is durable and the worker does not have it.
    values = [
        row["tolerance_ms"]
        for row in cp.execute("SELECT tolerance_ms FROM policy_gate_stage_tolerance")
        if row["tolerance_ms"] is not None
    ]
    answered = cp.execute(
        "SELECT tolerance_ms FROM policy_gate_stage_tolerance "
        "WHERE gate_type = 'worker_escalation' AND stage = 'answered'"
    ).fetchone()["tolerance_ms"]
    assert answered == min(values) == 2 * MINUTE_MS


def test_the_terminal_stage_has_no_tolerance_row(cp):
    # forwarded is terminal, the gate is closed, and there is nothing left to
    # be late for; a row here would age a gate that has already finished.
    assert (
        cp.execute(
            "SELECT COUNT(*) FROM policy_gate_stage_tolerance WHERE stage = 'forwarded'"
        ).fetchone()[0]
        == 0
    )


def test_undecided_gate_types_are_not_seeded(cp):
    # time-base-policy.md decides numbers for worker_escalation and
    # merge_approval only. Seeding a plan_approval tolerance would be a policy
    # decision taken in a migration file -- exactly what holding these values
    # as versioned data exists to prevent.
    for table in ("policy_gate_stage_tolerance", "policy_gate_stage_owner"):
        seeded = {row["gate_type"] for row in cp.execute(f"SELECT gate_type FROM {table}")}
        assert seeded == {"worker_escalation", "merge_approval"}


# --------------------------------------------------------------------------
# section 6.1 -- gate ownership, resolved
# --------------------------------------------------------------------------


def test_exactly_the_owners_of_section_6_1_are_seeded(cp):
    seeded = {
        (row["gate_type"], row["stage"]): (row["ball_holder"], row["standing_owner"])
        for row in cp.execute("SELECT * FROM policy_gate_stage_owner")
    }
    assert seeded == GATE_STAGE_OWNER


def test_the_ball_holder_moves_with_the_stage_and_the_standing_owner_does_not(cp):
    for gate_type, expected_standing in (("worker_escalation", "secretary"), ("merge_approval", "human")):
        rows = cp.execute(
            "SELECT * FROM policy_gate_stage_owner WHERE gate_type = ?", (gate_type,)
        ).fetchall()
        # standing_owner is a function of gate_type alone: one value across
        # every stage, or it is not standing.
        assert {row["standing_owner"] for row in rows} == {expected_standing}
        # ball_holder is a function of (gate_type, stage), and it must actually
        # vary -- if it did not, the distinction section 6.1 draws would be
        # decorative and a relay_gap incident could name the wrong role.
        assert len({row["ball_holder"] for row in rows}) > 1


def test_ownership_lives_only_in_policy_and_never_on_the_gate_row(cp):
    # Neither field is a column on gate, which is what makes drift between the
    # stage and its owner unrepresentable rather than merely unlikely.
    columns = {row[1] for row in cp.execute("PRAGMA table_info(gate)")}
    assert "ball_holder" not in columns
    assert "standing_owner" not in columns
    assert "owner" not in columns


def test_every_stage_that_can_hold_a_gate_has_a_ball_holder(cp):
    # Ownership covers exactly the stages at which a gate can still be waiting
    # on someone. forwarded is not one of them.
    staged = {
        row["stage"] for row in cp.execute("SELECT DISTINCT stage FROM policy_gate_stage_owner")
    }
    assert staged == {"received", "presented", "answered"}


def test_the_terminal_stage_has_no_ball_holder_row(cp):
    # time-base-policy.md section 4 gives the forwarded cell as "--" on every
    # column, ball holder included: the gate is closed and no one holds the
    # ball. Naming one to satisfy the NOT NULL column would be policy decided
    # in a migration file, and the invented value is the one a report would
    # cite.
    assert (
        cp.execute(
            "SELECT COUNT(*) FROM policy_gate_stage_owner WHERE stage = 'forwarded'"
        ).fetchone()[0]
        == 0
    )


def test_the_standing_owner_of_both_gate_types_survives_the_absent_forwarded_row(cp):
    # standing_owner is a function of gate_type alone, so dropping the terminal
    # stage costs no information: both types are still answerable from the
    # stages that remain.
    standing = {
        row["gate_type"]: row["standing_owner"]
        for row in cp.execute("SELECT DISTINCT gate_type, standing_owner FROM policy_gate_stage_owner")
    }
    assert standing == {"worker_escalation": "secretary", "merge_approval": "human"}


def test_no_stage_carries_a_tolerance_without_a_ball_holder_to_name(cp):
    # The relay-gap detector matches through the tolerance table and then names
    # the stage's ball holder, so a stage with a tolerance and no owner row
    # would raise an incident it cannot attribute. Holding the two tables to the
    # same stage set is what keeps the closed gate out of the detector entirely.
    timed = {
        (row["gate_type"], row["stage"])
        for row in cp.execute(
            "SELECT gate_type, stage FROM policy_gate_stage_tolerance WHERE tolerance_ms IS NOT NULL"
        )
    }
    owned = {
        (row["gate_type"], row["stage"])
        for row in cp.execute("SELECT gate_type, stage FROM policy_gate_stage_owner")
    }
    assert timed <= owned
    assert not any(stage == "forwarded" for _, stage in timed | owned)


# --------------------------------------------------------------------------
# the seed is data, and it is versioned data
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    ["policy_detection_latency", "policy_gate_stage_tolerance", "policy_gate_stage_owner"],
)
def test_a_second_revision_supersedes_by_insertion_rather_than_by_update(cp, revision_id, table):
    # Changing a tolerance is a new policy_revision and a fresh set of rows, so
    # a report written last month can still be recomputed under the tolerances
    # it was actually judged by. The revision is part of every primary key,
    # which is what makes the old rows survivable rather than merely
    # conventionally preserved.
    cp.execute(
        "INSERT INTO policy_revision (note, decided_by, effective_at_ms) VALUES (?, ?, ?)",
        ("a later revision", "D-9999", T0),
    )
    later = cp.execute("SELECT MAX(revision_id) FROM policy_revision").fetchone()[0]
    assert later != revision_id

    original = cp.execute(
        f"SELECT COUNT(*) FROM {table} WHERE revision_id = ?", (revision_id,)
    ).fetchone()[0]
    cp.execute(
        f"INSERT INTO {table} SELECT ? AS revision_id, "
        + ", ".join(
            column[1] for column in cp.execute(f"PRAGMA table_info({table})") if column[1] != "revision_id"
        )
        + f" FROM {table} WHERE revision_id = ?",
        (later, revision_id),
    )

    assert (
        cp.execute(f"SELECT COUNT(*) FROM {table} WHERE revision_id = ?", (revision_id,)).fetchone()[0]
        == original
    )
