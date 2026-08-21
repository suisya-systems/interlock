"""The one place a policy revision is resolved, and the only place ``policy_*`` is read.

``D-0031``'s corollary is the reason this module exists at all, and it is worth
stating before anything else:

    **A ``policy_*`` join without a ``revision_id`` predicate is a defect.**

Policy rows are versioned and never updated in place (``production-schema.md``
section 10), so a join that omits the revision matches *every* tolerance ever
recorded for the subject. A detector written that way emits one incident per
revision in the table and some of those incidents alarm against a tolerance that
was retired months ago -- and the failure is invisible while there is only one
revision on record, which is exactly the state a fresh database is in. It starts
misbehaving on the day someone changes a number, which is the day the tolerances
matter most.

Making that impossible to get wrong is a structural job rather than a review
convention: every reader in the control plane takes its numbers from here, and
every function below **requires** a ``revision_id`` the caller resolved through
:func:`effective_revision_id` (a detector, judging now) or
:func:`revision_over_period` (a report, judging a past window). No function
resolves a revision implicitly as a convenience, because a convenience default
is how the predicate goes missing again.

**Which revision, and the two callers who need different answers.** A detector
binds the revision effective *at* ``:now_ms``. A report binds the revisions in
force *across* its period -- plural, because a period that straddles a policy
change was judged under two sets of numbers and averaging across them produces a
figure that was never anyone's ceiling. ``D-0040`` requires the report to say so
at the top, so :func:`revision_over_period` returns the set and lets a member
count above one be the report's own signal.

**Windows are half-open, ``[start, end)``** (``time-base-policy.md`` section 2,
rule 4). A revision that takes effect exactly at a period's end belongs to the
next period and to exactly one; a closed interval would put it in both and
double-count the change.

**A relative threshold is not a duration until a subject is named.** Three of
the classes in ``time-base-policy.md`` section 3.2 carry a ``threshold_kind``
that is not ``'absolute_ms'``: ``watcher_silence`` is a multiple of *that
scope's* ``expected_interval_ms``, ``lease_orphan`` a multiple of *that lease's*
own TTL, and ``watcher_error_streak`` a count of consecutive failures with no
duration in it at all. :func:`resolve_tolerance_ms` is where the multiple meets
its subject; ``consecutive_count`` is **refused** there rather than coerced,
because the only coercion available -- treating 5 as 5 milliseconds -- produces a
tolerance that every subject crosses instantly.

**The budget has the same problem on the other side**, which is why
``budget_kind`` exists (the adjudication recorded on ``policy_detection_latency``
in ``0001_initial.sql``): ``lease_orphan``'s ``L`` is *twice the lease's own TTL*,
which no absolute millisecond value can hold. The DDL's ``T + P <= L`` ``CHECK``
therefore fires only when both sides are absolute, and :func:`budget_violations`
is the per-subject pass that asserts the identical inequality for every case a
``CHECK`` cannot reach across tables to see. A watcher scope registered with an
``expected_interval_ms`` so large that three missed polls exceed the
``watcher_silence`` budget is a misconfiguration; without this pass it presents
as a detector quietly slower than its own stated ceiling, for that one scope,
with nothing anywhere saying so.

**Nothing here writes.** Every function is a ``SELECT``, so no transaction helper
appears in this module: a policy change is a new revision inserted by a migration
step, never an ``UPDATE`` issued by a reader. Timestamps are integer epoch
milliseconds supplied by the caller -- no function consults a clock, and no
column consulted here has a ``DEFAULT``.
"""

from __future__ import annotations

import sqlite3
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

#: The vocabulary of ``policy_detection_latency.threshold_kind``, mirroring the
#: DDL's ``CHECK``. Held here so a caller can branch on the kind exhaustively
#: without re-deriving the list from the schema; the schema remains the
#: authority and :func:`detection_latency` refuses anything outside it.
THRESHOLD_KINDS: frozenset[str] = frozenset(
    {
        "absolute_ms",
        "scope_interval_multiple",
        "lease_ttl_multiple",
        "consecutive_count",
    }
)

#: The vocabulary of ``policy_detection_latency.budget_kind``. Shorter than
#: :data:`THRESHOLD_KINDS` because only one class has a relative budget, and the
#: DDL's ``CHECK`` says which two members are legal.
BUDGET_KINDS: frozenset[str] = frozenset({"absolute_ms", "lease_ttl_multiple"})

#: The threshold kinds whose ``T`` is a duration only once a subject is named.
#: ``consecutive_count`` is deliberately absent: it is not a duration for *any*
#: subject.
_RELATIVE_THRESHOLD_KINDS: frozenset[str] = frozenset(
    {"scope_interval_multiple", "lease_ttl_multiple"}
)

#: Which table a relative kind draws its subject from. The mapping is what makes
#: "live subject" a decidable question in :func:`budget_violations`.
_SUBJECT_KIND_OF: Mapping[str, str] = MappingProxyType(
    {
        "scope_interval_multiple": "watcher_scope",
        "lease_ttl_multiple": "lease",
    }
)


class PolicyRefusal(Exception):
    """A policy read that cannot be answered, stated rather than guessed at."""


class NoEffectiveRevision(PolicyRefusal):
    """No ``policy_revision`` is effective at the instant asked about.

    Distinct from an empty result: a detector that silently skipped its pass
    because policy had not been seeded would look exactly like a detector that
    found nothing wrong.
    """


class PolicyRowMissing(PolicyRefusal):
    """The revision exists but says nothing about this class, gate type or stage.

    An absent row is not the same fact as a ``NULL`` tolerance. ``NULL`` is the
    seeded, deliberate "this stage is never a relay gap"; an absent row means the
    revision was never asked to decide, and returning ``None`` for both would let
    an unseeded gate type inherit the human stage's exemption.
    """


class NotADuration(PolicyRefusal):
    """``T`` is a count, and no subject turns it into milliseconds.

    ``watcher_error_streak``'s threshold is five *consecutive failures*. The one
    coercion available -- reading 5 as 5 ms -- yields a tolerance every subject
    crosses on its first poll, so the refusal is the only honest answer.
    """


class PolicyUsageError(ValueError):
    """The caller asked in a way the policy row cannot be applied to."""


def effective_revision_id(connection: sqlite3.Connection, *, now_ms: int) -> int:
    """The revision in force at *now_ms* -- what a detector binds.

    ``ORDER BY effective_at_ms DESC, revision_id DESC`` and not ``effective_at_ms``
    alone: two revisions may legitimately share an instant (a correction inserted
    in the same pass as the row it corrects), and ``AUTOINCREMENT`` makes the
    higher ``revision_id`` the later decision. Without the tiebreaker SQLite is
    free to return either, and a detector would silently alternate between two
    sets of numbers across restarts.
    """

    row = connection.execute(
        """
        SELECT revision_id FROM policy_revision
         WHERE effective_at_ms <= ?
         ORDER BY effective_at_ms DESC, revision_id DESC
         LIMIT 1
        """,
        (now_ms,),
    ).fetchone()
    if row is None:
        raise NoEffectiveRevision(
            f"no policy_revision is effective at now_ms={now_ms}; "
            "the time base has not been seeded for this instant"
        )
    return int(row[0])


def revision_over_period(
    connection: sqlite3.Connection, *, period_start_ms: int, period_end_ms: int
) -> tuple[int, ...]:
    """Every revision in force across the half-open period, oldest first.

    Two disjoint sources, and both are needed. The revision already in force when
    the period opened is found by the same "latest at or before" rule as
    :func:`effective_revision_id` -- it governs the period's first millisecond
    even though it took effect long before. Every revision that took effect
    *inside* ``[start, end)`` then joins it.

    A revision whose ``effective_at_ms`` equals ``period_end_ms`` is excluded: the
    window is half-open, so that instant belongs to the next period, and to
    exactly one (``time-base-policy.md`` section 2, rule 4). A report that
    included it would attribute latencies to numbers that were not yet in force
    when they were measured.

    More than one member means the period is **non-homogeneous** (``D-0040``):
    the report must say so at the top rather than averaging a figure across a
    tolerance change. Returning the set rather than a single value is what makes
    that statable at all.
    """

    if period_end_ms < period_start_ms:
        raise PolicyUsageError(
            f"period_end_ms ({period_end_ms}) precedes period_start_ms ({period_start_ms})"
        )

    revisions: list[int] = []
    opening = connection.execute(
        """
        SELECT revision_id FROM policy_revision
         WHERE effective_at_ms <= ?
         ORDER BY effective_at_ms DESC, revision_id DESC
         LIMIT 1
        """,
        (period_start_ms,),
    ).fetchone()
    if opening is not None:
        revisions.append(int(opening[0]))

    for row in connection.execute(
        """
        SELECT revision_id FROM policy_revision
         WHERE effective_at_ms > ? AND effective_at_ms < ?
         ORDER BY effective_at_ms ASC, revision_id ASC
        """,
        (period_start_ms, period_end_ms),
    ):
        revisions.append(int(row[0]))

    return tuple(revisions)


def detection_latency(
    connection: sqlite3.Connection, *, revision_id: int, incident_class: str
) -> Mapping[str, Any]:
    """The whole ``T`` / ``P`` / ``L`` row for one incident class under one revision.

    Returned as one mapping rather than three accessors because the three numbers
    are only meaningful together: ``threshold_value`` without ``threshold_kind``
    is an integer of unknown unit, and ``budget_ms`` without ``budget_kind`` is
    the ``lease_orphan`` row read as 2 milliseconds.
    """

    row = connection.execute(
        """
        SELECT revision_id, incident_class, threshold_kind, threshold_value,
               reconcile_period_ms, budget_ms, budget_kind
          FROM policy_detection_latency
         WHERE revision_id = ? AND incident_class = ?
        """,
        (revision_id, incident_class),
    ).fetchone()
    if row is None:
        raise PolicyRowMissing(
            f"revision {revision_id} decides no detection latency for "
            f"incident_class={incident_class!r}"
        )
    return _detection_latency_mapping(row)


def gate_stage_tolerance(
    connection: sqlite3.Connection, *, revision_id: int, gate_type: str, stage: str
) -> int | None:
    """This stage's relay-gap tolerance, or ``None`` for "never a gap".

    ``None`` is the seeded value of the ``presented`` stage and carries
    ``time-base-policy.md`` section 4's claim that a slow human is not a gap. It
    is data precisely so the detector query has no ``stage = 'presented'``
    special case, which is where a future gate type would be handed a human
    exemption by accident.

    An absent row raises :class:`PolicyRowMissing` instead, because the two facts
    differ: ``forwarded`` has no row because it is terminal, and a gate type this
    revision never decided has no row because nobody decided it. Collapsing both
    into ``None`` would make an undecided gate type silently unpoliced.
    """

    row = connection.execute(
        """
        SELECT tolerance_ms FROM policy_gate_stage_tolerance
         WHERE revision_id = ? AND gate_type = ? AND stage = ?
        """,
        (revision_id, gate_type, stage),
    ).fetchone()
    if row is None:
        raise PolicyRowMissing(
            f"revision {revision_id} decides no stage tolerance for "
            f"gate_type={gate_type!r} stage={stage!r}"
        )
    return None if row[0] is None else int(row[0])


def gate_stage_owner(
    connection: sqlite3.Connection, *, revision_id: int, gate_type: str, stage: str
) -> Mapping[str, Any]:
    """Who holds the ball at this stage, and who answers for the gate type.

    ``D-0032`` keeps both out of the ``gate`` row: one column cannot mean two
    things, and an owner stored on the row *drifts* -- the gate advances, the
    column does not, and the ``relay_gap`` incident then names a role that has
    not held the ball for an hour. Deriving both from ``(gate_type, stage)`` in
    versioned policy makes that drift unrepresentable and lets a report say who
    the owner *was* by binding the revision that was effective then.
    """

    row = connection.execute(
        """
        SELECT ball_holder, standing_owner FROM policy_gate_stage_owner
         WHERE revision_id = ? AND gate_type = ? AND stage = ?
        """,
        (revision_id, gate_type, stage),
    ).fetchone()
    if row is None:
        raise PolicyRowMissing(
            f"revision {revision_id} decides no owner for "
            f"gate_type={gate_type!r} stage={stage!r}"
        )
    return MappingProxyType(
        {
            "revision_id": revision_id,
            "gate_type": gate_type,
            "stage": stage,
            "ball_holder": str(row[0]),
            "standing_owner": str(row[1]),
        }
    )


def resolve_tolerance_ms(
    connection: sqlite3.Connection,
    *,
    revision_id: int,
    incident_class: str,
    subject: str | None,
) -> int:
    """``T`` for this subject, in milliseconds.

    *subject* is the identity the multiple applies to, and which identity that is
    follows from ``threshold_kind``: a ``watcher_scope.scope_id`` for
    ``scope_interval_multiple``, a ``lease.resource`` for ``lease_ttl_multiple``.
    It is ignored for ``absolute_ms``, where the row already *is* the answer and
    no subject can change it -- a caller iterating classes uniformly may pass one
    and get the same number back.

    ``consecutive_count`` raises :class:`NotADuration`. It is the one kind no
    subject rescues, and refusing it here is what keeps the coercion from
    happening silently three call sites away.

    A relative kind with ``subject=None`` raises :class:`PolicyUsageError` rather
    than falling back to the bare multiple: the bare multiple is a small integer
    of milliseconds, so the fallback would produce a tolerance of three
    milliseconds for a scope whose real tolerance is nine minutes.
    """

    row = detection_latency(
        connection, revision_id=revision_id, incident_class=incident_class
    )
    kind = row["threshold_kind"]
    value = int(row["threshold_value"])

    if kind == "absolute_ms":
        return value
    if kind == "consecutive_count":
        raise NotADuration(
            f"{incident_class!r} has threshold_kind='consecutive_count' "
            f"(T = {value} consecutive failures); it is a count, not a duration"
        )
    if subject is None:
        raise PolicyUsageError(
            f"{incident_class!r} has threshold_kind={kind!r}, which is a multiple "
            "of the subject's own interval or TTL; a subject is required"
        )
    return value * subject_unit_ms(connection, threshold_kind=kind, subject=subject)


def subject_unit_ms(
    connection: sqlite3.Connection, *, threshold_kind: str, subject: str
) -> int:
    """The duration one unit of a relative multiple stands for, for this subject.

    A *subject unit* is the number a relative policy value is multiplied by, and
    which number that is follows from the kind:

    * ``lease_ttl_multiple`` -- **that lease's own TTL**, i.e.
      ``expires_at_ms - acquired_at_ms`` for the ``lease`` row named by
      *subject* (its ``resource``). ``lease_orphan``'s ``T`` and ``L`` are both
      multiples of it (``time-base-policy.md`` section 3.2).
    * ``scope_interval_multiple`` -- **that scope's** ``expected_interval_ms``,
      for the ``watcher_scope`` row named by *subject* (its ``scope_id``).

    **This is public, and it is one function, because both sides of the budget
    inequality need it.** :func:`resolve_tolerance_ms` scales ``T`` here, and
    :func:`~claude_org_runtime.measurement.windows.resolve_budget_ms`
    scales ``L`` here. ``D-0041``
    narrowed the DDL's ``T + P <= L`` ``CHECK`` to the rows where both sides are
    absolute, on the explicit promise that relative rows are asserted per subject
    at reconcile time (:func:`budget_violations`) -- so the two sides are compared
    to each other, and comparing them is only meaningful while they were scaled by
    the *same* number.

    A second copy of these two queries on the ``L`` side would agree with this one
    exactly until the day a unit changed -- a lease re-acquired with a different
    TTL, a scope re-registered with a different interval, a column renamed in a
    migration -- and from that day ``T`` and ``L`` would be scaled by different
    numbers for the same subject. Nothing raises when that happens: the pass still
    runs and still reports, but ``T + P <= L`` is then an inequality between two
    different units. Which way it lies is whichever way the drift went -- a budget
    silently too generous hides a detector that is late, a budget silently too
    tight alarms on scopes that are fine -- so there is one function, and callers
    outside this module call this name.
    """

    if threshold_kind == "scope_interval_multiple":
        row = connection.execute(
            "SELECT expected_interval_ms FROM watcher_scope WHERE scope_id = ?",
            (subject,),
        ).fetchone()
        if row is None:
            raise PolicyUsageError(f"no watcher_scope with scope_id={subject!r}")
        return int(row[0])

    if threshold_kind == "lease_ttl_multiple":
        row = connection.execute(
            "SELECT expires_at_ms - acquired_at_ms FROM lease WHERE resource = ?",
            (subject,),
        ).fetchone()
        if row is None:
            raise PolicyUsageError(f"no lease with resource={subject!r}")
        return int(row[0])

    raise PolicyRefusal(f"{threshold_kind!r} names no subject unit")


def budget_violations(
    connection: sqlite3.Connection, *, revision_id: int, now_ms: int
) -> tuple[Mapping[str, Any], ...]:
    """Every live subject whose own numbers break ``T + P <= L``.

    Section 10's ``policy_budget_violation`` pass. The DDL asserts ``T + P <= L``
    for the rows where both sides are absolute; it cannot assert it for the rest,
    because ``T`` or ``L`` is a multiple of something in *another table* and a
    ``CHECK`` sees only its own row. This function is that missing half, run per
    subject at reconcile time against the same inequality -- not a similar one.

    The worked case from ``time-base-policy.md`` section 3.3: with
    ``watcher_silence``'s ``L`` at 10 min, ``P`` at 120 s and ``T`` at three of
    the scope's own polls, a scope may poll no slower than
    ``(600000 - 120000) / 3 = 160000`` ms. A scope registered slower than that is
    served by a detector that cannot possibly meet its stated ceiling for it, and
    the whole point of naming the misconfiguration is that the alternative is a
    ceiling quietly violated for one scope while every report says the ceiling
    holds.

    "Live" is per subject kind: an enabled, unretired ``watcher_scope``, and a
    ``lease`` that has not expired as of *now_ms*. A retired scope has no watcher
    to be slow, and an expired lease has no orphan window left to size.

    ``consecutive_count`` rows are skipped, not refused: their ``T`` is not a
    duration for any subject (:class:`NotADuration`), so there is no inequality to
    evaluate, and raising would stop a whole reconcile pass over a row that is
    correctly configured.
    """

    violations: list[Mapping[str, Any]] = []
    for row in connection.execute(
        """
        SELECT revision_id, incident_class, threshold_kind, threshold_value,
               reconcile_period_ms, budget_ms, budget_kind
          FROM policy_detection_latency
         WHERE revision_id = ?
           AND (threshold_kind <> 'absolute_ms' OR budget_kind <> 'absolute_ms')
         ORDER BY incident_class ASC
        """,
        (revision_id,),
    ).fetchall():
        policy = _detection_latency_mapping(row)
        if policy["threshold_kind"] == "consecutive_count":
            continue
        violations.extend(_violations_for_class(connection, policy=policy, now_ms=now_ms))
    return tuple(violations)


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _detection_latency_mapping(row: Sequence[Any]) -> Mapping[str, Any]:
    """One ``policy_detection_latency`` row, with its two kinds validated.

    The kinds are re-checked against :data:`THRESHOLD_KINDS` / :data:`BUDGET_KINDS`
    even though the DDL constrains them, because this module is also read against
    databases migrated by an older head, and a kind this code has no branch for
    would otherwise fall through to whichever ``if`` happened to be last.
    """

    threshold_kind = str(row[2])
    budget_kind = str(row[6])
    if threshold_kind not in THRESHOLD_KINDS:
        raise PolicyRefusal(
            f"unknown threshold_kind {threshold_kind!r} on incident_class={row[1]!r}"
        )
    if budget_kind not in BUDGET_KINDS:
        raise PolicyRefusal(
            f"unknown budget_kind {budget_kind!r} on incident_class={row[1]!r}"
        )
    return MappingProxyType(
        {
            "revision_id": int(row[0]),
            "incident_class": str(row[1]),
            "threshold_kind": threshold_kind,
            "threshold_value": int(row[3]),
            "reconcile_period_ms": int(row[4]),
            "budget_ms": int(row[5]),
            "budget_kind": budget_kind,
        }
    )


def _violations_for_class(
    connection: sqlite3.Connection, *, policy: Mapping[str, Any], now_ms: int
) -> Iterable[Mapping[str, Any]]:
    """``T + P <= L`` for every live subject of one relative class."""

    threshold_kind = policy["threshold_kind"]
    budget_kind = policy["budget_kind"]

    relative_sides = {
        kind
        for kind in (threshold_kind, budget_kind)
        if kind in _RELATIVE_THRESHOLD_KINDS
    }
    if len(relative_sides) != 1:
        # Both sides relative to different subjects has no evaluable meaning: T
        # would be scaled by a scope's poll interval and L by some lease's TTL,
        # with nothing tying the two subjects together. It is a defective policy
        # row rather than a violated budget, so it is refused rather than
        # reported as one subject's misconfiguration.
        raise PolicyRefusal(
            f"{policy['incident_class']!r} mixes threshold_kind={threshold_kind!r} "
            f"with budget_kind={budget_kind!r}; the two name different subjects"
        )
    (relative_kind,) = relative_sides
    subject_kind = _SUBJECT_KIND_OF[relative_kind]

    period_ms = policy["reconcile_period_ms"]
    for subject, unit_ms in _live_subjects(
        connection, subject_kind=subject_kind, now_ms=now_ms
    ):
        tolerance_ms = (
            policy["threshold_value"] * unit_ms
            if threshold_kind == relative_kind
            else policy["threshold_value"]
        )
        budget_ms = (
            policy["budget_ms"] * unit_ms
            if budget_kind == relative_kind
            else policy["budget_ms"]
        )
        if tolerance_ms + period_ms <= budget_ms:
            continue
        yield MappingProxyType(
            {
                "revision_id": policy["revision_id"],
                "incident_class": policy["incident_class"],
                "subject_kind": subject_kind,
                "subject_id": subject,
                "tolerance_ms": tolerance_ms,
                "reconcile_period_ms": period_ms,
                "budget_ms": budget_ms,
                "excess_ms": tolerance_ms + period_ms - budget_ms,
            }
        )


def _live_subjects(
    connection: sqlite3.Connection, *, subject_kind: str, now_ms: int
) -> Iterable[tuple[str, int]]:
    """Each live subject of a kind, paired with the duration its multiple scales.

    Liveness is the subject's own notion and neither definition is a filter of
    convenience. A retired or disabled ``watcher_scope`` has no watcher obliged to
    poll it, so its interval cannot make a detector late. A lease whose
    ``expires_at_ms`` has passed has no orphan window left to be sized against;
    the next acquisition raises the epoch and brings its own TTL.
    """

    if subject_kind == "watcher_scope":
        rows = connection.execute(
            """
            SELECT scope_id, expected_interval_ms FROM watcher_scope
             WHERE enabled = 1 AND retired_at_ms IS NULL
             ORDER BY scope_id ASC
            """
        )
    elif subject_kind == "lease":
        rows = connection.execute(
            """
            SELECT resource, expires_at_ms - acquired_at_ms FROM lease
             WHERE expires_at_ms > ?
             ORDER BY resource ASC
            """,
            (now_ms,),
        )
    else:
        raise PolicyRefusal(f"no live-subject query for subject_kind={subject_kind!r}")

    for row in rows.fetchall():
        yield str(row[0]), int(row[1])
