"""The staged session<->run binding: item 2's durable record, phase by phase.

Gate item 2 (issue ``#18``) injects kills around a commit-before-spawn ordering
(D-0024): the ``--session-id`` UUID is chosen and committed durably *before*
``claude -p`` exists, the spawn is recorded, and the identity the provider
actually assigned is read back and committed -- exit 0 is never the evidence
(D-0027). This module owns the SQLite half of that walk and nothing else:

    prepared            the identity is committed; no spawn attempted yet
    spawned             the provider was asked to start; identity unconfirmed
    identity_confirmed  the provider's own read-back named the committed id,
                        and the read-back is itself committed

Every transition is a fenced write (``ACCEPTANCE.md`` section 2): it carries
the lease epoch, validated atomically as part of the write, and a stale
writer's transition is refused and recorded rather than merged. The ``session``
table has no ``writer_epoch`` column -- its partial unique index
(``session_one_active_binding_per_run``) is what makes "at most one active
binding per run" the database's rule -- so the fence decides *whether* the row
changes and the ``action`` rows record who wrote, exactly as the S3
substitution harness already does for its one-step bind.

What this module deliberately is not:

- It is not the orchestration. Lease acquisition, spawning, read-back and
  crash recovery compose in ``claude_org_runtime.supervisor``; this module
  never imports the session package (D-0009 -- the binding is control-plane
  state, and the provider must stay swappable under it).
- It is not an exclusion of its own. "At most one active binding" is the
  index's; "this writer may write" is the lease's. Nothing here consults
  liveness before writing -- expiry discovery alone is insufficient, per S6.

Spike status: built against the S5 spike schema, throwaway by default
(D-0026); the durable half is the tests. Q-0001 stays open.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .lease import (
    Lease,
    ProtectedWrite,
    and_,
    effect_kind,
    eq,
    fenced_insert,
    fenced_update,
    is_null,
    param,
    protected_write,
    value,
)

__all__ = [
    "PHASE_PREPARED",
    "PHASE_SPAWNED",
    "PHASE_IDENTITY_CONFIRMED",
    "REASON_PREPARED",
    "REASON_SPAWNED",
    "SessionBinding",
    "prepare_binding",
    "mark_spawned",
    "confirm_identity",
    "release_binding",
    "active_binding",
    "binding_for_session",
]

PHASE_PREPARED = "prepared"
PHASE_SPAWNED = "spawned"
PHASE_IDENTITY_CONFIRMED = "identity_confirmed"

#: The honest pre-spawn observation reasons. The schema requires an
#: ``observation_reason`` for every unobserved row (R4: "could not observe" is
#: never stored empty), and before the read-back these are the truthful words.
REASON_PREPARED = "binding committed; spawn not yet attempted"
REASON_SPAWNED = "spawn requested; identity not yet read back"

_SELECT = (
    "SELECT session_id, run_id, provider, binding_phase, observation,"
    " provider_state, observation_reason, bound_at_ms, released_at_ms"
    " FROM session "
)


@dataclass(frozen=True)
class SessionBinding:
    """One ``session`` row, read back as recovery reads it (D-0001)."""

    session_id: str
    run_id: str
    provider: str
    binding_phase: str
    observation: str
    provider_state: Optional[str]
    observation_reason: Optional[str]
    bound_at_ms: int
    released_at_ms: Optional[int]


def _binding(row: sqlite3.Row | tuple) -> SessionBinding:
    (session_id, run_id, provider, binding_phase, observation, provider_state,
     observation_reason, bound_at_ms, released_at_ms) = tuple(row)
    return SessionBinding(
        session_id=str(session_id),
        run_id=str(run_id),
        provider=str(provider),
        binding_phase=str(binding_phase),
        observation=str(observation),
        provider_state=None if provider_state is None else str(provider_state),
        observation_reason=(
            None if observation_reason is None else str(observation_reason)
        ),
        bound_at_ms=int(bound_at_ms),
        released_at_ms=None if released_at_ms is None else int(released_at_ms),
    )


def prepare_binding(
    connection: sqlite3.Connection,
    lease: Lease,
    *,
    session_id: str,
    run_id: str,
    provider: str,
    now_ms: int,
    attempt_id: str | None = None,
) -> int:
    """Commit the session<->run binding *before* the process exists.

    This is the spawn-admission write: the orchestration layer spawns only
    after this commit succeeds under a live token, so a claimant whose lease
    was taken over is refused here -- durably, as an ``action`` row -- and
    never becomes a process. A second active binding for the run is refused by
    ``session_one_active_binding_per_run`` regardless of who asks.

    :raises StaleWriterRefused: the token was not live; refusal recorded.
    :raises sqlite3.IntegrityError: the run already has an active binding.
    """

    statement = fenced_insert(
        "session",
        values={
            "session_id": param("session_id"),
            "run_id": param("run_id"),
            "provider": param("provider"),
            "binding_phase": value(PHASE_PREPARED),
            "observation": value("unobserved"),
            "provider_state": value(None),
            "observation_reason": value(REASON_PREPARED),
            "bound_at_ms": param("now_ms"),
        },
        stamps_writer_epoch=False,
    )
    write = ProtectedWrite(
        kind=effect_kind(lease.resource, "prepare_binding"),
        idempotency_key=f"prepare_binding:{session_id}",
        statement=statement,
        # The admission and its record are the same row in the same
        # transaction: the one case where this mechanism is the truthful
        # answer. The *spawn* is a separate, later side effect -- its
        # exactly-once story belongs to the orchestration layer and is
        # documented there, not claimed here.
        exactly_once_mechanism="transactional_with_record",
        params={
            "session_id": session_id,
            "run_id": run_id,
            "provider": provider,
            "now_ms": now_ms,
        },
        run_id=run_id,
    )
    return protected_write(
        connection, lease, write, now_ms=now_ms, attempt_id=attempt_id
    )


def mark_spawned(
    connection: sqlite3.Connection,
    lease: Lease,
    *,
    session_id: str,
    run_id: str,
    now_ms: int,
    attempt_id: str | None = None,
) -> int:
    """Record that the provider was asked to start the process.

    Matches only a ``prepared``, still-active binding: a kill between the
    admission commit and the spawn leaves the row honestly ``prepared``, and
    recovery reads that as "the spawn may or may not have been attempted"
    rather than trusting this mark to exist.

    :raises StaleWriterRefused: the token was not live; refusal recorded.
    :raises ProtectedWriteMissed: no active ``prepared`` binding to mark.
    """

    statement = fenced_update(
        "session",
        set={
            "binding_phase": value(PHASE_SPAWNED),
            "observation_reason": value(REASON_SPAWNED),
        },
        where=and_(
            eq("session_id", param("session_id")),
            eq("run_id", param("run_id")),
            eq("binding_phase", value(PHASE_PREPARED)),
            is_null("released_at_ms"),
        ),
        stamps_writer_epoch=False,
    )
    write = ProtectedWrite(
        kind=effect_kind(lease.resource, "mark_spawned"),
        idempotency_key=f"mark_spawned:{session_id}",
        statement=statement,
        exactly_once_mechanism="transactional_with_record",
        params={"session_id": session_id, "run_id": run_id},
        run_id=run_id,
    )
    return protected_write(
        connection, lease, write, now_ms=now_ms, attempt_id=attempt_id
    )


def confirm_identity(
    connection: sqlite3.Connection,
    lease: Lease,
    *,
    session_id: str,
    run_id: str,
    provider_state: str,
    now_ms: int,
    attempt_id: str | None = None,
) -> int:
    """Commit the provider's own read-back of the identity.

    "After the read-back" -- the fourth injection point -- means after *this*
    write commits, not after the provider's answer was merely seen in memory.
    The caller passes the provider's uninterpreted state word; what it must
    have already verified is that the read-back named the committed identity
    (D-0027: never treat exit 0, or the binding's existence, as acceptance).

    :raises StaleWriterRefused: the token was not live; refusal recorded.
    :raises ProtectedWriteMissed: no active ``spawned`` binding to confirm.
    """

    statement = fenced_update(
        "session",
        set={
            "binding_phase": value(PHASE_IDENTITY_CONFIRMED),
            "observation": value("observed"),
            "provider_state": param("provider_state"),
            "observation_reason": value(None),
        },
        where=and_(
            eq("session_id", param("session_id")),
            eq("run_id", param("run_id")),
            eq("binding_phase", value(PHASE_SPAWNED)),
            is_null("released_at_ms"),
        ),
        stamps_writer_epoch=False,
    )
    write = ProtectedWrite(
        kind=effect_kind(lease.resource, "confirm_identity"),
        idempotency_key=f"confirm_identity:{session_id}",
        statement=statement,
        exactly_once_mechanism="transactional_with_record",
        params={
            "session_id": session_id,
            "run_id": run_id,
            "provider_state": provider_state,
        },
        run_id=run_id,
    )
    return protected_write(
        connection, lease, write, now_ms=now_ms, attempt_id=attempt_id
    )


def release_binding(
    connection: sqlite3.Connection,
    lease: Lease,
    *,
    session_id: str,
    run_id: str,
    now_ms: int,
    attempt_id: str | None = None,
) -> int:
    """Release the binding, freeing the run for its next session.

    :raises StaleWriterRefused: the token was not live; refusal recorded.
    :raises ProtectedWriteMissed: no active binding to release.
    """

    statement = fenced_update(
        "session",
        set={"released_at_ms": param("now_ms")},
        where=and_(
            eq("session_id", param("session_id")),
            eq("run_id", param("run_id")),
            is_null("released_at_ms"),
        ),
        stamps_writer_epoch=False,
    )
    write = ProtectedWrite(
        kind=effect_kind(lease.resource, "release_binding"),
        idempotency_key=f"release_binding:{session_id}",
        statement=statement,
        exactly_once_mechanism="transactional_with_record",
        params={"session_id": session_id, "run_id": run_id, "now_ms": now_ms},
        run_id=run_id,
    )
    return protected_write(
        connection, lease, write, now_ms=now_ms, attempt_id=attempt_id
    )


def active_binding(
    connection: sqlite3.Connection, run_id: str
) -> Optional[SessionBinding]:
    """The run's single active binding, or ``None`` -- recovery's first read.

    A pure read (D-0001: state is reconstructed by query). At most one row can
    exist by the partial unique index; this function asserts that invariant
    rather than silently taking the first of several.
    """

    rows = connection.execute(
        _SELECT + "WHERE run_id = :run_id AND released_at_ms IS NULL",
        {"run_id": run_id},
    ).fetchall()
    if len(rows) > 1:  # pragma: no cover - the index makes this unreachable
        raise AssertionError(
            f"run {run_id!r} has {len(rows)} active bindings; "
            "session_one_active_binding_per_run should have refused the second"
        )
    return _binding(rows[0]) if rows else None


def binding_for_session(
    connection: sqlite3.Connection, session_id: str
) -> Optional[SessionBinding]:
    """The binding row for one session id, released or not. A pure read."""

    row = connection.execute(
        _SELECT + "WHERE session_id = :session_id", {"session_id": session_id}
    ).fetchone()
    return _binding(row) if row else None
