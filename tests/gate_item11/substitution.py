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
from typing import Any, Mapping

from claude_org_runtime.control_plane.lease import (
    Lease,
    ProtectedWrite,
    effect_kind,
    fenced_insert,
    protected_write,
)
from claude_org_runtime.session.provider import (
    Failure,
    Observation,
    Ok,
    ProviderResult,
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
        columns=(
            "session_id",
            "run_id",
            "provider",
            "observation",
            "provider_state",
            "observation_reason",
            "bound_at_ms",
        ),
        values=(
            ":session_id",
            ":run_id",
            ":provider",
            ":observation",
            ":provider_state",
            ":observation_reason",
            ":bound_at_ms",
        ),
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
