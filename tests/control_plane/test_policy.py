"""What ``policy.py`` must keep true, with the second revision always on record.

``D-0031``'s corollary -- a ``policy_*`` join without a ``revision_id`` predicate
is a defect -- has a property that makes it dangerous to test carelessly: it is
*invisible* while only one revision exists. A suite that seeds one revision and
reads it back passes identically whether the module binds a revision or ignores
the column entirely. So the fixture here puts **two** revisions on record before
anything is asserted, and every reader test asserts it got exactly the one it
asked for. A regression that dropped the predicate would return two rows, or the
wrong row, in every one of them.

The rest of the file is the arithmetic of ``time-base-policy.md`` section 3
exercised against real subjects rather than restated:

* the section 3.3 worked bound (``L`` = 10 min, ``P`` = 120 s, ``T`` = 3 polls,
  so a scope may poll no slower than 160 s) driven as an actual
  ``watcher_scope`` on both sides of the boundary, since the boundary is where a
  ``<`` written as a ``<=`` hides;
* the half-open window rule (section 2, rule 4) at an exact boundary instant,
  asserting both halves of the claim -- the instant belongs to the later window,
  *and* it belongs to exactly one; and
* ``consecutive_count`` refused as a duration, because the coercion that a
  refusal prevents (5 failures read as 5 ms) is silent and produces a tolerance
  every subject crosses immediately.

Every timestamp is :data:`T0` plus arithmetic. No test reads a clock: the module
takes ``now_ms`` from its caller precisely so a tolerance boundary can be driven
to either side of itself, and a suite that used a wall clock could not do that.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import policy
from claude_org_runtime.control_plane.migrator import create_production_control_plane

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

#: The note of the revision ``0002_policy_seed.sql`` writes. Looked up by note
#: rather than assumed to be ``1`` so these tests survive a later seed step.
SEED_NOTE = (
    "initial time base: detection latency budgets, gate stage tolerances "
    "and gate stage owners as first decided"
)

#: ``L`` and ``P`` for ``watcher_silence`` as seeded (section 3.2 / 3.3), and the
#: scope poll interval the two of them bound: (600000 - 120000) / 3.
SILENCE_BUDGET_MS = 600_000
RECONCILE_PERIOD_MS = 120_000
SILENCE_MULTIPLE = 3
MAX_LEGAL_INTERVAL_MS = (SILENCE_BUDGET_MS - RECONCILE_PERIOD_MS) // SILENCE_MULTIPLE


@pytest.fixture
def cp(tmp_path: Path):
    connection = create_production_control_plane(tmp_path / "production.sqlite3", now_ms=T0)
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------
# helpers -- the smallest legal row of each kind
# --------------------------------------------------------------------------


def seed_revision_id(cp: sqlite3.Connection) -> int:
    row = cp.execute(
        "SELECT revision_id FROM policy_revision WHERE note = ?", (SEED_NOTE,)
    ).fetchone()
    assert row is not None, "0002_policy_seed.sql must have applied"
    return int(row[0])


def add_revision(cp: sqlite3.Connection, *, note: str, at: int,
                 decided_by: str = "D-test") -> int:
    cursor = cp.execute(
        "INSERT INTO policy_revision (note, decided_by, effective_at_ms) VALUES (?, ?, ?)",
        (note, decided_by, at),
    )
    return int(cursor.lastrowid)


def add_detection_latency(cp: sqlite3.Connection, revision_id: int, incident_class: str,
                          threshold_kind: str, threshold_value: int,
                          reconcile_period_ms: int = RECONCILE_PERIOD_MS,
                          budget_ms: int = SILENCE_BUDGET_MS,
                          budget_kind: str = "absolute_ms") -> None:
    cp.execute(
        """
        INSERT INTO policy_detection_latency (revision_id, incident_class, threshold_kind,
                                              threshold_value, reconcile_period_ms, budget_ms,
                                              budget_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (revision_id, incident_class, threshold_kind, threshold_value, reconcile_period_ms,
         budget_ms, budget_kind),
    )


def add_stage_tolerance(cp: sqlite3.Connection, revision_id: int, gate_type: str, stage: str,
                        tolerance_ms: int | None) -> None:
    cp.execute(
        "INSERT INTO policy_gate_stage_tolerance (revision_id, gate_type, stage, tolerance_ms)"
        " VALUES (?, ?, ?, ?)",
        (revision_id, gate_type, stage, tolerance_ms),
    )


def add_stage_owner(cp: sqlite3.Connection, revision_id: int, gate_type: str, stage: str,
                    ball_holder: str, standing_owner: str) -> None:
    cp.execute(
        """
        INSERT INTO policy_gate_stage_owner (revision_id, gate_type, stage, ball_holder,
                                             standing_owner)
        VALUES (?, ?, ?, ?, ?)
        """,
        (revision_id, gate_type, stage, ball_holder, standing_owner),
    )


def add_repository(cp: sqlite3.Connection, repo_id: str = "repo-1", at: int = T0) -> str:
    cp.execute(
        """
        INSERT INTO repository (repo_id, provider, provider_repo_id, owner, name,
                                created_at_ms, updated_at_ms)
        VALUES (?, 'github', NULL, 'acme', 'widget', ?, ?)
        """,
        (repo_id, at, at),
    )
    return repo_id


def add_watcher_scope(cp: sqlite3.Connection, scope_id: str, *, expected_interval_ms: int,
                      enabled: int = 1, retired_at_ms: int | None = None,
                      at: int = T0) -> str:
    cp.execute(
        """
        INSERT INTO watcher_scope (scope_id, scope_kind, repo_id, pr_id, expected_interval_ms,
                                   enabled, registered_at_ms, retired_at_ms)
        VALUES (?, 'ci_repository', 'repo-1', NULL, ?, ?, ?, ?)
        """,
        (scope_id, expected_interval_ms, enabled, at, retired_at_ms),
    )
    return scope_id


def add_lease(cp: sqlite3.Connection, resource: str, *, ttl_ms: int, at: int = T0,
              holder: str = "watcher-a", epoch: int = 1) -> str:
    cp.execute(
        "INSERT INTO lease (resource, holder, epoch, acquired_at_ms, expires_at_ms)"
        " VALUES (?, ?, ?, ?, ?)",
        (resource, holder, epoch, at, at + ttl_ms),
    )
    return resource


@pytest.fixture
def two_revisions(cp: sqlite3.Connection) -> tuple[int, int]:
    """The seeded revision, plus a later one that changes every value it can.

    The later revision is deliberately *different* in every column the readers
    return, so a reader that bound the wrong revision -- or none -- returns a
    value no assertion below accepts, rather than the same number by luck.
    """

    first = seed_revision_id(cp)
    second = add_revision(cp, note="tightened after the 2026-09 review", at=T0 + 10_000)
    add_detection_latency(cp, second, "watcher_silence", "scope_interval_multiple", 2,
                          budget_ms=SILENCE_BUDGET_MS)
    add_detection_latency(cp, second, "ci_outcome_undrained", "absolute_ms", 60_000,
                          budget_ms=300_000)
    add_detection_latency(cp, second, "watcher_error_streak", "consecutive_count", 9)
    add_stage_tolerance(cp, second, "worker_escalation", "received", 60_000)
    add_stage_tolerance(cp, second, "worker_escalation", "presented", None)
    add_stage_owner(cp, second, "worker_escalation", "received", "dispatcher_core", "human")
    return first, second


# --------------------------------------------------------------------------
# resolving a revision -- the predicate every other reader depends on
# --------------------------------------------------------------------------


def test_the_effective_revision_is_the_latest_one_at_or_before_now(cp, two_revisions):
    first, second = two_revisions

    assert policy.effective_revision_id(cp, now_ms=T0) == first
    assert policy.effective_revision_id(cp, now_ms=T0 + 9_999) == first
    assert policy.effective_revision_id(cp, now_ms=T0 + 10_000) == second
    assert policy.effective_revision_id(cp, now_ms=T0 + 10_001) == second


def test_two_revisions_sharing_an_instant_resolve_to_the_higher_revision_id(cp):
    first = seed_revision_id(cp)
    corrected = add_revision(cp, note="a correction filed in the same pass", at=T0 + 5_000)
    superseding = add_revision(cp, note="and the row that corrects it", at=T0 + 5_000)

    assert policy.effective_revision_id(cp, now_ms=T0 + 5_000) == superseding
    assert superseding > corrected > first


def test_an_instant_before_every_revision_is_refused_rather_than_answered(cp):
    cp.execute("DELETE FROM policy_gate_stage_owner")
    cp.execute("DELETE FROM policy_gate_stage_tolerance")
    cp.execute("DELETE FROM policy_detection_latency")
    cp.execute("DELETE FROM policy_revision")

    with pytest.raises(policy.NoEffectiveRevision):
        policy.effective_revision_id(cp, now_ms=T0)


def test_a_period_within_one_revision_is_homogeneous(cp, two_revisions):
    first, _second = two_revisions

    assert policy.revision_over_period(
        cp, period_start_ms=T0, period_end_ms=T0 + 5_000
    ) == (first,)


def test_a_period_spanning_a_change_reports_both_revisions_oldest_first(cp, two_revisions):
    first, second = two_revisions

    assert policy.revision_over_period(
        cp, period_start_ms=T0, period_end_ms=T0 + 20_000
    ) == (first, second)


def test_a_revision_effective_exactly_at_a_boundary_belongs_to_the_later_window_only(
    cp, two_revisions
):
    """Half-open ``[start, end)``: the boundary instant is in one window, the later one."""

    first, second = two_revisions
    boundary = T0 + 10_000

    earlier = policy.revision_over_period(cp, period_start_ms=T0, period_end_ms=boundary)
    later = policy.revision_over_period(
        cp, period_start_ms=boundary, period_end_ms=boundary + 10_000
    )

    assert earlier == (first,)
    assert later == (second,)
    assert set(earlier) & set(later) == set()


def test_a_period_is_refused_when_its_end_precedes_its_start(cp, two_revisions):
    with pytest.raises(policy.PolicyUsageError):
        policy.revision_over_period(cp, period_start_ms=T0 + 1, period_end_ms=T0)


# --------------------------------------------------------------------------
# every reader returns exactly the bound revision's row
# --------------------------------------------------------------------------


def test_detection_latency_returns_one_row_per_bound_revision(cp, two_revisions):
    first, second = two_revisions

    under_first = policy.detection_latency(
        cp, revision_id=first, incident_class="ci_outcome_undrained"
    )
    under_second = policy.detection_latency(
        cp, revision_id=second, incident_class="ci_outcome_undrained"
    )

    assert under_first["threshold_value"] == 180_000
    assert under_first["budget_ms"] == 300_000
    assert under_first["threshold_kind"] == "absolute_ms"
    assert under_first["budget_kind"] == "absolute_ms"
    assert under_second["threshold_value"] == 60_000


def test_a_class_the_bound_revision_never_decided_is_missing_not_empty(cp, two_revisions):
    _first, second = two_revisions

    with pytest.raises(policy.PolicyRowMissing):
        policy.detection_latency(cp, revision_id=second, incident_class="lease_orphan")


def test_gate_stage_tolerance_returns_the_bound_revisions_value(cp, two_revisions):
    first, second = two_revisions

    assert policy.gate_stage_tolerance(
        cp, revision_id=first, gate_type="worker_escalation", stage="received"
    ) == 180_000
    assert policy.gate_stage_tolerance(
        cp, revision_id=second, gate_type="worker_escalation", stage="received"
    ) == 60_000


def test_the_human_stage_is_never_a_gap_and_says_so_as_a_null_tolerance(cp):
    first = seed_revision_id(cp)

    assert policy.gate_stage_tolerance(
        cp, revision_id=first, gate_type="worker_escalation", stage="presented"
    ) is None


def test_a_stage_the_revision_never_decided_is_not_the_same_fact_as_never_a_gap(cp):
    first = seed_revision_id(cp)

    with pytest.raises(policy.PolicyRowMissing):
        policy.gate_stage_tolerance(
            cp, revision_id=first, gate_type="worker_escalation", stage="forwarded"
        )
    with pytest.raises(policy.PolicyRowMissing):
        policy.gate_stage_tolerance(
            cp, revision_id=first, gate_type="plan_approval", stage="received"
        )


def test_gate_stage_owner_returns_the_bound_revisions_ball_holder_and_standing_owner(
    cp, two_revisions
):
    first, second = two_revisions

    under_first = policy.gate_stage_owner(
        cp, revision_id=first, gate_type="worker_escalation", stage="received"
    )
    under_second = policy.gate_stage_owner(
        cp, revision_id=second, gate_type="worker_escalation", stage="received"
    )

    assert under_first["ball_holder"] == "secretary"
    assert under_first["standing_owner"] == "secretary"
    assert under_second["ball_holder"] == "dispatcher_core"
    assert under_second["standing_owner"] == "human"


def test_the_standing_owner_differs_from_the_ball_holder_where_the_gate_type_says_so(cp):
    first = seed_revision_id(cp)

    received = policy.gate_stage_owner(
        cp, revision_id=first, gate_type="merge_approval", stage="received"
    )

    assert received["ball_holder"] == "secretary"
    assert received["standing_owner"] == "human"


# --------------------------------------------------------------------------
# a relative threshold meets its subject
# --------------------------------------------------------------------------


def test_an_absolute_tolerance_is_the_row_itself(cp):
    first = seed_revision_id(cp)

    assert policy.resolve_tolerance_ms(
        cp, revision_id=first, incident_class="ci_outcome_undrained", subject=None
    ) == 180_000


def test_a_scope_multiple_is_scaled_by_that_scopes_own_poll_interval(cp):
    first = seed_revision_id(cp)
    add_repository(cp)
    add_watcher_scope(cp, "scope-fast", expected_interval_ms=30_000)
    add_watcher_scope(cp, "scope-slow", expected_interval_ms=120_000)

    fast = policy.resolve_tolerance_ms(
        cp, revision_id=first, incident_class="watcher_silence", subject="scope-fast"
    )
    slow = policy.resolve_tolerance_ms(
        cp, revision_id=first, incident_class="watcher_silence", subject="scope-slow"
    )

    assert fast == SILENCE_MULTIPLE * 30_000
    assert slow == SILENCE_MULTIPLE * 120_000


def test_a_lease_ttl_multiple_is_scaled_by_that_leases_own_ttl(cp):
    first = seed_revision_id(cp)
    add_lease(cp, "watcher_scope:scope-1", ttl_ms=300_000)

    assert policy.resolve_tolerance_ms(
        cp, revision_id=first, incident_class="lease_orphan",
        subject="watcher_scope:scope-1",
    ) == 300_000


def test_a_consecutive_count_is_refused_as_a_duration(cp):
    """5 consecutive failures is not 5 milliseconds, and no subject makes it one."""

    first = seed_revision_id(cp)
    add_repository(cp)
    add_watcher_scope(cp, "scope-1", expected_interval_ms=60_000)

    with pytest.raises(policy.NotADuration):
        policy.resolve_tolerance_ms(
            cp, revision_id=first, incident_class="watcher_error_streak", subject="scope-1"
        )
    with pytest.raises(policy.NotADuration):
        policy.resolve_tolerance_ms(
            cp, revision_id=first, incident_class="watcher_error_streak", subject=None
        )


def test_a_relative_threshold_without_a_subject_is_refused_not_read_as_the_bare_multiple(cp):
    first = seed_revision_id(cp)

    with pytest.raises(policy.PolicyUsageError):
        policy.resolve_tolerance_ms(
            cp, revision_id=first, incident_class="watcher_silence", subject=None
        )


def test_a_subject_that_does_not_exist_is_refused(cp):
    first = seed_revision_id(cp)

    with pytest.raises(policy.PolicyUsageError):
        policy.resolve_tolerance_ms(
            cp, revision_id=first, incident_class="watcher_silence", subject="scope-absent"
        )


# --------------------------------------------------------------------------
# section 10's per-subject T + P <= L pass
# --------------------------------------------------------------------------


def test_a_scope_polling_within_the_bound_raises_no_violation(cp):
    first = seed_revision_id(cp)
    add_repository(cp)
    add_watcher_scope(cp, "scope-ok", expected_interval_ms=MAX_LEGAL_INTERVAL_MS)

    assert policy.budget_violations(cp, revision_id=first, now_ms=T0) == ()


def test_a_scope_polling_one_millisecond_slower_than_the_bound_is_reported(cp):
    """The section 3.3 arithmetic: (600000 - 120000) / 3 = 160000 ms, exercised at the edge."""

    first = seed_revision_id(cp)
    add_repository(cp)
    add_watcher_scope(cp, "scope-slow", expected_interval_ms=MAX_LEGAL_INTERVAL_MS + 1)

    (violation,) = policy.budget_violations(cp, revision_id=first, now_ms=T0)

    assert violation["incident_class"] == "watcher_silence"
    assert violation["subject_kind"] == "watcher_scope"
    assert violation["subject_id"] == "scope-slow"
    assert violation["tolerance_ms"] == SILENCE_MULTIPLE * (MAX_LEGAL_INTERVAL_MS + 1)
    assert violation["budget_ms"] == SILENCE_BUDGET_MS
    assert violation["excess_ms"] == SILENCE_MULTIPLE


def test_only_the_misconfigured_scope_is_named_when_others_are_fine(cp):
    first = seed_revision_id(cp)
    add_repository(cp)
    add_watcher_scope(cp, "scope-a", expected_interval_ms=60_000)
    add_watcher_scope(cp, "scope-b", expected_interval_ms=600_000)
    add_watcher_scope(cp, "scope-c", expected_interval_ms=120_000)

    violations = policy.budget_violations(cp, revision_id=first, now_ms=T0)

    assert tuple(v["subject_id"] for v in violations) == ("scope-b",)


def test_a_retired_or_disabled_scope_has_no_watcher_to_be_late_and_is_not_reported(cp):
    first = seed_revision_id(cp)
    add_repository(cp)
    add_watcher_scope(cp, "scope-retired", expected_interval_ms=600_000,
                      retired_at_ms=T0 + 1_000)
    add_watcher_scope(cp, "scope-disabled", expected_interval_ms=600_000, enabled=0)

    assert policy.budget_violations(cp, revision_id=first, now_ms=T0) == ()


def test_a_lease_whose_orphan_window_fits_its_own_ttl_raises_no_violation(cp):
    """``lease_orphan`` is relative on BOTH sides: T = 1 x TTL, L = 2 x TTL."""

    first = seed_revision_id(cp)
    add_lease(cp, "watcher_scope:scope-1", ttl_ms=RECONCILE_PERIOD_MS)

    assert policy.budget_violations(cp, revision_id=first, now_ms=T0) == ()


def test_a_lease_ttl_shorter_than_the_reconcile_period_breaks_its_own_budget(cp):
    """T + P <= L with T = TTL and L = 2 x TTL reduces to P <= TTL."""

    first = seed_revision_id(cp)
    add_lease(cp, "watcher_scope:scope-1", ttl_ms=RECONCILE_PERIOD_MS - 1)

    (violation,) = policy.budget_violations(cp, revision_id=first, now_ms=T0)

    assert violation["incident_class"] == "lease_orphan"
    assert violation["subject_kind"] == "lease"
    assert violation["subject_id"] == "watcher_scope:scope-1"
    assert violation["tolerance_ms"] == RECONCILE_PERIOD_MS - 1
    assert violation["budget_ms"] == 2 * (RECONCILE_PERIOD_MS - 1)
    assert violation["excess_ms"] == 1


def test_an_expired_lease_has_no_orphan_window_left_to_size(cp):
    first = seed_revision_id(cp)
    add_lease(cp, "watcher_scope:scope-1", ttl_ms=RECONCILE_PERIOD_MS - 1)

    assert policy.budget_violations(
        cp, revision_id=first, now_ms=T0 + RECONCILE_PERIOD_MS
    ) == ()


def test_the_pass_reads_only_the_bound_revision(cp, two_revisions):
    """The whole point of D-0031's corollary, as one assertion.

    The second revision halves ``watcher_silence``'s multiple, so the same scope
    is a violation under one revision and not under the other. A pass that
    omitted the ``revision_id`` predicate would report it twice.
    """

    first, second = two_revisions
    add_repository(cp)
    add_watcher_scope(cp, "scope-1", expected_interval_ms=200_000)

    under_first = policy.budget_violations(cp, revision_id=first, now_ms=T0)
    under_second = policy.budget_violations(cp, revision_id=second, now_ms=T0)

    assert tuple(v["subject_id"] for v in under_first) == ("scope-1",)
    assert under_second == ()


def test_a_consecutive_count_class_is_skipped_rather_than_stopping_the_pass(cp):
    """It has no duration to compare, and it is not the misconfigured thing."""

    first = seed_revision_id(cp)
    add_repository(cp)
    add_watcher_scope(cp, "scope-slow", expected_interval_ms=600_000)

    violations = policy.budget_violations(cp, revision_id=first, now_ms=T0)

    assert tuple(v["incident_class"] for v in violations) == ("watcher_silence",)


def test_an_absolute_threshold_under_a_relative_budget_scales_only_the_budget(cp):
    """The other asymmetry: T absolute, L a multiple of the subject's own TTL.

    Every case above has the *relative* side on ``T`` (``watcher_silence``) or on
    both (``lease_orphan``), so the branch that leaves ``T`` alone while scaling
    ``L`` has never been driven. It is the branch most easily written backwards,
    because "the relative kind" appears on both sides of the pass and scaling the
    wrong one still produces a plausible number -- here it would produce
    ``480001 * 300000``, which no reader would recognise as a millisecond
    tolerance but which no assertion would have caught either.

    The arithmetic: ``L = 2 x 300000 = 600000``, ``P = 120000``, so ``T`` may be
    ``480000`` and no more.
    """

    revision = add_revision(cp, note="an absolute T under a relative L", at=T0 + 1_000)
    add_detection_latency(cp, revision, "forward_stall", "absolute_ms", 480_000,
                          budget_ms=2, budget_kind="lease_ttl_multiple")
    add_lease(cp, "watcher_scope:scope-1", ttl_ms=300_000)

    assert policy.budget_violations(cp, revision_id=revision, now_ms=T0) == ()

    cp.execute(
        "UPDATE policy_detection_latency SET threshold_value = 480001"
        " WHERE revision_id = ? AND incident_class = 'forward_stall'",
        (revision,),
    )
    (violation,) = policy.budget_violations(cp, revision_id=revision, now_ms=T0)

    assert violation["subject_kind"] == "lease"
    assert violation["subject_id"] == "watcher_scope:scope-1"
    # T is untouched by the lease's TTL; only L is scaled by it.
    assert violation["tolerance_ms"] == 480_001
    assert violation["budget_ms"] == 600_000
    assert violation["excess_ms"] == 1


def test_a_row_whose_two_relative_sides_name_different_subjects_is_refused(cp):
    """T scaled by a scope's interval and L by some lease's TTL ties nothing to anything."""

    revision = add_revision(cp, note="a defective row", at=T0 + 1_000)
    add_detection_latency(cp, revision, "incoherent", "scope_interval_multiple", 3,
                          budget_ms=2, budget_kind="lease_ttl_multiple")

    with pytest.raises(policy.PolicyRefusal):
        policy.budget_violations(cp, revision_id=revision, now_ms=T0 + 1_000)


def test_a_fully_absolute_row_is_left_to_the_ddl_check(cp):
    """Nothing here re-evaluates what the CHECK already refused at insert time."""

    first = seed_revision_id(cp)
    add_repository(cp)
    add_watcher_scope(cp, "scope-ok", expected_interval_ms=60_000)
    add_lease(cp, "watcher_scope:scope-ok", ttl_ms=300_000)

    violations = policy.budget_violations(cp, revision_id=first, now_ms=T0)
    absolute_classes = {
        row[0]
        for row in cp.execute(
            "SELECT incident_class FROM policy_detection_latency"
            " WHERE revision_id = ? AND threshold_kind = 'absolute_ms'"
            "   AND budget_kind = 'absolute_ms'",
            (first,),
        )
    }

    assert absolute_classes
    assert {v["incident_class"] for v in violations} & absolute_classes == set()
