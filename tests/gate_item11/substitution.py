"""The whole of what a provider swap costs the control plane: this file.

Item 11's claim is measured by *where the provider knowledge is*, not by an
assertion that there is none. Something has to turn a provider's own words into
the rows S5 keeps, and the question the gate asks is whether that translation
lives inside the control plane -- where a second provider would force an edit --
or outside it, in the fixture, where a second provider costs one entry in
:mod:`tests.gate_item11.registry`.

It lives here. This module is the **only** one in the repository that imports
both :mod:`claude_org_runtime.session` and
:mod:`claude_org_runtime.control_plane`, and
``test_no_provider_detail_leaks.py`` asserts that rather than trusting it.

Two translations are needed, and both are provider-neutral -- they are between
S1's vocabulary and S5's, not between any particular backend's and S5's:

``Observation`` to the ``session.observation`` word
    S1 spells R4's second case ``could-not-observe``; S5's CHECK spells it
    ``unobserved``. Neither is wrong and neither may guess: an adapter that let
    an unrecognised observation fall through to "observed" would put back
    exactly the collapse R4 records, in the one place nothing would see it.

``SessionReadout`` to a row of ``session``
    S5 splits the readout across ``provider_state`` and ``observation_reason``
    under a CHECK that refuses a row carrying both or neither. The split is
    performed from the readout's own case rather than from whichever field
    happens to be set.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from claude_org_runtime.control_plane.destination import KeyedDropbox
from claude_org_runtime.control_plane.handlers import NOTIFY_RECIPIENT, spike_registry
from claude_org_runtime.control_plane.lease import (
    Lease,
    ProtectedWrite,
    acquire,
    effect_kind,
    fenced_insert,
    param,
    protected_write,
)
from claude_org_runtime.control_plane.outbox import Outbox
from claude_org_runtime.control_plane.schema import create_control_plane, reconstruct
from claude_org_runtime.session.provider import (
    Failure,
    Observation,
    Ok,
    ProviderResult,
    SessionProvider,
    SessionReadout,
)

#: S1's observation cases, spelled as S5's DDL spells them. A closed mapping,
#: not a ``.value.replace()``: the two vocabularies are independent and a
#: derivation would make S5's CHECK follow S1's spelling silently.
OBSERVATION_WORD: Mapping[Observation, str] = {
    Observation.OBSERVED: "observed",
    Observation.COULD_NOT_OBSERVE: "unobserved",
}

#: The effect a session binding is recorded as in the write history. Composed
#: with :func:`effect_kind` at use, so the row says which lease allocated its
#: epoch (``Q-0001`` leaves ``action`` without a resource column).
BIND_EFFECT = "bind_session"


def unwrap(result: ProviderResult[Any], what: str) -> Any:
    """The value of an ``Ok``, or an ``AssertionError`` naming the failure.

    Written out rather than left to ``result.value``: a bare attribute access on
    a :class:`Failure` raises an ``AttributeError`` whose message says nothing
    about which verb failed or why, and R4's whole point is that a failure
    carries its reason all the way to whoever reads it.
    """

    if isinstance(result, Ok):
        return result.value
    assert isinstance(result, Failure), f"{what} returned {result!r}, neither Ok nor Failure"
    raise AssertionError(f"{what} failed: {result.kind.value}: {result.detail}")


def session_row(
    readout: SessionReadout, *, run_id: str, provider: str, bound_at_ms: int
) -> dict[str, Any]:
    """One ``session`` row, from one provider readout.

    ``provider`` is the registry handle of the backend that produced the
    readout, carried so that a database recovered after a swap says which
    provider bound each session rather than leaving it to be inferred.
    """

    word = OBSERVATION_WORD.get(readout.observation)
    if word is None:
        raise AssertionError(
            f"observation {readout.observation!r} has no S5 spelling; add one to "
            "OBSERVATION_WORD rather than letting it fall through -- an "
            "unrecognised observation written as 'observed' is R4 again"
        )
    return {
        "session_id": readout.session_id,
        "run_id": run_id,
        "provider": provider,
        # This harness binds a session it has already asked the provider about,
        # so the spawn has happened; whether the identity is *confirmed* is
        # exactly whether the readout observed anything (the schema ties
        # 'identity_confirmed' to 'observed' by CHECK). The staged pre-spawn
        # walk is the session-start orchestration's (issue #18), not S3's.
        "binding_phase": "identity_confirmed" if word == "observed" else "spawned",
        "observation": word,
        "provider_state": readout.provider_state,
        "observation_reason": readout.could_not_observe_reason,
        "bound_at_ms": bound_at_ms,
    }


def bind_session(
    connection: sqlite3.Connection,
    lease: Lease,
    readout: SessionReadout,
    *,
    run_id: str,
    provider: str,
    now_ms: int,
) -> int:
    """Persist the session<->run binding at spawn, under the fencing token.

    Fenced like every other control-plane write, and for the reason S6 gives:
    the binding is the row item 2's re-identification reads, so a holder that
    lost its lease may not write one. ``session`` carries no ``writer_epoch``
    column -- S5 gives it a partial unique index instead, which is what makes
    "exactly one active binding per run" the database's rule rather than the
    caller's -- so the stamp is turned off and the fence still decides whether
    the row appears at all.

    :raises sqlite3.IntegrityError: if the run already has an active binding.
        That refusal is the database's, and it is the one this harness is here
        to show survives a provider that offers no exclusion of its own (U27,
        U32).
    """

    row = session_row(readout, run_id=run_id, provider=provider, bound_at_ms=now_ms)
    statement = fenced_insert(
        "session",
        values={
            "session_id": param("session_id"),
            "run_id": param("run_id"),
            "provider": param("provider"),
            "binding_phase": param("binding_phase"),
            "observation": param("observation"),
            "provider_state": param("provider_state"),
            "observation_reason": param("observation_reason"),
            "bound_at_ms": param("bound_at_ms"),
        },
        stamps_writer_epoch=False,
    )
    write = ProtectedWrite(
        kind=effect_kind(lease.resource, BIND_EFFECT),
        idempotency_key=f"{BIND_EFFECT}:{row['session_id']}",
        statement=statement,
        # The effect *is* the row: it commits in the same transaction as its own
        # record, which is the one situation in which this mechanism is the
        # truthful answer rather than the convenient one.
        exactly_once_mechanism="transactional_with_record",
        params=row,
        run_id=run_id,
    )
    return protected_write(connection, lease, write, now_ms=now_ms)


def release_session(
    connection: sqlite3.Connection, session_id: str, *, released_at_ms: int
) -> None:
    """Mark a binding released, freeing the run for the next session.

    Unfenced on purpose, and only because nothing in this harness reads it back
    as evidence: it exists so a scenario can express stop-then-resume without
    the release becoming a second thing under test.
    """

    with connection:
        connection.execute(
            "UPDATE session SET released_at_ms = ? WHERE session_id = ?",
            (released_at_ms, session_id),
        )


# --------------------------------------------------------------------------
# One full round trip, used to qualify a provider before the suite runs
# --------------------------------------------------------------------------

#: The fixed instant the round trip is dated at. A constant rather than the wall
#: clock: nothing here measures duration, and a real clock would make the one
#: thing this must never be -- flaky -- possible.
DRIVE_T0 = 1_700_000_000_000
DRIVE_TTL_MS = 30_000
DRIVE_RUN_ID = "item11-drive-run"
DRIVE_RESOURCE = "item11-drive-resource"
DRIVE_HOLDER = "item11-drive-writer"


def drive_once(
    provider: SessionProvider,
    readout: SessionReadout,
    *,
    provider_id: str,
    root: Path,
) -> str:
    """Run the control plane end to end with *readout*'s session as its subject.

    This is what makes the bound run in ``test_suite_runs_unchanged.py`` a
    measurement rather than a coincidence. Without it the plugin would only
    prove that a provider can start a child *next to* the suite, and a provider
    the control plane could not use at all would produce the same green run.
    Here the provider's readout has to become a binding S5 accepts, under a
    fencing token, with an outbox delivery on top -- so a provider that cannot
    drive the control plane fails at ``pytest_configure`` and the suite never
    runs.

    Deliberately *not* a test. It is a precondition on the run, which is why it
    raises rather than asserting through pytest: a failure here must abort
    collection, not appear as one red case among the suite's own.

    :returns: a one-line summary for the run header, so the log says what was
        driven rather than only that something was.
    """

    connection = create_control_plane(root / "drive-control-plane.sqlite3")
    try:
        connection.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
            " VALUES (?, 'running', ?, ?)",
            (DRIVE_RUN_ID, DRIVE_T0, DRIVE_T0),
        )
        connection.commit()
        lease = acquire(
            connection,
            resource=DRIVE_RESOURCE,
            holder=DRIVE_HOLDER,
            now_ms=DRIVE_T0,
            ttl_ms=DRIVE_TTL_MS,
        )
        bind_session(
            connection,
            lease,
            readout,
            run_id=DRIVE_RUN_ID,
            provider=provider_id,
            now_ms=DRIVE_T0,
        )

        # The provider's list verb has to agree that the session it just bound
        # exists. A binding written from a readout the provider no longer knows
        # about would be a row about nothing.
        listed = {r.session_id for r in unwrap(provider.list_sessions(), "list_sessions")}
        if readout.session_id not in listed:
            raise AssertionError(
                f"{provider_id} bound session {readout.session_id!r} is not in its "
                f"own roster {sorted(listed)}"
            )

        dropbox = KeyedDropbox(root / "drive-destination", name="item11-drive-dropbox")
        outbox = Outbox(
            connection,
            resource=DRIVE_RESOURCE,
            holder=DRIVE_HOLDER,
            registry=spike_registry(dropbox),
        )
        outbox.enqueue(
            message_id="item11-drive-msg",
            recipient=NOTIFY_RECIPIENT,
            payload=f'{{"session":"{readout.session_id}"}}',
            dedup_key=f"item11-drive:{readout.session_id}",
            now_ms=DRIVE_T0,
            epoch=lease.epoch,
            run_id=DRIVE_RUN_ID,
        )
        attempt = outbox.attempt("item11-drive-msg", now_ms=DRIVE_T0 + 1, epoch=lease.epoch)
        if not outbox.record_ack("item11-drive-msg", now_ms=DRIVE_T0 + 2).recorded:
            raise AssertionError("the delivery was never acked")
        if dropbox.effect_count(attempt.idempotency_key) != 1:
            raise AssertionError(
                f"the destination applied "
                f"{dropbox.effect_count(attempt.idempotency_key)} effects, not one"
            )

        state = reconstruct(connection, now_ms=DRIVE_T0 + 3)
        bound = [row["session_id"] for row in state.active_sessions]
        if bound != [readout.session_id]:
            raise AssertionError(f"active sessions are {bound}, not [{readout.session_id!r}]")
        row = state.active_sessions[0]
        return (
            f"bound {row['session_id']} to {row['run_id']} as {row['observation']}"
            f"/{row['provider_state'] or row['observation_reason']} under epoch "
            f"{lease.epoch}, one effect delivered and acked"
        )
    finally:
        connection.close()
