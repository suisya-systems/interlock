"""G6 -- AC-10's ground truth: the labelled corpus, its loader, and its evaluator.

The failure this module is written against is a harness that grades itself.
``docs/measurement-harness.md`` section 3.1 states it exactly: **Interlock's own
tables cannot contain a miss.** A missed condition produces no ``incident`` row,
so an aggregate over ``incident`` counts what was detected and is structurally
blind to what was not; and the latencies that survive are the fast ones by
definition, because a slow detection that never happened contributes no row to
slow the distribution down. A harness that reads only our rows therefore
measures its own recall as 100% and its own latency as excellent, and it does so
on a database with nothing wrong in it. No amount of care in the query fixes
that -- the number the query needs is not in the table.

``D-0039`` puts the ground truth **outside** the thing being measured, and the
source implemented here is source A, the labelled corpus: a set of traces whose
correct outcome was decided by a human before any detector ran, so a condition
that produces no incident is still on the record as a condition. Source B (the
shadow reconciliation against v1) is the other half and is deliberately not
here, because it only exists **during** the canary -- and AC-10 is a gate *on*
the canary. The corpus is the ground truth that exists before it, which is the
whole reason this module is written first.

**The layout is section 3.2's, verbatim.**

.. code-block:: text

    <root>/<class>/<case>/
        trace.jsonl     -- the observations, each with an offset in ms from t0
        expected.json   -- the label

The corpus root is a parameter, not a constant: section 3.2 names the layout
under a root, not a path in a repository. The corpus this branch ships lives at
``tests/fixtures/labelled/``, beside the repository's existing fixture home.

**``onset_offset_ms`` is when the condition BEGAN, and that is not a detail.**
It is the state entry -- the instant the escalation was received, the instant
the probe started failing -- and **not** the tolerance crossing.
``time-base-policy.md`` section 3.1 is why: ``T`` is part of ``L``, not a head
start on it. Label the crossing instead and every fixture silently acquires an
extra ``T`` of slack, so an alarm that landed at ``T + L`` -- a detector one full
tolerance over its budget -- is graded as having landed inside it. For
``relay_gap`` that is a three-minute error on a five-minute budget: the corpus
would pass the detector it exists to fail. The loader enforces the reading it
can (an onset is an offset into the trace, never derived from the tolerance) and
:func:`evaluate` measures latency from it.

**Negative cases are mandatory, and the build fails without them.** ``D-0006``
requires observation-failure fixtures alongside stall fixtures, and section 3.2
spells out the arithmetic: a corpus of only positive cases lets a detector that
alarms on *everything* score a perfect miss rate. There is nothing in a
positive-only corpus that can tell that detector from a good one. So
:func:`load_corpus` **refuses** a corpus with no negative cases
(:class:`NegativeCasesRequired`) rather than warning about it -- a warning is
read once, by the person who already knows, and never again.

**The composition is reported for the same reason.** Miss rate and
false-positive rate are printed in one table over one corpus
(:func:`render_fixture_report`), so a recall improvement bought by widening
every predicate shows up in the same table as the false positives it bought.
That coupling *is* the measurement; a report of recall alone is a report of how
loudly the detector alarms.

**A malformed fixture is refused, never skipped.** A skipped fixture is a
silently shrunken corpus: the run stays green, the case count drops, and the one
number that would have shown it -- the composition -- moves in the direction
that looks like progress. Every refusal here names the case.

**The clock is structural, not a convention.** Section 3.2 requires detection
latency to be exact rather than sampled, which means the detector under test
must read the injected clock and not a wall clock. Asking politely produces a
harness that is exact until someone calls ``time.time()`` in a detector and the
latencies quietly acquire scheduler noise -- indistinguishable, in the report,
from a detector that got slower. So :class:`SyntheticClock` **mints** every
instant it hands out and :func:`evaluate` refuses an incident stamped with an
instant this clock never minted (:class:`ClockNotSynthetic`). The detector
cannot pass by accident and cannot fail to be noticed.

**Scope.** This module loads and grades. It runs no detector, raises no
incident, applies no remedy and opens no control-plane database -- it reads
files the caller names and the outcomes the caller hands it. It writes nothing,
anywhere.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from claude_org_runtime.measurement.reader import ControlPlaneRefusal

__all__ = [
    "CASE_FILES",
    "DETECTED",
    "EXPECTED_FILENAME",
    "FACT_STATES",
    "FALSE_POSITIVE",
    "ClockNotSynthetic",
    "CorpusCompositionRefused",
    "CaseIncomplete",
    "CaseOutcome",
    "ClassDirectoryMismatch",
    "EvaluationRefusal",
    "ExpectedLabel",
    "FixtureCase",
    "FixtureCorpus",
    "FixtureEvaluation",
    "FixtureRefusal",
    "IncidentBeforeOnset",
    "LABEL_FIELDS",
    "LabelMalformed",
    "MISS",
    "NONE_CLASS",
    "NegativeCasesRequired",
    "Observation",
    "OutcomeMissing",
    "PROVENANCE_KINDS",
    "PositiveCasesRequired",
    "ProducedIncident",
    "StrayEntryRefused",
    "SyntheticClock",
    "TRACE_FILENAME",
    "TRUE_NEGATIVE",
    "TraceMalformed",
    "UnknownCaseInOutcomes",
    "VERDICTS",
    "evaluate",
    "load_case",
    "load_corpus",
    "render_fixture_report",
]


TRACE_FILENAME = "trace.jsonl"
EXPECTED_FILENAME = "expected.json"

#: The only two files a case directory may hold. Exactly two, because a third
#: file is either an input nothing reads -- in which case the case is graded
#: against less than it contains -- or a leftover, and neither is something a
#: ground-truth corpus should carry silently.
CASE_FILES: tuple[str, ...] = (TRACE_FILENAME, EXPECTED_FILENAME)

#: The literal section 3.2 gives a negative case. Spelled out rather than
#: encoded as ``None`` in the JSON: a reader of ``expected.json`` sees the word
#: and knows the case is deliberate, where a ``null`` reads as a field somebody
#: forgot to fill in -- and the difference between "this must raise nothing" and
#: "unlabelled" is the difference between a false-positive test and a gap.
NONE_CLASS = "none"

#: The seven fields of section 3.2's table. **All seven are required on every
#: case**, positive and negative alike; three of them are ``null`` on a negative
#: case (see :func:`_parse_label`) but the key is still there. A missing key is
#: a refusal and never a default, because the default that would be chosen --
#: an empty ``must_not_recommend``, a zero onset -- is in every case the value
#: that makes the fixture easiest to pass.
LABEL_FIELDS: tuple[str, ...] = (
    "incident_class",
    "onset_offset_ms",
    "tolerance_ms",
    "budget_ms",
    "fact_state",
    "must_not_recommend",
    "provenance",
)

#: ``D-0005``'s closed set. The ``incident`` table deliberately carries **no**
#: ``CHECK`` for it (``0001_initial.sql``, at ``fact_state``): a ``CHECK`` would
#: turn a ``D-`` entry extending the set into a migration step. That reasoning
#: governs the persisted schema and does not govern a fixture label, where the
#: opposite risk dominates: a label with a mistyped fact state is a fixture no
#: detector can ever satisfy and no test can ever fail informatively. So the
#: loader refuses an unknown state and names ``D-0005``; a seventh state is a
#: new ``D-`` entry, and then this tuple.
FACT_STATES: tuple[str, ...] = (
    "ACTIVE_EVIDENCE",
    "KNOWN_WAIT",
    "EXPLICIT_BLOCK",
    "NO_ACTIVITY_EVIDENCE",
    "OBSERVATION_UNAVAILABLE",
    "TERMINAL",
)

#: Section 3.2: "where the case came from: an accident, a dogfood capture, or a
#: constructed edge". A closed set, because the field exists to answer "is this
#: corpus made of things that happened, or of things we imagined" -- and free
#: text cannot be counted. Detail may follow a colon.
PROVENANCE_KINDS: tuple[str, ...] = (
    "accident",
    "dogfood_capture",
    "constructed_edge",
)

#: What one case resolved to. Four names, not two: a negative case that produced
#: nothing is a **result**, not the absence of one, and it is the only evidence
#: the corpus holds that the detector is not simply alarming on everything.
DETECTED = "detected"
MISS = "miss"
FALSE_POSITIVE = "false_positive"
TRUE_NEGATIVE = "true_negative"

VERDICTS: tuple[str, ...] = (DETECTED, MISS, FALSE_POSITIVE, TRUE_NEGATIVE)


class FixtureRefusal(ControlPlaneRefusal):
    """A fixture the corpus cannot stand behind, named rather than skipped."""


class CaseIncomplete(FixtureRefusal):
    """A case directory is missing ``trace.jsonl`` or ``expected.json``.

    Half a case is not a smaller case: a trace with no label has no correct
    outcome, and a label with no trace grades a detector against nothing.
    """


class StrayEntryRefused(FixtureRefusal):
    """The corpus tree holds something that is not a case and not a README.

    A stray file at class level, or a third file inside a case, is either an
    input nothing loads or a leftover. Both are refused for the same reason a
    malformed case is: the alternative is a corpus whose contents and whose
    reported composition disagree.
    """


class LabelMalformed(FixtureRefusal):
    """``expected.json`` is missing a field, or a field has an unusable value."""


class TraceMalformed(FixtureRefusal):
    """``trace.jsonl`` is unparseable, empty, or not ordered by offset."""


class ClassDirectoryMismatch(FixtureRefusal):
    """A positive case sits under a class directory that is not its class.

    The directory is what the composition table groups by, so a case filed
    under ``relay_gap`` and labelled ``consumer_backlog`` makes the table report
    coverage of a class that has none. A **negative** case may sit under any
    class directory -- the directory then names the detector the case is aimed
    at, which is exactly what "an outage that must not raise ``relay_gap``"
    means -- so the rule is one-sided on purpose.
    """


class CorpusCompositionRefused(FixtureRefusal):
    """The corpus as a whole cannot support the claim AC-10 makes on it."""


class NegativeCasesRequired(CorpusCompositionRefused):
    """The corpus has no negative case, so it cannot detect a loud detector.

    ``D-0039`` and section 3.2 both make negatives mandatory, and the arithmetic
    is the argument: a detector that raises every class on every trace scores a
    **perfect** miss rate on a positive-only corpus. The refusal is at build
    time and is not a warning, because a warning leaves a green suite behind it
    and green is what everyone reads.
    """


class PositiveCasesRequired(CorpusCompositionRefused):
    """The corpus has no positive case, so it cannot detect a silent detector.

    The mirror of :class:`NegativeCasesRequired`, and mandatory for the mirror
    reason: a detector that raises nothing at all scores a perfect
    false-positive rate over negatives alone. Section 3.2 names only the
    negative half because that is the half a corpus loses by accident, but a
    corpus that cannot express a miss is not AC-10's ground truth either.
    """


class EvaluationRefusal(ControlPlaneRefusal):
    """The evaluation cannot be carried out over the inputs it was handed."""


class OutcomeMissing(EvaluationRefusal):
    """A case in the corpus was handed no detector outcome.

    Treating an absent entry as "the detector produced nothing" is the one
    default this module must not take: a harness that failed to run half the
    corpus would then report those cases as misses (for positives) and as clean
    true negatives (for negatives), and the second half of that is a wiring bug
    scoring points. An empty outcome is written down explicitly instead.
    """


class UnknownCaseInOutcomes(EvaluationRefusal):
    """An outcome names a case this corpus does not contain.

    Ignoring it would hide the two ways it happens -- a renamed case, or an
    evaluation run against a different corpus than the one loaded -- and both
    produce a report about cases nobody graded.
    """


class ClockNotSynthetic(EvaluationRefusal):
    """An incident is stamped with an instant the synthetic clock never minted.

    Section 3.2 requires latency to be exact rather than sampled, which is only
    true if the detector read the injected clock. An instant from anywhere else
    -- ``time.time()``, a second clock, arithmetic on ``t0`` that bypassed the
    clock -- makes the reported latency a measurement of the test runner, and
    the resulting drift is indistinguishable in the report from a detector that
    got slower.
    """


class IncidentBeforeOnset(EvaluationRefusal):
    """A matching incident predates the labelled onset of its own condition.

    The latency would be negative, and a negative latency has exactly two
    causes: the label's onset is wrong, or the detector alarmed on something
    other than this condition. Both are defects in the ground truth itself, and
    clamping to zero or filing the case as detected would let the corpus certify
    a detector using evidence the corpus knows is broken.
    """


@dataclass(frozen=True)
class Observation:
    """One line of ``trace.jsonl``.

    *fields* carries every key other than ``offset_ms`` and ``kind`` unread and
    unvalidated. The observation vocabulary belongs to the detectors, not to the
    grader: a loader that validated it would have to be edited every time a
    detector learned a new signal, and the edit would be made by whoever was
    adding the signal.
    """

    offset_ms: int
    kind: str
    fields: Mapping[str, object]


@dataclass(frozen=True)
class ExpectedLabel:
    """Section 3.2's seven fields, as loaded.

    ``tolerance_ms`` and ``budget_ms`` are the ``T`` and ``L`` of the policy
    revision the case was labelled under, copied into the label on purpose:
    section 3.2 asks what the condition was *entitled* to, and a corpus that
    resolved them live would silently re-grade every past case the day a
    revision changed a budget -- which is precisely what ``D-0031`` versions
    policy to prevent.
    """

    incident_class: str
    onset_offset_ms: int | None
    tolerance_ms: int | None
    budget_ms: int | None
    fact_state: str
    must_not_recommend: tuple[str, ...]
    provenance: str

    @property
    def is_negative(self) -> bool:
        """Is this a ``none`` case -- one that must raise no anomaly?"""

        return self.incident_class == NONE_CLASS

    @property
    def deadline_offset_ms(self) -> int | None:
        """``onset_offset_ms + budget_ms``, the instant a miss becomes a miss.

        ``None`` on a negative case, which has no window: a false positive
        counts wherever in the trace it lands (see :func:`_parse_label`).
        """

        if self.is_negative:
            return None
        assert self.onset_offset_ms is not None and self.budget_ms is not None
        return self.onset_offset_ms + self.budget_ms


@dataclass(frozen=True)
class FixtureCase:
    """One labelled case: its trace, its label, and where it came from on disk."""

    case_id: str
    class_dir: str
    name: str
    path: Path
    observations: tuple[Observation, ...]
    expected: ExpectedLabel

    @property
    def is_negative(self) -> bool:
        return self.expected.is_negative


@dataclass(frozen=True)
class FixtureCorpus:
    """Every case under one root, with the digest that pins this exact content.

    *content_digest* is a sha256 over the ordered bytes of every case file. It
    is here because section 6 requires a report to carry a ``fixture_suite_ref``
    -- and a case count alone does not identify a corpus: editing one label
    changes every number the report prints and moves no count at all. The same
    argument section 6 makes for ``db_fingerprint`` being a content hash rather
    than a row count, applied to the corpus.
    """

    root: Path
    cases: tuple[FixtureCase, ...]
    content_digest: str

    def positives(self) -> tuple[FixtureCase, ...]:
        return tuple(case for case in self.cases if not case.is_negative)

    def negatives(self) -> tuple[FixtureCase, ...]:
        return tuple(case for case in self.cases if case.is_negative)

    def composition(self) -> Mapping[str, int]:
        """Positive, negative and total counts -- section 3.2's reported figure.

        Reported beside the rates and never on its own: the miss rate over a
        corpus is only as meaningful as the negatives that bound it, and the
        composition is what lets a reader see a recall gain and the
        false-positive count it was bought with in one place.
        """

        positive = len(self.positives())
        negative = len(self.negatives())
        return MappingProxyType(
            {
                "positive": positive,
                "negative": negative,
                "total": positive + negative,
            }
        )

    def by_class_dir(self) -> Mapping[str, int]:
        """Case count per class directory, so a thin class is visible."""

        tally: dict[str, int] = {}
        for case in self.cases:
            tally[case.class_dir] = tally.get(case.class_dir, 0) + 1
        return MappingProxyType(dict(sorted(tally.items())))

    def case(self, case_id: str) -> FixtureCase:
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise FixtureRefusal(f"no case {case_id!r} in the corpus at {self.root}")


class SyntheticClock:
    """The evaluation's only source of instants, and the record of what it gave.

    Every offset a detector is run at goes through :meth:`at`, which mints
    ``t0_ms + offset_ms`` and remembers it. :func:`evaluate` then refuses any
    incident stamped with an instant this clock did not mint
    (:class:`ClockNotSynthetic`), which is what makes "the clock is synthetic" a
    property of the harness rather than a rule detectors are asked to follow.

    Minting is deliberately *recording* rather than monotonic advancing: a
    detector may be replayed over a trace in any order the harness likes, and
    forcing a single moving hand would make the clock a schedule as well as a
    clock. What matters here is only that no instant enters the report that did
    not come from ``t0`` plus a declared offset.
    """

    def __init__(self, t0_ms: int) -> None:
        if not isinstance(t0_ms, int) or isinstance(t0_ms, bool):
            raise EvaluationRefusal(
                f"t0_ms must be an integer epoch-ms instant, got {t0_ms!r}"
            )
        self._t0_ms = t0_ms
        self._minted: set[int] = {t0_ms}

    @property
    def t0_ms(self) -> int:
        return self._t0_ms

    def at(self, offset_ms: int) -> int:
        """The instant *offset_ms* after ``t0``, minted and remembered."""

        if not isinstance(offset_ms, int) or isinstance(offset_ms, bool):
            raise EvaluationRefusal(
                f"offset_ms must be an integer, got {offset_ms!r}"
            )
        if offset_ms < 0:
            raise EvaluationRefusal(
                f"offset_ms={offset_ms} precedes t0; a trace's offsets are "
                "measured forward from t0 (measurement-harness.md section 3.2)"
            )
        instant = self._t0_ms + offset_ms
        self._minted.add(instant)
        return instant

    def offset_of(self, instant_ms: int) -> int:
        """The offset of *instant_ms* from ``t0``, for reporting."""

        return instant_ms - self._t0_ms

    def minted(self, instant_ms: int) -> bool:
        """Did this clock hand out *instant_ms*?"""

        return instant_ms in self._minted


@dataclass(frozen=True)
class ProducedIncident:
    """One incident a detector raised while being replayed over a case.

    *incident_class* is an explicit field and not something parsed out of a
    ``dedup_key``: the ``incident`` table carries no class column on purpose
    (``0001_initial.sql``, at ``incident``), the class reaching the row as data,
    and a grader that recovered it by splitting a key would be grading the key
    format. *fact_state* is required for the same reason the column is ``NOT
    NULL`` -- ``D-0005``'s fact is what an incident *is*, and a negative case is
    graded on it (see :func:`_grade_negative`).

    *applied_recommendations* holds only recommendations that were **applied**.
    Section 3.4 is emphatic: ``D-0004`` and AC-6 mean the AI cannot terminate
    anything, so a false termination is an applied ``action``, and counting
    recommendations here would grade Interlock's suggestions against v1's
    executions.
    """

    incident_class: str
    fact_state: str
    created_at_ms: int
    applied_recommendations: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseOutcome:
    """How one case resolved, with every number the verdict rests on."""

    case_id: str
    verdict: str
    latency_ms: int | None
    deadline_ms: int | None
    matching_incidents: int
    late_latency_ms: int | None
    other_class_incidents: tuple[str, ...]
    fact_state_mismatches: tuple[str, ...]
    forbidden_applied: tuple[str, ...]


@dataclass(frozen=True)
class FixtureEvaluation:
    """The graded corpus: verdicts, rates, latencies and the composition."""

    corpus_root: Path
    content_digest: str
    t0_ms: int
    composition: Mapping[str, int]
    outcomes: tuple[CaseOutcome, ...]

    def counts(self) -> Mapping[str, int]:
        """Per-verdict counts, all four keys present **even at zero**.

        An absent key reads as "nothing to report" when it means "this report
        was produced by code that did not look" -- and the key most likely to be
        zero here, ``false_positive``, is the one whose zero is the claim.
        """

        tally = {verdict: 0 for verdict in VERDICTS}
        for outcome in self.outcomes:
            tally[outcome.verdict] += 1
        return MappingProxyType(tally)

    def ids_for(self, verdict: str) -> tuple[str, ...]:
        if verdict not in VERDICTS:
            raise EvaluationRefusal(
                f"{verdict!r} is not one of {', '.join(VERDICTS)}"
            )
        return tuple(
            outcome.case_id
            for outcome in self.outcomes
            if outcome.verdict == verdict
        )

    def miss_rate(self) -> float | None:
        """Misses over positive cases, or ``None`` with no positive cases.

        ``None`` rather than zero: a rate over an empty denominator is not a
        good score, and printing ``0.0`` for it is the harness claiming a result
        it has no cases to support.
        """

        positives = self.composition["positive"]
        if positives == 0:
            return None
        return len(self.ids_for(MISS)) / positives

    def false_positive_rate(self) -> float | None:
        """False positives over negative cases, ``None`` with no negatives."""

        negatives = self.composition["negative"]
        if negatives == 0:
            return None
        return len(self.ids_for(FALSE_POSITIVE)) / negatives

    def latencies_ms(self) -> tuple[int, ...]:
        """Detection latencies, ascending, over detected cases only.

        Misses contribute nothing on purpose and that is *not* a silent drop:
        the miss count is printed beside the distribution, because a latency
        distribution that improved by turning slow detections into misses must
        not read as an improvement.
        """

        return tuple(
            sorted(
                outcome.latency_ms
                for outcome in self.outcomes
                if outcome.latency_ms is not None
            )
        )

    def forbidden_applied(self) -> tuple[tuple[str, str], ...]:
        """``(case_id, recommendation)`` for every applied forbidden action.

        Section 3.4's shape: counted at the applied effect, not at the
        recommendation.
        """

        return tuple(
            (outcome.case_id, recommendation)
            for outcome in self.outcomes
            for recommendation in outcome.forbidden_applied
        )


def _refuse_label(case_path: Path, message: str) -> LabelMalformed:
    return LabelMalformed(f"{case_path}/{EXPECTED_FILENAME}: {message}")


def _require_int(
    value: object, *, field: str, case_path: Path, minimum: int = 0
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _refuse_label(
            case_path, f"{field} must be an integer, got {value!r}"
        )
    if value < minimum:
        raise _refuse_label(
            case_path, f"{field}={value} must be >= {minimum}"
        )
    return value


def _parse_label(payload: object, *, case_path: Path) -> ExpectedLabel:
    """Section 3.2's table, checked field by field.

    The one rule not stated in the table and derived here: on a **negative**
    case ``onset_offset_ms``, ``tolerance_ms`` and ``budget_ms`` must be
    ``null``. A ``none`` case has no condition, so it has no state entry to
    measure from and no budget it was entitled to; a number in those fields
    would be a window, and a window would suggest a false positive counts only
    inside it. It does not -- an alarm on a healthy worker is wrong at every
    offset, so the whole trace is the test.
    """

    if not isinstance(payload, dict):
        raise _refuse_label(case_path, "the label must be a JSON object")

    missing = [field for field in LABEL_FIELDS if field not in payload]
    if missing:
        raise _refuse_label(
            case_path,
            "missing required field(s) "
            + ", ".join(sorted(missing))
            + "; section 3.2 requires all seven, and every default that could "
            "be chosen for a missing one makes the fixture easier to pass",
        )
    unknown = [field for field in payload if field not in LABEL_FIELDS]
    if unknown:
        raise _refuse_label(
            case_path,
            "unknown field(s) "
            + ", ".join(sorted(unknown))
            + "; a field nothing reads is a label the grader ignores",
        )

    incident_class = payload["incident_class"]
    if not isinstance(incident_class, str) or not incident_class:
        raise _refuse_label(
            case_path,
            f"incident_class must be a non-empty string or {NONE_CLASS!r}, "
            f"got {incident_class!r}",
        )

    fact_state = payload["fact_state"]
    if fact_state not in FACT_STATES:
        raise _refuse_label(
            case_path,
            f"fact_state={fact_state!r} is not one of D-0005's closed set "
            f"({', '.join(FACT_STATES)}); a seventh state is a new D- entry, "
            "not a fixture",
        )

    provenance = payload["provenance"]
    if not isinstance(provenance, str) or not provenance.strip():
        raise _refuse_label(
            case_path, f"provenance must be a non-empty string, got {provenance!r}"
        )
    kind = provenance.split(":", 1)[0].strip()
    if kind not in PROVENANCE_KINDS:
        raise _refuse_label(
            case_path,
            f"provenance kind {kind!r} is not one of "
            f"{', '.join(PROVENANCE_KINDS)}; the field exists to answer whether "
            "this corpus is made of things that happened or things we imagined, "
            "and free text cannot be counted",
        )

    recommendations = payload["must_not_recommend"]
    if not isinstance(recommendations, list) or any(
        not isinstance(item, str) or not item for item in recommendations
    ):
        raise _refuse_label(
            case_path,
            "must_not_recommend must be a list of non-empty strings, got "
            f"{recommendations!r} (an empty list is allowed and says so)",
        )

    windowed = ("onset_offset_ms", "tolerance_ms", "budget_ms")
    if incident_class == NONE_CLASS:
        present = [field for field in windowed if payload[field] is not None]
        if present:
            raise _refuse_label(
                case_path,
                "a negative case must leave "
                + ", ".join(present)
                + " null: it has no condition, so no state entry to measure "
                "from and no budget it was entitled to, and a false positive "
                "is wrong at every offset rather than inside a window",
            )
        onset = tolerance = budget = None
    else:
        onset = _require_int(
            payload["onset_offset_ms"], field="onset_offset_ms", case_path=case_path
        )
        tolerance = _require_int(
            payload["tolerance_ms"], field="tolerance_ms", case_path=case_path
        )
        budget = _require_int(
            payload["budget_ms"], field="budget_ms", case_path=case_path, minimum=1
        )
        if tolerance > budget:
            raise _refuse_label(
                case_path,
                f"tolerance_ms={tolerance} exceeds budget_ms={budget}; T is "
                "part of L, not a head start on it (time-base-policy.md "
                "section 3.1), so a label with T > L describes a class whose "
                "detector is out of budget before it is allowed to look",
            )

    return ExpectedLabel(
        incident_class=incident_class,
        onset_offset_ms=onset,
        tolerance_ms=tolerance,
        budget_ms=budget,
        fact_state=fact_state,
        must_not_recommend=tuple(recommendations),
        provenance=provenance,
    )


def _parse_trace(text: str, *, case_path: Path) -> tuple[Observation, ...]:
    """The observations, in the order the detector will see them.

    Offsets must be non-decreasing. A trace that goes backwards would hand a
    replayed detector an observation from before the one it already processed,
    and a detector that behaved differently under that ordering would be graded
    on a world no clock can produce.
    """

    observations: list[Observation] = []
    previous = -1
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise TraceMalformed(
                f"{case_path}/{TRACE_FILENAME} line {number}: not JSON ({error})"
            ) from error
        if not isinstance(payload, dict):
            raise TraceMalformed(
                f"{case_path}/{TRACE_FILENAME} line {number}: each observation "
                "must be a JSON object"
            )
        if "offset_ms" not in payload:
            raise TraceMalformed(
                f"{case_path}/{TRACE_FILENAME} line {number}: no offset_ms; "
                "section 3.2 makes every observation an offset in ms from t0, "
                "and an observation with no time cannot be replayed"
            )
        offset = payload["offset_ms"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise TraceMalformed(
                f"{case_path}/{TRACE_FILENAME} line {number}: offset_ms must be "
                f"a non-negative integer, got {offset!r}"
            )
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind:
            raise TraceMalformed(
                f"{case_path}/{TRACE_FILENAME} line {number}: kind must be a "
                f"non-empty string, got {kind!r}"
            )
        if offset < previous:
            raise TraceMalformed(
                f"{case_path}/{TRACE_FILENAME} line {number}: offset_ms={offset} "
                f"precedes the previous observation's {previous}; a trace is "
                "replayed in file order and must not go backwards"
            )
        previous = offset
        observations.append(
            Observation(
                offset_ms=offset,
                kind=kind,
                fields=MappingProxyType(
                    {
                        key: value
                        for key, value in payload.items()
                        if key not in ("offset_ms", "kind")
                    }
                ),
            )
        )
    if not observations:
        raise TraceMalformed(
            f"{case_path}/{TRACE_FILENAME} holds no observations; a case with "
            "an empty trace grades a detector against nothing and would score "
            "as a clean true negative"
        )
    return tuple(observations)


def load_case(case_path: Path, *, class_dir: str | None = None) -> FixtureCase:
    """Load one ``<class>/<case>/`` directory, refusing anything malformed.

    *class_dir* defaults to the parent directory's name, which is what
    :func:`load_corpus` passes; it is a parameter so a caller loading a single
    case out of tree still gets the class-directory check rather than skipping
    it.

    :raises CaseIncomplete: a required file is absent.
    :raises StrayEntryRefused: the directory holds a third file.
    :raises LabelMalformed: ``expected.json`` fails section 3.2's table.
    :raises TraceMalformed: ``trace.jsonl`` is unparseable, empty or unordered.
    :raises ClassDirectoryMismatch: a positive case is filed under another class.
    """

    case_path = Path(case_path)
    if not case_path.is_dir():
        raise CaseIncomplete(f"{case_path} is not a directory")
    if class_dir is None:
        class_dir = case_path.parent.name

    present = {entry.name for entry in case_path.iterdir()}
    missing = [name for name in CASE_FILES if name not in present]
    if missing:
        raise CaseIncomplete(
            f"{case_path} is missing {', '.join(missing)}; half a case has no "
            "correct outcome to grade against"
        )
    stray = sorted(present - set(CASE_FILES))
    if stray:
        raise StrayEntryRefused(
            f"{case_path} holds {', '.join(stray)} beside the two case files; "
            "an input nothing loads is a case graded against less than it "
            "contains"
        )

    try:
        label_payload = json.loads(
            (case_path / EXPECTED_FILENAME).read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as error:
        raise _refuse_label(case_path, f"not JSON ({error})") from error
    expected = _parse_label(label_payload, case_path=case_path)
    observations = _parse_trace(
        (case_path / TRACE_FILENAME).read_text(encoding="utf-8"),
        case_path=case_path,
    )

    if not expected.is_negative and expected.incident_class != class_dir:
        raise ClassDirectoryMismatch(
            f"{case_path} is filed under class directory {class_dir!r} but is "
            f"labelled {expected.incident_class!r}; the composition table "
            "groups by directory, so this case would report coverage of a "
            "class it does not test"
        )

    return FixtureCase(
        case_id=f"{class_dir}/{case_path.name}",
        class_dir=class_dir,
        name=case_path.name,
        path=case_path,
        observations=observations,
        expected=expected,
    )


def _digest(cases: Sequence[FixtureCase]) -> str:
    """sha256 over the ordered bytes of every case file.

    Content, not counts, for section 6's reason: editing one label changes every
    number a report prints and moves no count at all, so a suite reference built
    from counts would certify two materially different corpora as the same one.
    """

    digest = hashlib.sha256()
    for case in sorted(cases, key=lambda case: case.case_id):
        for filename in CASE_FILES:
            digest.update(case.case_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(filename.encode("utf-8"))
            digest.update(b"\0")
            digest.update((case.path / filename).read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def load_corpus(root: Path) -> FixtureCorpus:
    """Load every case under *root*, refusing a corpus AC-10 cannot rest on.

    Walks ``<root>/<class>/<case>/`` exactly as section 3.2 lays it out. A
    ``README.md`` at either level is allowed and ignored; every other stray
    entry is refused, because the corpus and its reported composition must not
    be able to disagree.

    :raises NegativeCasesRequired: the corpus has no ``none`` case.
    :raises PositiveCasesRequired: the corpus has no labelled condition.
    """

    root = Path(root)
    if not root.is_dir():
        raise FixtureRefusal(f"corpus root {root} is not a directory")

    cases: list[FixtureCase] = []
    for class_entry in sorted(root.iterdir(), key=lambda path: path.name):
        if class_entry.name == "README.md" or class_entry.name.startswith("."):
            continue
        if not class_entry.is_dir():
            raise StrayEntryRefused(
                f"{class_entry} is not a class directory; section 3.2's layout "
                "is <root>/<class>/<case>/"
            )
        for case_entry in sorted(class_entry.iterdir(), key=lambda path: path.name):
            if case_entry.name == "README.md" or case_entry.name.startswith("."):
                continue
            if not case_entry.is_dir():
                raise StrayEntryRefused(
                    f"{case_entry} is not a case directory; section 3.2's "
                    "layout is <root>/<class>/<case>/"
                )
            # No duplicate check: ``case_id`` is ``<class>/<case>``, which is
            # the path, so the filesystem already guarantees uniqueness. A
            # defensive check here would be code no input can reach, and
            # unreachable code is the kind that stops being true quietly.
            cases.append(load_case(case_entry, class_dir=class_entry.name))

    corpus = FixtureCorpus(
        root=root, cases=tuple(cases), content_digest=_digest(cases)
    )
    composition = corpus.composition()
    if composition["negative"] == 0:
        raise NegativeCasesRequired(
            f"the corpus at {root} has {composition['positive']} positive "
            "case(s) and no negative case; D-0006 requires observation-failure "
            "fixtures alongside stall fixtures, and a detector that alarms on "
            "everything scores a perfect miss rate over positives alone "
            "(measurement-harness.md section 3.2, D-0039)"
        )
    if composition["positive"] == 0:
        raise PositiveCasesRequired(
            f"the corpus at {root} has {composition['negative']} negative "
            "case(s) and no positive case, so it cannot express a miss at all; "
            "a detector that raises nothing scores a perfect false-positive "
            "rate over negatives alone"
        )
    return corpus


def _grade_positive(
    case: FixtureCase,
    incidents: Sequence[ProducedIncident],
    *,
    clock: SyntheticClock,
) -> CaseOutcome:
    """Detected or missed, by section 3.2's definition of a match.

    A match is an incident **of the labelled class** raised at or before
    ``t0 + onset_offset_ms + budget_ms``. Incidents of other classes are
    recorded rather than counted: they are neither the detection this case asks
    for nor a false positive this case can prove, and a grader that quietly
    accepted one would let a detector pass by raising the wrong alarm loudly.
    """

    expected = case.expected
    assert expected.onset_offset_ms is not None
    onset_ms = clock.at(expected.onset_offset_ms)
    deadline_offset = expected.deadline_offset_ms
    assert deadline_offset is not None
    deadline_ms = clock.at(deadline_offset)

    in_budget: list[int] = []
    late: list[int] = []
    other_classes: list[str] = []
    mismatched_states: list[str] = []
    for incident in incidents:
        if incident.incident_class != expected.incident_class:
            other_classes.append(incident.incident_class)
            continue
        if incident.fact_state != expected.fact_state:
            # Not a reason to withhold the detection -- section 3.2 defines a
            # match by class -- but never swallowed either: an alarm of the
            # right class carrying the wrong D-0005 fact is a detector that
            # found the condition and described it as something else, and that
            # is what the Dispatcher AI will read.
            mismatched_states.append(incident.fact_state)
        if incident.created_at_ms < onset_ms:
            raise IncidentBeforeOnset(
                f"{case.case_id}: an incident of class "
                f"{incident.incident_class!r} is stamped "
                f"{onset_ms - incident.created_at_ms} ms before the labelled "
                "onset; the latency would be negative, which means either the "
                "label's onset or the detector's attribution is wrong"
            )
        if incident.created_at_ms <= deadline_ms:
            in_budget.append(incident.created_at_ms)
        else:
            late.append(incident.created_at_ms)

    forbidden = _forbidden_applied(case, incidents)
    if in_budget:
        # The earliest alarm is the detection: a second incident for one
        # condition is a re-notification, and grading on the last one would
        # report the detector's repeat interval as its latency.
        latency = min(in_budget) - onset_ms
        return CaseOutcome(
            case_id=case.case_id,
            verdict=DETECTED,
            latency_ms=latency,
            deadline_ms=deadline_ms,
            matching_incidents=len(in_budget),
            late_latency_ms=None,
            other_class_incidents=tuple(other_classes),
            fact_state_mismatches=tuple(mismatched_states),
            forbidden_applied=forbidden,
        )
    return CaseOutcome(
        case_id=case.case_id,
        verdict=MISS,
        latency_ms=None,
        deadline_ms=deadline_ms,
        matching_incidents=0,
        # A late alarm is still a miss (section 3.2 defines a miss by the
        # budget) but it is a different miss from silence, and the report says
        # which: one is a detector that is slow, the other a detector that is
        # blind, and the fixes have nothing in common.
        late_latency_ms=(min(late) - onset_ms) if late else None,
        other_class_incidents=tuple(other_classes),
        fact_state_mismatches=tuple(mismatched_states),
        forbidden_applied=forbidden,
    )


def _grade_negative(
    case: FixtureCase, incidents: Sequence[ProducedIncident]
) -> CaseOutcome:
    """False positive or true negative, judged on ``D-0005``'s fact.

    A negative case is not "the detector must emit nothing". AC-3 requires the
    opposite for the case that matters most: an observation outage **must** be
    classified ``OBSERVATION_UNAVAILABLE``, and a row saying so is the required
    output rather than an alarm. ``D-0006`` says the same for
    ``NO_ACTIVITY_EVIDENCE``, which is explicitly not an anomaly. So the label's
    ``fact_state`` is what the detector is permitted to say, and a false
    positive is an incident carrying **any other** fact -- the outage read as a
    stall, the quiet worker read as dead. Grading a negative case as "produced
    no row at all" would fail a detector for obeying AC-3.
    """

    offending = tuple(
        incident.fact_state
        for incident in incidents
        if incident.fact_state != case.expected.fact_state
    )
    conforming = len(incidents) - len(offending)
    forbidden = _forbidden_applied(case, incidents)
    return CaseOutcome(
        case_id=case.case_id,
        verdict=FALSE_POSITIVE if offending else TRUE_NEGATIVE,
        latency_ms=None,
        deadline_ms=None,
        matching_incidents=conforming,
        late_latency_ms=None,
        other_class_incidents=tuple(
            incident.incident_class
            for incident in incidents
            if incident.fact_state != case.expected.fact_state
        ),
        fact_state_mismatches=offending,
        forbidden_applied=forbidden,
    )


def _forbidden_applied(
    case: FixtureCase, incidents: Sequence[ProducedIncident]
) -> tuple[str, ...]:
    """Recommendations in ``must_not_recommend`` that were actually applied.

    Section 3.4: the count is at the applied effect. A recommendation the
    Secretary or a human declined is not a false termination -- it is the
    human gate working -- and folding it in here would report the gate's value
    as a defect.
    """

    forbidden = set(case.expected.must_not_recommend)
    return tuple(
        recommendation
        for incident in incidents
        for recommendation in incident.applied_recommendations
        if recommendation in forbidden
    )


def evaluate(
    corpus: FixtureCorpus,
    *,
    clock: SyntheticClock,
    outcomes: Mapping[str, Sequence[ProducedIncident]],
) -> FixtureEvaluation:
    """Grade *corpus* against what a detector produced, on a synthetic clock.

    *outcomes* maps ``case_id`` to the incidents the detector raised while being
    replayed over that case. **Every case must appear**, an empty sequence
    included: see :class:`OutcomeMissing` for why an absent entry cannot be read
    as "produced nothing".

    Every ``created_at_ms`` must be an instant *clock* minted, which is what
    makes the reported latency exact rather than sampled
    (:class:`ClockNotSynthetic`).

    :raises UnknownCaseInOutcomes: an outcome names a case not in the corpus.
    :raises IncidentBeforeOnset: a matching incident predates its own onset.
    """

    case_ids = {case.case_id for case in corpus.cases}
    unknown = sorted(set(outcomes) - case_ids)
    if unknown:
        raise UnknownCaseInOutcomes(
            "outcomes name case(s) not in this corpus: "
            + ", ".join(unknown)
            + f" (corpus root {corpus.root}); the report would be about cases "
            "nobody graded"
        )
    missing = sorted(case_ids - set(outcomes))
    if missing:
        raise OutcomeMissing(
            "no detector outcome for case(s): "
            + ", ".join(missing)
            + "; pass an empty sequence to state that the detector produced "
            "nothing, so that a harness that failed to run a case cannot score "
            "it as a clean result"
        )

    # Every instant is checked BEFORE any case is graded, and the order is not
    # incidental: grading mints a case's own onset and deadline, so a check
    # interleaved with grading would let one case's minting vouch for the next
    # case's wall-clock stamp. The whole point of the check is that no instant
    # from outside the clock enters the report.
    for case in corpus.cases:
        for incident in outcomes[case.case_id]:
            if not clock.minted(incident.created_at_ms):
                raise ClockNotSynthetic(
                    f"{case.case_id}: incident created_at_ms="
                    f"{incident.created_at_ms} was not minted by this "
                    f"evaluation's clock (t0={clock.t0_ms}); latency measured "
                    "against it would be sampled, not exact "
                    "(measurement-harness.md section 3.2)"
                )

    graded: list[CaseOutcome] = []
    for case in corpus.cases:
        incidents = tuple(outcomes[case.case_id])
        if case.is_negative:
            graded.append(_grade_negative(case, incidents))
        else:
            graded.append(_grade_positive(case, incidents, clock=clock))

    return FixtureEvaluation(
        corpus_root=corpus.root,
        content_digest=corpus.content_digest,
        t0_ms=clock.t0_ms,
        composition=corpus.composition(),
        outcomes=tuple(graded),
    )


def _percentile(values: Sequence[int], fraction: float) -> int | None:
    """Nearest-rank percentile over an ascending sequence.

    Nearest-rank rather than an interpolated one: an interpolated p90 over four
    detections reports a latency no detection had, and a corpus is small by
    construction.
    """

    if not values:
        return None
    rank = max(1, min(len(values), int(-(-len(values) * fraction // 1))))
    return values[rank - 1]


def render_fixture_report(evaluation: FixtureEvaluation) -> str:
    """Render *evaluation* as plain ASCII text, composition first.

    ASCII only, ``-`` never an em-dash: this reaches a cp932 console, where a
    single U+2014 turns a report into a ``UnicodeEncodeError``.

    The composition and both rates print in **one** table, which is section
    3.2's point rather than a layout preference: recall bought by widening every
    predicate arrives as a false-positive regression, and it can only be read as
    a trade if the reader sees both numbers without turning a page. There is no
    pass/fail line -- ``Q-0005`` leaves AC-10's threshold open, and a verdict
    here would answer it by inertia.
    """

    counts = evaluation.counts()
    composition = evaluation.composition
    latencies = evaluation.latencies_ms()
    lines: list[str] = []
    lines.append("AC-10 fixture corpus -- labelled ground truth (source A)")
    lines.append(f"  corpus root     {evaluation.corpus_root}")
    lines.append(f"  content digest  {evaluation.content_digest}")
    lines.append(f"  synthetic t0    {evaluation.t0_ms} (epoch ms)")
    lines.append("")

    lines.append("Composition and rates (one table on purpose)")
    lines.append(
        f"  positive cases  {composition['positive']}"
        f"    misses {counts[MISS]}    miss rate {_rate(evaluation.miss_rate())}"
    )
    lines.append(
        f"  negative cases  {composition['negative']}"
        f"    false positives {counts[FALSE_POSITIVE]}"
        f"    fp rate {_rate(evaluation.false_positive_rate())}"
    )
    lines.append(f"  total cases     {composition['total']}")
    lines.append(
        "  a recall gain bought by widening predicates lands in the "
        "false-positive row above"
    )
    lines.append("")

    lines.append("Verdicts")
    for verdict in VERDICTS:
        lines.append(f"  {verdict:<15} {counts[verdict]}")
    lines.append("")

    lines.append("Detection latency, onset to incident (detected cases only)")
    if latencies:
        lines.append(
            f"  count {len(latencies)}"
            f"    median {_percentile(latencies, 0.5)} ms"
            f"    p90 {_percentile(latencies, 0.9)} ms"
            f"    max {latencies[-1]} ms"
        )
    else:
        lines.append("  no detected case; the distribution is not computable")
    lines.append(
        f"  misses excluded from the distribution: {counts[MISS]}"
        "    (a distribution improved by missing the slow ones is not an "
        "improvement)"
    )
    lines.append("")

    lines.append("Cases needing a reader")
    reported = False
    for outcome in evaluation.outcomes:
        notes: list[str] = []
        if outcome.verdict == MISS:
            notes.append(
                f"alarmed late at {outcome.late_latency_ms} ms after onset"
                if outcome.late_latency_ms is not None
                else "no alarm of the labelled class at all"
            )
        if outcome.fact_state_mismatches:
            notes.append(
                "fact_state " + ", ".join(sorted(set(outcome.fact_state_mismatches)))
            )
        if outcome.other_class_incidents:
            notes.append(
                "other classes raised: "
                + ", ".join(sorted(set(outcome.other_class_incidents)))
            )
        if outcome.forbidden_applied:
            notes.append(
                "APPLIED a forbidden recommendation: "
                + ", ".join(sorted(set(outcome.forbidden_applied)))
            )
        if notes:
            reported = True
            lines.append(f"  {outcome.case_id} [{outcome.verdict}]")
            for note in notes:
                lines.append(f"      {note}")
    if not reported:
        lines.append("  none")

    return "\n".join(lines)


def _rate(value: float | None) -> str:
    """A rate, or the reason there is not one.

    ``None`` prints as "no denominator" rather than as ``0.00``: a rate over
    zero cases is not a good score.
    """

    if value is None:
        return "no denominator"
    return f"{value:.2f}"
