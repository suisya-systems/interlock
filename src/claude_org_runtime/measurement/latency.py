"""G6 -- the onset-to-incident distribution, its two references, and the lag beside it.

The failure this module is written against is a latency report that is *true and
useless*, and ``docs/measurement-harness.md`` section 4 names the two shapes it
takes.

**1. One reference rendered as though it were both.** Section 4 requires every
class to be reported against two bounds -- the budget ``L`` from the policy
revision in force (the **acceptance** bound) and the v1 shadow distribution over
``both``-bucket episodes (the **non-regression** bound) -- and says in terms that
"neither substitutes for the other, and a report states both even when one of
them is unavailable". Outside the shadow period there is no v1 distribution at
all, and the tempting rendering is to print the budget comparison alone under a
heading that implies both were considered. A reader then takes "inside budget"
for "no regression", which is the one thing the budget cannot tell them: a class
whose detection got four times slower and still fits inside a generous ``L``
passes the acceptance bound and fails the non-regression one.

So the shadow side is **structural, not optional**. :class:`ShadowSource` cannot
be constructed without either a distribution or a stated reason there is none
(:class:`ShadowReferenceUnstated`), :func:`measure_latency` takes it as a
required keyword with no default, and :meth:`ShadowSource.for_class` turns
"present overall but empty for this class" into an *absent* reference carrying
that as its reason rather than into a silent zero. There is no code path here
that emits a class's figures without also emitting what happened to its shadow
reference.

**2. A provider's bad afternoon read as our regression.** Onset-to-incident
latency contains the time the fact spent getting to us, and that segment is not
ours. ``time-base-policy.md`` section 2 rule 3 is explicit: end-to-end latency is
reported with both clocks and the difference is kept, as its own series -- the
**ingestion lag**, ``ingested_at_ms - occurred_at_ms`` -- "so that a latency
regression caused by a slow provider is distinguishable from one caused by us".
Without it, GitHub delivering webhooks ten minutes late for one afternoon lands
in the detection distribution and reads as a detector that got slower, and the
remedy chosen is a change to code that was never the problem.
:func:`measure_ingestion_lag` therefore runs on every report, prints beside the
distribution, and is never added into it or subtracted out of it: the two are
separate series because separating them is the entire point.

**Negative lag is printed, not clamped.** A provider clock ahead of ours yields
``ingested_at_ms < occurred_at_ms``. That is skew -- the thing rule 1 says we
cannot bound -- and a clamp to zero would hide the only evidence of it we hold.

**What this module does not do.** It does not decide whether a class passed. It
does not convert a shadow comparison into a verdict (``Q-0005`` is open;
``measurement-harness.md`` section 5 says a harness that emitted one would be
answering it by inertia). It does not classify episodes: censoring is
``windows.py``'s and arrives here already decided, so a censored episode is
excluded from the distribution by :meth:`~...windows.WindowReport.numerator_ids`
and counted rather than re-judged. And it raises no incident and applies no
remedy -- this branch implements detectors and reporting only.

**``Q-0011`` stays open, and this module is not where it gets closed.** Section 7
holds that Secretary window latency under load is **gate item 8's** measurement,
not this harness's, and that no threshold for it is invented here. The
distinction is easy to lose because both quantities are called latency and both
are milliseconds: what this module measures is *onset to incident* -- how long a
condition existed before a detector filed it -- and it compares that against the
policy budget ``L`` and the v1 shadow distribution, neither of which says
anything about how long a Secretary window takes to answer while loaded. So
there is no Secretary series here, no constant standing in for one, and nothing
in :func:`measure_latency` that would let a caller pass a Secretary figure in
and have it judged against ``L``. A number invented here would be a threshold
for ``Q-0011`` in everything but name, and gate item 8 would then find its
question already answered by a module that never measured it.

**Read-only, and the clock is the caller's.** The connection is the handle from
:func:`~claude_org_runtime.measurement.reader.open_for_measurement`; every
statement issued here is a ``SELECT``. ``now_ms`` is a parameter
(``time-base-policy.md`` section 2 rule 2) so a report can be driven to any
instant by a test.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from claude_org_runtime.measurement.reader import ControlPlaneRefusal

# The bucket names are imported, never re-spelled: a second copy of a closed set
# agrees with the original right up until the day one of them is renamed, and
# the disagreement shows up here as a censored episode silently entering the
# latency numerator that section 3.5 excluded it from.
from claude_org_runtime.measurement.windows import (
    CENSORED,
    CENSORED_LEFT,
    WindowReport,
)

__all__ = [
    "ClassLatency",
    "DetectionBeforeOnset",
    "Distribution",
    "IngestionLag",
    "LatencyRefusal",
    "LatencyReport",
    "SHADOW_ABSENT",
    "SHADOW_PRESENT",
    "ShadowReference",
    "ShadowReferenceUnstated",
    "ShadowSource",
    "UnknownEpisodeDetection",
    "measure_ingestion_lag",
    "measure_latency",
    "no_shadow_reference",
    "render_latency_report",
    "shadow_from_both_bucket",
]


#: Whether a reference has a distribution behind it. Two named states rather
#: than a nullable distribution, because the absent state carries an obligation
#: -- a reason -- that ``None`` cannot hold (module docstring, point 1).
SHADOW_PRESENT = "present"
SHADOW_ABSENT = "absent"


class LatencyRefusal(ControlPlaneRefusal):
    """A latency figure that cannot be computed, stated rather than guessed."""


class ShadowReferenceUnstated(LatencyRefusal):
    """A shadow reference has neither a distribution nor a reason it has none.

    This is the refusal that makes section 4's "states both even when one is
    unavailable" a property of the type instead of a habit of the caller. A
    reference in :data:`SHADOW_ABSENT` with no reason renders as an empty second
    heading, which reads to a reviewer exactly like a reference that was
    considered and found equal.
    """


class DetectionBeforeOnset(LatencyRefusal):
    """An incident was recorded before the onset it is supposed to have detected.

    Latency is ``incident.created_at_ms - onset`` (section 3.2), and a negative
    value is not a fast detection: it is a correlation that paired an incident
    with the wrong episode, or an onset taken from the source clock while the
    incident carries ours (``time-base-policy.md`` section 2 rule 1). Clamping it
    to zero would leave the mispairing in the sample, pulling the median toward
    a speed nothing achieved.
    """


class UnknownEpisodeDetection(LatencyRefusal):
    """A detection names an episode this report never classified.

    The detection map and the window report have to be over the same episode
    set: an id in one and not the other means the caller assembled the two from
    different selections, and whichever number came out would be over neither
    set. Dropping the stray silently is how a selection bug survives a review.
    """


@dataclass(frozen=True)
class Distribution:
    """Section 4's four figures over one sample: count, median, p90, max.

    The three figures are ``None`` when ``count`` is zero, which is a different
    statement from a zero millisecond latency and is rendered as one.

    **Percentiles are nearest-rank**, for the reason ``ac9.py``'s ``_p95`` gives:
    a nearest-rank percentile returns a value some episode actually exhibited,
    and it is reproducible byte for byte across builds and languages, which
    ``D-0040`` asks of every figure a report is recomputed from. An interpolating
    median would report a duration no detection took -- harmless in a large
    sample, and a fabricated number in the small ones this harness will mostly
    see.
    """

    count: int
    median_ms: int | None
    p90_ms: int | None
    max_ms: int | None

    @classmethod
    def of(cls, values: Sequence[int]) -> "Distribution":
        """The distribution of *values*, empty-safe."""

        if not values:
            return cls(count=0, median_ms=None, p90_ms=None, max_ms=None)
        ordered = sorted(values)
        return cls(
            count=len(ordered),
            median_ms=_nearest_rank(ordered, 0.50),
            p90_ms=_nearest_rank(ordered, 0.90),
            max_ms=ordered[-1],
        )


@dataclass(frozen=True)
class ShadowReference:
    """One class's non-regression bound, or the stated reason it has none.

    Constructed only through :meth:`present` and :meth:`absent` so the
    exclusive-or below is the only shape that exists; ``__post_init__`` holds it
    for anything that builds one by hand.
    """

    status: str
    distribution: Distribution | None
    both_bucket_count: int | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.status == SHADOW_PRESENT:
            if self.distribution is None:
                raise ShadowReferenceUnstated(
                    "a present shadow reference must carry the v1 distribution "
                    "it is a reference to"
                )
            return
        if self.status != SHADOW_ABSENT:
            raise ShadowReferenceUnstated(
                f"shadow status {self.status!r} is neither {SHADOW_PRESENT!r} "
                f"nor {SHADOW_ABSENT!r}"
            )
        if not self.reason:
            raise ShadowReferenceUnstated(
                "an absent shadow reference must say WHY there is none; a blank "
                "second heading reads as a reference that was checked and found "
                "equal (measurement-harness.md section 4)"
            )

    @classmethod
    def present(
        cls, *, samples: Sequence[int], both_bucket_count: int
    ) -> "ShadowReference":
        """The v1 distribution over this class's ``both``-bucket episodes."""

        return cls(
            status=SHADOW_PRESENT,
            distribution=Distribution.of(samples),
            both_bucket_count=both_bucket_count,
            reason=None,
        )

    @classmethod
    def absent(cls, reason: str) -> "ShadowReference":
        """No v1 distribution for this class in this period, and why."""

        return cls(
            status=SHADOW_ABSENT,
            distribution=None,
            both_bucket_count=None,
            reason=reason,
        )

    @property
    def available(self) -> bool:
        return self.status == SHADOW_PRESENT


@dataclass(frozen=True)
class ShadowSource:
    """The report's whole shadow input: per-class samples, or one stated absence.

    v1's numbers never come from this database. ``D-0013`` makes a ``run`` row's
    existence the assertion that the run is Interlock-owned, and there is no
    ownership column to read a v1 episode out of; the shadow distribution is a
    **v1 shadow input** the caller supplies from the other store, which is also
    why it is a parameter rather than a query.

    :meth:`for_class` is where the structural obligation is discharged: a class
    with no ``both``-bucket episode gets an *absent* reference naming that fact,
    never an empty distribution that would render as "0 episodes, no regression".
    """

    status: str
    samples: Mapping[str, tuple[int, ...]] | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.status == SHADOW_PRESENT:
            if self.samples is None:
                raise ShadowReferenceUnstated(
                    "a present shadow source must carry per-class samples"
                )
            return
        if self.status != SHADOW_ABSENT:
            raise ShadowReferenceUnstated(
                f"shadow status {self.status!r} is neither {SHADOW_PRESENT!r} "
                f"nor {SHADOW_ABSENT!r}"
            )
        if not self.reason:
            raise ShadowReferenceUnstated(
                "a report outside the shadow period must say so in words; see "
                "ShadowReference.absent"
            )

    def for_class(self, incident_class: str) -> ShadowReference:
        """This class's non-regression bound, always answering one way or the other."""

        if self.status == SHADOW_ABSENT:
            # The report-level reason, carried down verbatim: every class says
            # the same true thing, which is the point -- a reader scanning one
            # class must not have to look elsewhere to learn there was no v1.
            return ShadowReference.absent(str(self.reason))
        samples = (self.samples or {}).get(incident_class)
        if not samples:
            return ShadowReference.absent(
                f"the shadow period covers this report, but no both-bucket "
                f"episode of class {incident_class!r} was correlated in it, so "
                "there is no v1 distribution to compare against for this class"
            )
        return ShadowReference.present(
            samples=tuple(samples), both_bucket_count=len(samples)
        )


def shadow_from_both_bucket(
    samples: Mapping[str, Sequence[int]],
) -> ShadowSource:
    """A shadow source from v1's onset-to-detection samples, per incident class.

    *samples* holds only ``both``-bucket episodes (section 3.3): an episode v1
    raised and Interlock did not is a candidate **miss**, not a slow detection,
    and folding its v1 latency into this reference would let a miss improve the
    non-regression comparison.
    """

    return ShadowSource(
        status=SHADOW_PRESENT,
        samples=MappingProxyType(
            {name: tuple(values) for name, values in samples.items()}
        ),
        reason=None,
    )


def no_shadow_reference(reason: str) -> ShadowSource:
    """No v1 distribution for this period, with the reason recorded.

    The reason is required. "This period lies outside the shadow window" and
    "the v1 export failed" are different facts with different remedies, and a
    report that said only "unavailable" would make them look alike.
    """

    return ShadowSource(status=SHADOW_ABSENT, samples=None, reason=reason)


@dataclass(frozen=True)
class IngestionLag:
    """``ingested_at_ms - occurred_at_ms`` over the period's spine rows.

    Its own series, printed beside the detection distribution and added into
    nothing (``time-base-policy.md`` section 2 rule 3). ``negative_count`` is the
    skew indicator: a provider clock ahead of ours produces a negative lag, and
    the count is kept because rule 1 says skew is not something we can bound and
    this is the only place it becomes visible.
    """

    distribution: Distribution
    negative_count: int
    event_count: int


@dataclass(frozen=True)
class ClassLatency:
    """One incident class, its distribution, and BOTH of its references.

    *shadow* has no default and cannot be omitted: see the module docstring on
    why a class rendered against the budget alone is the failure this module
    exists to prevent.

    *budgets_ms* is a tuple because ``L`` is per **episode**, not per class:
    ``lease_orphan``'s budget is twice *that lease's* TTL
    (``time-base-policy.md`` section 3.2), so one class in one period can be
    judged against several ceilings. Collapsing them to one number would judge
    every lease by an arbitrary one of them, so the budget comparison is done
    per episode (:attr:`over_budget_ids`) and the distinct ceilings are printed.
    """

    incident_class: str
    distribution: Distribution
    budgets_ms: tuple[int, ...]
    over_budget_ids: tuple[str, ...]
    undetected_ids: tuple[str, ...]
    censored_ids: tuple[str, ...]
    censored_left_ids: tuple[str, ...]
    shadow: ShadowReference


@dataclass(frozen=True)
class LatencyReport:
    """Section 4's report: per-class distributions, both references, and the lag.

    ``revision_id``, ``grace_ms`` and ``grace_source`` are carried up from the
    :class:`~...windows.WindowReport` rather than re-resolved, because a latency
    figure judged against one revision's ``L`` and censored under another's grace
    is a figure over no revision at all (``D-0031``, ``D-0040``).
    """

    period_start_ms: int
    period_end_ms: int
    generated_at_ms: int
    revision_id: int
    grace_ms: int
    grace_source: str
    classes: tuple[ClassLatency, ...]
    shadow: ShadowSource
    ingestion_lag: IngestionLag

    @property
    def shadow_available(self) -> bool:
        return self.shadow.status == SHADOW_PRESENT


def measure_ingestion_lag(
    connection: sqlite3.Connection, *, period_start_ms: int, period_end_ms: int
) -> IngestionLag:
    """The period's ingestion lag, over the event spine.

    Bounded on ``ingested_at_ms`` -- **our** clock -- because
    ``time-base-policy.md`` section 2 rule 1 puts every period boundary and every
    aging predicate on our clock. Selecting on ``occurred_at_ms`` instead would
    let a provider's skew move rows between reports, which is precisely the
    effect this series exists to expose rather than to suffer. Half-open
    ``[start, end)`` per rule 4.

    Only *this* module's read: the spine is the one table that carries both
    clocks for every fact, so one query covers CI observations, PR events and
    everything else a later producer appends.
    """

    if period_end_ms <= period_start_ms:
        raise LatencyRefusal(
            f"the report period [{period_start_ms}, {period_end_ms}) is empty or "
            "inverted (time-base-policy.md section 2, rule 4)"
        )
    lags = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT ingested_at_ms - occurred_at_ms
              FROM event
             WHERE ingested_at_ms >= :period_start_ms
               AND ingested_at_ms < :period_end_ms
             ORDER BY seq
            """,
            {"period_start_ms": period_start_ms, "period_end_ms": period_end_ms},
        )
    ]
    return IngestionLag(
        distribution=Distribution.of(lags),
        negative_count=sum(1 for lag in lags if lag < 0),
        event_count=len(lags),
    )


def measure_latency(
    connection: sqlite3.Connection,
    *,
    windows: WindowReport,
    detections: Mapping[str, int],
    shadow: ShadowSource,
    now_ms: int,
) -> LatencyReport:
    """Section 4's latency report over one already-classified episode set.

    *windows* is the output of
    :func:`~claude_org_runtime.measurement.windows.classify_episodes`: censoring
    is decided there, and this function excludes a censored episode from the
    distribution rather than re-deciding it. That division is deliberate -- one
    classifier means the miss numerator and the latency numerator are drawn from
    the same episodes, which section 3.5 requires and two classifiers would
    eventually violate.

    *detections* maps ``episode_id`` to ``incident.created_at_ms``. An in-period
    episode absent from it is **not** a latency sample: it is a candidate miss,
    and it is reported as :attr:`ClassLatency.undetected_ids` so it cannot be
    mistaken for either a fast detection or a nonexistent episode.

    *shadow* is required and has no default. See the module docstring.

    :raises UnknownEpisodeDetection: if a detection names an unclassified episode.
    :raises DetectionBeforeOnset: if a detection precedes its episode's onset.
    """

    known = {window.episode_id: window for window in windows.windows}
    for episode_id in sorted(detections):
        if episode_id not in known:
            raise UnknownEpisodeDetection(
                f"detection for episode_id={episode_id!r} was supplied, but the "
                "window report does not classify that episode; the detection map "
                "and the episode set are over different selections"
            )

    # Grouped by class in first-seen order, so the rendered report is stable
    # across runs over the same input (D-0040 asks a report to be recomputable,
    # which includes byte-for-byte).
    classes: list[str] = []
    for window in windows.windows:
        if window.incident_class not in classes:
            classes.append(window.incident_class)

    measured: list[ClassLatency] = []
    for incident_class in classes:
        members = [
            window
            for window in windows.windows
            if window.incident_class == incident_class
        ]
        latencies: list[int] = []
        over_budget: list[str] = []
        undetected: list[str] = []
        budgets: list[int] = []
        for window in members:
            if window.censored:
                # windows.py decided this, and it decided it for both
                # numerators at once (EpisodeWindow.censored). Re-deciding it
                # here is how the miss numerator and the latency numerator
                # start disagreeing about which episodes they are over.
                continue
            if window.budget_ms not in budgets:
                budgets.append(window.budget_ms)
            detected_at_ms = detections.get(window.episode_id)
            if detected_at_ms is None:
                undetected.append(window.episode_id)
                continue
            latency_ms = detected_at_ms - window.onset_ms
            if latency_ms < 0:
                raise DetectionBeforeOnset(
                    f"episode_id={window.episode_id!r} was detected at "
                    f"{detected_at_ms} and onset at {window.onset_ms}, a latency "
                    f"of {latency_ms} ms; a negative detection latency is a "
                    "mispaired incident or a mixed clock, not a fast detector"
                )
            latencies.append(latency_ms)
            # Strictly greater: a detection landing exactly on the ceiling met
            # it. The budget is the ceiling on onset-to-alarm, and a `>=` here
            # would fail the one detection that did exactly what the policy
            # asked (time-base-policy.md section 3.1).
            if latency_ms > window.budget_ms:
                over_budget.append(window.episode_id)
        measured.append(
            ClassLatency(
                incident_class=incident_class,
                distribution=Distribution.of(latencies),
                budgets_ms=tuple(sorted(budgets)),
                over_budget_ids=tuple(over_budget),
                undetected_ids=tuple(undetected),
                censored_ids=tuple(
                    window.episode_id
                    for window in members
                    if window.classification == CENSORED
                ),
                censored_left_ids=tuple(
                    window.episode_id
                    for window in members
                    if window.classification == CENSORED_LEFT
                ),
                shadow=shadow.for_class(incident_class),
            )
        )

    return LatencyReport(
        period_start_ms=windows.period_start_ms,
        period_end_ms=windows.period_end_ms,
        generated_at_ms=now_ms,
        revision_id=windows.revision_id,
        grace_ms=windows.grace_ms,
        grace_source=windows.grace_source,
        classes=tuple(measured),
        shadow=shadow,
        ingestion_lag=measure_ingestion_lag(
            connection,
            period_start_ms=windows.period_start_ms,
            period_end_ms=windows.period_end_ms,
        ),
    )


def render_latency_report(report: LatencyReport) -> str:
    """Render *report* as plain ASCII text, with both references on every class.

    ASCII only, ``-`` never an em-dash: this reaches a cp932 console, where a
    single U+2014 turns a report into a ``UnicodeEncodeError``.

    The two reference blocks are emitted from one loop body, so there is no
    arrangement of the data that prints the budget block and skips the shadow
    block -- an absent shadow reference prints its reason under its own heading.
    """

    lines: list[str] = []
    lines.append("Detection latency -- onset to incident, per incident class")
    lines.append(
        f"  period          [{report.period_start_ms}, {report.period_end_ms}) "
        "(half-open, epoch ms)"
    )
    lines.append(f"  generated at    {report.generated_at_ms}")
    lines.append(f"  policy revision {report.revision_id}")
    lines.append(
        f"  grace           {report.grace_ms} ms ({report.grace_source})"
    )
    lines.append("")

    if not report.classes:
        lines.append("  No episode was classified for this period.")
        lines.append("")

    for measured in report.classes:
        lines.append(f"Class {measured.incident_class}")
        lines.append(
            "  distribution    " + _distribution_line(measured.distribution)
        )
        lines.append(
            f"  excluded        censored {len(measured.censored_ids)}, "
            f"censored_left {len(measured.censored_left_ids)} "
            "(section 3.5; in no numerator)"
        )
        lines.append(
            f"  undetected      {len(measured.undetected_ids)} in-period "
            "episode(s) with no incident - candidate misses, not fast detections"
        )
        lines.extend(_itemise(measured.undetected_ids, "none"))

        lines.append("  reference 1 of 2 - budget L (the acceptance bound)")
        if measured.budgets_ms:
            lines.append(
                "      L in force: "
                + ", ".join(f"{budget} ms" for budget in measured.budgets_ms)
                + "  (per episode; a relative L differs by subject)"
            )
        else:
            lines.append("      no in-period episode, so no L was resolved")
        lines.append(
            f"      over budget: {len(measured.over_budget_ids)} episode(s)"
        )
        lines.extend(_itemise(measured.over_budget_ids, "none"))

        lines.append(
            "  reference 2 of 2 - v1 shadow distribution (the non-regression "
            "bound)"
        )
        if measured.shadow.available:
            assert measured.shadow.distribution is not None
            lines.append(
                "      v1: " + _distribution_line(measured.shadow.distribution)
            )
            lines.append(
                f"      over {measured.shadow.both_bucket_count} both-bucket "
                "episode(s)"
            )
        else:
            lines.append("      NO SHADOW REFERENCE FOR THIS PERIOD")
            lines.append(f"      reason: {measured.shadow.reason}")
            lines.append(
                "      the budget comparison above is an acceptance bound only; "
                "it says nothing about regression against v1"
            )
        lines.append("")

    lag = report.ingestion_lag
    lines.append(
        "Ingestion lag -- ingested_at_ms minus occurred_at_ms (own series, "
        "added into nothing)"
    )
    lines.append("  distribution    " + _distribution_line(lag.distribution))
    lines.append(f"  spine rows      {lag.event_count}")
    lines.append(
        f"  negative lag    {lag.negative_count}  (provider clock ahead of "
        "ours; skew, printed rather than clamped)"
    )
    lines.append(
        "  A rise here and a rise in detection latency are different findings: "
        "the first is the provider getting slower, the second is us."
    )
    lines.append("")
    lines.append(
        "This harness reports the measurements a judgement will be made from. "
        "It does not make the judgement."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _nearest_rank(ordered: Sequence[int], quantile: float) -> int:
    """The ``ceil(q * n)``-th smallest of *ordered* (1-indexed).

    Nearest rank, never interpolating: see :class:`Distribution`.
    """

    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def _distribution_line(distribution: Distribution) -> str:
    if distribution.count == 0:
        return "count 0, median -, p90 -, max -  (no sample)"
    return (
        f"count {distribution.count}, median {distribution.median_ms} ms, "
        f"p90 {distribution.p90_ms} ms, max {distribution.max_ms} ms"
    )


def _itemise(ids: Sequence[str], empty: str) -> list[str]:
    if not ids:
        return [f"      {empty}"]
    return [f"      {identifier}" for identifier in ids]
