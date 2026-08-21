"""Both half-open ends, grace proved to be data, and the two censored buckets kept apart.

The defect ``windows.py`` exists to prevent is invisible to a suite whose
episodes sit comfortably inside the period: every classifier, right or wrong,
calls those ``in_period``. So every boundary case here is driven **to the
instant** -- an episode whose window ends exactly at ``period_end_ms`` and the
same episode one millisecond later, an onset exactly at ``period_start_ms`` and
one millisecond before -- because a ``<`` written as a ``<=`` moves exactly one
episode per report and shows up nowhere else.

Two properties get adversarial treatment beyond the boundaries:

* **Grace is data.** A hardcoded 120 s would pass every classification test in
  this file. It is caught by changing the *revision*'s ``reconcile_period_ms``
  and asserting the same episode, over the same period, changes bucket --
  which no constant can do.
* **A relative class resolves through its subject.** ``lease_orphan``'s ``L`` is
  twice *that lease's* TTL, so the test builds two leases with different TTLs
  and asserts two different windows, and separately asserts that an episode with
  no subject is **refused**. A default there would produce a two-millisecond
  window, which is the failure that looks like a detector missing everything.

Nothing here re-implements the classification to compare against. Where a test
needs to know how long a window is, it reads ``L`` out of the same
``policy_detection_latency`` row the module reads and does arithmetic the module
does not: the expected bucket is then stated by hand.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import policy
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.measurement.reader import open_for_measurement
from claude_org_runtime.measurement.windows import (
    CENSORED,
    CENSORED_LEFT,
    GRACE_DECLARED,
    GRACE_REVISION_RECONCILE_PERIOD,
    IN_PERIOD,
    WINDOW_CLASSIFICATIONS,
    DuplicateEpisodeRefused,
    Episode,
    EpisodeOutsidePeriod,
    GraceNotDeclared,
    PeriodRefused,
    SubjectRequired,
    WindowRefusal,
    classify_episodes,
    default_grace_ms,
    episode_window,
    resolve_budget_ms,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS

#: The note ``0002_policy_seed.sql`` writes. Looked up by note rather than
#: assumed to be revision 1, so these tests survive a later seed step.
SEED_NOTE = (
    "initial time base: detection latency budgets, gate stage tolerances "
    "and gate stage owners as first decided"
)

#: An absolute-``L`` class with room on both sides: T = 10 min, L = 15 min
#: (``time-base-policy.md`` section 3.2). Used wherever the test is about the
#: window boundary rather than about resolution.
ABSOLUTE_CLASS = "session_no_evidence"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    return path


def writable(path: Path) -> sqlite3.Connection:
    """An ordinary writable handle -- deliberately not the harness's.

    The harness's own connection cannot write (``reader.py``), which is the
    property under test everywhere else in this package, so fixtures are built
    through a second connection rather than by relaxing that one.
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
    """``L`` as the policy row states it, so onsets are positioned from data.

    Read through :func:`policy.detection_latency` -- the same row the module
    reads -- rather than typed in, so that a test asserting "this window ends
    exactly at the period end" keeps meaning that if the seed's numbers change.
    """

    connection = open_for_measurement(path)
    try:
        row = policy.detection_latency(
            connection, revision_id=revision_id, incident_class=incident_class
        )
    finally:
        connection.close()
    assert row["budget_kind"] == "absolute_ms", (
        "budget_ms_of is for absolute-L classes; a relative L is not a duration "
        "until a subject is named"
    )
    return int(row["budget_ms"])


def report_over(path: Path, revision_id: int, episodes, **kwargs):
    connection = open_for_measurement(path)
    try:
        return classify_episodes(
            connection,
            revision_id=revision_id,
            period_start_ms=kwargs.pop("period_start_ms", PERIOD_START),
            period_end_ms=kwargs.pop("period_end_ms", PERIOD_END),
            episodes=episodes,
            **kwargs,
        )
    finally:
        connection.close()


def window_for(path: Path, revision_id: int, episode: Episode, grace_ms: int):
    connection = open_for_measurement(path)
    try:
        return episode_window(
            connection,
            revision_id=revision_id,
            episode=episode,
            grace_ms=grace_ms,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
        )
    finally:
        connection.close()


def add_revision(path: Path, *, note: str, effective_at_ms: int) -> int:
    connection = writable(path)
    try:
        cursor = connection.execute(
            "INSERT INTO policy_revision (note, decided_by, effective_at_ms)"
            " VALUES (?, 'D-test', ?)",
            (note, effective_at_ms),
        )
        return int(cursor.lastrowid)
    finally:
        connection.close()


def add_detection_latency(
    path: Path,
    revision_id: int,
    incident_class: str,
    *,
    threshold_kind: str = "absolute_ms",
    threshold_value: int,
    reconcile_period_ms: int,
    budget_ms: int,
    budget_kind: str = "absolute_ms",
) -> None:
    connection = writable(path)
    try:
        connection.execute(
            """
            INSERT INTO policy_detection_latency
                (revision_id, incident_class, threshold_kind, threshold_value,
                 reconcile_period_ms, budget_ms, budget_kind)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                incident_class,
                threshold_kind,
                threshold_value,
                reconcile_period_ms,
                budget_ms,
                budget_kind,
            ),
        )
    finally:
        connection.close()


def add_lease(path: Path, resource: str, *, ttl_ms: int) -> str:
    connection = writable(path)
    try:
        connection.execute(
            "INSERT INTO lease (resource, holder, epoch, acquired_at_ms, expires_at_ms)"
            " VALUES (?, 'watcher-a', 1, ?, ?)",
            (resource, T0, T0 + ttl_ms),
        )
    finally:
        connection.close()
    return resource


def add_scope(path: Path, scope_id: str, *, expected_interval_ms: int) -> str:
    connection = writable(path)
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO repository
                (repo_id, provider, provider_repo_id, owner, name,
                 created_at_ms, updated_at_ms)
            VALUES ('repo-1', 'github', NULL, 'acme', 'widget', ?, ?)
            """,
            (T0, T0),
        )
        connection.execute(
            """
            INSERT INTO watcher_scope
                (scope_id, scope_kind, repo_id, pr_id, expected_interval_ms,
                 enabled, registered_at_ms, retired_at_ms)
            VALUES (?, 'ci_repository', 'repo-1', NULL, ?, 1, ?, NULL)
            """,
            (scope_id, expected_interval_ms, T0),
        )
    finally:
        connection.close()
    return scope_id


# --------------------------------------------------------------------------
# half-openness, at both ends, to the millisecond
# --------------------------------------------------------------------------


def test_window_ending_exactly_at_period_end_is_in_period(db: Path) -> None:
    """``end_ms == period_end_ms`` is inside: the window's last instant is end - 1.

    Both intervals are half-open (``time-base-policy.md`` section 2, rule 4), so
    a window that ends where the period ends was wholly observed. Judging it
    censored would discard a complete observation -- the exact over-correction
    that makes the censored bucket meaningless.
    """

    revision_id = seed_revision_id(db)
    grace_ms = 0
    onset = PERIOD_END - budget_ms_of(db, revision_id, ABSOLUTE_CLASS) - grace_ms

    window = window_for(
        db, revision_id, Episode("e", ABSOLUTE_CLASS, onset), grace_ms=grace_ms
    )

    assert window.end_ms == PERIOD_END
    assert window.classification == IN_PERIOD


def test_window_ending_one_ms_past_period_end_is_censored(db: Path) -> None:
    """One millisecond later, the same episode is right-censored.

    Paired with the test above on purpose: either assertion alone passes under a
    classifier off by one in the direction the other catches.
    """

    revision_id = seed_revision_id(db)
    grace_ms = 0
    onset = PERIOD_END - budget_ms_of(db, revision_id, ABSOLUTE_CLASS) - grace_ms + 1

    window = window_for(
        db, revision_id, Episode("e", ABSOLUTE_CLASS, onset), grace_ms=grace_ms
    )

    assert window.end_ms == PERIOD_END + 1
    assert window.classification == CENSORED
    assert window.censored is True


def test_onset_exactly_at_period_start_is_in_period(db: Path) -> None:
    """The period's first instant is inside it, so an onset there is not censored."""

    revision_id = seed_revision_id(db)

    window = window_for(
        db, revision_id, Episode("e", ABSOLUTE_CLASS, PERIOD_START), grace_ms=0
    )

    assert window.classification == IN_PERIOD


def test_onset_one_ms_before_period_start_is_censored_left(db: Path) -> None:
    """The mirror boundary: an onset the report did not observe is ``censored_left``.

    Its window still lies inside the period, which is precisely why this case
    needs its own bucket -- a classifier that only checked the window's end
    would call it ``in_period`` and then compute a latency from an onset it
    never saw.
    """

    revision_id = seed_revision_id(db)

    window = window_for(
        db, revision_id, Episode("e", ABSOLUTE_CLASS, PERIOD_START - 1), grace_ms=0
    )

    assert window.end_ms < PERIOD_END, "the window itself is inside the period"
    assert window.classification == CENSORED_LEFT
    assert window.censored is True


def test_episode_spanning_the_whole_period_is_censored_left(db: Path) -> None:
    """Censored at both ends lands in exactly one bucket, and it is the left one.

    An episode can only be counted once. Left wins because a window with an
    unobserved onset has no trustworthy latency at all, whereas a right-censored
    one merely has an unfinished budget.
    """

    revision_id = seed_revision_id(db)
    long_window = PERIOD_END - PERIOD_START + 10

    window = window_for(
        db,
        revision_id,
        Episode("e", ABSOLUTE_CLASS, PERIOD_START - 1),
        grace_ms=long_window,
    )

    assert window.end_ms > PERIOD_END
    assert window.classification == CENSORED_LEFT


# --------------------------------------------------------------------------
# grace is read from the revision, and a constant cannot do this
# --------------------------------------------------------------------------


def test_grace_defaults_to_the_revision_reconcile_period(db: Path) -> None:
    revision_id = seed_revision_id(db)

    report = report_over(db, revision_id, [])

    connection = open_for_measurement(db)
    try:
        expected = default_grace_ms(connection, revision_id=revision_id)
    finally:
        connection.close()
    assert report.grace_ms == expected
    assert report.grace_source == GRACE_REVISION_RECONCILE_PERIOD


def test_changing_the_revisions_reconcile_period_changes_the_classification(
    db: Path,
) -> None:
    """The same episode, the same period, a different revision -- a different bucket.

    This is the test a hardcoded 120 s cannot pass. The second revision keeps
    ``L`` identical and moves only ``reconcile_period_ms``, so the *only* thing
    that can move the episode across the boundary is grace having been read from
    policy data (``D-0031``: the numbers live in versioned rows so a past report
    recomputes under the numbers it was judged by).
    """

    seeded = seed_revision_id(db)
    connection = open_for_measurement(db)
    try:
        seeded_grace = default_grace_ms(connection, revision_id=seeded)
        row = policy.detection_latency(
            connection, revision_id=seeded, incident_class=ABSOLUTE_CLASS
        )
    finally:
        connection.close()
    budget_ms = int(row["budget_ms"])
    wider_grace = seeded_grace + 60_000

    coarser = add_revision(db, note="a coarser pass", effective_at_ms=T0 + 1)
    add_detection_latency(
        db,
        coarser,
        ABSOLUTE_CLASS,
        threshold_value=int(row["threshold_value"]),
        reconcile_period_ms=wider_grace,
        budget_ms=budget_ms,
    )

    # Positioned to end exactly at the period end under the seeded grace.
    onset = PERIOD_END - budget_ms - seeded_grace
    episodes = [Episode("e", ABSOLUTE_CLASS, onset)]

    under_seed = report_over(db, seeded, episodes)
    under_coarser = report_over(db, coarser, episodes)

    assert under_seed.grace_ms == seeded_grace
    assert under_coarser.grace_ms == wider_grace
    assert under_seed.counts() == {IN_PERIOD: 1, CENSORED: 0, CENSORED_LEFT: 0}
    assert under_coarser.counts() == {IN_PERIOD: 0, CENSORED: 1, CENSORED_LEFT: 0}


def test_declared_grace_is_used_and_recorded_as_declared(db: Path) -> None:
    """A caller-declared grace overrides the default and says so on the report.

    ``D-0040`` makes the value part of the report because the classification
    cannot be recomputed without it.
    """

    revision_id = seed_revision_id(db)
    budget_ms = budget_ms_of(db, revision_id, ABSOLUTE_CLASS)

    report = report_over(
        db, revision_id, [Episode("e", ABSOLUTE_CLASS, PERIOD_START)], grace_ms=7
    )

    assert report.grace_source == GRACE_DECLARED
    assert report.grace_ms == 7
    assert report.windows[0].end_ms == PERIOD_START + budget_ms + 7


def test_a_revision_with_two_reconcile_periods_refuses_to_default(db: Path) -> None:
    """"One reconcile period" names no single value when the revision has two.

    Section 3.3 permits a class to run on a coarser pass, and section 3.5 wants
    one grace per report. Choosing between them here would be this file deciding
    policy: the smaller manufactures misses for the coarse class, the larger
    excuses real ones for the tight classes. The caller declares instead -- and
    declaring still works, which the second half asserts so the refusal is not a
    dead end.
    """

    revision_id = add_revision(db, note="two periods", effective_at_ms=T0 + 1)
    add_detection_latency(
        db,
        revision_id,
        "relay_gap",
        threshold_value=180_000,
        reconcile_period_ms=120_000,
        budget_ms=300_000,
    )
    add_detection_latency(
        db,
        revision_id,
        ABSOLUTE_CLASS,
        threshold_value=600_000,
        reconcile_period_ms=300_000,
        budget_ms=900_000,
    )

    with pytest.raises(GraceNotDeclared):
        report_over(db, revision_id, [Episode("e", ABSOLUTE_CLASS, PERIOD_START)])

    declared = report_over(
        db,
        revision_id,
        [Episode("e", ABSOLUTE_CLASS, PERIOD_START)],
        grace_ms=120_000,
    )
    assert declared.counts()[IN_PERIOD] == 1


def test_negative_grace_is_refused(db: Path) -> None:
    """Grace shortens nothing: a negative value holds the detector past its own budget."""

    revision_id = seed_revision_id(db)

    with pytest.raises(WindowRefusal):
        window_for(
            db, revision_id, Episode("e", ABSOLUTE_CLASS, PERIOD_START), grace_ms=-1
        )


# --------------------------------------------------------------------------
# relative classes resolve through their subject, or refuse
# --------------------------------------------------------------------------


def test_relative_budget_scales_with_that_lease(db: Path) -> None:
    """``lease_orphan``'s L is twice *that lease's* TTL, so two leases give two windows.

    A single-lease test would pass against a module that resolved the wrong
    lease, or the first one it found. Two TTLs with a ratio the multiple cannot
    produce by accident is what binds the window to its own subject.
    """

    revision_id = seed_revision_id(db)
    short = add_lease(db, "watcher/short", ttl_ms=60_000)
    long = add_lease(db, "watcher/long", ttl_ms=300_000)

    short_window = window_for(
        db, revision_id, Episode("s", "lease_orphan", PERIOD_START, short), grace_ms=0
    )
    long_window = window_for(
        db, revision_id, Episode("l", "lease_orphan", PERIOD_START, long), grace_ms=0
    )

    # The seed's multiples: T = 1 x TTL, L = 2 x TTL (0002_policy_seed.sql).
    assert short_window.budget_ms == 2 * 60_000
    assert short_window.tolerance_ms == 60_000
    assert long_window.budget_ms == 2 * 300_000
    assert long_window.tolerance_ms == 300_000
    assert long_window.end_ms - short_window.end_ms == 2 * (300_000 - 60_000)


def test_tolerance_and_budget_scale_by_the_same_subject_unit(db: Path) -> None:
    """T and L of one ``lease_orphan`` subject come from ONE unit lookup.

    ``lease_orphan`` is relative on both sides, so this is the only class where
    the two resolvers can disagree: ``policy.resolve_tolerance_ms`` scales T and
    ``windows.resolve_budget_ms`` scales L, and D-0041 narrowed the DDL's
    ``T + P <= L`` ``CHECK`` to absolute rows on the promise that the relative
    rows are asserted per subject instead -- which is an inequality between two
    numbers only while both were scaled by the same unit.

    The TTL here is deliberately not a round number: dividing each resolved side
    by its own multiple recovers the unit each side actually used, so a second
    copy of the lookup that drifted to a different lease, a different column, or
    a stale TTL fails this rather than passing quietly in whichever direction it
    drifted. The multiples are read from the policy row, never typed in.
    """

    revision_id = seed_revision_id(db)
    ttl_ms = 137_000
    resource = add_lease(db, "watcher/shared-unit", ttl_ms=ttl_ms)

    connection = open_for_measurement(db)
    try:
        row = policy.detection_latency(
            connection, revision_id=revision_id, incident_class="lease_orphan"
        )
        assert row["threshold_kind"] == "lease_ttl_multiple"
        assert row["budget_kind"] == "lease_ttl_multiple", (
            "this test is only meaningful while both sides are relative"
        )

        tolerance_ms = policy.resolve_tolerance_ms(
            connection,
            revision_id=revision_id,
            incident_class="lease_orphan",
            subject=resource,
        )
        budget_ms = resolve_budget_ms(
            connection,
            revision_id=revision_id,
            incident_class="lease_orphan",
            subject=resource,
        )
        unit_the_public_lookup_gives = policy.subject_unit_ms(
            connection, threshold_kind="lease_ttl_multiple", subject=resource
        )
    finally:
        connection.close()

    unit_behind_t, remainder_t = divmod(tolerance_ms, int(row["threshold_value"]))
    unit_behind_l, remainder_l = divmod(budget_ms, int(row["budget_ms"]))

    assert (remainder_t, remainder_l) == (0, 0)
    assert unit_behind_t == unit_behind_l, (
        "T and L were scaled by different subject units; the per-subject "
        "T + P <= L assertion D-0041 relies on is then comparing two units"
    )
    assert unit_behind_t == unit_the_public_lookup_gives == ttl_ms


def test_relative_threshold_scales_with_that_scope(db: Path) -> None:
    """``watcher_silence``'s T is three of *that scope's* polls; its L is absolute."""

    revision_id = seed_revision_id(db)
    fast = add_scope(db, "scope/fast", expected_interval_ms=30_000)
    slow = add_scope(db, "scope/slow", expected_interval_ms=60_000)

    fast_window = window_for(
        db, revision_id, Episode("f", "watcher_silence", PERIOD_START, fast), grace_ms=0
    )
    slow_window = window_for(
        db, revision_id, Episode("s", "watcher_silence", PERIOD_START, slow), grace_ms=0
    )

    assert fast_window.tolerance_ms == 3 * 30_000
    assert slow_window.tolerance_ms == 3 * 60_000
    assert fast_window.budget_ms == slow_window.budget_ms, "L here is absolute"
    assert fast_window.end_ms == slow_window.end_ms


@pytest.mark.parametrize("incident_class", ["lease_orphan", "watcher_silence"])
def test_a_relative_class_refuses_rather_than_defaults_without_a_subject(
    db: Path, incident_class: str
) -> None:
    """No subject, no window -- and no fallback to the bare multiple.

    The fallback available is the multiple itself (2, or 3), which yields a
    window a few milliseconds long. Every episode of the class would then be
    right-censored or judged missed, uniformly, with no error anywhere.
    """

    revision_id = seed_revision_id(db)
    add_lease(db, "watcher/some", ttl_ms=60_000)
    add_scope(db, "scope/some", expected_interval_ms=30_000)

    with pytest.raises(SubjectRequired):
        window_for(
            db, revision_id, Episode("e", incident_class, PERIOD_START), grace_ms=0
        )


def test_resolve_budget_refuses_the_relative_budget_without_a_subject(db: Path) -> None:
    """The refusal is on the budget resolver itself, not only on the window."""

    revision_id = seed_revision_id(db)
    connection = open_for_measurement(db)
    try:
        with pytest.raises(SubjectRequired):
            resolve_budget_ms(
                connection,
                revision_id=revision_id,
                incident_class="lease_orphan",
                subject=None,
            )
    finally:
        connection.close()


def test_a_count_threshold_yields_no_tolerance_but_still_a_window(db: Path) -> None:
    """``watcher_error_streak``'s T is a count, and its window is still well defined.

    ``tolerance_ms`` is ``None`` and ``threshold_kind`` says why, so a consumer
    can tell "a count" from "policy said nothing". Refusing the window over the
    unavailable side quantity would make the class unmeasurable for a reason
    unrelated to measuring it -- its L is an absolute 10 minutes.
    """

    revision_id = seed_revision_id(db)

    window = window_for(
        db, revision_id, Episode("e", "watcher_error_streak", PERIOD_START), grace_ms=0
    )

    assert window.threshold_kind == "consecutive_count"
    assert window.tolerance_ms is None
    assert window.budget_ms == 600_000
    assert window.classification == IN_PERIOD


# --------------------------------------------------------------------------
# the buckets: distinguished, leaking into neither numerator, emitted at zero
# --------------------------------------------------------------------------


def test_censored_buckets_are_distinguished_and_leak_into_neither_numerator(
    db: Path,
) -> None:
    """One episode of each kind: three buckets, one numerator, no overlap.

    The numerator assertion is the load-bearing one. Section 3.5 excludes a
    censored episode from the miss numerator **and** the latency numerator, and
    a module that applied the exclusion to one only would report a latency
    distribution over episodes it had already agreed it could not judge.
    """

    revision_id = seed_revision_id(db)
    budget_ms = budget_ms_of(db, revision_id, ABSOLUTE_CLASS)
    grace_ms = 0

    episodes = [
        Episode("inside", ABSOLUTE_CLASS, PERIOD_START + 1_000),
        Episode("right", ABSOLUTE_CLASS, PERIOD_END - budget_ms - grace_ms + 1),
        Episode("left", ABSOLUTE_CLASS, PERIOD_START - 1),
    ]

    report = report_over(db, revision_id, episodes, grace_ms=grace_ms)

    assert report.counts() == {IN_PERIOD: 1, CENSORED: 1, CENSORED_LEFT: 1}
    assert report.ids_for(IN_PERIOD) == ("inside",)
    assert report.ids_for(CENSORED) == ("right",)
    assert report.ids_for(CENSORED_LEFT) == ("left",)
    assert report.numerator_ids() == ("inside",)

    # Every episode lands in exactly one bucket: the partition is a property of
    # the whole classification, not of any one branch, and a fourth case falling
    # through the bottom of the loop is invisible to the per-branch assertions.
    filed = [
        episode_id
        for classification in WINDOW_CLASSIFICATIONS
        for episode_id in report.ids_for(classification)
    ]
    assert sorted(filed) == sorted(episode.episode_id for episode in episodes)


def test_counts_are_emitted_even_at_zero(db: Path) -> None:
    """All three keys, always. An absent key reads as "nothing to report".

    It would mean "this report was produced by code that did not look", and the
    censored count is the number that makes a too-short period visible -- so a
    reader diffing two reports must see the zero.
    """

    revision_id = seed_revision_id(db)

    report = report_over(db, revision_id, [])

    assert report.counts() == {IN_PERIOD: 0, CENSORED: 0, CENSORED_LEFT: 0}
    assert set(report.counts()) == set(WINDOW_CLASSIFICATIONS)
    assert report.numerator_ids() == ()


def test_an_episode_outside_the_period_is_refused_not_censored(db: Path) -> None:
    """Neither filed as censored nor dropped.

    Filing it censored would inflate the one number that says "this period is
    too short for these budgets"; dropping it would let a selection bug live
    forever.
    """

    revision_id = seed_revision_id(db)
    budget_ms = budget_ms_of(db, revision_id, ABSOLUTE_CLASS)

    with pytest.raises(EpisodeOutsidePeriod):
        report_over(
            db, revision_id, [Episode("after", ABSOLUTE_CLASS, PERIOD_END)], grace_ms=0
        )

    with pytest.raises(EpisodeOutsidePeriod):
        report_over(
            db,
            revision_id,
            [Episode("before", ABSOLUTE_CLASS, PERIOD_START - budget_ms)],
            grace_ms=0,
        )


def test_duplicate_episode_ids_are_refused(db: Path) -> None:
    """One condition is one episode; a repeated id is two votes in one numerator."""

    revision_id = seed_revision_id(db)
    episode = Episode("e", ABSOLUTE_CLASS, PERIOD_START)

    with pytest.raises(DuplicateEpisodeRefused):
        report_over(db, revision_id, [episode, episode], grace_ms=0)


def test_an_empty_or_inverted_period_is_refused(db: Path) -> None:
    """Every episode would be censored, for a reason that is not censoring."""

    revision_id = seed_revision_id(db)

    with pytest.raises(PeriodRefused):
        report_over(
            db, revision_id, [], period_start_ms=PERIOD_END, period_end_ms=PERIOD_START
        )
    with pytest.raises(PeriodRefused):
        report_over(
            db, revision_id, [], period_start_ms=PERIOD_START, period_end_ms=PERIOD_START
        )
