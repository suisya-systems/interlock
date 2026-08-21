"""G6 -- AC-9's denominator: the cohort, and the four reasons a run is not in it.

The failure this module is written against is not a crash; it is a number that
was quietly never comparable to the thing it was compared against.
``docs/measurement-harness.md`` section 2.1 records the shape of it. AC-9 is
stated "per 100 worker runs", which is a normalisation and not a cohort, and the
design review found three defensible readings of "100 runs" -- started,
completed, canary-owned -- each producing a different denominator from the same
database. A rate whose denominator is decided by whoever wrote the query most
recently moves when nothing about the system moved, and the v1 baseline it is
compared against (195 *completed* runs normalised to roughly 1,576 dispatcher
ticks per 100 runs) is a completed-run figure, so a started-run cohort would not
be against that number at all.

``D-0038`` closed it, and this module is the closure in code:

    **The cohort is the runs whose entire lifetime falls inside the report
    period -- created at or after ``period_start_ms`` and terminal before
    ``period_end_ms`` -- and that were Interlock-owned throughout.**

**"Entire lifetime" is not a restatement of "terminal in period", and the
difference is the whole reason the clause is worded that way.** A run that
started before the window and finished inside it *is* terminal in the window,
and it is **excluded** -- it lands in :data:`STARTED_BEFORE_PERIOD`. Its prompts
lie on both sides of the boundary, so counting it puts a full run in the
denominator against a partial numerator, and the alternative (attributing
prompts to the window they happened in and the run to the window it finished in)
makes numerator and denominator count different things, which is how a rate
silently stops meaning anything. Stating the cohort as "terminal in period"
alone leaves both readings open and therefore leaves two denominators; the
lifetime clause is what makes ``started_before_period`` an exclusion rather than
a contradiction.

**A started-run cohort is right-censored by construction**, which is the other
half of section 2.1's argument and the reason a run still in flight at the
period's end is excluded too: it has produced some of its prompts and not
others, so counting it deflates the per-run figure by exactly the work it has
not done yet. With a median run of 0.66 h against periods measured in days the
bias is small, but it is always in the flattering direction, and a target must
not carry a bias that flatters it.

**Ownership is asserted, not assumed, and the assertion rests on ``D-0013``.**
Ownership is decided once, at run start, and the cutover happens at the run
boundary with no state conversion, so a run is Interlock-owned for its whole
life or for none of it -- "owned throughout" is automatic rather than a filter.
What that means here is concrete and worth stating plainly: **a row in this
database is itself the ownership assertion.** There is no ownership column to
read, because a v1-owned run never becomes a row here. The consequence is that
:data:`V1_OWNED` can never be derived from the ``run`` table -- deriving it
would mean inventing a distinction the schema deliberately does not carry -- so
:func:`select_cohort` takes the v1 shadow input as a parameter and refuses (see
:class:`OwnershipAssertionRefused`) if that input names a run this database also
holds. That refusal is the assertion being *checked*: two systems claiming one
run contradicts ``D-0013``, and a harness that silently excluded the row instead
would report a smaller denominator and no reason to doubt it.

**Excluded runs are not silently dropped** (section 2.1). Every run that touches
the period lands in exactly one place -- the cohort, or one of the four buckets
in :data:`EXCLUDED_REASONS` -- and all four buckets are emitted every time, even
empty. A reader diffing two reports must see a zero rather than a missing key:
an absent key reads as "nothing to report" when it means "this report was
produced by code that did not look".

**Nothing here writes and nothing here reads a clock.** The connection comes
from :func:`~claude_org_runtime.measurement.reader.open_for_measurement`, which
is read-only by capability rather than by this module's good behaviour, and
every bound -- ``period_start_ms``, ``period_end_ms``, ``now_ms`` -- is the
caller's (``time-base-policy.md`` section 2, rule 4: windows are half-open,
``[start, end)``, at both ends).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

# The terminal set is the schema's, not a copy of it. gates.py owns the
# constant because section 9.4's subject_gone sweep reads the same fact out of
# the same column, and a second copy here would agree with it right up until
# the day the vocabulary changed -- the one day terminal_status_unknown exists
# to notice. Importing a writer module is not a write capability: the harness's
# read-only property lives on the connection (reader.py), and this module never
# hands its connection to anything.
from claude_org_runtime.control_plane.gates import TERMINAL_RUN_STATUSES
from claude_org_runtime.measurement.reader import ControlPlaneRefusal

__all__ = [
    "COHORT_REASONS",
    "COHORT_RUNS_QUERY",
    "EXCLUDED_REASONS",
    "IN_FLIGHT_AT_PERIOD_END",
    "KNOWN_RUN_STATUSES",
    "OWNERSHIP_COLLISION_QUERY",
    "OwnershipAssertionRefused",
    "PeriodNotClosedRefused",
    "QUERY_DEFINITIONS",
    "RunCohort",
    "STARTED_BEFORE_PERIOD",
    "TERMINAL_RUN_STATUSES",
    "TERMINAL_STATUS_UNKNOWN",
    "UnknownRunStatusRefused",
    "V1_OWNED",
    "select_cohort",
    "terminal_instant_ms",
    "touches_period",
]


#: The statuses a run can hold, as the ``run`` table's own ``CHECK`` enumerates
#: them (``0001_initial.sql``, ``production-schema.md`` section 2). ``D-0041``
#: closed this set in DDL, which is what makes
#: :data:`TERMINAL_STATUS_UNKNOWN` a schema-integrity signal rather than a
#: routine bucket. The terminal half is imported rather than repeated;
#: ``test_cohort.py`` reads the ``CHECK`` clause out of a migrated database and
#: asserts this tuple equals it, so the copy cannot drift in silence.
KNOWN_RUN_STATUSES: tuple[str, ...] = (
    "created",
    "running",
    "suspended",
) + TERMINAL_RUN_STATUSES

#: The four excluded reasons of ``measurement-harness.md`` section 2.1, named
#: once so the report, the buckets and the tests cannot disagree about spelling.
IN_FLIGHT_AT_PERIOD_END = "in_flight_at_period_end"
STARTED_BEFORE_PERIOD = "started_before_period"
V1_OWNED = "v1_owned"
TERMINAL_STATUS_UNKNOWN = "terminal_status_unknown"

#: Emitted in this order, **always**, empty or not (see the module docstring's
#: last point on a missing key versus a zero).
EXCLUDED_REASONS: tuple[str, ...] = (
    IN_FLIGHT_AT_PERIOD_END,
    STARTED_BEFORE_PERIOD,
    V1_OWNED,
    TERMINAL_STATUS_UNKNOWN,
)

#: The buckets that partition the runs *this database holds* which touch the
#: period. :data:`V1_OWNED` is deliberately absent: it is not derived from the
#: ``run`` table at all (module docstring, ``D-0013``), so it is not part of
#: that partition and a test asserting the partition must not include it.
COHORT_REASONS: tuple[str, ...] = (
    IN_FLIGHT_AT_PERIOD_END,
    STARTED_BEFORE_PERIOD,
    TERMINAL_STATUS_UNKNOWN,
)


#: The statements this module executes, as the text that is **executed**.
#:
#: ``measurement-harness.md`` section 6 requires ``query_definitions`` to carry
#: "every query the report ran, as text ... so a reader can run them by hand".
#: A statement written inline at its call site cannot honour that -- the header
#: could only name a pasted copy, which is right on the day it is pasted and
#: goes on being printed after the executed text changes, certifying a query
#: that never ran. Lifted here and executed from here, the way
#: ``control_plane/events.py`` holds ``ORPHANED_OUTBOX_SQL``: a statement that
#: exists only inline can be changed without any test noticing.
#:
#: ``created_at_ms < :period_end_ms`` is the only bound SQL carries; the rest of
#: the walk goes through :func:`terminal_instant_ms` so the terminal-instant
#: derivation exists in exactly one place. ``ORDER BY run_id`` makes the report
#: byte-reproducible (``D-0040``).
COHORT_RUNS_QUERY = """
SELECT run_id, status, created_at_ms, updated_at_ms
  FROM run
 WHERE created_at_ms < :period_end_ms
 ORDER BY run_id
"""

#: ``{placeholders}`` expands to one ``?`` per shadow run id in the chunk.
#: SQLite has no parameter form for an ``IN`` list, so the placeholders are
#: generated and the ids are still bound -- no run id reaches the statement as
#: text. The catalogue carries the template, which is what a reader re-runs;
#: the expansion is mechanical.
OWNERSHIP_COLLISION_QUERY = "SELECT run_id FROM run WHERE run_id IN ({placeholders})"

QUERY_DEFINITIONS: Mapping[str, str] = MappingProxyType(
    {
        "cohort_runs": COHORT_RUNS_QUERY,
        "cohort_ownership_collision": OWNERSHIP_COLLISION_QUERY,
    }
)


class UnknownRunStatusRefused(ControlPlaneRefusal):
    """A ``run.status`` outside :data:`KNOWN_RUN_STATUSES` reached a caller.

    Its own type because the honest answer to "is this run terminal?" for an
    unrecognised status is neither yes nor no. Returning ``None`` -- "not
    terminal" -- would file the run as in-flight and hide a database whose
    ``CHECK`` this build does not share; returning a terminal instant would put
    a run of unknown shape into the denominator. :func:`select_cohort` catches
    this and files the run under :data:`TERMINAL_STATUS_UNKNOWN` instead, which
    is the one place in the harness that is allowed to have an answer for it.
    """


class PeriodNotClosedRefused(ControlPlaneRefusal):
    """The report period has not ended yet at *now_ms*, or is empty/inverted.

    A cohort over a period whose end is in the future is not merely provisional,
    it is wrong in a specific direction: every run still running would be filed
    ``in_flight_at_period_end`` on the strength of a period end that has not
    happened, and re-running the same report tomorrow would move runs out of
    that bucket and into the denominator. The rate would change with no change
    in the system, which is the defect this module exists to prevent, arriving
    through the clock instead of through the query.
    """


class OwnershipAssertionRefused(ControlPlaneRefusal):
    """The v1 shadow input named a run this database also holds.

    ``D-0013`` decides ownership once at run start and cuts over at the run
    boundary, so a run is v1's or Interlock's and never both. A row here *is*
    the claim that it is Interlock's (there is no ownership column), so a
    collision is two systems claiming one run -- a contradiction in the input,
    the schema, or the cutover, and the report cannot tell which. Excluding the
    row quietly would shrink the denominator and leave nothing anywhere saying
    why, so the harness stops instead.
    """


@dataclass(frozen=True)
class RunCohort:
    """AC-9's denominator, with the runs it left out and why.

    ``run_ids`` is the cohort. ``excluded`` always carries all four keys of
    :data:`EXCLUDED_REASONS`; ``D-0038`` makes the breakdown required output --
    "a reduction rate printed without them is not a valid report" -- so it is
    not optional here either.
    """

    period_start_ms: int
    period_end_ms: int
    run_ids: tuple[str, ...]
    excluded: Mapping[str, tuple[str, ...]]

    @property
    def denominator(self) -> int:
        """The number of runs AC-9's rate is normalised over."""

        return len(self.run_ids)

    def excluded_counts(self) -> Mapping[str, int]:
        """Per-reason counts, all four keys present even at zero.

        A zero and a missing key are different statements to a reader diffing
        two reports, and only one of them is the truth this harness has.
        """

        return MappingProxyType(
            {reason: len(self.excluded[reason]) for reason in EXCLUDED_REASONS}
        )


def terminal_instant_ms(status: str, updated_at_ms: int) -> int | None:
    """The instant *status*/*updated_at_ms* say the run terminated, or ``None``.

    **This is a derivation, resting on writer discipline, not a fact the schema
    enforces.** There is no terminal-timestamp column; the reasoning that lets
    ``updated_at_ms`` stand in for one is:

    1. ``status`` is the only mutable column on ``run`` -- ``run_id`` and
       ``created_at_ms`` are written once (``production-schema.md`` section 2),
       so every ``UPDATE`` that moves ``updated_at_ms`` is a status transition;
    2. the ``run_status_is_forward_only`` trigger (``0001_initial.sql``,
       ``D-0041``) refuses to leave ``completed``/``failed``/``cancelled``, so a
       terminal status is absorbing and no later transition can occur;
    3. therefore a terminal run's **last** mutation *is* its terminalisation,
       and ``updated_at_ms`` is the instant it happened.

    Step 1 is the assumption a schema change could invalidate without the
    trigger noticing -- add one more mutable column to ``run`` and a bump of it
    after termination would push this value forward, silently moving a run
    across a period boundary. That is why the reasoning lives **here, once**:
    every part of the harness that needs a terminal instant calls this function,
    so swapping to a dedicated ``terminated_at_ms`` column later is a one-place
    change and not a hunt through the report.

    :raises UnknownRunStatusRefused: if *status* is outside
        :data:`KNOWN_RUN_STATUSES`. No silent default: see that class.
    """

    if status not in KNOWN_RUN_STATUSES:
        raise UnknownRunStatusRefused(
            f"run.status {status!r} is outside the closed set "
            f"{', '.join(KNOWN_RUN_STATUSES)} that D-0041 put in the run "
            "table's CHECK; this build cannot say whether such a run is "
            "terminal, and will not guess in either direction"
        )
    if status not in TERMINAL_RUN_STATUSES:
        return None
    return updated_at_ms


def touches_period(
    status: str,
    created_at_ms: int,
    updated_at_ms: int,
    *,
    period_start_ms: int,
    period_end_ms: int,
) -> bool:
    """Does any part of this run's lifetime fall inside ``[start, end)``?

    The lifetime is ``created_at_ms`` up to the terminal instant, or up to
    "still going" for a run that has not terminated. Touching is what makes a
    run the report's business at all: a run that lies wholly outside the period
    appears in neither the cohort nor any bucket, because a bucket entry is a
    statement that the report considered the run and set it aside, and the
    report has nothing to say about a run that never overlapped its window.

    Both ends are the half-open ends of ``time-base-policy.md`` section 2 rule
    4: a run created exactly at ``period_end_ms`` belongs to the next period,
    and a run whose terminal instant is exactly ``period_start_ms`` did overlap
    this one (that instant is inside it) and is therefore considered -- and then
    excluded as :data:`STARTED_BEFORE_PERIOD`.

    A run whose status this build does not know has no computable terminal
    instant, so it is treated as unbounded above: it touches if it was created
    before the period ended. Erring that way puts it in front of the reader as
    :data:`TERMINAL_STATUS_UNKNOWN` instead of dropping the evidence.
    """

    if created_at_ms >= period_end_ms:
        return False
    try:
        terminal_ms = terminal_instant_ms(status, updated_at_ms)
    except UnknownRunStatusRefused:
        return True
    return terminal_ms is None or terminal_ms >= period_start_ms


def select_cohort(
    connection: sqlite3.Connection,
    *,
    period_start_ms: int,
    period_end_ms: int,
    now_ms: int,
    v1_shadow_run_ids: Iterable[str] = (),
) -> RunCohort:
    """AC-9's cohort over ``[period_start_ms, period_end_ms)``, with its exclusions.

    *connection* must be the read-only handle from
    :func:`~claude_org_runtime.measurement.reader.open_for_measurement`; this
    function issues one ``SELECT`` and nothing else.

    *v1_shadow_run_ids* is the v1 shadow input. It is a **parameter** because
    the :data:`V1_OWNED` bucket cannot be derived from this database at all --
    ``D-0013`` leaves no v1-owned run here to find (module docstring). Passing
    nothing therefore yields an empty ``v1_owned`` bucket, which is the honest
    answer for a report with no shadow input, not an assertion that no v1 run
    existed.

    Each touching run this database holds is filed in **exactly one** place, in
    this order, and the order is part of the contract because two reasons can
    apply at once:

    1. :data:`TERMINAL_STATUS_UNKNOWN` -- nothing else can be decided about a
       run whose status this build cannot interpret;
    2. :data:`IN_FLIGHT_AT_PERIOD_END` -- no terminal instant *before* the
       period's end. This is checked before ``started_before_period`` for a run
       that spans the whole window because right-censoring is the heavier
       disqualification: a partly-outside run has a known count that is wrong,
       an in-flight one has no final count at all;
    3. :data:`STARTED_BEFORE_PERIOD` -- terminal in the window, created before
       it opened;
    4. otherwise: the cohort.

    :raises PeriodNotClosedRefused: if the period is empty, inverted, or has not
        ended at *now_ms*.
    :raises OwnershipAssertionRefused: if the shadow input names a run held
        here.
    """

    if period_end_ms <= period_start_ms:
        raise PeriodNotClosedRefused(
            f"the report period [{period_start_ms}, {period_end_ms}) is empty or "
            "inverted; a half-open window must have an end strictly after its "
            "start (time-base-policy.md section 2, rule 4)"
        )
    if now_ms < period_end_ms:
        raise PeriodNotClosedRefused(
            f"the report period [{period_start_ms}, {period_end_ms}) has not "
            f"ended at now_ms={now_ms}; the cohort would count runs as in flight "
            "at an end that has not happened, and the same report run again "
            "later would move them into the denominator (D-0038)"
        )

    shadow = tuple(sorted(set(v1_shadow_run_ids)))
    _assert_no_run_is_claimed_by_both(connection, shadow)

    buckets: dict[str, list[str]] = {reason: [] for reason in EXCLUDED_REASONS}
    buckets[V1_OWNED].extend(shadow)
    cohort: list[str] = []

    # The statement is COHORT_RUNS_QUERY rather than a literal here so that the
    # provenance header names the text that ran (section 6); see that constant
    # for why the window bound is the only one SQL carries.
    rows = connection.execute(
        COHORT_RUNS_QUERY, {"period_end_ms": period_end_ms}
    ).fetchall()

    for run_id, status, created_at_ms, updated_at_ms in rows:
        if not touches_period(
            status,
            created_at_ms,
            updated_at_ms,
            period_start_ms=period_start_ms,
            period_end_ms=period_end_ms,
        ):
            continue
        try:
            terminal_ms = terminal_instant_ms(status, updated_at_ms)
        except UnknownRunStatusRefused:
            # D-0041 closed the CHECK, so this bucket should stay empty and a
            # non-zero count here is a schema-integrity signal -- a database
            # written by a build with a wider vocabulary, or a CHECK dropped by
            # hand -- rather than routine noise. It is still emitted at zero,
            # unconditionally, so that a reader diffing two reports sees the
            # zero and knows the check ran.
            buckets[TERMINAL_STATUS_UNKNOWN].append(run_id)
            continue
        if terminal_ms is None or terminal_ms >= period_end_ms:
            buckets[IN_FLIGHT_AT_PERIOD_END].append(run_id)
        elif created_at_ms < period_start_ms:
            buckets[STARTED_BEFORE_PERIOD].append(run_id)
        else:
            cohort.append(run_id)

    return RunCohort(
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        run_ids=tuple(cohort),
        excluded=MappingProxyType(
            {reason: tuple(buckets[reason]) for reason in EXCLUDED_REASONS}
        ),
    )


def _assert_no_run_is_claimed_by_both(
    connection: sqlite3.Connection, shadow: Sequence[str]
) -> None:
    """Refuse if the shadow input names a run this database holds.

    This is the ownership assertion of ``D-0013`` being checked rather than
    recited (:class:`OwnershipAssertionRefused`). The ids are chunked because
    SQLite's default parameter ceiling is 999 and a shadow input is a list of
    whatever length v1 hands over; a query that worked in testing and failed on
    the first real period would be a poor place to discover that.
    """

    collisions: list[str] = []
    for start in range(0, len(shadow), 500):
        chunk = shadow[start : start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        collisions.extend(
            row[0]
            for row in connection.execute(
                OWNERSHIP_COLLISION_QUERY.format(placeholders=placeholders), chunk
            )
        )
    if collisions:
        raise OwnershipAssertionRefused(
            "the v1 shadow input names "
            f"{', '.join(sorted(collisions))}, which this Interlock database "
            "also holds; a run row here is itself the assertion that the run is "
            "Interlock-owned (D-0013 decides ownership once at run start and "
            "cuts over at the run boundary), so one run claimed by both systems "
            "is a contradiction the report cannot resolve by picking a side"
        )
