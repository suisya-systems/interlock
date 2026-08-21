"""Fixtures for the mediated crash-window proof: a scripted S1 provider.

The provider here is deliberately *adversarial in the U27/U32 direction*: it
refuses nothing. A second start on a claimed id is admitted, a second resume is
admitted, and no call ever excludes another -- exactly the surface the real C2
provider measures out (the ``already in use`` refusal is not atomic inside the
admission window, and ``--resume`` excludes nothing at all). Every test in this
package therefore passes with the provider's own refusal assumed absent, which
is what the issue requires: the exclusion under test is Interlock's, not the
provider's.
"""

from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest

from claude_org_runtime.control_plane import lease as lease_module
from claude_org_runtime.control_plane import schema
from claude_org_runtime.session.provider import (
    CapabilityReport,
    Failure,
    FailureKind,
    Observation,
    Ok,
    ProviderResult,
    REQUIRED_CAPABILITIES,
    SessionProvider,
    SessionReadout,
    StartRequest,
)
from claude_org_runtime.supervisor import SessionOrchestrator

T0 = 1_000_000
TTL_MS = 30_000
RUN_ID = "run-1"


class Clock:
    """The caller's clock, advanced one millisecond per observation.

    The auto-advance keeps every fenced write's idempotency key distinct
    without any test importing a measured duration -- no U27/U34 figure is
    ever a constant here.
    """

    def __init__(self, start: int = T0) -> None:
        self.t = start

    def now_ms(self) -> int:
        self.t += 1
        return self.t

    def advance_past_expiry(self, ttl_ms: int = TTL_MS) -> None:
        self.t += ttl_ms + 1


def observed(session_id: str, state: str = "running") -> SessionReadout:
    return SessionReadout(
        session_id=session_id,
        observation=Observation.OBSERVED,
        provider_state=state,
    )


def unconfirmed(session_id: str, reason: str = "no event has named an identity yet") -> SessionReadout:
    return SessionReadout(
        session_id=session_id,
        observation=Observation.COULD_NOT_OBSERVE,
        could_not_observe_reason=reason,
    )


@dataclass
class _ScriptedSession:
    session_id: str
    #: Readouts served by successive ``read_state`` calls; the last one
    #: repeats. Empty means "confirm immediately as running".
    readouts: list[SessionReadout] = field(default_factory=list)
    live: bool = True


class ScriptedProvider(SessionProvider):
    """An S1 provider that records everything and refuses nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.sessions: dict[str, _ScriptedSession] = {}
        self.start_calls: list[StartRequest] = []
        self.resume_calls: list[str] = []
        self.stop_calls: list[str] = []
        #: Called (with the request) before a start is admitted; may return a
        #: ProviderResult to override, or None to proceed. This is the seam a
        #: test uses to advance the world *inside* the critical section.
        self.on_start: Optional[Callable[[StartRequest], Optional[ProviderResult]]] = None
        self.on_resume: Optional[Callable[[str], Optional[ProviderResult]]] = None
        #: Readouts to serve for the *next* started/resumed session.
        self.next_readouts: list[SessionReadout] = []

    # -- the capability probe ------------------------------------------------

    def probe_capabilities(self) -> ProviderResult[CapabilityReport]:
        return Ok(
            CapabilityReport(
                provider_version="scripted 1.0",
                supported=REQUIRED_CAPABILITIES,
                detail="in-memory scripted provider; refuses nothing (U27/U32)",
            )
        )

    # -- the five verbs ------------------------------------------------------

    def _start_session(self, request: StartRequest) -> ProviderResult[SessionReadout]:
        self.start_calls.append(request)
        if self.on_start is not None:
            override = self.on_start(request)
            if override is not None:
                return override
        # Deliberately no "already exists" refusal: U27's admission window
        # means the real provider admits this shape too.
        session = _ScriptedSession(
            session_id=request.session_id, readouts=list(self.next_readouts)
        )
        self.next_readouts = []
        self.sessions[request.session_id] = session
        return Ok(unconfirmed(request.session_id, "child spawned; nothing parseable yet"))

    def list_sessions(self):
        return Ok(tuple(self._readout(s) for s in self.sessions.values()))

    def read_state(self, session_id: str) -> ProviderResult[SessionReadout]:
        session = self.sessions.get(session_id)
        if session is None:
            return Failure(
                FailureKind.UNKNOWN_SESSION, f"no session {session_id!r} on record"
            )
        return Ok(self._readout(session))

    def stop(self, session_id: str) -> ProviderResult[SessionReadout]:
        self.stop_calls.append(session_id)
        session = self.sessions.get(session_id)
        if session is None:
            return Failure(
                FailureKind.UNKNOWN_SESSION, f"no session {session_id!r} on record"
            )
        session.live = False
        session.readouts = [observed(session_id, "exited-137")]
        return Ok(observed(session_id, "exited-137"))

    def resume(self, session_id: str) -> ProviderResult[SessionReadout]:
        self.resume_calls.append(session_id)
        if self.on_resume is not None:
            override = self.on_resume(session_id)
            if override is not None:
                return override
        session = self.sessions.get(session_id)
        if session is None:
            # Deliberately no exclusion and no existence check beyond the
            # record (U32: --resume refuses nothing) -- but a resume of a
            # session this provider has no record of at all cannot invent one.
            return Failure(
                FailureKind.UNKNOWN_SESSION, f"no session {session_id!r} on record"
            )
        session.live = True
        session.readouts = list(self.next_readouts) or session.readouts
        self.next_readouts = []
        return Ok(unconfirmed(session_id, "resumed; nothing parseable yet"))

    # -- scripting helpers ---------------------------------------------------

    def _readout(self, session: _ScriptedSession) -> SessionReadout:
        if session.readouts:
            head = session.readouts[0]
            if len(session.readouts) > 1:
                session.readouts.pop(0)
            return head
        return observed(session.session_id)

    def plant(self, session_id: str, *readouts: SessionReadout, live: bool = True) -> None:
        """A session the provider already knows (a prior life's record)."""

        self.sessions[session_id] = _ScriptedSession(
            session_id=session_id, readouts=list(readouts), live=live
        )


@pytest.fixture()
def cp(tmp_path):
    connection = schema.create_control_plane(tmp_path / "control-plane.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
        " VALUES (?, 'running', ?, ?)",
        (RUN_ID, T0, T0),
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def provider():
    return ScriptedProvider()


@pytest.fixture()
def uuids():
    counter = itertools.count(1)
    return lambda: f"00000000-0000-4000-8000-{next(counter):012d}"


@pytest.fixture()
def make_orchestrator(cp, clock, provider, uuids, tmp_path):
    def factory(holder: str = "sup-a", **overrides: Any) -> SessionOrchestrator:
        options: dict[str, Any] = dict(
            run_id=RUN_ID,
            holder=holder,
            workspace=str(tmp_path),
            role="worker",
            now_ms=clock.now_ms,
            session_uuid_factory=uuids,
            ttl_ms=TTL_MS,
            readback_attempts=3,
            wait=None,  # the scripted provider answers synchronously
            provider_name="scripted",
        )
        options.update(overrides)
        return SessionOrchestrator(cp, provider, **options)

    return factory


def take_over(cp, clock, holder: str = "sup-b", resource: str = f"session-run:{RUN_ID}"):
    """Another claimant takes the lease after expiry, raising the epoch."""

    clock.advance_past_expiry()
    return lease_module.acquire(
        cp, resource=resource, holder=holder, now_ms=clock.now_ms(), ttl_ms=TTL_MS
    )


def refusals(cp) -> list[sqlite3.Row]:
    rows = cp.execute(
        "SELECT action_id, kind, refusal_reason, writer_epoch FROM action"
        " WHERE status = 'refused' ORDER BY rowid"
    ).fetchall()
    return list(rows)


def active_rows(cp) -> list[sqlite3.Row]:
    return list(
        cp.execute(
            "SELECT session_id, binding_phase, observation FROM session"
            " WHERE released_at_ms IS NULL"
        ).fetchall()
    )
