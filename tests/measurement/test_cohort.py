"""The denominator, taken apart: both half-open ends, the four buckets, and the partition.

``docs/measurement-harness.md`` section 2.1 is emphatic that "entire lifetime" is
not a restatement of "terminal in period", and a test suite that only builds
runs comfortably inside the window would pass under either reading -- which is
the defect, not the fix. So the cases here are the ones that separate the two
readings: a run terminal *inside* the period but created *before* it, and runs
sitting exactly on each boundary instant.

The partition test is the load-bearing one. ``Excluded runs are not silently
dropped`` is a property of the whole classification and not of any one branch,
so it is asserted as a property -- every touching run appears exactly once
across the cohort and the buckets -- over a population built to hit every branch
at once. A per-branch test can stay green while a fifth case falls through the
bottom of the loop.

Nothing here re-implements the classification to check it against: the
vocabulary test reads the ``CHECK`` clause out of a real migrated database and
holds the module's own tuple against it, and every other test states expected
membership by hand.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.measurement.cohort import (
    EXCLUDED_REASONS,
    IN_FLIGHT_AT_PERIOD_END,
    KNOWN_RUN_STATUSES,
    STARTED_BEFORE_PERIOD,
    TERMINAL_STATUS_UNKNOWN,
    V1_OWNED,
    OwnershipAssertionRefused,
    PeriodNotClosedRefused,
    UnknownRunStatusRefused,
    select_cohort,
    terminal_instant_ms,
    touches_period,
)
from claude_org_runtime.measurement.reader import open_for_measurement

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

#: One day, so the boundary instants below are unmistakably distinct from the
#: durations in between.
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS
#: The report is produced after the period closed; select_cohort refuses
#: otherwise, and every test that is not about that refusal uses this.
NOW = PERIOD_END + 1


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    return path


def add_runs(path: Path, *rows: tuple[str, str, int, int]) -> None:
    """Insert ``(run_id, status, created_at_ms, updated_at_ms)`` rows.

    Through an ordinary writable connection, deliberately: the harness's own
    handle cannot write, which is the point of it.
    """

    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.executemany(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
    finally:
        connection.close()


def cohort_of(path: Path, **kwargs):
    connection = open_for_measurement(path)
    try:
        return select_cohort(
            connection,
            period_start_ms=kwargs.pop("period_start_ms", PERIOD_START),
            period_end_ms=kwargs.pop("period_end_ms", PERIOD_END),
            now_ms=kwargs.pop("now_ms", NOW),
            **kwargs,
        )
    finally:
        connection.close()


def widen_the_status_check(path: Path) -> None:
    """Remove the ``status IN (...)`` CHECK, to build a row this build cannot read.

    ``D-0041`` closed that set in DDL, so a status outside it is unreachable
    through the schema -- which is exactly why ``terminal_status_unknown``
    should stay empty. Proving the bucket still *works* therefore requires
    forging the condition it watches for: a database written by a build with a
    wider vocabulary, or a CHECK dropped by hand. ``writable_schema`` is how
    that is reproduced without shipping a second schema.
    """

    connection = sqlite3.connect(path, isolation_level=None)
    try:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'run'"
        ).fetchone()[0]
        widened = re.sub(r",\s*CHECK \(status IN \([^)]*\)\)", "", sql)
        assert widened != sql, "the CHECK clause was not found to remove"
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'run'",
            (widened,),
        )
        connection.execute("PRAGMA writable_schema = OFF")
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the vocabulary is the schema's, not a copy that drifts
# --------------------------------------------------------------------------


def test_the_known_statuses_are_exactly_the_run_tables_own_check(db: Path):
    """KNOWN_RUN_STATUSES equals the CHECK in the migrated database.

    The module keeps a tuple of status names, and the whole meaning of
    ``terminal_status_unknown`` rests on that tuple being the schema's set. A
    test asserting the tuple against a second hand-written tuple would agree
    with itself forever, so this one reads the DDL that shipped.
    """

    connection = open_for_measurement(db)
    try:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'run'"
        ).fetchone()[0]
    finally:
        connection.close()
    clause = re.search(r"CHECK \(status IN \(([^)]*)\)\)", sql)
    assert clause is not None
    in_ddl = tuple(re.findall(r"'([a-z_]+)'", clause.group(1)))
    assert set(in_ddl) == set(KNOWN_RUN_STATUSES)


# --------------------------------------------------------------------------
# the terminal instant, and the derivation it rests on
# --------------------------------------------------------------------------


def test_the_terminal_instant_is_updated_at_for_a_terminal_run():
    for status in ("completed", "failed", "cancelled"):
        assert terminal_instant_ms(status, T0 + 5) == T0 + 5


def test_a_run_that_has_not_terminated_has_no_terminal_instant():
    for status in ("created", "running", "suspended"):
        assert terminal_instant_ms(status, T0 + 5) is None


def test_an_unknown_status_is_refused_rather_than_read_as_in_flight():
    """Neither answer is available, so neither is invented.

    Returning ``None`` would be the convenient default and it is the dangerous
    one: it files the run as in flight, and a database whose CHECK this build
    does not share would report as an ordinary period with some long-running
    work in it.
    """

    with pytest.raises(UnknownRunStatusRefused):
        terminal_instant_ms("zombie", T0)


# --------------------------------------------------------------------------
# both ends of the half-open period
# --------------------------------------------------------------------------


def test_a_run_created_exactly_at_the_period_start_is_in_the_cohort(db: Path):
    """``[start, end)`` includes its start instant (time-base-policy.md 2.4)."""

    add_runs(db, ("on-start", "completed", PERIOD_START, PERIOD_START + 10))
    assert cohort_of(db).run_ids == ("on-start",)


def test_a_run_created_one_millisecond_before_the_start_is_excluded(db: Path):
    """The neighbouring instant is on the other side, and the bucket says why."""

    add_runs(db, ("just-before", "completed", PERIOD_START - 1, PERIOD_START + 10))
    result = cohort_of(db)
    assert result.run_ids == ()
    assert result.excluded[STARTED_BEFORE_PERIOD] == ("just-before",)


def test_a_run_terminal_one_millisecond_before_the_end_is_in_the_cohort(db: Path):
    add_runs(db, ("just-inside", "completed", PERIOD_START + 1, PERIOD_END - 1))
    assert cohort_of(db).run_ids == ("just-inside",)


def test_a_run_terminal_exactly_at_the_period_end_is_excluded(db: Path):
    """``[start, end)`` excludes its end instant, so this run is not yet done.

    "Terminal *before* ``period_end_ms``" is the wording of section 2.1, and a
    closed upper end would put this run in two consecutive periods.
    """

    add_runs(db, ("on-end", "completed", PERIOD_START + 1, PERIOD_END))
    result = cohort_of(db)
    assert result.run_ids == ()
    assert result.excluded[IN_FLIGHT_AT_PERIOD_END] == ("on-end",)


def test_a_run_created_exactly_at_the_period_end_belongs_to_the_next_period(db: Path):
    add_runs(db, ("next-period", "running", PERIOD_END, PERIOD_END))
    result = cohort_of(db)
    assert result.run_ids == ()
    assert result.excluded_counts() == {reason: 0 for reason in EXCLUDED_REASONS}


# --------------------------------------------------------------------------
# the distinction section 2.1 insists on
# --------------------------------------------------------------------------


def test_terminal_in_the_period_but_started_before_it_is_not_the_cohort(db: Path):
    """The case that separates "entire lifetime" from "terminal in period".

    Under the rejected reading this run is in the denominator; under ``D-0038``
    it is an exclusion with a reason, because its prompts lie on both sides of
    the boundary and counting it puts a whole run against a partial numerator.
    """

    add_runs(db, ("crosses-in", "completed", PERIOD_START - DAY_MS, PERIOD_START + 60))
    result = cohort_of(db)
    assert result.run_ids == ()
    assert result.denominator == 0
    assert result.excluded[STARTED_BEFORE_PERIOD] == ("crosses-in",)
    assert result.excluded[IN_FLIGHT_AT_PERIOD_END] == ()


def test_a_run_still_in_flight_is_bucketed_not_counted(db: Path):
    """Right-censoring: it has produced some of its prompts and not the rest."""

    add_runs(
        db,
        ("still-running", "running", PERIOD_START + 5, PERIOD_END - 5),
        ("still-suspended", "suspended", PERIOD_START + 5, PERIOD_END - 5),
        ("never-started", "created", PERIOD_START + 5, PERIOD_START + 5),
    )
    result = cohort_of(db)
    assert result.run_ids == ()
    assert result.excluded[IN_FLIGHT_AT_PERIOD_END] == (
        "never-started",
        "still-running",
        "still-suspended",
    )


def test_a_run_spanning_the_whole_period_is_in_flight_at_its_end(db: Path):
    """Two reasons apply; the stated order files it under the heavier one."""

    add_runs(db, ("spans", "completed", PERIOD_START - 10, PERIOD_END + 10))
    result = cohort_of(db)
    assert result.excluded[IN_FLIGHT_AT_PERIOD_END] == ("spans",)
    assert result.excluded[STARTED_BEFORE_PERIOD] == ()


# --------------------------------------------------------------------------
# runs the report has nothing to say about
# --------------------------------------------------------------------------


def test_a_run_wholly_outside_the_period_appears_nowhere(db: Path):
    """Not in the cohort and not in a bucket: it never overlapped the window.

    A bucket entry is the statement "the report considered this run and set it
    aside", which would be a false statement here, and a bucket that filled up
    with the entire history of the database would bury the exclusions that do
    matter.
    """

    add_runs(
        db,
        ("ancient", "completed", PERIOD_START - 10 * DAY_MS, PERIOD_START - DAY_MS),
        ("ended-on-the-start-boundary", "completed", PERIOD_START - DAY_MS, PERIOD_START - 1),
        ("future", "completed", PERIOD_END + DAY_MS, PERIOD_END + 2 * DAY_MS),
    )
    result = cohort_of(db, now_ms=PERIOD_END + 10 * DAY_MS)
    assert result.run_ids == ()
    assert result.excluded_counts() == {reason: 0 for reason in EXCLUDED_REASONS}


# --------------------------------------------------------------------------
# the partition property
# --------------------------------------------------------------------------


def test_every_touching_run_lands_in_exactly_one_place(db: Path):
    """Cohort plus buckets account for every touching run, once each.

    Built to hit every branch in one population, because the property under
    test is about the classification as a whole: a run falling out of the
    bottom of the loop, or counted twice by two overlapping predicates, is
    invisible to any single-branch test.
    """

    rows = (
        ("a-inside", "completed", PERIOD_START, PERIOD_END - 1),
        ("b-inside-failed", "failed", PERIOD_START + 1, PERIOD_START + 2),
        ("c-inside-cancelled", "cancelled", PERIOD_START + 3, PERIOD_START + 4),
        ("d-crosses-in", "completed", PERIOD_START - 1, PERIOD_START + 4),
        ("e-crosses-out", "completed", PERIOD_START + 1, PERIOD_END),
        ("f-running", "running", PERIOD_START + 1, PERIOD_START + 9),
        ("g-spans", "completed", PERIOD_START - DAY_MS, PERIOD_END + DAY_MS),
        ("h-terminal-on-start", "completed", PERIOD_START - DAY_MS, PERIOD_START),
        ("x-before", "completed", PERIOD_START - DAY_MS, PERIOD_START - 1),
        ("y-after", "created", PERIOD_END, PERIOD_END),
    )
    add_runs(db, *rows)
    widen_the_status_check(db)
    add_runs(db, ("i-unknown", "zombie", PERIOD_START + 1, PERIOD_START + 2))

    result = cohort_of(db, now_ms=PERIOD_END + DAY_MS + 1)

    touching = {
        run_id
        for run_id, status, created, updated in rows + (
            ("i-unknown", "zombie", PERIOD_START + 1, PERIOD_START + 2),
        )
        if touches_period(
            status,
            created,
            updated,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
        )
    }
    assert touching == {
        "a-inside",
        "b-inside-failed",
        "c-inside-cancelled",
        "d-crosses-in",
        "e-crosses-out",
        "f-running",
        "g-spans",
        "h-terminal-on-start",
        "i-unknown",
    }

    placed = list(result.run_ids)
    for reason in EXCLUDED_REASONS:
        placed.extend(result.excluded[reason])
    # No omission, and -- because a list is compared against a set of the same
    # length -- no double counting either.
    assert len(placed) == len(touching)
    assert set(placed) == touching

    assert result.run_ids == ("a-inside", "b-inside-failed", "c-inside-cancelled")
    assert result.excluded[IN_FLIGHT_AT_PERIOD_END] == ("e-crosses-out", "f-running", "g-spans")
    assert result.excluded[STARTED_BEFORE_PERIOD] == ("d-crosses-in", "h-terminal-on-start")
    assert result.excluded[TERMINAL_STATUS_UNKNOWN] == ("i-unknown",)


# --------------------------------------------------------------------------
# the buckets are always emitted
# --------------------------------------------------------------------------


def test_all_four_buckets_are_emitted_over_an_empty_database(db: Path):
    """A zero and a missing key are different statements to a reader.

    Only one of them is true of a report that ran the check and found nothing.
    """

    result = cohort_of(db)
    assert tuple(result.excluded) == EXCLUDED_REASONS
    assert all(result.excluded[reason] == () for reason in EXCLUDED_REASONS)
    assert result.excluded_counts() == {reason: 0 for reason in EXCLUDED_REASONS}


def test_terminal_status_unknown_is_a_schema_integrity_signal(db: Path):
    """Non-zero only when a status escaped the CHECK D-0041 closed."""

    add_runs(db, ("ok", "completed", PERIOD_START, PERIOD_START + 1))
    widen_the_status_check(db)
    add_runs(db, ("weird", "half-done", PERIOD_START, PERIOD_START + 1))
    result = cohort_of(db)
    assert result.run_ids == ("ok",)
    assert result.excluded[TERMINAL_STATUS_UNKNOWN] == ("weird",)


# --------------------------------------------------------------------------
# ownership: asserted, never derived
# --------------------------------------------------------------------------


def test_v1_owned_is_empty_without_a_shadow_input_however_many_runs_exist(db: Path):
    """D-0013 leaves no v1-owned run in this database to find.

    An empty bucket here is the honest answer for a report with no shadow
    input, and any non-empty one would mean the harness invented the
    distinction the schema deliberately does not carry.
    """

    add_runs(
        db,
        ("one", "completed", PERIOD_START, PERIOD_START + 1),
        ("two", "running", PERIOD_START, PERIOD_START + 1),
    )
    assert cohort_of(db).excluded[V1_OWNED] == ()


def test_v1_owned_is_populated_only_from_the_supplied_shadow_input(db: Path):
    add_runs(db, ("ours", "completed", PERIOD_START, PERIOD_START + 1))
    result = cohort_of(db, v1_shadow_run_ids=["v1-b", "v1-a", "v1-a"])
    assert result.excluded[V1_OWNED] == ("v1-a", "v1-b")
    assert result.run_ids == ("ours",)


def test_a_shadow_id_this_database_also_holds_is_refused(db: Path):
    """One run claimed by two systems contradicts D-0013's run-boundary cutover.

    Excluding the row quietly would shrink the denominator with nothing
    anywhere saying why, which is the class of silent movement this module
    exists to prevent.
    """

    add_runs(db, ("disputed", "completed", PERIOD_START, PERIOD_START + 1))
    with pytest.raises(OwnershipAssertionRefused) as refusal:
        cohort_of(db, v1_shadow_run_ids=["disputed"])
    assert "disputed" in str(refusal.value)


# --------------------------------------------------------------------------
# the period bounds themselves
# --------------------------------------------------------------------------


def test_a_period_that_has_not_ended_is_refused(db: Path):
    """The same report run again later would move runs into the denominator."""

    with pytest.raises(PeriodNotClosedRefused):
        cohort_of(db, now_ms=PERIOD_END - 1)


def test_the_instant_the_period_closes_is_already_reportable(db: Path):
    """``now_ms == period_end_ms`` is closed: the window is half-open at the end."""

    assert cohort_of(db, now_ms=PERIOD_END).run_ids == ()


def test_an_empty_or_inverted_period_is_refused(db: Path):
    with pytest.raises(PeriodNotClosedRefused):
        cohort_of(db, period_end_ms=PERIOD_START)
    with pytest.raises(PeriodNotClosedRefused):
        cohort_of(db, period_start_ms=PERIOD_END, period_end_ms=PERIOD_START)


# --------------------------------------------------------------------------
# the instrument does not disturb what it measures
# --------------------------------------------------------------------------


def test_selecting_the_cohort_writes_nothing(db: Path):
    """Read-only is the connection's capability; this proves the module uses it.

    A write attempted through the harness handle would raise rather than land,
    so a green run of the tests above is already evidence -- this asserts the
    file's bytes directly so the evidence does not depend on that inference.
    """

    add_runs(db, ("one", "completed", PERIOD_START, PERIOD_START + 1))
    before = db.read_bytes()
    cohort_of(db, v1_shadow_run_ids=["v1-a"])
    assert db.read_bytes() == before
