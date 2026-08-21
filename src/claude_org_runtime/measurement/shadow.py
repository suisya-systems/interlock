"""G6 -- shadow reconciliation: four correlation keys, five buckets, no silent drop.

The failure this module is written against is the one
``docs/measurement-harness.md`` section 3.1 states outright: **Interlock's own
tables cannot contain a miss.** A missed condition produces no ``incident`` row,
so an aggregate over ``incident`` counts what was detected and is structurally
blind to what was not, and any harness reading only our rows measures its own
recall as 100%. AC-10 is a gate; a gate that reads its own answer off the thing
it is gating is not a gate.

Section 3.3's answer is a second observer. During the shadow period v1 and
Interlock watch the same world, so v1's episodes are ground truth Interlock did
not produce. The comparison is **episode to episode, never row to row** -- the
two systems have different schemas and different vocabularies, and an episode is
*one real-world condition as seen by one system*. What joins them is a
correlation key computed, on each side, out of what that side already stores.

**The four keys, section 3.3's table, and where each one comes from here:**

============================  =====================================================
Subject class                 Interlock source
============================  =====================================================
``ci_outcome``                ``ci_observation`` joined to ``repository``, via the
                              ``ci_current_verdict`` projection
``pr_merge``                  ``pull_request`` joined to ``repository``
``worker_escalation``         ``gate`` ordered by ``created_at_ms`` -- **positional**
``session_liveness``          ``incident`` joined to ``session``, onset bucketed to 60 s
============================  =====================================================

**The escalation key is positional and this module says so out loud, twice.**
Sections 3.3 and 7 both name it the weakest join in the reconciliation: v1's
register has its own entry id Interlock never sees, so "the nth escalation of
this run by ordered receipt time" is the only key both sides can compute. It is
sound *as long as both systems saw the same escalations in the same order* --
which is exactly what a divergence violates. That is not a hole, it is the safe
direction: an ordering mismatch shifts every subsequent position on one side, so
the episodes stop pairing and surface as ``interlock_only`` / ``v1_only``
noise rather than as a silently wrong pairing that would report one system's
escalation as the other's. The caveat rides on :data:`POSITIONAL_KEY_CAVEAT`,
on every :class:`CorrelationKey` this class produces
(:attr:`CorrelationKey.positional`), and on the report, so a reader cannot see a
run of unmatched escalation episodes without also seeing why the key is the
first thing to doubt.

**The five buckets are output, not bookkeeping.** v1's own reporter established
the policy -- its CI-to-run join is "a 3-stage fallback (never a silent drop)"
ending in an explicit ``unmatched`` bucket -- and section 3.3 carries it:
:data:`BOTH`, :data:`INTERLOCK_ONLY`, :data:`V1_ONLY`, :data:`UNMATCHED_KEY`,
:data:`CENSORED`. Every episode handed in lands in exactly one of them
(:meth:`ShadowReconciliation.filed_episode_ids` is the partition, and it is
asserted rather than assumed).

**``v1_only`` is a candidate miss, and the two ways to get it wrong are both
made unreachable.** v1 raising something Interlock did not can mean Interlock
missed it -- or that v1 false-positived, which is the whole reason AC-10 has a
false-positive series at all. So:

* Converting ``v1_only`` into a miss count without adjudicating it is
  impossible: :meth:`ShadowReconciliation.confirmed_miss_count` raises
  :class:`AdjudicationPending` while any ``v1_only`` episode carries no
  classification. There is no other method that returns a miss number.
* Discarding it is impossible: an unclassified episode is still in
  :attr:`ShadowReconciliation.v1_only`, still counted by
  :meth:`~ShadowReconciliation.counts`, and listed with its evidence by
  :meth:`~ShadowReconciliation.awaiting_adjudication`, which the renderer
  prints. A fixture label settles the ones a fixture covers (section 3.2); the
  rest go in front of a human, named.

**The v1 side is a separable adapter, and that is a requirement rather than
tidiness.** Outside the shadow period there is no v1 data at all, and the
harness still has to run. So this module never goes looking for v1's files: it
takes :class:`V1Reference`, and a reference that is absent -- or that came back
empty, which is the same statement made by a reader that ran and found nothing
to say -- produces the **no-shadow-reference state**, not a comparison. In that
state :meth:`~ShadowReconciliation.counts` *refuses*, because five zeroes and
"there was nothing to compare against" read identically to a human and only one
of them is true; an empty v1 list silently reconciled would file every Interlock
episode as ``interlock_only`` and report a period of pure improvement.

**``censored`` is not computed here.** It comes from
:mod:`~claude_org_runtime.measurement.windows` (section 3.5), and
:func:`censored_episode_ids` is the one adaptor between the two. Censoring wins
over every other bucket, and a matched pair is censored if *either* half is:
judging half a pair against a period that only observed half of it is the same
manufactured miss section 3.5 exists to remove.

**Nothing here writes, and nothing here reads a clock.** The connection is the
read-only handle from
:func:`~claude_org_runtime.measurement.reader.open_for_measurement`; every bound
is the caller's; every statement issued is a ``SELECT``.

**Scope.** This module correlates and files. It does not raise an incident, does
not apply a remedy, and does not decide AC-10's verdict -- the reconcile driver
that would do any of that is out of scope for this branch and is not implied by
anything here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Collection, Iterable, Mapping, Sequence

from claude_org_runtime.control_plane import ci_ingest
from claude_org_runtime.measurement.reader import ControlPlaneRefusal
from claude_org_runtime.measurement.windows import (
    CENSORED as WINDOW_CENSORED,
    CENSORED_LEFT as WINDOW_CENSORED_LEFT,
    WindowReport,
)

__all__ = [
    "ADJUDICATIONS",
    "AWAITING_HUMAN",
    "AdjudicationPending",
    "BOTH",
    "CENSORED",
    "CorrelationKey",
    "DuplicateCorrelationKey",
    "DuplicateEpisodeIdRefused",
    "EpisodeKeyRefused",
    "FROM_FIXTURE_LABEL",
    "INTERLOCK_ONLY",
    "MISS",
    "MatchedPair",
    "ONSET_BUCKET_MS",
    "POSITIONAL_KEY_CAVEAT",
    "POSITIONAL_SUBJECT_CLASSES",
    "RECONCILIATION_BUCKETS",
    "SHADOW_ABSENT",
    "SHADOW_PRESENT",
    "SUBJECT_CI_OUTCOME",
    "SUBJECT_CLASSES",
    "SUBJECT_PR_MERGE",
    "SUBJECT_SESSION_LIVENESS",
    "SUBJECT_WORKER_ESCALATION",
    "ShadowEpisode",
    "ShadowReconciliation",
    "ShadowReferenceAbsent",
    "ShadowRefusal",
    "UNDETERMINED",
    "UNMATCHED_KEY",
    "UnknownAdjudication",
    "UnknownSubjectClass",
    "V1_FALSE_POSITIVE",
    "V1_ONLY",
    "V1OnlyEpisode",
    "V1Reference",
    "censored_episode_ids",
    "read_ci_outcome_episodes",
    "read_interlock_episodes",
    "read_pr_merge_episodes",
    "read_session_liveness_episodes",
    "read_worker_escalation_episodes",
    "reconcile",
    "render_shadow_reconciliation",
]


#: The subject classes of section 3.3's correlation table. Closed, because a
#: fifth class arriving as free text would be reconciled against nothing on the
#: v1 side and would quietly file every one of its episodes as
#: ``interlock_only`` -- a candidate improvement invented by a typo.
SUBJECT_CI_OUTCOME = "ci_outcome"
SUBJECT_PR_MERGE = "pr_merge"
SUBJECT_WORKER_ESCALATION = "worker_escalation"
SUBJECT_SESSION_LIVENESS = "session_liveness"

SUBJECT_CLASSES: tuple[str, ...] = (
    SUBJECT_CI_OUTCOME,
    SUBJECT_PR_MERGE,
    SUBJECT_WORKER_ESCALATION,
    SUBJECT_SESSION_LIVENESS,
)

#: The classes whose key is positional rather than natural on either side.
#: Kept as data so the caveat attaches itself: a hand-built escalation episode
#: gets the same flag as one this module read, and no consumer has to remember
#: which class was the weak one.
POSITIONAL_SUBJECT_CLASSES: frozenset[str] = frozenset({SUBJECT_WORKER_ESCALATION})

#: Carried into the report verbatim (sections 3.3 and 7). ASCII only: this
#: string reaches stdout through :func:`render_shadow_reconciliation`, and a
#: cp932 console cannot encode an em-dash.
POSITIONAL_KEY_CAVEAT = (
    "worker_escalation is keyed positionally - the nth escalation of a run by "
    "ordered receipt time - because v1's register id is not visible to "
    "Interlock. It is sound only while both systems saw the same escalations "
    "in the same order; an ordering divergence shifts every later position and "
    "surfaces as unmatched episodes rather than as a wrong pairing, which is "
    "the safe direction. Many unmatched escalation episodes mean the key needs "
    "replacing before these numbers mean anything."
)

#: Section 3.3: the session-liveness key buckets the onset to 60 s. It is
#: document data, not policy data, which is why it is a constant here and not a
#: ``policy_*`` read: the two systems detect the same condition at different
#: latencies, and the bucket is what absorbs that difference without letting
#: two genuinely distinct conditions on one run collapse into one episode.
ONSET_BUCKET_MS = 60_000

#: Section 3.3's five buckets, always emitted in this order.
BOTH = "both"
INTERLOCK_ONLY = "interlock_only"
V1_ONLY = "v1_only"
UNMATCHED_KEY = "unmatched_key"
CENSORED = "censored"

RECONCILIATION_BUCKETS: tuple[str, ...] = (
    BOTH,
    INTERLOCK_ONLY,
    V1_ONLY,
    UNMATCHED_KEY,
    CENSORED,
)

#: How a ``v1_only`` episode was settled. ``undetermined`` is a first-class
#: answer, not a failure to answer: ``D-0006``'s "cannot determine is a
#: legitimate outcome" applied to the measurement instead of to the detection
#: (section 3.4 says the same of false termination).
MISS = "miss"
V1_FALSE_POSITIVE = "v1_false_positive"
UNDETERMINED = "undetermined"

ADJUDICATIONS: tuple[str, ...] = (MISS, V1_FALSE_POSITIVE, UNDETERMINED)

#: Where the adjudication came from, recorded because ``D-0040`` makes the
#: provenance of a number part of the report: a miss settled by a fixture label
#: is reproducible, and one settled by a human is not, and a reader who cannot
#: tell them apart cannot recompute either.
FROM_FIXTURE_LABEL = "fixture_label"
AWAITING_HUMAN = "awaiting_human_adjudication"

#: Whether this report had a second observer at all.
SHADOW_PRESENT = "present"
SHADOW_ABSENT = "absent"

#: The separator inside a key token. ASCII unit separator, chosen because no
#: component of any of the four keys can contain it: a repository slug, a
#: decimal PR number, a 40-hex SHA, a run id and a bucket ordinal are all
#: printable. A separator that *could* occur (``|``, ``:``) would let two
#: different keys spell one token and pair two unrelated episodes.
_KEY_SEPARATOR = "\x1f"


class ShadowRefusal(ControlPlaneRefusal):
    """A reconciliation that cannot be computed honestly, stated rather than guessed."""


class UnknownSubjectClass(ShadowRefusal):
    """An episode named a subject class outside :data:`SUBJECT_CLASSES`.

    Refused rather than passed through: an unrecognised class has no counterpart
    on the v1 side, so every episode carrying it would pair with nothing and be
    filed ``interlock_only`` -- reported as a candidate improvement that is
    really a spelling mistake.
    """


class EpisodeKeyRefused(ShadowRefusal):
    """An episode carries neither a key nor a reason for not having one, or both.

    The pair is exclusive by construction so that "the key could not be
    computed" is a *statement in the data* rather than the absence of one.
    A ``None`` key with no reason attached is how an episode gets dropped in
    silence: the reconciliation has no bucket to file it under and no sentence
    to print about it.
    """


class DuplicateEpisodeIdRefused(ShadowRefusal):
    """One ``episode_id`` reached the report twice, or from both sides.

    Ids are the report's own handles -- the windows module classifies by them
    and :func:`censored_episode_ids` is looked up by them -- so a collision
    makes censoring apply to the wrong episode, and a collision *across* sides
    makes an Interlock episode inherit a v1 episode's censoring. Counting the
    same id twice also moves a numerator with nothing visible in the counts:
    the totals simply come out one too high.
    """


class DuplicateCorrelationKey(ShadowRefusal):
    """Two episodes on one side computed the same correlation key.

    Matching is one-to-one; with two candidates for one key, whichever the
    dictionary happened to keep would pair and the other would be filed
    ``interlock_only`` / ``v1_only`` -- a fabricated improvement or a fabricated
    miss, chosen by iteration order. For ``worker_escalation`` a collision is
    the positional key failing exactly as section 3.3 warns (two escalations at
    the same ordinal), and it is named here rather than absorbed.
    """


class UnknownAdjudication(ShadowRefusal):
    """A fixture label settled a ``v1_only`` episode with a word outside :data:`ADJUDICATIONS`."""


class AdjudicationPending(ShadowRefusal):
    """A miss count was asked for while ``v1_only`` episodes remain unclassified.

    Section 3.3: "the report never silently converts ``v1_only`` into a miss
    count". This is that sentence made structural -- the only method returning a
    miss number refuses until every candidate has been settled by a fixture
    label or a human. The refusal names the episodes, so the answer to it is to
    adjudicate them, not to widen a filter.
    """


class ShadowReferenceAbsent(ShadowRefusal):
    """A comparison number was asked for from a report that had no second observer.

    Not an error condition -- outside the shadow period this is the normal state
    of the world, and the harness still runs. It is a refusal because the
    alternative is worse than an exception: five zero buckets say "the two
    systems agreed about nothing at all", which is what a reader takes from a
    printed table, and the truth is "there was no other system to agree with".
    """


class ShadowReferenceRefused(ShadowRefusal):
    """A :class:`V1Reference` was constructed without the provenance it must carry."""


def _freeze(evidence: Mapping[str, str] | None) -> Mapping[str, str]:
    return MappingProxyType(dict(evidence or {}))


@dataclass(frozen=True)
class CorrelationKey:
    """One episode's join key: its subject class and the key's components.

    Section 3.3 gives a different tuple per subject class, so the class is part
    of the key rather than a label beside it. Without it, a ``pr_merge`` key
    ``(github, o/r, 7)`` and a truncated ``ci_outcome`` key would be free to
    collide, and the pairing would cross subject classes -- a merge episode
    reported as agreeing with a CI outcome.
    """

    subject_class: str
    parts: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.subject_class not in SUBJECT_CLASSES:
            raise UnknownSubjectClass(
                f"subject_class {self.subject_class!r} is outside section 3.3's "
                f"table ({', '.join(SUBJECT_CLASSES)})"
            )
        if not self.parts or any(part == "" for part in self.parts):
            # An empty component is a missing component wearing a value's type.
            # Section 3.3's whole unmatched_key bucket exists for the missing
            # case, and admitting '' here would route it past that bucket into a
            # pairing on a key that is partly blank.
            raise EpisodeKeyRefused(
                f"correlation key for {self.subject_class!r} has an empty or "
                "absent component; an episode that cannot compute every "
                f"component belongs in the {UNMATCHED_KEY!r} bucket"
            )

    @property
    def positional(self) -> bool:
        """Is this the weak, order-dependent join? See :data:`POSITIONAL_KEY_CAVEAT`."""

        return self.subject_class in POSITIONAL_SUBJECT_CLASSES

    def token(self) -> str:
        """The hashable spelling both sides must agree on, byte for byte."""

        return _KEY_SEPARATOR.join((self.subject_class,) + self.parts)


@dataclass(frozen=True)
class ShadowEpisode:
    """One real-world condition as one system saw it.

    *shape* is what a fixture can recognise the episode by -- the CI verdict,
    the merge, the incident's ``fact_state``. It is what section 3.3's "where a
    fixture covers the same shape" is looked up on, and it is deliberately not
    the episode id: an id is unique to one occurrence, and a label is about a
    *kind* of occurrence.

    *key* and *key_gap* are exclusive and one of them is required. An episode
    that could not compute its key carries the reason it could not, because that
    reason is the ``unmatched_key`` bucket's entire content -- section 7 says a
    canary producing many of them is telling us the key needs replacing, and a
    bucket of bare ids says nothing about which component went missing.
    """

    episode_id: str
    subject_class: str
    shape: str
    onset_ms: int
    key: CorrelationKey | None = None
    key_gap: str | None = None
    evidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.subject_class not in SUBJECT_CLASSES:
            raise UnknownSubjectClass(
                f"subject_class {self.subject_class!r} is outside section 3.3's "
                f"table ({', '.join(SUBJECT_CLASSES)})"
            )
        if not self.episode_id:
            raise EpisodeKeyRefused("an episode must carry a non-empty episode_id")
        if (self.key is None) == (self.key_gap is None):
            raise EpisodeKeyRefused(
                f"episode {self.episode_id!r} must carry exactly one of a "
                "correlation key or the reason it has none; carrying neither "
                "is how an episode leaves a report without being counted, and "
                "carrying both leaves the bucket ambiguous"
            )
        if self.key is not None and self.key.subject_class != self.subject_class:
            raise EpisodeKeyRefused(
                f"episode {self.episode_id!r} is a {self.subject_class!r} "
                f"episode carrying a {self.key.subject_class!r} key"
            )
        object.__setattr__(self, "evidence", _freeze(self.evidence))

    @property
    def positional_key(self) -> bool:
        """Does this episode rest on the weak positional join?"""

        return self.subject_class in POSITIONAL_SUBJECT_CLASSES


@dataclass(frozen=True)
class MatchedPair:
    """One condition, seen by both systems.

    ``onset_delta_ms`` and ``shape_agrees`` are carried because section 3.3 says
    a matched episode is where "latency and outcome are compared", and a bucket
    that recorded only the fact of the match would make the comparison a second
    pass over data the report had already thrown away.
    """

    key: CorrelationKey
    interlock: ShadowEpisode
    v1: ShadowEpisode

    @property
    def onset_delta_ms(self) -> int:
        """v1's onset minus Interlock's. Positive means Interlock saw it first."""

        return self.v1.onset_ms - self.interlock.onset_ms

    @property
    def shape_agrees(self) -> bool:
        """Did the two systems call it the same thing?

        A pair that matched on key and disagrees on shape is a real finding --
        both systems saw the condition and named it differently -- and it is
        *not* a miss. Folding it into ``v1_only`` would count one condition
        twice and call the second copy a miss.
        """

        return self.interlock.shape == self.v1.shape


@dataclass(frozen=True)
class V1OnlyEpisode:
    """A candidate miss, with the verdict on it or the fact that there is none.

    ``adjudication`` is ``None`` until something settles it. That ``None`` is
    load-bearing: it is what makes :meth:`ShadowReconciliation.confirmed_miss_count`
    refuse, and it is why an unsettled candidate cannot be counted as a miss and
    cannot be dropped.
    """

    episode: ShadowEpisode
    adjudication: str | None
    adjudication_source: str

    def __post_init__(self) -> None:
        if self.adjudication is not None and self.adjudication not in ADJUDICATIONS:
            raise UnknownAdjudication(
                f"{self.adjudication!r} is not one of "
                f"{', '.join(ADJUDICATIONS)}"
            )

    @property
    def is_miss(self) -> bool:
        return self.adjudication == MISS


@dataclass(frozen=True)
class V1Reference:
    """The v1 side of the comparison, as a **separable adapter** hands it over.

    This type exists so that this module never opens one of v1's files. Outside
    the shadow period there is no v1 data, during it the data lives in
    ``.state/pending_decisions.json``, an ``events`` table and notification
    records, and by the time AC-10 is re-run those paths may not exist at all.
    A harness that reached for them directly would stop running when they moved;
    one that takes episodes as an input keeps running and says what it has.

    Construct through :meth:`absent`, :meth:`observed` or :meth:`attests_empty`,
    never by hand: the constructors are where "no reference" and "a reference
    that saw nothing" are forced apart.
    """

    source: str | None
    episodes: tuple[ShadowEpisode, ...]
    absent_reason: str | None

    @property
    def available(self) -> bool:
        return self.source is not None

    @classmethod
    def absent(cls, *, reason: str) -> "V1Reference":
        """There is no v1 reference for this period, and here is why."""

        if not reason:
            raise ShadowReferenceRefused(
                "an absent shadow reference must say why it is absent; the "
                "report prints the reason instead of a comparison"
            )
        return cls(source=None, episodes=(), absent_reason=reason)

    @classmethod
    def observed(
        cls, *, source: str, episodes: Iterable[ShadowEpisode]
    ) -> "V1Reference":
        """v1 episodes read by *source*.

        **An empty *episodes* degrades to :meth:`absent`, deliberately.** An
        adapter that returned nothing and an adapter that did not run are
        indistinguishable from their output, and the two readings differ by the
        entire report: treated as "v1 saw nothing", every Interlock episode is
        filed ``interlock_only`` and the period reads as pure improvement with
        no miss anywhere -- the flattering answer, produced by the absence of
        data rather than by the presence of any. A canary period where v1
        genuinely ran and saw nothing is a real state and says so through
        :meth:`attests_empty`, which is a claim someone has to make on purpose.
        """

        if not source:
            raise ShadowReferenceRefused(
                "an observed shadow reference must name its source (D-0040: a "
                "report records where its numbers came from)"
            )
        materialised = tuple(episodes)
        if not materialised:
            return cls.absent(
                reason=(
                    f"the v1 adapter {source!r} returned no episodes; an empty "
                    "read is not evidence that v1 saw nothing (use "
                    "V1Reference.attests_empty to claim that on purpose)"
                )
            )
        return cls(source=source, episodes=materialised, absent_reason=None)

    @classmethod
    def attests_empty(cls, *, source: str) -> "V1Reference":
        """*source* ran over this period and asserts v1 raised no episode in it."""

        if not source:
            raise ShadowReferenceRefused(
                "an attestation that v1 saw nothing must name who attests it"
            )
        return cls(source=source, episodes=(), absent_reason=None)


@dataclass(frozen=True)
class ShadowReconciliation:
    """Section 3.3's five buckets, and the provenance to recompute them.

    Every accessor that returns a comparison number refuses when
    :attr:`shadow_reference` is :data:`SHADOW_ABSENT`; see
    :class:`ShadowReferenceAbsent` for why five zeroes are not an acceptable
    stand-in for "there was nothing to compare against".
    """

    period_start_ms: int
    period_end_ms: int
    shadow_reference: str
    shadow_source: str | None
    shadow_absent_reason: str | None
    interlock_episode_count: int
    both: tuple[MatchedPair, ...]
    interlock_only: tuple[ShadowEpisode, ...]
    v1_only: tuple[V1OnlyEpisode, ...]
    unmatched_key: tuple[ShadowEpisode, ...]
    censored: tuple[ShadowEpisode, ...]
    positional_caveat: str = POSITIONAL_KEY_CAVEAT

    @property
    def available(self) -> bool:
        """Was there a second observer for this period at all?"""

        return self.shadow_reference == SHADOW_PRESENT

    def _require_reference(self) -> None:
        if not self.available:
            raise ShadowReferenceAbsent(
                "this report has no shadow reference for "
                f"[{self.period_start_ms}, {self.period_end_ms}): "
                f"{self.shadow_absent_reason}. "
                f"{self.interlock_episode_count} Interlock episode(s) were read "
                "and none of them can be called an improvement or a miss "
                "without a second observer"
            )

    def counts(self) -> Mapping[str, int]:
        """Per-bucket counts, all five keys present **even at zero**.

        A zero and a missing key are different statements to a reader diffing
        two reports, and only one of them is the truth this harness has. A
        matched pair counts once, as one condition -- it is one episode of the
        world seen twice, not two episodes.
        """

        self._require_reference()
        return MappingProxyType(
            {
                BOTH: len(self.both),
                INTERLOCK_ONLY: len(self.interlock_only),
                V1_ONLY: len(self.v1_only),
                UNMATCHED_KEY: len(self.unmatched_key),
                CENSORED: len(self.censored),
            }
        )

    def filed_episode_ids(self) -> tuple[str, ...]:
        """Every episode id this report filed, once per episode, in bucket order.

        The partition, made checkable. Both sides of a matched pair appear,
        because both were inputs; the test suite asserts this tuple is a
        permutation of the inputs, which is what makes "no silent drop" a
        property of the code rather than a claim in a docstring.
        """

        self._require_reference()
        filed: list[str] = []
        for pair in self.both:
            filed.append(pair.interlock.episode_id)
            filed.append(pair.v1.episode_id)
        filed.extend(episode.episode_id for episode in self.interlock_only)
        filed.extend(candidate.episode.episode_id for candidate in self.v1_only)
        filed.extend(episode.episode_id for episode in self.unmatched_key)
        filed.extend(episode.episode_id for episode in self.censored)
        return tuple(filed)

    def awaiting_adjudication(self) -> tuple[V1OnlyEpisode, ...]:
        """The candidate misses nothing has settled yet, with their evidence.

        Section 3.3: those a fixture does not cover are "listed for human
        adjudication with evidence attached". This is that list, and
        :func:`render_shadow_reconciliation` prints it, so the report cannot
        show a miss-related number without also showing what is still open.
        """

        self._require_reference()
        return tuple(
            candidate for candidate in self.v1_only if candidate.adjudication is None
        )

    def adjudication_counts(self) -> Mapping[str, int]:
        """How the ``v1_only`` candidates were settled, ``None`` included."""

        self._require_reference()
        tally = {name: 0 for name in ADJUDICATIONS}
        tally[AWAITING_HUMAN] = 0
        for candidate in self.v1_only:
            if candidate.adjudication is None:
                tally[AWAITING_HUMAN] += 1
            else:
                tally[candidate.adjudication] += 1
        return MappingProxyType(tally)

    def confirmed_miss_count(self) -> int:
        """AC-10's miss numerator -- and the only method that returns one.

        :raises AdjudicationPending: while any ``v1_only`` candidate is
            unclassified. That is section 3.3's "never silently converts
            ``v1_only`` into a miss count" with the silence removed: there is no
            path from a candidate to a number that does not pass through here.
        :raises ShadowReferenceAbsent: if there was no second observer.
        """

        self._require_reference()
        pending = self.awaiting_adjudication()
        if pending:
            raise AdjudicationPending(
                f"{len(pending)} v1_only episode(s) are unclassified "
                f"({', '.join(candidate.episode.episode_id for candidate in pending)}); "
                "v1 raising an episode Interlock did not can also mean v1 false "
                "positived, so a miss count cannot be taken until each is "
                "settled by a fixture label or by human adjudication "
                "(measurement-harness.md section 3.3)"
            )
        return sum(1 for candidate in self.v1_only if candidate.is_miss)


def censored_episode_ids(report: WindowReport) -> frozenset[str]:
    """The ids the windows module says cannot be judged in this period.

    Both censored buckets, folded into one set: section 3.5 excludes right- and
    left-censored episodes from the *same* numerators, and the reconciliation
    has one censored bucket to file them in. They stay distinguishable where
    that matters -- on :class:`~claude_org_runtime.measurement.windows.WindowReport`,
    which keeps them apart so a reader can tell which end of the period is too
    tight.

    This is the only place censoring enters the reconciliation. Recomputing it
    here from onsets and budgets would be a second implementation of section
    3.5 that agrees with the first until a grace value changes.
    """

    return frozenset(
        report.ids_for(WINDOW_CENSORED) + report.ids_for(WINDOW_CENSORED_LEFT)
    )


def _rows(cursor: sqlite3.Cursor) -> tuple[Mapping[str, Any], ...]:
    """The cursor's rows as read-only mappings, whatever ``row_factory`` is set.

    Column names come from ``cursor.description`` rather than from a
    ``sqlite3.Row`` factory, for the reason ``watcher.py`` gives for the same
    helper: these functions are called on a connection this module did not open
    -- the read-only handle from
    :func:`~claude_org_runtime.measurement.reader.open_for_measurement` -- and
    setting ``row_factory`` on it would change what every other caller of that
    connection receives.
    """

    names = [column[0] for column in cursor.description]
    return tuple(MappingProxyType(dict(zip(names, row))) for row in cursor.fetchall())


# ---------------------------------------------------------------------------
# Interlock-side readers. Each one is section 3.3's row for its subject class.
# ---------------------------------------------------------------------------


def read_ci_outcome_episodes(
    connection: sqlite3.Connection,
    *,
    onset_from_ms: int,
    onset_to_ms: int,
) -> tuple[ShadowEpisode, ...]:
    """CI outcome episodes: one per PR head, keyed ``(provider, slug, pr, head)``.

    One episode per **head**, not per observation, because that is what the
    key's granularity says the condition is: a head has one CI outcome, observed
    many times across scopes and attempts. The outcome itself is
    :func:`ci_ingest.pr_verdict` -- section 6.3's projection, imported rather
    than folded again here, because a second severity fold would agree with the
    first until the day ``indeterminate`` stopped outranking ``passed`` and then
    report an unobservable check as a green one in exactly one place.

    The onset is the earliest ``occurred_at_ms`` among the head's *currently
    eligible* observations: the provider's instant at which this head's CI
    outcome began to be observable. Ingest time is not used anywhere --
    ``D-0033`` says arrival order never decides a verdict, and it must not
    decide an onset either, or a slow poll would move an episode across a period
    boundary that the world never crossed.

    *onset_from_ms* / *onset_to_ms* are a half-open **selection** window, not the
    report period. A caller that wants left-censored episodes (section 3.5)
    widens the lower bound past ``period_start_ms`` on purpose; the windows
    module, not this reader, decides what is censored.
    """

    _require_selection_window(onset_from_ms, onset_to_ms)
    rows = _rows(connection.execute(
        # lower(owner) || '/' || lower(name) is the SAME expression the
        # repository_by_slug UNIQUE INDEX is built on (0001_initial.sql:
        # "Case is preserved in the columns and folded in the index"). Spelling
        # the fold independently -- Python's str.lower(), which is
        # Unicode-aware and full-casefolding, against SQLite's lower(), which
        # folds ASCII only -- would make this module a second source of truth
        # for one thing: the two agree on every ASCII slug and disagree the
        # moment an owner name carries a non-ASCII letter, at which point the
        # key names a repository the database's own index never named, and the
        # episode fails to pair for a reason invisible in both systems' data.
        # Folding in SQL keeps one implementation of "the same repository".
        """
        SELECT p.pr_number        AS pr_number,
               p.head_sha         AS head_sha,
               p.repo_id          AS repo_id,
               r.provider         AS provider,
               lower(r.owner) || '/' || lower(r.name) AS slug,
               MIN(v.occurred_at_ms) AS onset_ms
          FROM pull_request p
          JOIN repository r
            ON r.repo_id = p.repo_id
          JOIN ci_current_verdict v
            ON v.repo_id = p.repo_id
           AND v.pr_number = p.pr_number
           AND v.head_sha = p.head_sha
         GROUP BY p.repo_id, p.pr_number, p.head_sha
        HAVING MIN(v.occurred_at_ms) >= ?
           AND MIN(v.occurred_at_ms) < ?
         ORDER BY onset_ms ASC, p.repo_id ASC, p.pr_number ASC
        """,
        (onset_from_ms, onset_to_ms),
    ))

    episodes: list[ShadowEpisode] = []
    for row in rows:
        pr_number = int(row["pr_number"])
        repo_id = str(row["repo_id"])
        verdict = ci_ingest.pr_verdict(
            connection, repo_id=repo_id, pr_number=pr_number
        )
        key = CorrelationKey(
            subject_class=SUBJECT_CI_OUTCOME,
            parts=(str(row["provider"]), str(row["slug"]), str(pr_number), str(row["head_sha"])),
        )
        episodes.append(
            ShadowEpisode(
                episode_id=f"ci:{repo_id}:{pr_number}:{row['head_sha']}",
                subject_class=SUBJECT_CI_OUTCOME,
                shape=verdict,
                onset_ms=int(row["onset_ms"]),
                key=key,
                evidence={
                    "repo_id": repo_id,
                    "head_sha": str(row["head_sha"]),
                    "verdict": verdict,
                },
            )
        )
    return tuple(episodes)


def read_pr_merge_episodes(
    connection: sqlite3.Connection,
    *,
    onset_from_ms: int,
    onset_to_ms: int,
) -> tuple[ShadowEpisode, ...]:
    """PR merge episodes, keyed ``(provider, slug, pr_number)``.

    The head SHA is deliberately **not** in this key even though
    ``pull_request`` holds one: a merge is a fact about the pull request, and
    the two systems can hold different heads for it (v1 recorded a ``pr_url``
    and re-resolved the head at read time). Including it would make every merge
    unmatched whenever a head update raced the merge, which is the ordinary
    case, not the exceptional one.

    The onset is ``merged_at_ms`` -- the provider's own instant, which the
    ``pull_request`` CHECKs tie to ``state = 'merged'`` so the two cannot
    disagree.
    """

    _require_selection_window(onset_from_ms, onset_to_ms)
    rows = _rows(connection.execute(
        # The same slug fold as read_ci_outcome_episodes, for the same reason.
        """
        SELECT p.pr_id       AS pr_id,
               p.repo_id     AS repo_id,
               p.pr_number   AS pr_number,
               p.merged_at_ms AS merged_at_ms,
               p.merge_commit_sha AS merge_commit_sha,
               r.provider    AS provider,
               lower(r.owner) || '/' || lower(r.name) AS slug
          FROM pull_request p
          JOIN repository r
            ON r.repo_id = p.repo_id
         WHERE p.state = 'merged'
           AND p.merged_at_ms >= ?
           AND p.merged_at_ms < ?
         ORDER BY p.merged_at_ms ASC, p.pr_id ASC
        """,
        (onset_from_ms, onset_to_ms),
    ))

    return tuple(
        ShadowEpisode(
            episode_id=f"merge:{row['pr_id']}",
            subject_class=SUBJECT_PR_MERGE,
            shape="merged",
            onset_ms=int(row["merged_at_ms"]),
            key=CorrelationKey(
                subject_class=SUBJECT_PR_MERGE,
                parts=(
                    str(row["provider"]),
                    str(row["slug"]),
                    str(int(row["pr_number"])),
                ),
            ),
            evidence={
                "pr_id": str(row["pr_id"]),
                "repo_id": str(row["repo_id"]),
                "merge_commit_sha": str(row["merge_commit_sha"]),
            },
        )
        for row in rows
    )


def read_worker_escalation_episodes(
    connection: sqlite3.Connection,
    *,
    onset_from_ms: int,
    onset_to_ms: int,
) -> tuple[ShadowEpisode, ...]:
    """Worker escalation episodes, keyed ``(run_id, nth escalation of that run)``.

    **This is the positional key, and it is the weakest join in the
    reconciliation** (sections 3.3 and 7). v1's ``.state/pending_decisions.json``
    entries carry an id Interlock never sees, so the only key both sides can
    compute is the ordinal of the escalation within its run, by receipt time. It
    holds while both systems saw the same escalations in the same order -- and
    an ordering divergence is precisely what the reconciliation exists to catch,
    so its failure mode is unmatched episodes on both sides rather than a
    confident wrong pairing. Every key this function produces reports
    :attr:`CorrelationKey.positional`, and
    :data:`POSITIONAL_KEY_CAVEAT` rides on the report.

    Two more details the position depends on, stated because both are silent
    when wrong:

    * The ordinal is computed over the run's **whole** escalation history and
      only then filtered to the selection window. Numbering within the window
      would renumber the same escalation differently in a weekly and a daily
      report, and the two reports would disagree about which episode is which.
    * Receipt time is ``created_at_ms`` -- the ``received`` stage's instant,
      which ``0001_initial.sql`` requires ``stage_entered_at_ms`` to be at or
      after. Ties are broken by ``gate_id`` so the numbering is deterministic;
      a tie means two escalations arrived in the same millisecond and their
      relative order is genuinely unknown, which is one more way the positional
      key can mispair, and it surfaces the same safe way.

    A gate with no ``run_id`` (the column is nullable: a merge approval or a
    risk approval need not belong to a run) can compute no key at all and is
    returned with a ``key_gap`` -- section 3.3's ``unmatched_key`` bucket --
    rather than dropped.
    """

    _require_selection_window(onset_from_ms, onset_to_ms)
    rows = _rows(connection.execute(
        """
        SELECT gate_id, run_id, created_at_ms, stage, outcome, ordinal
          FROM (
            SELECT g.gate_id        AS gate_id,
                   g.run_id         AS run_id,
                   g.created_at_ms  AS created_at_ms,
                   g.stage          AS stage,
                   g.outcome        AS outcome,
                   CASE WHEN g.run_id IS NULL THEN NULL
                        ELSE ROW_NUMBER() OVER (
                            PARTITION BY g.run_id
                            ORDER BY g.created_at_ms ASC, g.gate_id ASC)
                   END AS ordinal
              FROM gate g
             WHERE g.gate_type = 'worker_escalation'
          )
         WHERE created_at_ms >= ? AND created_at_ms < ?
         ORDER BY created_at_ms ASC, gate_id ASC
        """,
        (onset_from_ms, onset_to_ms),
    ))

    episodes: list[ShadowEpisode] = []
    for row in rows:
        gate_id = str(row["gate_id"])
        run_id = row["run_id"]
        evidence = {
            "gate_id": gate_id,
            "stage": str(row["stage"]),
            "outcome": "" if row["outcome"] is None else str(row["outcome"]),
        }
        if run_id is None:
            episodes.append(
                ShadowEpisode(
                    episode_id=f"escalation:{gate_id}",
                    subject_class=SUBJECT_WORKER_ESCALATION,
                    shape=str(row["stage"]),
                    onset_ms=int(row["created_at_ms"]),
                    key_gap=(
                        "gate.run_id is NULL, so the escalation has no run to be "
                        "the nth escalation of; section 3.3's key cannot be "
                        "composed for it"
                    ),
                    evidence=evidence,
                )
            )
            continue
        episodes.append(
            ShadowEpisode(
                episode_id=f"escalation:{gate_id}",
                subject_class=SUBJECT_WORKER_ESCALATION,
                shape=str(row["stage"]),
                onset_ms=int(row["created_at_ms"]),
                key=CorrelationKey(
                    subject_class=SUBJECT_WORKER_ESCALATION,
                    parts=(str(run_id), str(int(row["ordinal"]))),
                ),
                evidence=dict(evidence, run_id=str(run_id)),
            )
        )
    return tuple(episodes)


def read_session_liveness_episodes(
    connection: sqlite3.Connection,
    *,
    onset_from_ms: int,
    onset_to_ms: int,
    fact_states: Sequence[str],
) -> tuple[ShadowEpisode, ...]:
    """Session liveness episodes, keyed ``(run_id, 60 s onset bucket)``.

    *fact_states* is required and has no default. ``incident.fact_state`` is
    unconstrained text on purpose -- ``0001_initial.sql`` says the closed
    ``D-0005`` set lives in ``DECISIONS.md`` because a ``CHECK`` would turn
    extending it into a migration -- so the schema cannot tell this reader which
    states are the liveness class. A default here would be this module quietly
    deciding what AC-10's session-liveness denominator contains, which is the
    kind of convenient default that makes a predicate go missing: widen it and
    the miss rate falls, narrow it and it rises, and nothing in the report says
    which happened.

    **The onset is not ``created_at_ms``.** ``created_at_ms`` is when *we*
    raised the incident, and the two systems detect the same condition at
    different latencies -- which is the very quantity AC-10 measures, so keying
    on it would guarantee the two sides bucket differently exactly when they
    disagree most. The onset is ``created_at_ms - elapsed_ms``: ``elapsed_ms``
    is how long the condition had been running when the packet was built
    (``D-0007``'s packet), so the difference is the state entry -- section 3.2's
    "when the condition **began**", not the tolerance crossing. An incident with
    no ``elapsed_ms`` (the column is nullable) has no computable onset and comes
    back with a ``key_gap``; substituting ``created_at_ms`` would put the
    episode in a bucket up to a whole detection latency away from v1's and
    report a match as a miss.

    ``run_id`` is taken from the incident, falling back to the incident's
    session binding -- section 3.3's "``incident`` joined to ``session``". Both
    columns are nullable, and an incident that names neither carries a
    ``key_gap``.
    """

    _require_selection_window(onset_from_ms, onset_to_ms)
    if not fact_states:
        raise ShadowRefusal(
            "read_session_liveness_episodes needs the fact_state values that "
            "make up the session-liveness class; incident.fact_state is "
            "unconstrained text (0001_initial.sql) and this reader will not "
            "guess which states belong to the class"
        )

    placeholders = ", ".join("?" for _ in fact_states)
    rows = _rows(connection.execute(
        f"""
        SELECT i.incident_id                    AS incident_id,
               COALESCE(i.run_id, s.run_id)     AS run_id,
               i.session_id                     AS session_id,
               i.fact_state                     AS fact_state,
               i.created_at_ms                  AS created_at_ms,
               i.elapsed_ms                     AS elapsed_ms
          FROM incident i
          LEFT JOIN session s
            ON s.session_id = i.session_id
         WHERE i.fact_state IN ({placeholders})
         ORDER BY i.created_at_ms ASC, i.incident_id ASC
        """,
        tuple(fact_states),
    ))

    episodes: list[ShadowEpisode] = []
    for row in rows:
        incident_id = str(row["incident_id"])
        fact_state = str(row["fact_state"])
        created_at_ms = int(row["created_at_ms"])
        elapsed_ms = row["elapsed_ms"]
        run_id = row["run_id"]

        gaps: list[str] = []
        if run_id is None:
            gaps.append(
                "the incident names neither a run_id nor a session whose run_id "
                "could stand in for it"
            )
        if elapsed_ms is None:
            gaps.append(
                "incident.elapsed_ms is NULL, so the condition's onset cannot be "
                "derived from the instant we raised the incident"
            )
        if gaps:
            episodes.append(
                ShadowEpisode(
                    episode_id=f"liveness:{incident_id}",
                    subject_class=SUBJECT_SESSION_LIVENESS,
                    shape=fact_state,
                    # With no elapsed_ms the onset is unknown; created_at_ms is
                    # reported as the episode's only known instant and the
                    # key_gap says it is not the onset, so no consumer can read
                    # it as one.
                    onset_ms=created_at_ms,
                    key_gap="; ".join(gaps),
                    evidence={
                        "incident_id": incident_id,
                        "fact_state": fact_state,
                        "created_at_ms": str(created_at_ms),
                    },
                )
            )
            continue

        onset_ms = created_at_ms - int(elapsed_ms)
        if not (onset_from_ms <= onset_ms < onset_to_ms):
            continue
        # Floor division, so a negative onset (a clock the caller handed us from
        # before the epoch of the selection window) buckets downward like every
        # other instant rather than toward zero, which would put two adjacent
        # onsets either side of 0 into the same bucket.
        bucket = onset_ms // ONSET_BUCKET_MS
        episodes.append(
            ShadowEpisode(
                episode_id=f"liveness:{incident_id}",
                subject_class=SUBJECT_SESSION_LIVENESS,
                shape=fact_state,
                onset_ms=onset_ms,
                key=CorrelationKey(
                    subject_class=SUBJECT_SESSION_LIVENESS,
                    parts=(str(run_id), str(bucket)),
                ),
                evidence={
                    "incident_id": incident_id,
                    "run_id": str(run_id),
                    "session_id": "" if row["session_id"] is None else str(row["session_id"]),
                    "fact_state": fact_state,
                    "elapsed_ms": str(int(elapsed_ms)),
                },
            )
        )
    return tuple(episodes)


def read_interlock_episodes(
    connection: sqlite3.Connection,
    *,
    onset_from_ms: int,
    onset_to_ms: int,
    liveness_fact_states: Sequence[str],
) -> tuple[ShadowEpisode, ...]:
    """Every subject class of section 3.3's table, in table order.

    One call rather than four so that adding a fifth subject class cannot be
    half-done: a class present in :data:`SUBJECT_CLASSES` and absent here would
    contribute episodes on the v1 side and none on ours, and every one of them
    would be filed as a candidate miss.
    """

    return (
        read_ci_outcome_episodes(
            connection, onset_from_ms=onset_from_ms, onset_to_ms=onset_to_ms
        )
        + read_pr_merge_episodes(
            connection, onset_from_ms=onset_from_ms, onset_to_ms=onset_to_ms
        )
        + read_worker_escalation_episodes(
            connection, onset_from_ms=onset_from_ms, onset_to_ms=onset_to_ms
        )
        + read_session_liveness_episodes(
            connection,
            onset_from_ms=onset_from_ms,
            onset_to_ms=onset_to_ms,
            fact_states=liveness_fact_states,
        )
    )


def _require_selection_window(onset_from_ms: int, onset_to_ms: int) -> None:
    if onset_to_ms <= onset_from_ms:
        raise ShadowRefusal(
            f"the selection window [{onset_from_ms}, {onset_to_ms}) is empty or "
            "inverted; a half-open window must end strictly after it starts "
            "(time-base-policy.md section 2, rule 4)"
        )


# ---------------------------------------------------------------------------
# The reconciliation itself. Pure: no connection, no clock, no v1 file paths.
# ---------------------------------------------------------------------------


def reconcile(
    *,
    period_start_ms: int,
    period_end_ms: int,
    interlock_episodes: Iterable[ShadowEpisode],
    v1_reference: V1Reference,
    censored_ids: Collection[str],
    fixture_labels: Mapping[str, str],
) -> ShadowReconciliation:
    """File every episode from both systems into exactly one of the five buckets.

    *censored_ids* comes from :func:`censored_episode_ids` over the
    windows module's report (section 3.5). It is a required argument with no
    default: a caller who has not classified windows must say so by passing an
    empty set, because the alternative -- an implicit "nothing is censored" --
    is the manufactured-miss defect section 3.5 exists to remove, arriving
    through a keyword argument nobody typed.

    *fixture_labels* maps an episode :attr:`~ShadowEpisode.shape` to one of
    :data:`ADJUDICATIONS`, and it too is required with no default. ``{}`` is a
    legitimate value and means every ``v1_only`` episode goes to human
    adjudication; leaving it out would let a report claim a settled miss count
    on the strength of a corpus it never consulted.

    **Order of filing, and why censoring wins.** A censored episode's window
    extends outside the period, so the report cannot say whether it was detected
    in time -- and that is true whether or not its counterpart happens to be
    present in the same period. So episodes are matched *first* and censoring is
    applied *after*: a matched pair with either half censored is censored, and
    an unmatched censored episode is censored rather than a candidate miss.
    Matching first is what stops a censored Interlock episode from turning its
    perfectly-present v1 counterpart into a fabricated miss.
    """

    if period_end_ms <= period_start_ms:
        raise ShadowRefusal(
            f"the report period [{period_start_ms}, {period_end_ms}) is empty or "
            "inverted; a half-open window must end strictly after it starts "
            "(time-base-policy.md section 2, rule 4)"
        )
    for shape, label in fixture_labels.items():
        if label not in ADJUDICATIONS:
            raise UnknownAdjudication(
                f"fixture label for shape {shape!r} is {label!r}, which is not "
                f"one of {', '.join(ADJUDICATIONS)}"
            )

    interlock = tuple(interlock_episodes)
    _refuse_duplicate_ids(interlock, v1_reference.episodes)

    if not v1_reference.available:
        return ShadowReconciliation(
            period_start_ms=period_start_ms,
            period_end_ms=period_end_ms,
            shadow_reference=SHADOW_ABSENT,
            shadow_source=None,
            shadow_absent_reason=v1_reference.absent_reason,
            interlock_episode_count=len(interlock),
            both=(),
            interlock_only=(),
            v1_only=(),
            unmatched_key=(),
            censored=(),
        )

    censored_ids = frozenset(censored_ids)

    interlock_keyed, interlock_keyless = _split_by_key(interlock, side="interlock")
    v1_keyed, v1_keyless = _split_by_key(v1_reference.episodes, side="v1")

    both: list[MatchedPair] = []
    interlock_only: list[ShadowEpisode] = []
    v1_only: list[V1OnlyEpisode] = []
    unmatched_key: list[ShadowEpisode] = list(interlock_keyless) + list(v1_keyless)
    censored: list[ShadowEpisode] = []

    matched_v1_tokens: set[str] = set()
    for token, episode in interlock_keyed.items():
        counterpart = v1_keyed.get(token)
        if counterpart is None:
            if episode.episode_id in censored_ids:
                censored.append(episode)
            else:
                interlock_only.append(episode)
            continue
        matched_v1_tokens.add(token)
        if (
            episode.episode_id in censored_ids
            or counterpart.episode_id in censored_ids
        ):
            censored.append(episode)
            censored.append(counterpart)
            continue
        # _split_by_key only files an episode under a token it computed from a
        # key, so this key exists by construction; naming it in a local keeps
        # that fact where a reader can see it.
        matched_key = episode.key
        if matched_key is None:  # pragma: no cover - unreachable by construction
            raise EpisodeKeyRefused(
                f"episode {episode.episode_id!r} was matched by key and then "
                "found to have none"
            )
        both.append(MatchedPair(key=matched_key, interlock=episode, v1=counterpart))

    for token, episode in v1_keyed.items():
        if token in matched_v1_tokens:
            continue
        if episode.episode_id in censored_ids:
            censored.append(episode)
            continue
        v1_only.append(_adjudicate(episode, fixture_labels))

    # A keyless episode that is also censored is filed censored, for the same
    # reason a matched one is: the report cannot judge it either way, and
    # inflating the unmatched_key bucket with window problems would corrupt the
    # one signal section 7 reads out of it -- whether the KEY needs replacing.
    unmatched_key, key_censored = _partition_censored(unmatched_key, censored_ids)
    censored.extend(key_censored)

    return ShadowReconciliation(
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        shadow_reference=SHADOW_PRESENT,
        shadow_source=v1_reference.source,
        shadow_absent_reason=None,
        interlock_episode_count=len(interlock),
        both=tuple(both),
        interlock_only=tuple(interlock_only),
        v1_only=tuple(v1_only),
        unmatched_key=tuple(unmatched_key),
        censored=tuple(censored),
    )


def _adjudicate(
    episode: ShadowEpisode, fixture_labels: Mapping[str, str]
) -> V1OnlyEpisode:
    """Settle one candidate miss by fixture label, or hand it to a human.

    Section 3.2's corpus is the only automatic source: a fixture that covers the
    same *shape* already carries the ground-truth verdict for it. Anything else
    is listed, with its evidence, and nothing here invents a verdict from the
    episode's own fields -- doing so would make the miss count a function of
    Interlock's opinion about v1's data, which is the circularity section 3.1
    rules out.
    """

    label = fixture_labels.get(episode.shape)
    if label is None:
        return V1OnlyEpisode(
            episode=episode,
            adjudication=None,
            adjudication_source=AWAITING_HUMAN,
        )
    return V1OnlyEpisode(
        episode=episode, adjudication=label, adjudication_source=FROM_FIXTURE_LABEL
    )


def _split_by_key(
    episodes: Iterable[ShadowEpisode], *, side: str
) -> tuple[dict[str, ShadowEpisode], list[ShadowEpisode]]:
    keyed: dict[str, ShadowEpisode] = {}
    keyless: list[ShadowEpisode] = []
    for episode in episodes:
        if episode.key is None:
            keyless.append(episode)
            continue
        token = episode.key.token()
        existing = keyed.get(token)
        if existing is not None:
            raise DuplicateCorrelationKey(
                f"{side} episodes {existing.episode_id!r} and "
                f"{episode.episode_id!r} compute the same "
                f"{episode.key.subject_class!r} correlation key "
                f"{episode.key.parts!r}; matching is one-to-one and the loser "
                "would be filed as a fabricated improvement or a fabricated miss"
                + (
                    f". {POSITIONAL_KEY_CAVEAT}"
                    if episode.key.positional
                    else ""
                )
            )
        keyed[token] = episode
    return keyed, keyless


def _partition_censored(
    episodes: Iterable[ShadowEpisode], censored_ids: Collection[str]
) -> tuple[list[ShadowEpisode], list[ShadowEpisode]]:
    kept: list[ShadowEpisode] = []
    censored: list[ShadowEpisode] = []
    for episode in episodes:
        (censored if episode.episode_id in censored_ids else kept).append(episode)
    return kept, censored


def _refuse_duplicate_ids(
    interlock: Sequence[ShadowEpisode], v1: Sequence[ShadowEpisode]
) -> None:
    seen: dict[str, str] = {}
    for side, episodes in (("interlock", interlock), ("v1", v1)):
        for episode in episodes:
            previous = seen.get(episode.episode_id)
            if previous is not None:
                raise DuplicateEpisodeIdRefused(
                    f"episode_id {episode.episode_id!r} appears on the "
                    f"{previous} side and again on the {side} side; ids are how "
                    "censoring and the partition check address an episode, and "
                    "a collision applies one episode's window to another's"
                )
            seen[episode.episode_id] = side


def render_shadow_reconciliation(report: ShadowReconciliation) -> str:
    """The report as text. ASCII only -- this reaches a cp932 console.

    The unadjudicated candidates are printed unconditionally when there are any.
    A rendering that showed the bucket counts and left the open list to a
    separate command would let the reader take ``v1_only: 3`` for a miss count,
    which is the exact conversion section 3.3 forbids.
    """

    lines = [
        "Shadow reconciliation "
        f"[{report.period_start_ms}, {report.period_end_ms})",
    ]
    if not report.available:
        lines.append("  shadow reference: ABSENT")
        lines.append(f"  reason: {report.shadow_absent_reason}")
        lines.append(
            f"  Interlock episodes read: {report.interlock_episode_count}"
        )
        lines.append(
            "  No comparison is reported. Without a second observer none of "
            "these episodes can be called an improvement or a miss."
        )
        return "\n".join(lines)

    lines.append(f"  shadow reference: {report.shadow_source}")
    for bucket, count in report.counts().items():
        lines.append(f"  {bucket}: {count}")

    adjudications = report.adjudication_counts()
    lines.append(
        "  v1_only adjudication: "
        + ", ".join(f"{name}={adjudications[name]}" for name in ADJUDICATIONS)
        + f", {AWAITING_HUMAN}={adjudications[AWAITING_HUMAN]}"
    )

    pending = report.awaiting_adjudication()
    if pending:
        lines.append("  awaiting human adjudication:")
        for candidate in pending:
            episode = candidate.episode
            evidence = ", ".join(
                f"{name}={value}" for name, value in sorted(episode.evidence.items())
            )
            lines.append(
                f"    - {episode.episode_id} ({episode.subject_class}/"
                f"{episode.shape}, onset {episode.onset_ms}) {evidence}"
            )
        lines.append(
            "  No miss count is available until each of the above is settled."
        )
    else:
        lines.append(f"  confirmed misses: {report.confirmed_miss_count()}")

    if any(episode.positional_key for episode in report.unmatched_key):
        lines.append(f"  NOTE: {report.positional_caveat}")
    return "\n".join(lines)
