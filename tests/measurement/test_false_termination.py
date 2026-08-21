"""The numerator is the applied effect, the preference order is proved by disagreement.

Every test here is built around a shape that a plausible wrong implementation
gets right on friendly data:

* **A recommendation is not a termination.** The suite contains a declined
  recommendation labelled ``not_stuck`` -- the exact row a harness counting
  recommendations would charge to us as a false termination. It must appear in
  ``recommended_terminate`` and in ``declined_refused``, and in neither the
  numerator nor the denominator. Nothing about that is visible in a fixture
  where every recommendation was applied.
* **The preference order only exists when the sources disagree.** So the order
  is proved by constructing a case where the fixture label says ``stuck`` and
  the subject's own subsequent evidence says ``not_stuck``, and asserting the
  label wins -- and, separately, by asserting the winning source is
  ``GROUND_TRUTH_PREFERENCE[0]`` rather than the literal string, so the test
  binds to the module's declared order rather than restating it.
* **Undetermined is reachable.** An applied termination with no ground truth at
  all lands in its own bucket, moves neither rate bound on its own, and opens the
  gap between them. A harness that defaulted the unsettled case either way would
  pass a suite in which every case is settled.
* **The kind literal is load-bearing.** ``action.kind`` is unconstrained in the
  DDL, so a second applied action of a different kind is inserted and must not
  be counted.

Fixtures are written through an ordinary connection; every read under test goes
through the harness's read-only handle.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.measurement.false_termination import (
    GROUND_TRUTH_PREFERENCE,
    QUERY_DEFINITIONS,
    SOURCE_FIXTURE_LABEL,
    SOURCE_HUMAN_ADJUDICATION,
    SOURCE_NONE,
    SOURCE_SUBSEQUENT_EVIDENCE,
    STATUS_APPLIED,
    STATUS_PENDING,
    STATUS_REFUSED,
    TERMINATE_SESSION_KIND,
    VERDICT_NOT_STUCK,
    VERDICT_STUCK,
    VERDICT_UNDETERMINED,
    FalseTerminationRefusal,
    UnknownGroundTruthVerdict,
    adjudicate,
    measure_false_termination,
    read_terminate_actions,
    render_false_termination_report,
    subsequent_activity_verdicts,
)
from claude_org_runtime.measurement.reader import open_for_measurement

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
MINUTE_MS = 60_000
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS
NOW_MS = T0 + DAY_MS + MINUTE_MS

#: The event types this suite declares productive. Declared per report, never
#: defaulted: see ``subsequent_activity_verdicts``.
PRODUCTIVE = ("session_activity",)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    return path


def writable(path: Path) -> sqlite3.Connection:
    """An ordinary writable handle -- deliberately not the harness's."""

    return sqlite3.connect(path, isolation_level=None)


def make_subject(path: Path, *, run_id: str, session_id: str) -> None:
    connection = writable(path)
    try:
        connection.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) "
            "VALUES (?, 'running', ?, ?)",
            (run_id, T0, T0),
        )
        connection.execute(
            """
            INSERT INTO session (session_id, run_id, provider, binding_phase,
                                 observation, provider_state, bound_at_ms)
            VALUES (?, ?, 'test', 'identity_confirmed', 'observed', 'running', ?)
            """,
            (session_id, run_id, T0),
        )
    finally:
        connection.close()


def make_incident(path: Path, *, incident_id: str, run_id: str, session_id: str) -> None:
    connection = writable(path)
    try:
        connection.execute(
            """
            INSERT INTO incident (incident_id, run_id, session_id, fact_state,
                                  detector_version, dedup_key, created_at_ms,
                                  updated_at_ms)
            VALUES (?, ?, ?, 'NO_ACTIVITY_EVIDENCE', 'test-1', ?, ?, ?)
            """,
            (incident_id, run_id, session_id, f"dedup/{incident_id}", T0, T0),
        )
    finally:
        connection.close()


def make_action(
    path: Path,
    *,
    action_id: str,
    status: str,
    created_at_ms: int,
    applied_at_ms: int | None = None,
    run_id: str | None = None,
    incident_id: str | None = None,
    kind: str = TERMINATE_SESSION_KIND,
) -> None:
    connection = writable(path)
    try:
        connection.execute(
            """
            INSERT INTO action (action_id, run_id, incident_id, kind,
                                idempotency_key, exactly_once_mechanism, status,
                                refusal_reason, created_at_ms, applied_at_ms)
            VALUES (?, ?, ?, ?, ?, 'human_gate', ?, ?, ?, ?)
            """,
            (
                action_id,
                run_id,
                incident_id,
                kind,
                f"key/{action_id}",
                status,
                "declined at the human gate" if status == STATUS_REFUSED else None,
                created_at_ms,
                applied_at_ms,
            ),
        )
    finally:
        connection.close()


def append_activity(
    path: Path,
    *,
    event_id: str,
    subject_kind: str,
    subject_id: str,
    event_type: str,
    ingested_at_ms: int,
) -> None:
    connection = writable(path)
    try:
        connection.execute(
            """
            INSERT INTO event (event_id, event_type, subject_kind, subject_id,
                               producer, dedup_key, occurred_at_ms, ingested_at_ms)
            VALUES (?, ?, ?, ?, 'test', ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                subject_kind,
                subject_id,
                f"dedup/{event_id}",
                ingested_at_ms,
                ingested_at_ms,
            ),
        )
    finally:
        connection.close()


def measure(
    path: Path,
    *,
    fixture_labels=None,
    subsequent_evidence=None,
    human_adjudications=None,
    period_start_ms: int = PERIOD_START,
    period_end_ms: int = PERIOD_END,
):
    connection = open_for_measurement(path)
    try:
        return measure_false_termination(
            connection,
            period_start_ms=period_start_ms,
            period_end_ms=period_end_ms,
            now_ms=NOW_MS,
            fixture_labels=fixture_labels or {},
            subsequent_evidence=subsequent_evidence or {},
            human_adjudications=human_adjudications or {},
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the numerator: applied effects only
# --------------------------------------------------------------------------


def test_a_recommendation_that_was_not_applied_is_not_a_false_termination(
    db: Path,
) -> None:
    """The declined recommendation is in its own series and in neither rate term.

    This is section 3.4's first error direction made concrete: the declined row
    is labelled ``not_stuck``, so a harness counting recommendations would call
    it a false termination -- charging us for a termination the human gate
    prevented, which is the gate's value showing up as a defect.
    """

    make_subject(db, run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a-declined",
        status=STATUS_REFUSED,
        created_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
    )
    make_action(
        db,
        action_id="a-applied",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START + 2 * MINUTE_MS,
        applied_at_ms=PERIOD_START + 3 * MINUTE_MS,
        run_id="r1",
    )

    report = measure(
        db,
        fixture_labels={
            "a-declined": VERDICT_NOT_STUCK,
            "a-applied": VERDICT_STUCK,
        },
    )

    assert report.recommended_terminate == ("a-applied", "a-declined")
    assert report.declined_refused == ("a-declined",)
    assert report.recommended_but_not_applied == ("a-declined",)
    assert report.applied_terminate == ("a-applied",)
    assert report.false_termination_ids == ()
    assert report.justified_ids == ("a-applied",)
    assert "a-declined" not in report.adjudications, (
        "a row outside the denominator is not adjudicated at all"
    )
    assert report.rate_lower == 0.0

    rendered = render_false_termination_report(report)
    assert "INFORMATIVE, NOT alarming" in rendered
    assert rendered.isascii()


def test_a_pending_recommendation_is_reported_separately_from_a_declined_one(
    db: Path,
) -> None:
    """"A human said no" and "nobody has looked yet" are different facts."""

    make_subject(db, run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a-pending",
        status=STATUS_PENDING,
        created_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
    )
    make_action(
        db,
        action_id="a-refused",
        status=STATUS_REFUSED,
        created_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
    )

    report = measure(db)

    assert report.still_pending == ("a-pending",)
    assert report.declined_refused == ("a-refused",)
    assert set(report.recommended_but_not_applied) == {"a-pending", "a-refused"}
    assert report.applied_terminate == ()
    assert report.rate_lower is None, (
        "a rate over an empty denominator is not zero; printing zero would "
        "report 'we terminated nothing' as 'we never terminated wrongly'"
    )


def test_only_the_declared_kind_is_counted(db: Path) -> None:
    """``action.kind`` is unconstrained in the DDL, so the literal is a declaration.

    An applied ``restart_session`` is an applied effect on the same table with
    the same shape; counting it would put a remedy nobody called a termination
    into the termination rate.
    """

    make_subject(db, run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a-terminate",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START + MINUTE_MS,
        applied_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
    )
    make_action(
        db,
        action_id="a-restart",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START + MINUTE_MS,
        applied_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
        kind="restart_session",
    )

    report = measure(db, fixture_labels={"a-terminate": VERDICT_STUCK})

    assert report.applied_terminate == ("a-terminate",)
    assert QUERY_DEFINITIONS["terminate_session_kind"] == TERMINATE_SESSION_KIND
    assert TERMINATE_SESSION_KIND in render_false_termination_report(report), (
        "the report says which literal it counted, because the schema does not"
    )


def test_the_two_cohorts_are_counted_on_their_own_instants(db: Path) -> None:
    """Recommended-in-period and applied-in-period are different sets, both reported.

    A recommendation applied one millisecond after the period ends is in this
    report's recommendation series and in the *next* report's denominator; one
    carried over from the previous period is the mirror. Both boundaries are
    driven to the instant, because a ``<=`` at either end moves exactly one
    effect per report between a rate and a bucket.
    """

    make_subject(db, run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a-later",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_END - MINUTE_MS,
        applied_at_ms=PERIOD_END,
        run_id="r1",
    )
    make_action(
        db,
        action_id="a-earlier",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START - MINUTE_MS,
        applied_at_ms=PERIOD_START,
        run_id="r1",
    )

    report = measure(db, fixture_labels={"a-earlier": VERDICT_STUCK})

    assert report.recommended_terminate == ("a-later",)
    assert report.applied_after_period_end == ("a-later",)
    assert report.applied_terminate == ("a-earlier",)
    assert report.applied_from_earlier_recommendation == ("a-earlier",)


def test_an_action_naming_no_incident_still_counts(db: Path) -> None:
    """The join onto ``incident`` is LEFT because ``incident_id`` is nullable.

    An inner join would drop the row, shrinking the denominator and *raising* the
    rate for a reason that has nothing to do with terminations being wrong.
    """

    make_action(
        db,
        action_id="a-orphan",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START,
        applied_at_ms=PERIOD_START,
    )

    report = measure(db)
    assert report.applied_terminate == ("a-orphan",)

    connection = open_for_measurement(db)
    try:
        (action,) = read_terminate_actions(
            connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_END
        )
    finally:
        connection.close()
    assert action.subject_kind is None and action.subject_id is None


# --------------------------------------------------------------------------
# the preference order
# --------------------------------------------------------------------------


def test_the_fixture_label_wins_when_it_disagrees_with_subsequent_evidence(
    db: Path,
) -> None:
    """Constructed disagreement: label says stuck, the subject's behaviour says not.

    Only a disagreement can distinguish a preference order from a coincidence.
    The winner is asserted against ``GROUND_TRUTH_PREFERENCE[0]`` rather than
    against the string, so the test binds to the module's declared order instead
    of restating it, and the overruled source is asserted to be *recorded* --
    discarding it would hide a mislabelled fixture or a detector writing evidence
    it should not.
    """

    verdict = adjudicate(
        action_id="a",
        fixture_labels={"a": VERDICT_STUCK},
        subsequent_evidence={"a": VERDICT_NOT_STUCK},
        human_adjudications={"a": VERDICT_NOT_STUCK},
    )

    assert verdict.verdict == VERDICT_STUCK
    assert verdict.source == GROUND_TRUTH_PREFERENCE[0] == SOURCE_FIXTURE_LABEL
    assert verdict.overruled == (
        (SOURCE_SUBSEQUENT_EVIDENCE, VERDICT_NOT_STUCK),
        (SOURCE_HUMAN_ADJUDICATION, VERDICT_NOT_STUCK),
    )


def test_the_second_source_decides_when_the_first_is_silent(db: Path) -> None:
    """A source with no opinion is absent from its map, and the next one decides."""

    verdict = adjudicate(
        action_id="a",
        fixture_labels={},
        subsequent_evidence={"a": VERDICT_NOT_STUCK},
        human_adjudications={"a": VERDICT_STUCK},
    )

    assert verdict.source == GROUND_TRUTH_PREFERENCE[1] == SOURCE_SUBSEQUENT_EVIDENCE
    assert verdict.verdict == VERDICT_NOT_STUCK
    assert verdict.overruled == ((SOURCE_HUMAN_ADJUDICATION, VERDICT_STUCK),)


def test_human_adjudication_is_the_last_resort_and_still_decides(db: Path) -> None:
    """It is third, not absent: without it the case would be undetermined."""

    verdict = adjudicate(
        action_id="a",
        fixture_labels={},
        subsequent_evidence={},
        human_adjudications={"a": VERDICT_NOT_STUCK},
    )

    assert verdict.source == GROUND_TRUTH_PREFERENCE[2] == SOURCE_HUMAN_ADJUDICATION
    assert verdict.verdict == VERDICT_NOT_STUCK
    assert verdict.overruled == ()


def test_an_agreeing_lower_source_is_not_recorded_as_overruled(db: Path) -> None:
    """``overruled`` holds disagreement, not every source that spoke."""

    verdict = adjudicate(
        action_id="a",
        fixture_labels={"a": VERDICT_NOT_STUCK},
        subsequent_evidence={"a": VERDICT_NOT_STUCK},
        human_adjudications={},
    )

    assert verdict.overruled == ()


def test_a_verdict_outside_the_closed_set_is_refused(db: Path) -> None:
    """Including the word ``undetermined``, which is an outcome and not an input.

    Accepting it as an input would let a source that cannot decide *prevent* a
    lower-preference source that could.
    """

    with pytest.raises(UnknownGroundTruthVerdict):
        adjudicate(
            action_id="a",
            fixture_labels={"a": "probably"},
            subsequent_evidence={},
            human_adjudications={},
        )
    with pytest.raises(UnknownGroundTruthVerdict):
        adjudicate(
            action_id="a",
            fixture_labels={"a": VERDICT_UNDETERMINED},
            subsequent_evidence={},
            human_adjudications={"a": VERDICT_NOT_STUCK},
        )


# --------------------------------------------------------------------------
# undetermined
# --------------------------------------------------------------------------


def test_undetermined_is_reachable_counted_and_opens_the_rate_gap(db: Path) -> None:
    """Two applied terminations, one settled false and one settled by nothing.

    The undetermined row moves neither bound on its own: the lower rate counts
    the confirmed false termination alone, the upper counts what the undetermined
    row could turn out to be, and the gap between them is exactly the ground
    truth this report does not have.
    """

    make_subject(db, run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a-false",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START,
        applied_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
    )
    make_action(
        db,
        action_id="a-unknown",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START,
        applied_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
    )

    report = measure(db, fixture_labels={"a-false": VERDICT_NOT_STUCK})

    assert report.false_termination_ids == ("a-false",)
    assert report.undetermined_ids == ("a-unknown",)
    assert report.justified_ids == ()
    assert report.adjudications["a-unknown"].source == SOURCE_NONE
    assert report.rate_lower == 0.5
    assert report.rate_upper == 1.0
    assert report.rate_is_settled is False

    rendered = render_false_termination_report(report)
    assert "undetermined" in rendered
    assert "1 undetermined termination(s)" in rendered


def test_the_bounds_coincide_when_every_applied_row_is_settled(db: Path) -> None:
    """The gap exists only where ground truth is missing, and the report says so."""

    make_subject(db, run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a-false",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START,
        applied_at_ms=PERIOD_START,
        run_id="r1",
    )

    report = measure(db, fixture_labels={"a-false": VERDICT_NOT_STUCK})

    assert report.rate_lower == report.rate_upper == 1.0
    assert report.rate_is_settled is True
    assert "the two bounds coincide" in render_false_termination_report(report)


# --------------------------------------------------------------------------
# the subsequent-evidence source
# --------------------------------------------------------------------------


def test_activity_after_the_termination_says_the_subject_was_not_stuck(
    db: Path,
) -> None:
    """A session that resumed productive activity after termination was not stuck.

    The evidence is looked for on the subject the *incident* names -- the session
    -- and only after ``applied_at_ms``: activity before the termination is what
    every live session produces and says nothing about whether it was stuck when
    the effect landed.
    """

    make_subject(db, run_id="r1", session_id="s1")
    make_incident(db, incident_id="i1", run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a-resumed",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START,
        applied_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
        incident_id="i1",
    )
    append_activity(
        db,
        event_id="before",
        subject_kind="session",
        subject_id="s1",
        event_type=PRODUCTIVE[0],
        ingested_at_ms=PERIOD_START + MINUTE_MS - 1,
    )

    connection = open_for_measurement(db)
    try:
        actions = read_terminate_actions(
            connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_END
        )
        before = subsequent_activity_verdicts(
            connection,
            actions,
            productive_event_types=PRODUCTIVE,
            period_end_ms=PERIOD_END,
        )
    finally:
        connection.close()
    assert dict(before) == {}, "activity BEFORE the termination settles nothing"

    append_activity(
        db,
        event_id="after",
        subject_kind="session",
        subject_id="s1",
        event_type=PRODUCTIVE[0],
        ingested_at_ms=PERIOD_START + 2 * MINUTE_MS,
    )
    connection = open_for_measurement(db)
    try:
        actions = read_terminate_actions(
            connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_END
        )
        after = subsequent_activity_verdicts(
            connection,
            actions,
            productive_event_types=PRODUCTIVE,
            period_end_ms=PERIOD_END,
        )
    finally:
        connection.close()
    assert dict(after) == {"a-resumed": VERDICT_NOT_STUCK}

    report = measure(db, subsequent_evidence=after)
    assert report.false_termination_ids == ("a-resumed",)
    assert report.adjudications["a-resumed"].source == SOURCE_SUBSEQUENT_EVIDENCE


def test_silence_after_a_termination_never_says_the_subject_was_stuck(
    db: Path,
) -> None:
    """Absence of evidence is not evidence (``D-0006``).

    A terminated session produces nothing *because* it was terminated. If silence
    counted as confirmation, every termination would justify itself and the rate
    would be zero by construction -- so the source declines, and the row reaches
    the undetermined bucket instead.
    """

    make_subject(db, run_id="r1", session_id="s1")
    make_incident(db, incident_id="i1", run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a-silent",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START,
        applied_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
        incident_id="i1",
    )

    connection = open_for_measurement(db)
    try:
        actions = read_terminate_actions(
            connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_END
        )
        verdicts = subsequent_activity_verdicts(
            connection,
            actions,
            productive_event_types=PRODUCTIVE,
            period_end_ms=PERIOD_END,
        )
    finally:
        connection.close()

    assert dict(verdicts) == {}
    report = measure(db, subsequent_evidence=verdicts)
    assert report.undetermined_ids == ("a-silent",)


def test_an_undeclared_event_type_is_not_productive_activity(db: Path) -> None:
    """The declared set is the whole definition; an event outside it clears nothing.

    Without the restriction the termination's own bookkeeping event would clear
    the termination that produced it.
    """

    make_subject(db, run_id="r1", session_id="s1")
    make_incident(db, incident_id="i1", run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START,
        applied_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
        incident_id="i1",
    )
    append_activity(
        db,
        event_id="bookkeeping",
        subject_kind="session",
        subject_id="s1",
        event_type="session_terminated",
        ingested_at_ms=PERIOD_START + 2 * MINUTE_MS,
    )

    connection = open_for_measurement(db)
    try:
        actions = read_terminate_actions(
            connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_END
        )
        verdicts = subsequent_activity_verdicts(
            connection,
            actions,
            productive_event_types=PRODUCTIVE,
            period_end_ms=PERIOD_END,
        )
    finally:
        connection.close()

    assert dict(verdicts) == {}


def test_declaring_no_productive_event_type_is_refused(db: Path) -> None:
    """An empty set disables a ground-truth source without recording that it did."""

    connection = open_for_measurement(db)
    try:
        with pytest.raises(FalseTerminationRefusal):
            subsequent_activity_verdicts(
                connection, (), productive_event_types=(), period_end_ms=PERIOD_END
            )
    finally:
        connection.close()


def test_evidence_arriving_after_the_period_belongs_to_the_next_report(
    db: Path,
) -> None:
    """The answer is a function of the period, so a printed figure stays true.

    Activity ingested at exactly ``period_end_ms`` is outside this report
    (half-open, ``time-base-policy.md`` section 2 rule 4) and must not change a
    verdict this report already published.
    """

    make_subject(db, run_id="r1", session_id="s1")
    make_incident(db, incident_id="i1", run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a",
        status=STATUS_APPLIED,
        created_at_ms=PERIOD_START,
        applied_at_ms=PERIOD_START + MINUTE_MS,
        run_id="r1",
        incident_id="i1",
    )
    append_activity(
        db,
        event_id="just_after",
        subject_kind="session",
        subject_id="s1",
        event_type=PRODUCTIVE[0],
        ingested_at_ms=PERIOD_END,
    )

    connection = open_for_measurement(db)
    try:
        actions = read_terminate_actions(
            connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_END
        )
        inside = subsequent_activity_verdicts(
            connection,
            actions,
            productive_event_types=PRODUCTIVE,
            period_end_ms=PERIOD_END,
        )
        later = subsequent_activity_verdicts(
            connection,
            actions,
            productive_event_types=PRODUCTIVE,
            period_end_ms=PERIOD_END + DAY_MS,
        )
    finally:
        connection.close()

    assert dict(inside) == {}
    assert dict(later) == {"a": VERDICT_NOT_STUCK}


def test_a_pending_recommendation_has_no_subsequent_evidence_to_read(db: Path) -> None:
    """Nothing was terminated, so there is no "after" for activity to follow.

    Its subject is running for reasons that have nothing to do with a termination
    that never happened, and reading its activity as ``not_stuck`` would file a
    verdict about an effect the organisation declined to apply.
    """

    make_subject(db, run_id="r1", session_id="s1")
    make_incident(db, incident_id="i1", run_id="r1", session_id="s1")
    make_action(
        db,
        action_id="a-pending",
        status=STATUS_PENDING,
        created_at_ms=PERIOD_START,
        run_id="r1",
        incident_id="i1",
    )
    append_activity(
        db,
        event_id="busy",
        subject_kind="session",
        subject_id="s1",
        event_type=PRODUCTIVE[0],
        ingested_at_ms=PERIOD_START + MINUTE_MS,
    )

    connection = open_for_measurement(db)
    try:
        actions = read_terminate_actions(
            connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_END
        )
        verdicts = subsequent_activity_verdicts(
            connection,
            actions,
            productive_event_types=PRODUCTIVE,
            period_end_ms=PERIOD_END,
        )
    finally:
        connection.close()

    assert dict(verdicts) == {}


# --------------------------------------------------------------------------
# the report itself
# --------------------------------------------------------------------------


def test_an_empty_or_inverted_period_is_refused(db: Path) -> None:
    connection = open_for_measurement(db)
    try:
        with pytest.raises(FalseTerminationRefusal):
            measure_false_termination(
                connection,
                period_start_ms=PERIOD_END,
                period_end_ms=PERIOD_START,
                now_ms=NOW_MS,
                fixture_labels={},
                subsequent_evidence={},
                human_adjudications={},
            )
    finally:
        connection.close()


def test_the_report_states_what_it_does_not_count(db: Path) -> None:
    """Both error directions of section 3.4 are named in the rendering itself.

    A reader who never opens this module has to be able to see that AI
    recommendations and watcher candidates are excluded on purpose, and why.
    """

    report = measure(db)
    rendered = render_false_termination_report(report)

    assert "D-0004" in rendered and "AC-6" in rendered
    assert "watcher candidates" in rendered
    assert " > ".join(GROUND_TRUTH_PREFERENCE) in rendered
    assert rendered.isascii(), (
        "the report reaches a cp932 console; a single em-dash would raise "
        "UnicodeEncodeError there"
    )
