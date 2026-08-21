"""Percentiles on a hand-checkable sample, both references always rendered, lag kept apart.

Three properties get adversarial treatment here, because each of them is the
kind a plausible implementation satisfies by accident on a friendly input.

* **Both references, always.** A report that never leaves the shadow period
  would pass every test of the budget comparison while being structurally unable
  to say "there is no v1 distribution for this period". So the no-shadow path is
  driven twice -- once with no shadow source at all, once with a shadow source
  that simply holds nothing for the class under test -- and both are asserted to
  render their own reason under the second heading. A separate test walks *every*
  class block of a rendered report and asserts both headings are present, which
  is the structural claim ``latency.py`` makes rather than a claim about one
  input.
* **The percentiles are checked by hand, not recomputed.** The sample is ten
  latencies of 1..10 minutes, chosen so nearest-rank median and p90 are the 5th
  and 9th values and can be written down. Re-deriving them with ``statistics``
  here would be a copy of the code under test, and a copy agrees with a bug.
* **The lag is a different series.** Proved by moving one and asserting the
  other does not move: the same episodes are measured over a database whose
  spine has been given a nine-minute ingestion lag, and the detection
  distribution comes out identical. A harness that subtracted or folded lag into
  latency fails that comparison, and no assertion about a single report could
  catch it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import policy
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.measurement.latency import (
    SHADOW_ABSENT,
    SHADOW_PRESENT,
    DetectionBeforeOnset,
    Distribution,
    LatencyRefusal,
    ShadowReference,
    ShadowReferenceUnstated,
    ShadowSource,
    UnknownEpisodeDetection,
    measure_ingestion_lag,
    measure_latency,
    no_shadow_reference,
    render_latency_report,
    shadow_from_both_bucket,
)
from claude_org_runtime.measurement.reader import open_for_measurement
from claude_org_runtime.measurement.windows import Episode, classify_episodes

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
MINUTE_MS = 60_000
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS
NOW_MS = T0 + DAY_MS + MINUTE_MS

#: The note ``0002_policy_seed.sql`` writes. Looked up by note rather than
#: assumed to be revision 1, so these tests survive a later seed step.
SEED_NOTE = (
    "initial time base: detection latency budgets, gate stage tolerances "
    "and gate stage owners as first decided"
)

#: T = 10 min, L = 15 min (``time-base-policy.md`` section 3.2). Absolute on both
#: sides, so a test about the distribution is not also a test about subject
#: resolution -- ``test_windows.py`` owns that.
CLASS_A = "session_no_evidence"
#: T per stage, L = 5 min. A second class, so "per class" is exercised.
CLASS_B = "relay_gap"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    return path


def writable(path: Path) -> sqlite3.Connection:
    """An ordinary writable handle -- deliberately not the harness's.

    The harness's connection cannot write (``reader.py``), which is the property
    under test elsewhere in this package, so fixtures are built through a second
    connection rather than by relaxing that one.
    """

    return sqlite3.connect(path, isolation_level=None)


def seed_revision_id(path: Path) -> int:
    connection = writable(path)
    try:
        row = connection.execute(
            "SELECT revision_id FROM policy_revision WHERE note = ?", (SEED_NOTE,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "0002_policy_seed.sql must have applied"
    return int(row[0])


def budget_ms_of(path: Path, revision_id: int, incident_class: str) -> int:
    """``L`` as the policy row states it, read the way the module reads it."""

    connection = open_for_measurement(path)
    try:
        row = policy.detection_latency(
            connection, revision_id=revision_id, incident_class=incident_class
        )
    finally:
        connection.close()
    assert row["budget_kind"] == "absolute_ms"
    return int(row["budget_ms"])


def append_spine_event(
    path: Path,
    *,
    event_id: str,
    occurred_at_ms: int,
    ingested_at_ms: int,
) -> None:
    connection = writable(path)
    try:
        connection.execute(
            """
            INSERT INTO event (event_id, event_type, subject_kind, subject_id,
                               producer, dedup_key, occurred_at_ms, ingested_at_ms)
            VALUES (?, 'ci_outcome', 'pull_request', 'pr-1', 'test', ?, ?, ?)
            """,
            (event_id, f"dedup/{event_id}", occurred_at_ms, ingested_at_ms),
        )
    finally:
        connection.close()


def report_over(
    path: Path,
    revision_id: int,
    episodes,
    detections,
    shadow: ShadowSource,
    *,
    period_start_ms: int = PERIOD_START,
    period_end_ms: int = PERIOD_END,
):
    connection = open_for_measurement(path)
    try:
        windows = classify_episodes(
            connection,
            revision_id=revision_id,
            period_start_ms=period_start_ms,
            period_end_ms=period_end_ms,
            episodes=episodes,
        )
        return measure_latency(
            connection,
            windows=windows,
            detections=detections,
            shadow=shadow,
            now_ms=NOW_MS,
        )
    finally:
        connection.close()


def ten_minute_sample(path: Path, revision_id: int, shadow: ShadowSource):
    """Ten in-period episodes detected 1..10 minutes after their own onsets.

    Onsets are spaced an hour apart and start well inside the period, so every
    window (``L`` + grace, both far under an hour) lies wholly inside it and the
    censoring rules -- proved in ``test_windows.py`` -- are not what this sample
    is about.
    """

    episodes = []
    detections = {}
    for index in range(1, 11):
        onset = PERIOD_START + index * 60 * MINUTE_MS
        episode_id = f"e{index:02d}"
        episodes.append(
            Episode(
                episode_id=episode_id, incident_class=CLASS_A, onset_ms=onset
            )
        )
        detections[episode_id] = onset + index * MINUTE_MS
    return report_over(path, revision_id, episodes, detections, shadow)


# --------------------------------------------------------------------------
# the distribution
# --------------------------------------------------------------------------


def test_percentiles_are_nearest_rank_on_a_hand_checked_sample(db: Path) -> None:
    """Latencies of 1..10 minutes: median is the 5th, p90 the 9th, max the 10th.

    Nearest rank returns a value some episode actually exhibited, which an
    interpolating median would not: the interpolating median of this sample is
    5.5 minutes, a duration no detection here took. The expected numbers are
    written out rather than computed, so the test binds to the definition and
    not to the implementation of it.
    """

    revision_id = seed_revision_id(db)
    report = ten_minute_sample(
        db, revision_id, no_shadow_reference("no shadow period for this test")
    )

    (measured,) = report.classes
    assert measured.incident_class == CLASS_A
    assert measured.distribution == Distribution(
        count=10,
        median_ms=5 * MINUTE_MS,
        p90_ms=9 * MINUTE_MS,
        max_ms=10 * MINUTE_MS,
    )


def test_an_undetected_in_period_episode_is_not_a_latency_sample(db: Path) -> None:
    """A missing detection is a candidate miss, and it is named, not dropped.

    Including it at any value would be a fabrication; dropping it silently would
    leave the report unable to distinguish "nine detections" from "ten episodes,
    one never detected", which are the two readings AC-10 turns on.
    """

    revision_id = seed_revision_id(db)
    episodes = [
        Episode(episode_id="detected", incident_class=CLASS_A, onset_ms=PERIOD_START),
        Episode(
            episode_id="never",
            incident_class=CLASS_A,
            onset_ms=PERIOD_START + MINUTE_MS,
        ),
    ]
    report = report_over(
        db,
        revision_id,
        episodes,
        {"detected": PERIOD_START + MINUTE_MS},
        no_shadow_reference("no shadow period for this test"),
    )

    (measured,) = report.classes
    assert measured.distribution.count == 1
    assert measured.undetected_ids == ("never",)
    assert "candidate misses" in render_latency_report(report)


def test_a_censored_episode_is_excluded_from_the_distribution_and_counted(
    db: Path,
) -> None:
    """Censoring is windows.py's decision and this module honours it.

    The right-censored episode's window ends one millisecond past the period, so
    ``windows.py`` files it ``censored``; its detection is supplied anyway, and
    the distribution must still be over the in-period episode alone.
    """

    revision_id = seed_revision_id(db)
    budget_ms = budget_ms_of(db, revision_id, CLASS_A)
    connection = open_for_measurement(db)
    try:
        grace_ms = classify_episodes(
            connection,
            revision_id=revision_id,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            episodes=(),
        ).grace_ms
    finally:
        connection.close()

    inside_onset = PERIOD_START + MINUTE_MS
    over_the_edge_onset = PERIOD_END - budget_ms - grace_ms + 1
    episodes = [
        Episode(episode_id="inside", incident_class=CLASS_A, onset_ms=inside_onset),
        Episode(
            episode_id="right", incident_class=CLASS_A, onset_ms=over_the_edge_onset
        ),
        Episode(
            episode_id="left",
            incident_class=CLASS_A,
            onset_ms=PERIOD_START - 1,
        ),
    ]
    detections = {
        "inside": inside_onset + MINUTE_MS,
        "right": over_the_edge_onset + 2 * MINUTE_MS,
        "left": PERIOD_START + 3 * MINUTE_MS,
    }
    report = report_over(
        db,
        revision_id,
        episodes,
        detections,
        no_shadow_reference("no shadow period for this test"),
    )

    (measured,) = report.classes
    assert measured.distribution == Distribution(
        count=1, median_ms=MINUTE_MS, p90_ms=MINUTE_MS, max_ms=MINUTE_MS
    )
    assert measured.censored_ids == ("right",)
    assert measured.censored_left_ids == ("left",)


def test_over_budget_is_strictly_greater_than_L(db: Path) -> None:
    """A detection landing exactly on the ceiling met it; one millisecond later did not.

    ``L`` is the ceiling on onset-to-alarm (``time-base-policy.md`` section 3.1),
    so ``>=`` here would fail the one detection that did exactly what the policy
    asked, and the failure would be invisible in any sample not driven to the
    instant.
    """

    revision_id = seed_revision_id(db)
    budget_ms = budget_ms_of(db, revision_id, CLASS_A)
    on_time_onset = PERIOD_START + MINUTE_MS
    late_onset = PERIOD_START + 2 * MINUTE_MS
    episodes = [
        Episode(
            episode_id="exactly", incident_class=CLASS_A, onset_ms=on_time_onset
        ),
        Episode(episode_id="one_late", incident_class=CLASS_A, onset_ms=late_onset),
    ]
    detections = {
        "exactly": on_time_onset + budget_ms,
        "one_late": late_onset + budget_ms + 1,
    }
    report = report_over(
        db,
        revision_id,
        episodes,
        detections,
        no_shadow_reference("no shadow period for this test"),
    )

    (measured,) = report.classes
    assert measured.over_budget_ids == ("one_late",)
    assert measured.budgets_ms == (budget_ms,)


def test_a_detection_before_its_onset_is_refused(db: Path) -> None:
    """A negative latency is a mispairing or a mixed clock, never a fast detector."""

    revision_id = seed_revision_id(db)
    onset = PERIOD_START + 10 * MINUTE_MS
    with pytest.raises(DetectionBeforeOnset):
        report_over(
            db,
            revision_id,
            [Episode(episode_id="e", incident_class=CLASS_A, onset_ms=onset)],
            {"e": onset - 1},
            no_shadow_reference("no shadow period for this test"),
        )


def test_a_detection_for_an_unclassified_episode_is_refused(db: Path) -> None:
    """The detection map and the episode set must be over the same selection."""

    revision_id = seed_revision_id(db)
    with pytest.raises(UnknownEpisodeDetection):
        report_over(
            db,
            revision_id,
            [
                Episode(
                    episode_id="known",
                    incident_class=CLASS_A,
                    onset_ms=PERIOD_START + MINUTE_MS,
                )
            ],
            {"known": PERIOD_START + 2 * MINUTE_MS, "stray": PERIOD_START},
            no_shadow_reference("no shadow period for this test"),
        )


# --------------------------------------------------------------------------
# the two references
# --------------------------------------------------------------------------


def test_no_shadow_reference_renders_and_says_so(db: Path) -> None:
    """Outside the shadow period the report states the absence, with its reason.

    The budget block must still be there -- the acceptance bound is available
    and is printed -- and the shadow block must say in words that there is no
    non-regression reference, so a reader cannot take "inside budget" for "no
    regression".
    """

    revision_id = seed_revision_id(db)
    reason = "2026-08-21 lies outside the canary shadow window"
    report = ten_minute_sample(db, revision_id, no_shadow_reference(reason))

    (measured,) = report.classes
    assert measured.shadow.status == SHADOW_ABSENT
    assert measured.shadow.distribution is None
    assert measured.shadow.reason == reason
    assert report.shadow_available is False

    rendered = render_latency_report(report)
    assert "NO SHADOW REFERENCE FOR THIS PERIOD" in rendered
    assert reason in rendered
    assert "L in force" in rendered, "the acceptance bound is still printed"


def test_a_present_shadow_source_with_nothing_for_this_class_is_absent_for_it(
    db: Path,
) -> None:
    """An empty per-class sample is an absence with a reason, never a zero.

    This is the failure that would survive a test using only the report-level
    absence: the shadow period covers the report, so a naive implementation
    reports "v1: count 0" and a reader compares against nothing and sees no
    regression.
    """

    revision_id = seed_revision_id(db)
    report = ten_minute_sample(
        db,
        revision_id,
        shadow_from_both_bucket({CLASS_B: (1_000, 2_000)}),
    )

    (measured,) = report.classes
    assert measured.incident_class == CLASS_A
    assert measured.shadow.status == SHADOW_ABSENT
    assert measured.shadow.reason is not None
    assert CLASS_A in measured.shadow.reason
    assert report.shadow_available is True, (
        "the report-level source IS present; only this class has no both-bucket "
        "episode, and the two facts are different"
    )


def test_a_present_shadow_reference_carries_v1_percentiles(db: Path) -> None:
    """The non-regression bound is v1's own distribution, computed the same way."""

    revision_id = seed_revision_id(db)
    v1_samples = tuple(index * MINUTE_MS for index in range(1, 11))
    report = ten_minute_sample(
        db, revision_id, shadow_from_both_bucket({CLASS_A: v1_samples})
    )

    (measured,) = report.classes
    assert measured.shadow.status == SHADOW_PRESENT
    assert measured.shadow.both_bucket_count == 10
    assert measured.shadow.distribution == Distribution(
        count=10,
        median_ms=5 * MINUTE_MS,
        p90_ms=9 * MINUTE_MS,
        max_ms=10 * MINUTE_MS,
    )
    assert "both-bucket" in render_latency_report(report)


def test_every_class_block_renders_both_reference_headings(db: Path) -> None:
    """The structural claim: no class is rendered against one reference alone.

    Asserted over a report holding one class with a shadow distribution and one
    without, so a rendering that emitted the second heading only when it had
    something to put under it would fail here.
    """

    revision_id = seed_revision_id(db)
    episodes = [
        Episode(
            episode_id="a",
            incident_class=CLASS_A,
            onset_ms=PERIOD_START + 60 * MINUTE_MS,
        ),
        Episode(
            episode_id="b",
            incident_class=CLASS_B,
            onset_ms=PERIOD_START + 120 * MINUTE_MS,
        ),
    ]
    detections = {
        "a": PERIOD_START + 61 * MINUTE_MS,
        "b": PERIOD_START + 121 * MINUTE_MS,
    }
    report = report_over(
        db,
        revision_id,
        episodes,
        detections,
        shadow_from_both_bucket({CLASS_A: (30_000,)}),
    )
    rendered = render_latency_report(report)

    assert len(report.classes) == 2
    assert rendered.count("reference 1 of 2") == 2
    assert rendered.count("reference 2 of 2") == 2
    assert rendered.isascii(), (
        "the report reaches a cp932 console; a single em-dash would raise "
        "UnicodeEncodeError there"
    )


def test_a_shadow_reference_without_a_distribution_or_a_reason_is_refused() -> None:
    """The exclusive-or is the type's, not the caller's discipline."""

    with pytest.raises(ShadowReferenceUnstated):
        ShadowReference(
            status=SHADOW_ABSENT,
            distribution=None,
            both_bucket_count=None,
            reason="",
        )
    with pytest.raises(ShadowReferenceUnstated):
        ShadowReference(
            status=SHADOW_PRESENT,
            distribution=None,
            both_bucket_count=0,
            reason=None,
        )
    with pytest.raises(ShadowReferenceUnstated):
        ShadowSource(status=SHADOW_ABSENT, samples=None, reason=None)
    with pytest.raises(ShadowReferenceUnstated):
        ShadowSource(status="maybe", samples=None, reason="a reason")


# --------------------------------------------------------------------------
# the ingestion lag, beside the latency and never inside it
# --------------------------------------------------------------------------


def test_ingestion_lag_is_a_separate_series_from_detection_latency(db: Path) -> None:
    """Give the spine a nine-minute lag; the detection distribution must not move.

    This is the "GitHub having a bad afternoon" case of section 4. A harness that
    folded lag into latency, or subtracted it out, would report a different
    detection distribution over identical episodes, and the difference would look
    exactly like a detector regression.
    """

    revision_id = seed_revision_id(db)
    before = ten_minute_sample(
        db, revision_id, no_shadow_reference("no shadow period for this test")
    )

    for index in range(3):
        occurred = PERIOD_START + (index + 1) * 30 * MINUTE_MS
        append_spine_event(
            db,
            event_id=f"slow-{index}",
            occurred_at_ms=occurred,
            ingested_at_ms=occurred + 9 * MINUTE_MS,
        )

    after = ten_minute_sample(
        db, revision_id, no_shadow_reference("no shadow period for this test")
    )

    assert before.ingestion_lag.event_count == 0
    assert before.ingestion_lag.distribution.count == 0
    assert after.ingestion_lag.distribution == Distribution(
        count=3,
        median_ms=9 * MINUTE_MS,
        p90_ms=9 * MINUTE_MS,
        max_ms=9 * MINUTE_MS,
    )
    assert after.classes[0].distribution == before.classes[0].distribution, (
        "a provider's slow afternoon must not move the detection distribution"
    )

    rendered = render_latency_report(after)
    assert "Ingestion lag" in rendered
    assert "the first is the provider getting slower, the second is us" in rendered


def test_negative_ingestion_lag_is_counted_rather_than_clamped(db: Path) -> None:
    """A provider clock ahead of ours is skew, and this is the only record of it."""

    append_spine_event(
        db,
        event_id="ahead",
        occurred_at_ms=PERIOD_START + 10 * MINUTE_MS,
        ingested_at_ms=PERIOD_START + 9 * MINUTE_MS,
    )
    connection = open_for_measurement(db)
    try:
        lag = measure_ingestion_lag(
            connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_END
        )
    finally:
        connection.close()

    assert lag.negative_count == 1
    assert lag.distribution.max_ms == -MINUTE_MS


def test_the_lag_window_is_half_open_on_our_own_clock(db: Path) -> None:
    """Bounded on ``ingested_at_ms``, ``[start, end)``.

    Selecting on ``occurred_at_ms`` would let a provider's skew move rows between
    reports -- the effect this series exists to expose. The row whose *ingest*
    lands exactly at ``period_end_ms`` belongs to the next period (rule 4), and
    the one at ``period_start_ms`` belongs to this one.
    """

    append_spine_event(
        db, event_id="at_start", occurred_at_ms=PERIOD_START - 1, ingested_at_ms=PERIOD_START
    )
    append_spine_event(
        db, event_id="at_end", occurred_at_ms=PERIOD_END - 1, ingested_at_ms=PERIOD_END
    )
    # Ingested inside the period, but occurring long before it: kept, because
    # our clock decides membership.
    append_spine_event(
        db,
        event_id="old_fact",
        occurred_at_ms=PERIOD_START - 10 * MINUTE_MS,
        ingested_at_ms=PERIOD_START + MINUTE_MS,
    )
    connection = open_for_measurement(db)
    try:
        lag = measure_ingestion_lag(
            connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_END
        )
    finally:
        connection.close()

    assert lag.event_count == 2
    assert lag.distribution.max_ms == 11 * MINUTE_MS


def test_an_empty_or_inverted_period_is_refused(db: Path) -> None:
    """Every row would be outside a period containing no instant."""

    connection = open_for_measurement(db)
    try:
        with pytest.raises(LatencyRefusal):
            measure_ingestion_lag(
                connection, period_start_ms=PERIOD_END, period_end_ms=PERIOD_START
            )
        with pytest.raises(LatencyRefusal):
            measure_ingestion_lag(
                connection, period_start_ms=PERIOD_START, period_end_ms=PERIOD_START
            )
    finally:
        connection.close()


def test_the_report_carries_the_revision_and_grace_it_was_computed_under(
    db: Path,
) -> None:
    """A latency judged against one revision's L is a figure over that revision.

    Carried up from the window report rather than re-resolved, so the two halves
    of one report cannot end up over two revisions (``D-0031``, ``D-0040``).
    """

    revision_id = seed_revision_id(db)
    report = ten_minute_sample(
        db, revision_id, no_shadow_reference("no shadow period for this test")
    )

    assert report.revision_id == revision_id
    assert report.generated_at_ms == NOW_MS
    assert report.period_start_ms == PERIOD_START
    assert report.period_end_ms == PERIOD_END
    rendered = render_latency_report(report)
    assert f"policy revision {revision_id}" in rendered
    assert report.grace_source in rendered


def test_an_empty_distribution_prints_as_no_sample_not_as_zero(db: Path) -> None:
    """Zero milliseconds and no sample are different statements."""

    empty = Distribution.of([])
    assert empty == Distribution(count=0, median_ms=None, p90_ms=None, max_ms=None)

    revision_id = seed_revision_id(db)
    report = report_over(
        db,
        revision_id,
        (),
        {},
        no_shadow_reference("no shadow period for this test"),
    )
    assert report.classes == ()
    assert "No episode was classified for this period." in render_latency_report(
        report
    )
