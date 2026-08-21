"""G6 -- the observation window, and the two ways a report boundary invents a miss.

The failure this module is written against is a rate that moves when nothing in
the system moved. ``docs/measurement-harness.md`` section 3.5 names it: an
episode detected fifteen seconds after the report period ended is, to a harness
that judges every episode against the period it happens to fall in, a **miss**
-- and it is not one. The detector met its budget; the report simply stopped
watching first. Worse, the defect is not a constant offset: the shorter the
period, the larger the fraction of episodes whose budget outlives it, so the
manufactured miss rate **rises as the period shortens**. A weekly report and a
daily report over the same week would disagree, and the daily one would look
worse, with nothing anywhere saying why.

The rule that removes it, verbatim from section 3.5:

    Every episode gets a window ``[onset, onset + L_class + grace)``, half-open,
    with ``grace`` a single declared value per report. **An episode whose window
    is not fully inside the report period is censored: excluded from the miss
    and latency numerators, counted in its own bucket, and reported.** The
    mirror case -- an episode whose onset precedes the period -- is excluded the
    same way and counted as ``censored_left``.

**Both ends exist, and they are different facts.** A right-censored episode has
a trustworthy onset and an unfinished budget, so the report cannot yet say
whether it was detected in time. A left-censored one is worse: its onset is
outside the window the report read, so the latency it would compute is measured
from an instant it did not observe. Collapsing the two into one bucket would
hide which end of the period is too tight, so :data:`CENSORED` and
:data:`CENSORED_LEFT` are counted separately and an episode that is both is
filed left (see :func:`classify`).

**The censored counts are required output, at zero as much as at a thousand.**
They are not diagnostics for whoever is debugging the harness -- they are the
one number that makes a *period too short for the budgets it is judging*
visible. A report whose censored episodes are a large fraction of its total is
judging an ``L`` of ten minutes over a window that barely holds one, and its
miss rate is an artefact of that. Printing the miss rate without the censored
count leaves the reader no way to tell that report from a good one, so
:meth:`WindowReport.counts` always emits all three keys and never omits a bucket
for being empty.

**Grace is policy data, never a constant here.** It defaults to one reconcile
period, because an episode must not be judged a miss for losing a race with the
pass that would have caught it -- and that period is read from
``policy_detection_latency.reconcile_period_ms`` for the caller-resolved
revision (:func:`default_grace_ms`), not written into this file. ``D-0031`` puts
every tolerance and interval in versioned data precisely so a past report can be
recomputed under the numbers it was actually judged by; a ``120_000`` typed here
would be a policy decision that no ``policy_revision`` records and no new
revision can change.

**``L`` is resolved, never assumed.** ``time-base-policy.md`` section 3.2 makes
three of the ten classes relative: ``watcher_silence``'s ``T`` scales with *that
scope's* poll interval, ``lease_orphan``'s ``T`` **and** ``L`` with *that
lease's* own TTL, and ``watcher_error_streak``'s ``T`` is a count with no
duration in it at all. So the window's length depends on the subject, the
subject is an explicit parameter, and an absent one is
:class:`SubjectRequired` rather than a fallback -- the only fallback available
is the bare multiple, which would give ``lease_orphan`` a two-millisecond window
and censor nothing while marking everything a miss.

**Nothing here writes, and nothing here reads a clock.** The connection is the
read-only handle from :func:`~claude_org_runtime.measurement.reader.open_for_measurement`;
the period bounds and every onset are the caller's. Windows and the period are
half-open at both ends (``time-base-policy.md`` section 2, rule 4), which is
what lets an episode ending exactly at ``period_end_ms`` be inside the report
and an episode onsetting exactly at ``period_end_ms`` be outside it, with no
instant belonging to two periods.

**Scope.** This module classifies. It does not decide whether an episode was
detected, and it raises no incident and applies no remedy: the fixture
evaluator, the shadow reconciliation and the latency report consume these
windows and own those questions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from claude_org_runtime.control_plane import policy
from claude_org_runtime.measurement.reader import ControlPlaneRefusal

__all__ = [
    "CENSORED",
    "CENSORED_LEFT",
    "DuplicateEpisodeRefused",
    "Episode",
    "EpisodeOutsidePeriod",
    "EpisodeWindow",
    "GraceNotDeclared",
    "GRACE_DECLARED",
    "GRACE_REVISION_RECONCILE_PERIOD",
    "IN_PERIOD",
    "PeriodRefused",
    "SubjectRequired",
    "WINDOW_CLASSIFICATIONS",
    "WindowReport",
    "WindowRefusal",
    "classify",
    "classify_episodes",
    "default_grace_ms",
    "episode_window",
    "resolve_budget_ms",
]


#: The three places an episode can land. Emitted in this order, **always**, and
#: empty buckets are emitted too (module docstring: a zero and a missing key are
#: different statements, and only one of them is the truth this harness has).
IN_PERIOD = "in_period"
CENSORED = "censored"
CENSORED_LEFT = "censored_left"

WINDOW_CLASSIFICATIONS: tuple[str, ...] = (IN_PERIOD, CENSORED, CENSORED_LEFT)

#: How the report's single grace value was arrived at. Recorded on the report
#: because ``D-0040`` makes every number a report was computed with part of the
#: report: a reader who cannot tell a declared grace from the revision's own
#: reconcile period cannot recompute the classification.
GRACE_DECLARED = "declared"
GRACE_REVISION_RECONCILE_PERIOD = "revision_reconcile_period"


class WindowRefusal(ControlPlaneRefusal):
    """A window that cannot be computed or classified, stated rather than guessed."""


class GraceNotDeclared(WindowRefusal):
    """The report's single grace value is neither declared nor derivable.

    Section 3.5 makes grace *one* value per report, and its default is "one
    reconcile period". A revision whose classes carry more than one
    ``reconcile_period_ms`` (section 3.3 permits a coarse class) has no single
    such period, and picking one would be a policy decision made by this file:
    the smallest manufactures misses for the coarse class, the largest excuses
    real ones for the tight classes. The caller declares the value, and the
    report records which it was.
    """


class SubjectRequired(WindowRefusal):
    """A relative class was asked for a window with no subject to scale it.

    ``watcher_silence`` is three of *that scope's* polls; ``lease_orphan``'s
    budget is twice *that lease's* TTL. Without the subject the only number
    available is the bare multiple -- 3, or 2 -- and using it yields a window a
    few milliseconds long, which censors nothing and calls every episode a miss.
    """


class DuplicateEpisodeRefused(WindowRefusal):
    """One ``episode_id`` was handed to a report twice.

    Section 3.3's correlation keys are what an episode id is built from, and a
    positional key (the nth escalation of a run) can collide when the two
    systems disagree about ordering -- the very divergence the report exists to
    surface. Counting the collision twice would report it as two episodes and
    move both numerators; refusing shows it as what it is.
    """


class EpisodeOutsidePeriod(WindowRefusal):
    """An episode was handed to a report whose period it does not touch at all.

    Deliberately not filed as ``censored``. The censored count is the signal
    that *this period is too short for these budgets* (module docstring), and
    padding it with episodes that have nothing to do with the period destroys
    exactly that signal -- a report over an unrelated week would show a high
    censored fraction and read as a period problem. Nor is the episode dropped:
    a silent drop is how a selection bug survives, so the caller's selection is
    refused instead.
    """


class PeriodRefused(WindowRefusal):
    """The report period is empty or inverted.

    ``[start, end)`` with ``end <= start`` contains no instant, so every episode
    would be censored and the censored fraction -- the one number that says the
    period is too short -- would be 100% for a reason that is not about
    censoring at all.
    """


@dataclass(frozen=True)
class Episode:
    """One real-world condition, as the report was handed it.

    *subject* is the identity a relative class scales by: a
    ``watcher_scope.scope_id`` for ``scope_interval_multiple``, a
    ``lease.resource`` for ``lease_ttl_multiple``. It is an explicit field and
    not an inference from *episode_id*, because a guessed subject resolves to
    some other subject's interval and the window is then wrong by a factor
    nobody can see.
    """

    episode_id: str
    incident_class: str
    onset_ms: int
    subject: str | None = None


@dataclass(frozen=True)
class EpisodeWindow:
    """``[onset_ms, end_ms)`` for one episode, and where the period puts it.

    ``tolerance_ms`` is ``T`` where ``T`` is a duration and ``None`` where it is
    a count (``watcher_error_streak``). It is carried because
    ``time-base-policy.md`` section 3.4 asks a report to read an alarm's age
    against **both** ``T`` and ``L`` -- a detection at ``T + epsilon`` is prompt,
    one past ``L`` is a regression -- and a consumer that had only ``L`` could
    not make that distinction without resolving policy a second time.
    """

    episode_id: str
    incident_class: str
    subject: str | None
    onset_ms: int
    threshold_kind: str
    tolerance_ms: int | None
    budget_ms: int
    grace_ms: int
    end_ms: int
    classification: str

    @property
    def censored(self) -> bool:
        """Is this episode excluded from both numerators?

        One predicate rather than two, because the two exclusions are the same
        exclusion: section 3.5 removes a censored episode from the miss
        numerator *and* the latency numerator, and a consumer that applied it to
        one only would report a latency distribution over episodes it had
        already agreed it could not judge.
        """

        return self.classification != IN_PERIOD


@dataclass(frozen=True)
class WindowReport:
    """Every episode classified, with the numbers the classification rests on.

    ``grace_ms`` and ``grace_source`` are on the report and not left implicit:
    the same episodes under a different grace classify differently, so a report
    that did not state its grace could not be recomputed (``D-0040``), and
    section 3.5's "a single declared value per report" is only checkable if the
    value is written down.
    """

    period_start_ms: int
    period_end_ms: int
    revision_id: int
    grace_ms: int
    grace_source: str
    windows: tuple[EpisodeWindow, ...]

    def counts(self) -> Mapping[str, int]:
        """Per-bucket counts, all three keys present **even at zero**.

        Required output, not a convenience: see the module docstring on why the
        censored count is what makes a too-short period visible, and why an
        absent key reads as "nothing to report" when it means "this report was
        produced by code that did not look".
        """

        tally = {name: 0 for name in WINDOW_CLASSIFICATIONS}
        for window in self.windows:
            tally[window.classification] += 1
        return MappingProxyType(tally)

    def numerator_ids(self) -> tuple[str, ...]:
        """The episodes the miss and latency numerators may both draw from.

        The same tuple answers both questions on purpose (see
        :attr:`EpisodeWindow.censored`).
        """

        return tuple(
            window.episode_id for window in self.windows if not window.censored
        )

    def ids_for(self, classification: str) -> tuple[str, ...]:
        """The episode ids in one bucket, in the order they were classified."""

        if classification not in WINDOW_CLASSIFICATIONS:
            raise WindowRefusal(
                f"{classification!r} is not one of "
                f"{', '.join(WINDOW_CLASSIFICATIONS)}"
            )
        return tuple(
            window.episode_id
            for window in self.windows
            if window.classification == classification
        )


def default_grace_ms(connection: sqlite3.Connection, *, revision_id: int) -> int:
    """One reconcile period, as *this revision* declares it.

    Section 3.5's default is "one reconcile period", and section 3.3 puts that
    period in ``policy_detection_latency.reconcile_period_ms`` -- per class,
    explicitly, so that the ``T + P <= L`` invariant can be a ``CHECK`` rather
    than a convention. The seed sets every row to 120 s, which is why a single
    value exists to return at all; a revision that moved one class to a coarser
    pass has no single reconcile period, and this function refuses rather than
    choosing (:class:`GraceNotDeclared`).

    Reading it from the revision the caller resolved is the whole point:
    ``D-0031`` exists so that changing a number is a new ``policy_revision`` and
    a past report recomputes under the numbers it was judged by. A constant here
    would be a number no revision records.
    """

    periods = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT reconcile_period_ms
              FROM policy_detection_latency
             WHERE revision_id = ?
             ORDER BY reconcile_period_ms ASC
            """,
            (revision_id,),
        ).fetchall()
    ]
    if not periods:
        raise GraceNotDeclared(
            f"revision {revision_id} decides no detection latency rows, so it "
            "declares no reconcile period for grace to default to"
        )
    if len(periods) > 1:
        raise GraceNotDeclared(
            f"revision {revision_id} declares {len(periods)} reconcile periods "
            f"({', '.join(str(period) for period in periods)} ms), so 'one "
            "reconcile period' names no single value; declare grace_ms "
            "explicitly for this report (measurement-harness.md section 3.5)"
        )
    return periods[0]


def resolve_budget_ms(
    connection: sqlite3.Connection,
    *,
    revision_id: int,
    incident_class: str,
    subject: str | None,
) -> int:
    """``L`` for this class and subject, in milliseconds.

    ``budget_kind`` is why this is a function and not a column read.
    ``lease_orphan``'s ``L`` is *twice the lease's own TTL*
    (``0002_policy_seed.sql``: ``budget_ms`` is the multiple ``2``, not a
    duration), and reading that row as 2 ms would give the class a window
    shorter than the clock tick that opens it.

    The subject's unit comes from :func:`policy.subject_unit_ms`, which owns the
    mapping from a relative kind to the table its unit lives in. Calling it is
    deliberate: a second copy of the ``lease`` / ``watcher_scope`` lookup here
    would agree with policy exactly until the day one of those units changed,
    and then the tolerance and the budget would be scaled by different numbers
    for the same subject -- silently, since nothing compares the two scalings.
    """

    row = policy.detection_latency(
        connection, revision_id=revision_id, incident_class=incident_class
    )
    budget_kind = row["budget_kind"]
    budget_value = int(row["budget_ms"])
    if budget_kind == "absolute_ms":
        return budget_value
    if subject is None:
        raise SubjectRequired(
            f"{incident_class!r} has budget_kind={budget_kind!r}, so L is a "
            f"multiple ({budget_value}) of the subject's own TTL or interval; "
            "an episode of this class must name its subject"
        )
    return budget_value * policy.subject_unit_ms(
        connection, threshold_kind=budget_kind, subject=subject
    )


def _resolve_tolerance_ms(
    connection: sqlite3.Connection,
    *,
    revision_id: int,
    incident_class: str,
    subject: str | None,
    threshold_kind: str,
) -> int | None:
    """``T`` in milliseconds, or ``None`` where ``T`` is a count.

    ``watcher_error_streak``'s ``T`` is five consecutive failures, and
    :func:`policy.resolve_tolerance_ms` refuses to call that a duration
    (:class:`~...policy.NotADuration`). That refusal is right and is **not**
    escalated here: the window of such an episode is still perfectly well
    defined -- ``L`` is an absolute 10 minutes -- so refusing the whole window
    over an unavailable side quantity would make a class unmeasurable for a
    reason that has nothing to do with measuring it. The count is recorded as
    ``threshold_kind`` on the window instead, so a consumer knows the ``None``
    means "a count", not "policy said nothing".
    """

    if threshold_kind == "consecutive_count":
        return None
    if threshold_kind != "absolute_ms" and subject is None:
        # policy.resolve_tolerance_ms refuses this too, with PolicyUsageError.
        # It is checked here first so that both relative sides -- T and L --
        # refuse with the same type for the same missing thing; a caller that
        # had to catch two exception types for one absent subject would
        # eventually catch only the one it had seen.
        raise SubjectRequired(
            f"{incident_class!r} has threshold_kind={threshold_kind!r}, so T is "
            "a multiple of the subject's own interval or TTL; an episode of "
            "this class must name its subject"
        )
    return policy.resolve_tolerance_ms(
        connection,
        revision_id=revision_id,
        incident_class=incident_class,
        subject=subject,
    )


def classify(
    *, onset_ms: int, end_ms: int, period_start_ms: int, period_end_ms: int
) -> str:
    """Where ``[onset_ms, end_ms)`` sits relative to ``[period_start_ms, period_end_ms)``.

    Wholly inside is :data:`IN_PERIOD`, and "wholly" is evaluated in half-open
    terms at both ends: a window ending *exactly* at ``period_end_ms`` is inside
    (its last instant is ``period_end_ms - 1``), and an episode onsetting
    exactly at ``period_start_ms`` is inside. Getting either boundary wrong
    moves one episode per report between a bucket and the numerator, which is
    invisible in aggregate and wrong in every individual case.

    An episode that is both -- onset before the period and window past its end,
    a condition that outlived the whole report -- is filed :data:`CENSORED_LEFT`
    so that it lands in exactly one bucket. Left wins because it is the stronger
    disqualification: a right-censored episode has a trustworthy onset and an
    unfinished budget, whereas a left-censored one has an onset the report never
    observed, and a latency measured from an unobserved instant is not a slow
    measurement but an unfounded one.

    :raises EpisodeOutsidePeriod: if the window does not overlap the period.
    :raises PeriodRefused: if the period is empty or inverted.
    """

    if period_end_ms <= period_start_ms:
        raise PeriodRefused(
            f"the report period [{period_start_ms}, {period_end_ms}) is empty or "
            "inverted; a half-open window must have an end strictly after its "
            "start (time-base-policy.md section 2, rule 4)"
        )
    if onset_ms >= period_end_ms or end_ms <= period_start_ms:
        raise EpisodeOutsidePeriod(
            f"the window [{onset_ms}, {end_ms}) does not overlap the report "
            f"period [{period_start_ms}, {period_end_ms}); it is neither in the "
            "period nor censored by it"
        )
    if onset_ms < period_start_ms:
        return CENSORED_LEFT
    if end_ms > period_end_ms:
        return CENSORED
    return IN_PERIOD


def episode_window(
    connection: sqlite3.Connection,
    *,
    revision_id: int,
    episode: Episode,
    grace_ms: int,
    period_start_ms: int,
    period_end_ms: int,
) -> EpisodeWindow:
    """One episode's window and classification, under one resolved revision.

    *revision_id* is the caller's, always: ``D-0031``'s corollary is that a
    ``policy_*`` read without a revision predicate matches every tolerance ever
    recorded, and this function resolves none of its own.
    """

    if grace_ms < 0:
        raise WindowRefusal(
            f"grace_ms={grace_ms} is negative; grace exists so an episode is not "
            "judged a miss for losing a race with the pass that would have "
            "caught it, and a negative value shortens the window below the "
            "budget the detector is actually held to"
        )

    policy_row = policy.detection_latency(
        connection, revision_id=revision_id, incident_class=episode.incident_class
    )
    threshold_kind = str(policy_row["threshold_kind"])
    budget_ms = resolve_budget_ms(
        connection,
        revision_id=revision_id,
        incident_class=episode.incident_class,
        subject=episode.subject,
    )
    tolerance_ms = _resolve_tolerance_ms(
        connection,
        revision_id=revision_id,
        incident_class=episode.incident_class,
        subject=episode.subject,
        threshold_kind=threshold_kind,
    )

    end_ms = episode.onset_ms + budget_ms + grace_ms
    classification = classify(
        onset_ms=episode.onset_ms,
        end_ms=end_ms,
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
    )
    return EpisodeWindow(
        episode_id=episode.episode_id,
        incident_class=episode.incident_class,
        subject=episode.subject,
        onset_ms=episode.onset_ms,
        threshold_kind=threshold_kind,
        tolerance_ms=tolerance_ms,
        budget_ms=budget_ms,
        grace_ms=grace_ms,
        end_ms=end_ms,
        classification=classification,
    )


def classify_episodes(
    connection: sqlite3.Connection,
    *,
    revision_id: int,
    period_start_ms: int,
    period_end_ms: int,
    episodes: Iterable[Episode],
    grace_ms: int | None = None,
) -> WindowReport:
    """Classify every episode of a report against one period and one grace.

    *grace_ms* left as ``None`` takes section 3.5's default -- one reconcile
    period, read from this revision's own rows by :func:`default_grace_ms`. That
    is not a silent default: the value used and the fact that it came from the
    revision are both recorded on the returned :class:`WindowReport`, so the
    classification can be recomputed by a reader who has only the report.

    *connection* must be the read-only handle from
    :func:`~claude_org_runtime.measurement.reader.open_for_measurement`; every
    statement issued here is a ``SELECT``.

    :raises DuplicateEpisodeRefused: if two episodes share an id.
    :raises EpisodeOutsidePeriod: if an episode does not touch the period.
    """

    if period_end_ms <= period_start_ms:
        raise PeriodRefused(
            f"the report period [{period_start_ms}, {period_end_ms}) is empty or "
            "inverted; a half-open window must have an end strictly after its "
            "start (time-base-policy.md section 2, rule 4)"
        )

    if grace_ms is None:
        grace_ms = default_grace_ms(connection, revision_id=revision_id)
        grace_source = GRACE_REVISION_RECONCILE_PERIOD
    else:
        grace_source = GRACE_DECLARED

    windows: list[EpisodeWindow] = []
    seen: set[str] = set()
    for episode in episodes:
        if episode.episode_id in seen:
            # Two windows under one id are two votes in one numerator, and the
            # duplicate is invisible in the counts -- the totals simply come out
            # one too high. Refusing names the input defect where it happened.
            raise DuplicateEpisodeRefused(
                f"episode_id={episode.episode_id!r} appears more than once in "
                "this report's input; one episode is one condition and would be "
                "counted twice"
            )
        seen.add(episode.episode_id)
        windows.append(
            episode_window(
                connection,
                revision_id=revision_id,
                episode=episode,
                grace_ms=grace_ms,
                period_start_ms=period_start_ms,
                period_end_ms=period_end_ms,
            )
        )

    return WindowReport(
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        revision_id=revision_id,
        grace_ms=grace_ms,
        grace_source=grace_source,
        windows=tuple(windows),
    )
