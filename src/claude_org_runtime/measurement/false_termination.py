"""G6 -- false termination, counted at the applied effect and nowhere else.

The failure this module is written against is a precision figure computed over
the wrong rows, and ``docs/measurement-harness.md`` section 3.4 records the two
ways of getting it wrong. Both are tempting, both produce a number, and neither
number means what its heading says.

**1. Counting recommendations.** ``D-0004`` and AC-6 mean the Dispatcher AI
cannot terminate anything: it may *recommend*, and a human or the Secretary
applies. v1 had no such gate -- its terminations were executions. So a
false-termination rate computed over Interlock's recommendations compares our
**suggestions** against v1's **actions**, and every suggestion a human correctly
declined is charged to us as a false termination that never happened to anyone.
The gate that makes Interlock safer would show up as evidence that it is worse.

**2. Counting a capability we do not have.** The mirror error is to look for
Interlock's own executions of a terminate -- of which there are structurally
zero, because ``time-base-policy.md`` section 4's auto-stop row says Core may
name a stall *candidate* and may not conclude one, and the applied ``action`` row
is written by the third layer. A harness reading only rows the core wrote would
report 0/0 and present a definitional impossibility as a perfect score.

So the definition is taken verbatim and not loosened:

    A false termination is an ``action`` row with ``kind='terminate_session'``
    and ``status='applied'`` whose subject was not, in fact, stuck.

The applied effect is the unit because it is the only thing both systems did the
same kind of. A watcher candidate is not one; an AI recommendation is not one.

**"Not in fact stuck" has a preference order, and it is data.** Section 3.4 gives
it: the fixture label, then the subject's own subsequent evidence, then human
adjudication. :data:`GROUND_TRUTH_PREFERENCE` is that order as a tuple and
:func:`adjudicate` **iterates it**, rather than expressing it as three ``if``
statements whose order is a fact about the file's layout. The difference matters
when the sources disagree, which they will: a fixture label is a statement about
a constructed case and the strongest thing available; subsequent evidence is an
inference over rows a bug may have written; a human adjudication is the last
resort precisely because it does not scale and is not reproducible. When a
lower-preference source disagrees with the winner the disagreement is **recorded**
(:attr:`Adjudication.overruled`) rather than discarded -- a fixture label
contradicted by the subject's own behaviour is either a mislabelled fixture or a
detector writing evidence it should not, and both are findings.

**Undetermined is an outcome, not a gap.** Where none of the three settles it,
the episode is ``undetermined`` and gets its own bucket -- ``D-0006``'s "cannot
determine is a legitimate outcome", applied to the measurement rather than to the
detection. It is never folded into either the numerator (which would invent false
terminations) or the denominator's justified half (which would hide them), and
:attr:`FalseTerminationReport.rate_upper` exists so a reader can see how much of
the rate the undetermined rows could move.

**Absence of activity is never evidence of a stall.** The subsequent-evidence
source can return "not stuck" and can return "no opinion". It can never return
"stuck": a session that produced nothing after being terminated produced nothing
*because it was terminated*, and reading that silence as confirmation would make
every termination self-justifying. See :func:`subsequent_activity_verdicts`.

**Three supporting series, because the headline hides where precision lives.**
``recommended_terminate``, ``recommended_but_not_applied`` and
``applied_terminate`` are all reported. A rising
``recommended_but_not_applied`` is *informative rather than alarming* -- it is
the visible value of the human gate, and the report says so in those words so
that nobody optimises it downward.

**Read-only, and the clock is the caller's.** The connection is the handle from
:func:`~claude_org_runtime.measurement.reader.open_for_measurement`; every
statement issued here is a ``SELECT``, and they are declared as text in
:data:`QUERY_DEFINITIONS` for the report provenance header (``D-0040``). Nothing
here raises an incident or applies a remedy: this is a detector, and the
reconcile driver that would act on it is out of scope for this branch.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from claude_org_runtime.measurement.reader import ControlPlaneRefusal

__all__ = [
    "Adjudication",
    "FalseTerminationRefusal",
    "FalseTerminationReport",
    "GROUND_TRUTH_PREFERENCE",
    "PRODUCTIVE_EVENT_TYPES_REQUIRED",
    "QUERY_DEFINITIONS",
    "SOURCE_FIXTURE_LABEL",
    "SOURCE_HUMAN_ADJUDICATION",
    "SOURCE_NONE",
    "SOURCE_SUBSEQUENT_EVIDENCE",
    "STATUS_APPLIED",
    "STATUS_PENDING",
    "STATUS_REFUSED",
    "TERMINATE_ACTIONS_QUERY",
    "TERMINATE_SESSION_KIND",
    "TerminateAction",
    "UnknownGroundTruthVerdict",
    "VERDICT_NOT_STUCK",
    "VERDICT_STUCK",
    "VERDICT_UNDETERMINED",
    "adjudicate",
    "measure_false_termination",
    "read_terminate_actions",
    "render_false_termination_report",
    "subsequent_activity_verdicts",
]


#: The ``action.kind`` a termination is recorded under.
#:
#: **``action.kind`` is unconstrained in the DDL** -- ``0001_initial.sql`` checks
#: only ``length(kind) > 0`` (around line 525), deliberately, so that a new
#: effect does not need a migration. The consequence for a *report* is that the
#: literal is not discoverable from the schema: nothing in the database says
#: which spelling means "terminate a session", so a harness that inlined the
#: string would silently measure zero the day a writer used another one. It is
#: therefore declared here, exported, and carried in :data:`QUERY_DEFINITIONS`
#: as part of the report's own query definitions (``D-0040``), which is where a
#: reader checks what the number was actually over.
TERMINATE_SESSION_KIND = "terminate_session"

#: ``action.status``, as that table's own ``CHECK`` enumerates it. Named here so
#: the buckets, the queries and the tests cannot disagree about spelling.
STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_REFUSED = "refused"

#: The ground-truth sources of section 3.4, **in the order of preference the
#: section states**. :func:`adjudicate` iterates this tuple; reordering it
#: reorders the preference, which is the property that makes the order data
#: rather than an accident of where the ``if`` statements landed.
SOURCE_FIXTURE_LABEL = "fixture_label"
SOURCE_SUBSEQUENT_EVIDENCE = "subsequent_evidence"
SOURCE_HUMAN_ADJUDICATION = "human_adjudication"

GROUND_TRUTH_PREFERENCE: tuple[str, ...] = (
    SOURCE_FIXTURE_LABEL,
    SOURCE_SUBSEQUENT_EVIDENCE,
    SOURCE_HUMAN_ADJUDICATION,
)

#: The source of an ``undetermined`` verdict: none of the three settled it.
SOURCE_NONE = "none"

#: What a source may say. ``VERDICT_NOT_STUCK`` is the false-termination
#: numerator; ``VERDICT_STUCK`` is a termination that did its job.
VERDICT_STUCK = "stuck"
VERDICT_NOT_STUCK = "not_stuck"
#: Not a thing a source may say -- the outcome when none of them said anything.
VERDICT_UNDETERMINED = "undetermined"

#: The queries this report is over, as text, for the provenance header
#: (``D-0040``: "every query the report ran, as text ... The queries are data").
#: These constants are the text that is **executed**, not a transcription of it:
#: a second copy would be right on the day it was written and would go on being
#: printed after the executed query changed, which is a provenance header that
#: certifies the wrong thing.
TERMINATE_ACTIONS_QUERY = """
SELECT a.action_id, a.run_id, a.incident_id, i.session_id, a.status,
       a.created_at_ms, a.applied_at_ms
  FROM action AS a
  LEFT JOIN incident AS i ON i.incident_id = a.incident_id
 WHERE a.kind = :terminate_session_kind
   AND ((a.created_at_ms >= :period_start_ms
         AND a.created_at_ms < :period_end_ms)
        OR (a.applied_at_ms IS NOT NULL
            AND a.applied_at_ms >= :period_start_ms
            AND a.applied_at_ms < :period_end_ms))
 ORDER BY a.action_id
"""

#: ``{event_types}`` expands to one ``?`` per declared productive event type.
#: SQLite has no parameter form for an ``IN`` list, so the placeholders are
#: generated and the values are still bound -- the event types never reach the
#: statement as text.
SUBSEQUENT_ACTIVITY_QUERY = """
SELECT 1
  FROM event
 WHERE subject_kind = ?
   AND subject_id = ?
   AND event_type IN ({event_types})
   AND ingested_at_ms > ?
   AND ingested_at_ms < ?
 LIMIT 1
"""

QUERY_DEFINITIONS: Mapping[str, str] = MappingProxyType(
    {
        "terminate_actions": TERMINATE_ACTIONS_QUERY,
        "subsequent_activity": SUBSEQUENT_ACTIVITY_QUERY,
        "terminate_session_kind": TERMINATE_SESSION_KIND,
    }
)

#: Said once, so the refusal and the docstring cannot drift apart.
PRODUCTIVE_EVENT_TYPES_REQUIRED = (
    "declare which event types count as productive activity; an empty set "
    "disables the subsequent-evidence source without saying so, and every "
    "termination it would have cleared becomes undetermined for a reason no "
    "report records"
)


class FalseTerminationRefusal(ControlPlaneRefusal):
    """A false-termination figure that cannot be computed, stated not guessed."""


class UnknownGroundTruthVerdict(FalseTerminationRefusal):
    """A ground-truth source offered a verdict outside the closed set.

    The two legal answers decide opposite things, and an unrecognised third has
    no safe reading: treating it as ``stuck`` hides a false termination and
    treating it as ``not_stuck`` invents one. ``undetermined`` is not a legal
    *input* either -- a source that cannot decide says nothing, and saying
    nothing is how it reaches the undetermined bucket. Accepting the word as an
    input would let a source overrule a lower-preference source that *could*
    have settled it.
    """


@dataclass(frozen=True)
class TerminateAction:
    """One ``action`` row of :data:`TERMINATE_SESSION_KIND`, as read.

    *subject_id* is what the subsequent-evidence source looks for activity from:
    the incident's session where the action names an incident that names one,
    and otherwise the run. It can be ``None`` -- ``action.run_id`` and
    ``action.incident_id`` are both nullable -- and a ``None`` subject means that
    source cannot speak, which is different from it having spoken.
    """

    action_id: str
    run_id: str | None
    incident_id: str | None
    session_id: str | None
    status: str
    created_at_ms: int
    applied_at_ms: int | None

    @property
    def subject_kind(self) -> str | None:
        if self.session_id is not None:
            return "session"
        if self.run_id is not None:
            return "run"
        return None

    @property
    def subject_id(self) -> str | None:
        return self.session_id if self.session_id is not None else self.run_id


@dataclass(frozen=True)
class Adjudication:
    """One termination's verdict, which source settled it, and who disagreed.

    ``overruled`` holds every lower-preference source that offered a *different*
    verdict than the winner. It is kept because a disagreement is a finding in
    its own right (module docstring) and because a report that showed only the
    winner would make the preference order unfalsifiable -- there would be no
    way to tell an order that was applied from one that never had to be.
    """

    action_id: str
    verdict: str
    source: str
    overruled: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FalseTerminationReport:
    """Section 3.4's headline and its three supporting series.

    The two cohorts are counted on **their own instants** and are deliberately
    not nested. ``recommended_terminate`` is every terminate action *created* in
    the period; ``applied_terminate`` is every one *applied* in it. A
    recommendation made in this period and applied in the next is in the first
    and not the second (:attr:`applied_after_period_end`); one carried over from
    the previous period is in the second and not the first
    (:attr:`applied_from_earlier_recommendation`). Forcing them into one cohort
    would mean either a denominator containing effects that had not happened yet
    or a report that dropped effects which had.
    """

    period_start_ms: int
    period_end_ms: int
    generated_at_ms: int

    #: Supporting series 1: recommendations, applied or not (created in period).
    recommended_terminate: tuple[str, ...]
    #: Supporting series 2, split into its two very different halves.
    declined_refused: tuple[str, ...]
    still_pending: tuple[str, ...]
    #: Recommended in this period, applied after it ended: this report's
    #: denominator does not hold them, the next one's does.
    applied_after_period_end: tuple[str, ...]
    #: Recommended before this period and applied inside it.
    applied_from_earlier_recommendation: tuple[str, ...]

    #: Supporting series 3, and the denominator of the headline rate.
    applied_terminate: tuple[str, ...]

    #: The headline numerator and the two buckets beside it. These three
    #: partition :attr:`applied_terminate`.
    false_termination_ids: tuple[str, ...]
    justified_ids: tuple[str, ...]
    undetermined_ids: tuple[str, ...]

    adjudications: Mapping[str, Adjudication]

    @property
    def recommended_but_not_applied(self) -> tuple[str, ...]:
        """Section 3.4's second series: declined, plus not yet decided.

        Reported as one number because the section names one, and kept split in
        the fields because "a human said no" and "nobody has looked yet" are
        different facts about the gate.
        """

        return self.declined_refused + self.still_pending

    @property
    def rate_lower(self) -> float | None:
        """False terminations over applied terminations, counting only the settled.

        A lower bound on the rate: every undetermined row could turn out to be
        one. ``None`` where nothing was applied -- a rate over an empty
        denominator is not zero, and printing zero would report "we terminated
        nothing" as "we never terminated wrongly".
        """

        if not self.applied_terminate:
            return None
        return len(self.false_termination_ids) / len(self.applied_terminate)

    @property
    def rate_upper(self) -> float | None:
        """The same rate with every undetermined row counted against us.

        Not a prediction: the two bounds together are the honest statement, and
        their gap is exactly how much ground truth the report is missing.
        """

        if not self.applied_terminate:
            return None
        return (
            len(self.false_termination_ids) + len(self.undetermined_ids)
        ) / len(self.applied_terminate)

    @property
    def rate_is_settled(self) -> bool:
        """Do the two bounds coincide? Only when nothing is undetermined."""

        return not self.undetermined_ids


def adjudicate(
    *,
    action_id: str,
    fixture_labels: Mapping[str, str],
    subsequent_evidence: Mapping[str, str],
    human_adjudications: Mapping[str, str],
) -> Adjudication:
    """Was this terminated subject stuck? Section 3.4's preference order, applied.

    The three maps are keyed by ``action_id``; a key that is **absent** is a
    source declining to speak, which is what lets the next source in
    :data:`GROUND_TRUTH_PREFERENCE` decide. All three are required keyword
    arguments with no default: an empty map is a caller stating that a source
    has nothing to say, and a defaulted one would be this file deciding that on
    the caller's behalf and never recording it.

    :raises UnknownGroundTruthVerdict: for any verdict outside
        ``{stuck, not_stuck}``.
    """

    offered = {
        SOURCE_FIXTURE_LABEL: fixture_labels.get(action_id),
        SOURCE_SUBSEQUENT_EVIDENCE: subsequent_evidence.get(action_id),
        SOURCE_HUMAN_ADJUDICATION: human_adjudications.get(action_id),
    }
    for source, verdict in offered.items():
        if verdict is None:
            continue
        if verdict not in (VERDICT_STUCK, VERDICT_NOT_STUCK):
            raise UnknownGroundTruthVerdict(
                f"ground-truth source {source!r} answered {verdict!r} for "
                f"action_id={action_id!r}; the only answers are "
                f"{VERDICT_STUCK!r} and {VERDICT_NOT_STUCK!r}, and a source "
                "with no opinion says nothing rather than saying "
                f"{VERDICT_UNDETERMINED!r}"
            )

    # The preference order is walked, not written out as branches: the tuple is
    # the specification, so a change to section 3.4's order is a change to one
    # line of data and not a re-reading of this function.
    for rank, source in enumerate(GROUND_TRUTH_PREFERENCE):
        verdict = offered[source]
        if verdict is None:
            continue
        overruled = tuple(
            (lower, offered[lower])
            for lower in GROUND_TRUTH_PREFERENCE[rank + 1 :]
            if offered[lower] is not None and offered[lower] != verdict
        )
        return Adjudication(
            action_id=action_id,
            verdict=verdict,
            source=source,
            overruled=overruled,
        )

    return Adjudication(
        action_id=action_id,
        verdict=VERDICT_UNDETERMINED,
        source=SOURCE_NONE,
        overruled=(),
    )


def subsequent_activity_verdicts(
    connection: sqlite3.Connection,
    actions: Iterable[TerminateAction],
    *,
    productive_event_types: Sequence[str],
    period_end_ms: int,
) -> Mapping[str, str]:
    """Section 3.4's second source: the subject's own subsequent evidence.

    "A session that resumed productive activity after the termination window was
    not stuck." So this returns :data:`VERDICT_NOT_STUCK` for a subject with a
    productive event on the spine strictly after ``applied_at_ms``, and returns
    **nothing at all** for every other subject.

    The asymmetry is the whole design and is not an oversight. Silence after a
    termination is what a termination *produces*; reading it as confirmation
    that the subject was stuck would make every termination self-justifying and
    drive the false-termination rate to zero by construction (``D-0006``:
    absence of evidence is not evidence, and "cannot determine" is a legitimate
    outcome).

    *productive_event_types* has no default and may not be empty. Which event
    types are "productive" is a statement about what the organisation's
    producers write, and it belongs to the caller assembling the report; an
    "everything counts" default would admit the termination's own bookkeeping
    events and clear every termination it looked at.

    The window is bounded above by ``period_end_ms`` so the answer is a function
    of the period the report is over: evidence that arrived after the report
    closed belongs to the next report, not to a figure this one already printed
    (``time-base-policy.md`` section 2 rule 4). ``ingested_at_ms`` is the clock,
    per rule 1.
    """

    if not productive_event_types:
        raise FalseTerminationRefusal(PRODUCTIVE_EVENT_TYPES_REQUIRED)

    verdicts: dict[str, str] = {}
    statement = SUBSEQUENT_ACTIVITY_QUERY.format(
        event_types=", ".join("?" for _ in productive_event_types)
    )
    for action in actions:
        if action.status != STATUS_APPLIED or action.applied_at_ms is None:
            # Only an applied termination has a "subsequent" at all. A pending
            # recommendation's subject is still running for reasons that have
            # nothing to do with a termination that never happened.
            continue
        subject_kind = action.subject_kind
        subject_id = action.subject_id
        if subject_kind is None or subject_id is None:
            # Nothing to look for activity from, so this source declines --
            # which is not the same as finding no activity, and is recorded as
            # an absent key exactly like any other declining source.
            continue
        row = connection.execute(
            statement,
            (
                subject_kind,
                subject_id,
                *productive_event_types,
                action.applied_at_ms,
                period_end_ms,
            ),
        ).fetchone()
        if row is not None:
            verdicts[action.action_id] = VERDICT_NOT_STUCK
    return MappingProxyType(verdicts)


def measure_false_termination(
    connection: sqlite3.Connection,
    *,
    period_start_ms: int,
    period_end_ms: int,
    now_ms: int,
    fixture_labels: Mapping[str, str],
    subsequent_evidence: Mapping[str, str],
    human_adjudications: Mapping[str, str],
) -> FalseTerminationReport:
    """Section 3.4's report over one period.

    The three ground-truth maps are required keyword arguments; see
    :func:`adjudicate` on why none of them defaults. *now_ms* is the caller's
    clock (``time-base-policy.md`` section 2 rule 2) and is recorded as the
    report's ``generated_at_ms``.

    :raises FalseTerminationRefusal: if the period is empty or inverted.
    :raises UnknownGroundTruthVerdict: for a verdict outside the closed set.
    """

    if period_end_ms <= period_start_ms:
        raise FalseTerminationRefusal(
            f"the report period [{period_start_ms}, {period_end_ms}) is empty or "
            "inverted (time-base-policy.md section 2, rule 4)"
        )

    actions = read_terminate_actions(
        connection, period_start_ms=period_start_ms, period_end_ms=period_end_ms
    )

    recommended: list[str] = []
    declined: list[str] = []
    pending: list[str] = []
    applied_after: list[str] = []
    applied_earlier: list[str] = []
    applied: list[str] = []

    for action in actions:
        created_in_period = (
            period_start_ms <= action.created_at_ms < period_end_ms
        )
        applied_in_period = (
            action.applied_at_ms is not None
            and period_start_ms <= action.applied_at_ms < period_end_ms
        )
        if created_in_period:
            recommended.append(action.action_id)
            if action.status == STATUS_REFUSED:
                declined.append(action.action_id)
            elif action.status == STATUS_PENDING:
                pending.append(action.action_id)
            elif action.status == STATUS_APPLIED and not applied_in_period:
                applied_after.append(action.action_id)
        if applied_in_period:
            applied.append(action.action_id)
            if not created_in_period:
                applied_earlier.append(action.action_id)

    adjudications: dict[str, Adjudication] = {}
    false_terminations: list[str] = []
    justified: list[str] = []
    undetermined: list[str] = []
    for action_id in applied:
        verdict = adjudicate(
            action_id=action_id,
            fixture_labels=fixture_labels,
            subsequent_evidence=subsequent_evidence,
            human_adjudications=human_adjudications,
        )
        adjudications[action_id] = verdict
        if verdict.verdict == VERDICT_NOT_STUCK:
            false_terminations.append(action_id)
        elif verdict.verdict == VERDICT_STUCK:
            justified.append(action_id)
        else:
            undetermined.append(action_id)

    return FalseTerminationReport(
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        generated_at_ms=now_ms,
        recommended_terminate=tuple(recommended),
        declined_refused=tuple(declined),
        still_pending=tuple(pending),
        applied_after_period_end=tuple(applied_after),
        applied_from_earlier_recommendation=tuple(applied_earlier),
        applied_terminate=tuple(applied),
        false_termination_ids=tuple(false_terminations),
        justified_ids=tuple(justified),
        undetermined_ids=tuple(undetermined),
        adjudications=MappingProxyType(adjudications),
    )


def read_terminate_actions(
    connection: sqlite3.Connection, *, period_start_ms: int, period_end_ms: int
) -> tuple[TerminateAction, ...]:
    """Every terminate action this period recommended or applied, ordered by id.

    The ``LEFT JOIN`` onto ``incident`` is what supplies the session subject: a
    termination names an incident, and the incident names the session the
    subsequent-evidence source looks for activity from. It is a ``LEFT`` join
    because ``action.incident_id`` is nullable in the DDL and an action without
    one is still an applied effect that belongs in the denominator -- an inner
    join would drop it, shrinking the denominator and *raising* the rate for a
    reason that has nothing to do with terminations being wrong.

    ``ORDER BY action_id`` makes the itemisations, and therefore the rendered
    report, byte-reproducible (``D-0040``).
    """

    rows = connection.execute(
        TERMINATE_ACTIONS_QUERY,
        {
            "terminate_session_kind": TERMINATE_SESSION_KIND,
            "period_start_ms": period_start_ms,
            "period_end_ms": period_end_ms,
        },
    ).fetchall()
    return tuple(
        TerminateAction(
            action_id=str(row[0]),
            run_id=None if row[1] is None else str(row[1]),
            incident_id=None if row[2] is None else str(row[2]),
            session_id=None if row[3] is None else str(row[3]),
            status=str(row[4]),
            created_at_ms=int(row[5]),
            applied_at_ms=None if row[6] is None else int(row[6]),
        )
        for row in rows
    )


def render_false_termination_report(report: FalseTerminationReport) -> str:
    """Render *report* as plain ASCII text, with no verdict in it.

    ASCII only, ``-`` never an em-dash: this reaches a cp932 console, where a
    single U+2014 turns a report into a ``UnicodeEncodeError``.

    The three supporting series print beside the headline, and the
    declined-recommendation line carries section 3.4's reading of it in words --
    "informative rather than alarming" -- because a number rising in a report
    with no note attached gets optimised downward by whoever is asked to make it
    stop rising, and here that means removing the human gate.
    """

    lines: list[str] = []
    lines.append("False termination -- counted at the applied effect (section 3.4)")
    lines.append(
        f"  period          [{report.period_start_ms}, {report.period_end_ms}) "
        "(half-open, epoch ms)"
    )
    lines.append(f"  generated at    {report.generated_at_ms}")
    lines.append(
        f"  counted over    action rows with kind = "
        f"'{TERMINATE_SESSION_KIND}' and status = '{STATUS_APPLIED}'"
    )
    lines.append(
        "  NOT counted     AI recommendations (D-0004 / AC-6: the Dispatcher "
        "AI cannot terminate) and watcher candidates"
    )
    lines.append("")

    lines.append("Headline")
    lines.append(
        f"  false terminations {len(report.false_termination_ids)} of "
        f"{len(report.applied_terminate)} applied"
    )
    lines.append(f"    rate (settled only, a lower bound): {_percent(report.rate_lower)}")
    lines.append(
        f"    rate (every undetermined counted against us, an upper bound): "
        f"{_percent(report.rate_upper)}"
    )
    if report.rate_is_settled:
        lines.append(
            "    the two bounds coincide: every applied termination was settled "
            "by ground truth"
        )
    else:
        lines.append(
            f"    the gap is {len(report.undetermined_ids)} undetermined "
            "termination(s) - ground truth this report does not have, not "
            "terminations it judged"
        )
    lines.append("")

    lines.append("Supporting series (the headline alone hides where precision lives)")
    lines.append(
        f"  recommended_terminate        {len(report.recommended_terminate)}"
        "    recommendations created in the period, applied or not"
    )
    lines.append(
        f"  recommended_but_not_applied  "
        f"{len(report.recommended_but_not_applied)}"
        f"    declined {len(report.declined_refused)}, "
        f"awaiting a decision {len(report.still_pending)}"
    )
    lines.append(
        "      This is the visible value of the human gate. A rising number is "
        "INFORMATIVE, NOT alarming: it is terminations that did not happen to a "
        "subject that did not need one."
    )
    lines.append(
        f"  applied_terminate            {len(report.applied_terminate)}"
        "    the denominator above"
    )
    lines.append(
        f"  applied after period end     "
        f"{len(report.applied_after_period_end)}"
        "    recommended here, applied later; in the next report's denominator"
    )
    lines.append(
        f"  applied from earlier period  "
        f"{len(report.applied_from_earlier_recommendation)}"
        "    recommended before this period, applied inside it"
    )
    lines.append("")

    lines.append("Ground truth, in the order of preference of section 3.4")
    lines.append("  " + " > ".join(GROUND_TRUTH_PREFERENCE))
    lines.append(
        "  A source with no opinion is silent, and where all three are silent "
        "the termination is undetermined (D-0006: cannot determine is a "
        "legitimate outcome)."
    )
    for bucket, ids in (
        ("false termination (subject was NOT stuck)", report.false_termination_ids),
        ("justified (subject WAS stuck)", report.justified_ids),
        ("undetermined", report.undetermined_ids),
    ):
        lines.append(f"  {bucket} ({len(ids)}):")
        if not ids:
            lines.append("      none")
        for action_id in ids:
            decision = report.adjudications[action_id]
            lines.append(f"      {action_id}  settled by: {decision.source}")
            for source, verdict in decision.overruled:
                lines.append(
                    f"          overruled {source} = {verdict} "
                    "(lower preference; recorded because a disagreement is a "
                    "finding)"
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


def _percent(value: float | None) -> str:
    if value is None:
        return "not computable (nothing was applied in this period)"
    return f"{value * 100:.2f} percent"
