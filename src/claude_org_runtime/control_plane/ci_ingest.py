"""G3 -- CI outcome ingestion: one identity, evidence that is never overwritten.

``docs/production-schema.md`` sections 6.1-6.3 and ``D-0033`` are the design this
module implements; nothing here decides anything they left open. Two failures are
what the shape is answering, and both are worth restating because every rule
below is one of them made unreachable.

**Without an identity**, a re-poll, a CI rerun, a PR head update and a late
arrival are indistinguishable, and the event spine's ``dedup_key`` has nothing to
be made of. The identity is
``(provider, repo_id, pr_number, head_sha, check_scope, scope_id, attempt, verdict)``.
It lives in exactly one place in this module -- :class:`ObservationIdentity` --
because section 6.2 says the unique index on ``ci_observation`` and the event's
``dedup_key`` are *the same constraint expressed twice*, and two hand-written
renderings of one tuple drift silently: the index would keep refusing a re-poll
while the spine started accepting it, or the reverse, and neither shows up as an
error anywhere. So the dedup key is *derived* from the same object the ``INSERT``
parameters are derived from, and :func:`observation_dedup_key` is that derivation
exposed rather than a second copy of it.

``verdict`` being **in** the identity is the part that costs a real result if it
is dropped. A fetch failure records ``indeterminate`` for a scope; the next poll
succeeds and the provider says ``failed``. Provider, repo, PR, head, scope and
attempt are all unchanged -- the rerun never happened, only our observation of it
improved -- so an identity without ``verdict`` collides, the append is an
idempotent no-op, and the PR stays projected ``indeterminate`` forever with the
real verdict discarded.

**Without an ordering rule**, arrival-order last-write-wins lets a stale verdict
overwrite a newer one -- a red PR reported green because the red observation was
slower, which is ``D-0006``'s verdict honesty violated in the most direct way
available. So observations are evidence and are never updated or deleted; the
current verdict is the ``ci_current_verdict`` **view** (a projection, not a
column, so it cannot drift from the rows it summarises) folded by
:func:`pr_verdict`. A late arrival that orders lower is stored and moves nothing.

**The edge is where a malformed identity is refused.** An abbreviated or
upper-case ``head_sha``, an ``attempt`` below 1, a verdict outside the closed set
and a provider that is not ``github`` are all also ``CHECK``ed in the DDL, and
that duplication is deliberate rather than redundant: the ``CHECK`` fires *inside*
the append transaction, after the event row has already been written, so the
producer learns "your database rejected something" rather than "this SHA is an
abbreviation". Refusing at the edge names the defect at the only place that knows
which field the caller got wrong, and it keeps a doomed transaction from taking
the write lock at all.

**Nothing in this module opens a transaction.** The append is
:func:`~claude_org_runtime.control_plane.events.append_event`'s, and the
``ci_observation`` row is written as its ``side_effect`` so that the fact and its
evidence commit together or not at all. Reimplementing the append here would give
the spine a second writer with its own idea of the fan-out, which is exactly the
push-vs-poll duplication section 5.4 exists to remove.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from claude_org_runtime.control_plane.events import AppendedEvent, append_event
from claude_org_runtime.control_plane.schema import ControlPlaneRefusal

__all__ = [
    "CI_PROVIDERS",
    "CI_VERDICTS",
    "CHECK_SCOPES",
    "CI_OBSERVED_EVENT_TYPE",
    "VERDICT_SEVERITY",
    "ObservationIdentity",
    "CiObservationRefused",
    "UnsupportedProviderRefused",
    "MalformedHeadShaRefused",
    "MalformedAttemptRefused",
    "MalformedPrNumberRefused",
    "UnknownVerdictRefused",
    "UnknownCheckScopeRefused",
    "EmptyIdentityFieldRefused",
    "observation_dedup_key",
    "record_ci_observation",
    "scope_verdicts",
    "pr_verdict",
]


#: The only provider designed in. ``D-0033``: "a second provider widens the
#: ``CHECK`` in a migration step and brings its substitution test then". A
#: case-variant such as ``'GITHUB'`` is a *different* string in the identity and
#: would therefore admit the same fact twice, so this set is compared against
#: exactly, never case-folded.
CI_PROVIDERS = frozenset({"github"})

#: The closed verdict vocabulary of ``ci_observation``. ``indeterminate`` and
#: ``no_run`` are separate members because collapsing either into ``failed`` is
#: the v1 defect ``D-0006`` records.
CI_VERDICTS = frozenset(
    {"passed", "failed", "cancelled", "timed_out", "no_run", "indeterminate"}
)

#: The scopes an observation may be attributed to. ``rollup`` is the coarse
#: fallback an old ``gh`` forces, and section 6.3 rule 3 makes it subordinate --
#: it is not a peer of the fine-grained scopes.
CHECK_SCOPES = frozenset({"check_suite", "workflow_run", "rollup"})

#: The event type this module appends. The DDL leaves ``event.event_type`` open
#: text on purpose; this is the implementation's vocabulary, not a constraint.
CI_OBSERVED_EVENT_TYPE = "ci_observed"

#: The severity fold of section 6.3 rule 5: ``failed > timed_out > cancelled >
#: indeterminate > passed``.
#:
#: ``indeterminate`` outranking ``passed`` is ``D-0006`` again -- an unobservable
#: check is not a green one. ``no_run`` is ranked lowest but that rank is never
#: what decides an answer: :func:`pr_verdict` removes ``no_run`` from the
#: evidence *before* folding, because ``no_run`` means "no eligible evidence"
#: rather than "this PR passed", and a fold that merely ranked it below
#: ``passed`` would still report a PR green the moment one scope said nothing.
VERDICT_SEVERITY: Mapping[str, int] = MappingProxyType(
    {
        "failed": 5,
        "timed_out": 4,
        "cancelled": 3,
        "indeterminate": 2,
        "passed": 1,
        "no_run": 0,
    }
)

#: The verdict :func:`pr_verdict` reports when nothing eligible remains to fold.
NO_ELIGIBLE_EVIDENCE = "no_run"

_HEX = frozenset("0123456789abcdef")
_SHA_LENGTH = 40


class CiObservationRefused(ControlPlaneRefusal):
    """An observation was refused at the edge; nothing was written.

    A subclass of :class:`~.schema.ControlPlaneRefusal` because the answer is the
    same one that family carries everywhere else -- the state was neither
    repaired nor guessed at. The subclasses below exist so that a caller (and a
    test) can say *which* field of the identity was wrong; a bare ``bool`` return
    would make "refused" indistinguishable from "already on the spine", and those
    two need opposite responses from a watcher.
    """


class UnsupportedProviderRefused(CiObservationRefused):
    """The provider is not ``'github'``.

    Separate from the other refusals because a case variant is the dangerous
    shape: ``provider`` is part of the identity but the ``ci_current_verdict``
    per-scope subquery does not discriminate on it, so a ``'GITHUB'`` duplicate
    of a green observation would compete against the real red one on
    ``(attempt, occurred_at_ms, event_seq)`` and a later-timestamped bogus row
    would win -- section 6.1's verdict-honesty failure in its most direct form.
    """


class MalformedHeadShaRefused(CiObservationRefused):
    """The head SHA is not a full 40-character lowercase hex commit id.

    An abbreviation is not an identity: two heads can share a prefix, and the
    observation would then be attributed to the wrong head -- and, through
    ``ci_current_verdict``'s join on ``pull_request.head_sha``, to the wrong
    projection.
    """


class MalformedAttemptRefused(CiObservationRefused):
    """The attempt number is not an integer of at least 1.

    ``attempt`` is the leading term of the projection's ordering, so a zero or
    negative attempt does not merely look wrong -- it sorts a rerun *below* the
    run it replaced.
    """


class MalformedPrNumberRefused(CiObservationRefused):
    """The PR number is not a positive integer.

    Its own class rather than a shared "bad number" refusal because the caller's
    next move differs: a bad ``attempt`` is a parsing bug in the adapter's read
    of one check run, while a bad ``pr_number`` means the whole observation is
    attributed to nothing -- and section 7.1's dated incident is precisely a PR
    number resolved against the wrong thing and stored anyway.
    """


class UnknownVerdictRefused(CiObservationRefused):
    """The verdict is outside :data:`CI_VERDICTS`.

    The vocabulary is closed so that :data:`VERDICT_SEVERITY` is total over it:
    an unrecognised verdict reaching the fold would have no rank, and the only
    available failure modes there are "crash" or "silently treated as green".
    """


class UnknownCheckScopeRefused(CiObservationRefused):
    """The check scope is outside :data:`CHECK_SCOPES`.

    A scope the view does not know is a scope rule 3 cannot classify as coarse
    or fine, so it would take part in the fold as a peer of the real checks.
    """


class EmptyIdentityFieldRefused(CiObservationRefused):
    """A field of the identity is empty, so the rendered dedup key is ambiguous.

    ``'ci/github//7/...'`` and ``'ci/github/x//...'`` are different facts that a
    reader cannot tell apart, and an empty component makes the separator-joined
    key non-injective -- which is the one property the whole rendering rests on.
    """


@dataclass(frozen=True)
class ObservationIdentity:
    """The single tuple both the ``dedup_key`` and the row's key columns come from.

    Validated on construction, so an instance of this class is by definition an
    identity the DDL will accept: the edge checks and the ``CHECK`` constraints
    say the same things, and this is the place they are kept saying them
    together.
    """

    provider: str
    repo_id: str
    pr_number: int
    head_sha: str
    check_scope: str
    scope_id: str
    attempt: int
    verdict: str

    def __post_init__(self) -> None:
        if self.provider not in CI_PROVIDERS:
            raise UnsupportedProviderRefused(
                f"provider {self.provider!r} is not one of "
                f"{sorted(CI_PROVIDERS)}; a case variant is a different string "
                "in the identity and would admit the same fact twice"
            )
        for field_name in ("repo_id", "scope_id"):
            if not getattr(self, field_name):
                raise EmptyIdentityFieldRefused(
                    f"{field_name} is empty; the rendered dedup key would be ambiguous"
                )
        if not isinstance(self.pr_number, int) or isinstance(self.pr_number, bool):
            raise MalformedPrNumberRefused(
                f"pr_number {self.pr_number!r} is not an integer"
            )
        if self.pr_number < 1:
            raise MalformedPrNumberRefused(
                f"pr_number {self.pr_number!r} is below 1"
            )
        if not _is_full_lowercase_sha(self.head_sha):
            raise MalformedHeadShaRefused(
                f"head_sha {self.head_sha!r} is not a full {_SHA_LENGTH}-character "
                "lowercase hex commit id; an abbreviation is not an identity "
                "because two heads can share a prefix"
            )
        if self.check_scope not in CHECK_SCOPES:
            raise UnknownCheckScopeRefused(
                f"check_scope {self.check_scope!r} is not one of {sorted(CHECK_SCOPES)}"
            )
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise MalformedAttemptRefused(
                f"attempt {self.attempt!r} is not an integer"
            )
        if self.attempt < 1:
            raise MalformedAttemptRefused(
                f"attempt {self.attempt!r} is below 1; attempt leads the "
                "projection's ordering, so a rerun must never sort below the "
                "run it replaced"
            )
        if self.verdict not in CI_VERDICTS:
            raise UnknownVerdictRefused(
                f"verdict {self.verdict!r} is not one of {sorted(CI_VERDICTS)}"
            )

    @property
    def dedup_key(self) -> str:
        """The identity rendered as section 6.2's event ``dedup_key`` string."""

        return "/".join(
            (
                "ci",
                self.provider,
                self.repo_id,
                str(self.pr_number),
                self.head_sha,
                self.check_scope,
                self.scope_id,
                str(self.attempt),
                self.verdict,
            )
        )

    @property
    def subject_id(self) -> str:
        """The event subject: the PR's provider-side identity, ``repo_id#number``.

        Not ``pull_request.pr_id``. A CI observation references ``repository``
        and ``event`` but deliberately not ``pull_request``, so an observation
        may legitimately arrive before we have ever recorded the PR row -- and a
        subject that could only be filled in once that row existed would either
        drop the event or make the subject depend on arrival order.
        ``(repo_id, pr_number)`` is an alternate key of ``pull_request``
        (``pull_request_identity``), so this names the same PR either way.
        """

        return f"{self.repo_id}#{self.pr_number}"


def _is_full_lowercase_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA_LENGTH
        and all(character in _HEX for character in value)
    )


def observation_dedup_key(
    *,
    provider: str = "github",
    repo_id: str,
    pr_number: int,
    head_sha: str,
    check_scope: str,
    scope_id: str,
    attempt: int,
    verdict: str,
) -> str:
    """Render section 6.2's dedup key, refusing an identity the DDL would refuse.

    The rendering is :attr:`ObservationIdentity.dedup_key`; this function exists
    so that a caller wanting only the key -- a reconcile pass asking "is this
    fact already on the spine?" -- gets it from the same construction
    :func:`record_ci_observation` uses rather than from a format string of its
    own.
    """

    return ObservationIdentity(
        provider=provider,
        repo_id=repo_id,
        pr_number=pr_number,
        head_sha=head_sha,
        check_scope=check_scope,
        scope_id=scope_id,
        attempt=attempt,
        verdict=verdict,
    ).dedup_key


def record_ci_observation(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    provider: str = "github",
    repo_id: str,
    pr_number: int,
    head_sha: str,
    check_scope: str,
    scope_id: str,
    attempt: int,
    verdict: str,
    observer: str,
    observer_epoch: int,
    occurred_at_ms: int,
    ingested_at_ms: int,
    verdict_detail: str | None = None,
    source_id: str | None = None,
    event_id: str | None = None,
    run_id: str | None = None,
) -> AppendedEvent:
    """Append one CI observation to the spine, with its evidence row, atomically.

    The event carries the identity as its ``dedup_key``, so a re-poll of the
    identical fact is refused at the *first* statement of the append transaction
    (section 5.4 step 1) and reported as
    :class:`~.events.AppendedEvent` with ``duplicate=True`` -- an idempotent
    no-op, not an error. Nothing downstream of that statement runs, which is why
    a watcher may re-poll as often as it likes without the fan-out, the outbox or
    this side table seeing the repeat at all.

    ``event_id`` defaults to ``observation_id``. Both are the caller's own
    identifier for *this observation attempt*, and giving them one value keeps
    the event and its evidence row trivially correlatable without inventing a
    second identifier scheme; a re-poll that mints a fresh ``observation_id``
    still collides on the ``dedup_key``, which is the constraint that is supposed
    to catch it.

    ``occurred_at_ms`` is the provider's clock and ``ingested_at_ms`` is ours;
    they are never conflated, and neither is read from a clock here -- both are
    the caller's, as every timestamp in this schema is.
    """

    identity = ObservationIdentity(
        provider=provider,
        repo_id=repo_id,
        pr_number=pr_number,
        head_sha=head_sha,
        check_scope=check_scope,
        scope_id=scope_id,
        attempt=attempt,
        verdict=verdict,
    )

    def insert_observation(inner: sqlite3.Connection, event_seq: int) -> None:
        inner.execute(
            """
            INSERT INTO ci_observation (observation_id, event_seq, provider, repo_id,
                                        pr_number, head_sha, check_scope, scope_id, attempt,
                                        verdict, verdict_detail, source_id, observer,
                                        observer_epoch, occurred_at_ms, ingested_at_ms)
            VALUES (:observation_id, :event_seq, :provider, :repo_id, :pr_number, :head_sha,
                    :check_scope, :scope_id, :attempt, :verdict, :verdict_detail, :source_id,
                    :observer, :observer_epoch, :occurred_at_ms, :ingested_at_ms)
            """,
            {
                "observation_id": observation_id,
                "event_seq": event_seq,
                "provider": identity.provider,
                "repo_id": identity.repo_id,
                "pr_number": identity.pr_number,
                "head_sha": identity.head_sha,
                "check_scope": identity.check_scope,
                "scope_id": identity.scope_id,
                "attempt": identity.attempt,
                "verdict": identity.verdict,
                "verdict_detail": verdict_detail,
                "source_id": source_id,
                "observer": observer,
                "observer_epoch": observer_epoch,
                "occurred_at_ms": occurred_at_ms,
                "ingested_at_ms": ingested_at_ms,
            },
        )

    return append_event(
        connection,
        event_id=observation_id if event_id is None else event_id,
        event_type=CI_OBSERVED_EVENT_TYPE,
        subject_kind="pull_request",
        subject_id=identity.subject_id,
        dedup_key=identity.dedup_key,
        producer=observer,
        producer_epoch=observer_epoch,
        occurred_at_ms=occurred_at_ms,
        ingested_at_ms=ingested_at_ms,
        run_id=run_id,
        payload=None,
        side_effect=insert_observation,
    )


def scope_verdicts(
    connection: sqlite3.Connection, *, repo_id: str, pr_number: int
) -> tuple[Mapping[str, Any], ...]:
    """The eligible per-scope projection for one PR, straight out of the view.

    Reading ``ci_current_verdict`` rather than re-deriving it in Python is the
    point: rule 1 (only the PR's current head is eligible), rule 2 (the
    ``attempt DESC, occurred_at_ms DESC, event_seq DESC`` order) and rule 3 (a
    rollup drops out the moment a fine-grained scope exists for that head) are
    all in the view, and a second implementation of them here would be a second
    thing to keep true.

    The rows are returned oldest-scope-first by ``(check_scope, scope_id)`` so
    that a caller rendering them gets a stable order; the projection itself is
    unordered, one row per eligible scope.
    """

    cursor = connection.execute(
        """
        SELECT repo_id, pr_number, head_sha, check_scope, scope_id, verdict, attempt,
               occurred_at_ms, event_seq
          FROM ci_current_verdict
         WHERE repo_id = ? AND pr_number = ?
         ORDER BY check_scope, scope_id
        """,
        (repo_id, pr_number),
    )
    columns = [description[0] for description in cursor.description]
    return tuple(
        MappingProxyType(dict(zip(columns, row))) for row in cursor.fetchall()
    )


def pr_verdict(connection: sqlite3.Connection, *, repo_id: str, pr_number: int) -> str:
    """Fold the eligible per-scope verdicts into one, most severe wins.

    Section 6.3 rule 5. ``no_run`` rows are dropped *before* the fold rather than
    ranked within it: ``no_run`` is a fact about the repository ("no CI is
    configured for this head"), not about the change, and a fold that merely
    ranked it below ``passed`` would answer ``passed`` for a PR whose only
    evidence is that nothing ran. When nothing eligible survives -- no
    observation for the current head, or every one of them ``no_run`` -- the
    answer is :data:`NO_ELIGIBLE_EVIDENCE`, which says absent evidence and is not
    a pass.
    """

    verdicts = [
        str(row["verdict"])
        for row in scope_verdicts(connection, repo_id=repo_id, pr_number=pr_number)
        if row["verdict"] != NO_ELIGIBLE_EVIDENCE
    ]
    if not verdicts:
        return NO_ELIGIBLE_EVIDENCE
    return max(verdicts, key=lambda verdict: VERDICT_SEVERITY[verdict])
