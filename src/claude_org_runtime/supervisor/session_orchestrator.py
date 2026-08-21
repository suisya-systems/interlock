"""Lease-before-spawn orchestration across the crash window (gate item 2).

This module is the Interlock-mediated path issue ``#18`` is graded on. Under
C2 the provider supplies **no exclusion**: the ``--session-id`` refusal has a
measured admission window in which two writers both exited 0 and both wrote
(U27), and ``--resume`` excludes nothing at all (U32). The only exclusion in
the system is Interlock's own fencing token, validated atomically as part of
each protected write (``ACCEPTANCE.md`` section 2, D-0027 part 3) -- and this
module is where that token is put *in front of* the process, so the shapes the
provider is known to admit never reach a spawn:

**The spawn-admission critical section.** A claimant may execute the provider
verb that creates or resumes a process only between two fenced writes:

1. the **admission write** -- for a fresh spawn, ``prepare_binding`` followed
   by ``mark_spawned`` (both fenced; the binding row is the durable record
   the write-ahead leaves); for a recovery, the ``post_spawn_gate`` write on
   the ``run`` row. A claimant whose token is stale is refused *here*,
   durably, and never becomes a process. This is what makes "the losing
   claimant is never spawned" hold for F3's crash window: the retry that
   lands inside the provider's admission window acquires the lease (raising
   the epoch) before it spawns, and the dead claimant's token can no longer
   admit anything.
2. the **post-spawn validation** -- the same ``post_spawn_gate`` fenced write
   issued immediately after the provider verb returns. A claimant that lost
   its lease *inside* the critical section (SIGSTOP across an expiry, a
   takeover between its admission commit and its ``exec``) is detected at
   this write: the refusal is recorded, and the just-created process is
   terminated at once, the latency measured and reported
   (:class:`LoserTerminated`).

The window between the admission commit and the ``exec`` cannot be closed from
here -- a process creation is an external side effect, and SQLite cannot make
it transactional with a row (``ACCEPTANCE.md`` section 2). What this module
guarantees is the pair of fenced writes around it and the immediate, measured
termination on the losing side; the residual is stated in the gate record
rather than assumed away.

**Recovery protocol** (:meth:`SessionOrchestrator.recover`). A restarted
supervisor first takes the lease (raising the epoch -- from that instant the
previous claimant's token matches nothing), then reads the run's binding row
back from SQLite (D-0001) and resolves the *existing* process before any verb
that could create a second one:

- no active binding: nothing was admitted before the crash; a fresh admission
  walk runs.
- ``prepared``: the write-ahead ``mark_spawned`` never committed, so no spawn
  was attempted; the walk continues from the mark.
- ``spawned`` / ``identity_confirmed``: the spawn may have happened. If the
  provider does not know the session, it did not (the provider commits its
  own record before creating a process); the walk re-runs the spawn under the
  same committed identity. If the provider knows it, recovery goes through
  ``resume`` -- never a fresh ``--session-id`` claim (U28: the claim is still
  held by the dead session) -- and the provider's resume resolves a surviving
  process first (adopt without spawning) so a second live process on one
  session id is never created through this path (U32 is the provider refusing
  nothing; the mediation is that only the lease holder gets to call this at
  all, and only after the gate write).

Identity is never assumed: a binding is confirmed only after the provider's
own read-back names the committed identity (D-0027: neither exit 0 nor the
binding's existence is acceptance), and "after the read-back" means after
``confirm_identity`` committed that read-back to SQLite.

D-0009: this module lives in the supervisor join layer. It imports the S1
contract (not the C2 implementation) and the control plane; ``session/`` is
unchanged by it and ``control_plane`` still imports no session backend.

Spike status: throwaway by default (D-0026); the durable half is the tests.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from claude_org_runtime.control_plane import lease as lease_module
from claude_org_runtime.control_plane import session_binding
from claude_org_runtime.control_plane.lease import (
    Lease,
    ProtectedWrite,
    StaleWriterRefused,
    effect_kind,
    eq,
    fenced_update,
    param,
    protected_write,
)
from claude_org_runtime.control_plane.session_binding import (
    PHASE_IDENTITY_CONFIRMED,
    PHASE_PREPARED,
    PHASE_SPAWNED,
    SessionBinding,
)
from claude_org_runtime.session.provider import (
    Failure,
    FailureKind,
    Observation,
    Ok,
    SessionProvider,
    SessionReadout,
    StartRequest,
)

__all__ = [
    "IdentityUnconfirmed",
    "LoserTerminated",
    "OrchestrationOutcome",
    "OrchestrationRefused",
    "ProviderStartFailed",
    "SEAM_AFTER_ADMISSION_BEFORE_SPAWN",
    "SEAM_AFTER_READBACK_COMMIT",
    "SEAM_AFTER_SPAWN_BEFORE_READBACK_COMMIT",
    "SEAM_BEFORE_ADMISSION_COMMIT",
    "SEAMS",
    "SessionOrchestrator",
    "default_identity_confirmation",
]

#: The injection seams of the commit-before-spawn walk, named for the fault
#: harness (issue #18's four points). A ``seam`` callback passed at
#: construction is invoked with each name as the walk crosses it; the fault
#: driver maps them onto its barrier anchors, and production wiring passes
#: nothing. A seam is a place to *stop*, never a place to decide -- nothing in
#: this module reads anything back from the callback.
SEAM_BEFORE_ADMISSION_COMMIT = "before-admission-commit"
SEAM_AFTER_ADMISSION_BEFORE_SPAWN = "after-admission-before-spawn"
SEAM_AFTER_SPAWN_BEFORE_READBACK_COMMIT = "after-spawn-before-readback-commit"
SEAM_AFTER_READBACK_COMMIT = "after-readback-commit"
SEAMS = (
    SEAM_BEFORE_ADMISSION_COMMIT,
    SEAM_AFTER_ADMISSION_BEFORE_SPAWN,
    SEAM_AFTER_SPAWN_BEFORE_READBACK_COMMIT,
    SEAM_AFTER_READBACK_COMMIT,
)


#: Sentinel distinguishing "not given" (paced default) from an explicit None.
_DEFAULT_WAIT = object()


class OrchestrationRefused(RuntimeError):
    """Base for this module's own refusals.

    Lease-layer refusals (``LeaseHeld``, ``StaleWriterRefused``, ...) are
    raised as themselves wherever they already say everything; these types
    exist for the decisions that are this layer's own.
    """


class ProviderStartFailed(OrchestrationRefused):
    """The provider verb returned a ``Failure``; nothing was admitted twice."""

    def __init__(self, message: str, failure: Failure) -> None:
        super().__init__(message)
        self.failure = failure


class IdentityUnconfirmed(OrchestrationRefused):
    """The committed identity did not read back within the allowed attempts.

    The binding stays honestly at ``spawned`` -- never confirmed on trust --
    and the last thing the provider said rides along for the record.
    """

    def __init__(self, message: str, last_answer: object) -> None:
        super().__init__(message)
        self.last_answer = last_answer


class LoserTerminated(OrchestrationRefused):
    """A claimant lost its lease inside the spawn-admission critical section.

    A fenced write was refused (``StaleWriterRefused``), so the process this
    claimant had created was ordered stopped immediately. The refusal is
    already durable (an ``action`` row, written by the lease module); this
    exception carries the stop verdict and the measured latency, and it never
    overstates them: ``stop_confirmed`` is the provider's own answer, and a
    stop the provider could not confirm (S1's ``stop`` contract: acceptance is
    not evidence the session stopped) is surfaced as exactly that rather than
    reported as a termination that happened.
    """

    def __init__(
        self,
        message: str,
        *,
        session_id: str,
        refusal: StaleWriterRefused,
        detected_at_ms: int,
        terminated_at_ms: int,
        stop_answer: object,
        stop_confirmed: bool,
        stop_attempted: bool = True,
    ) -> None:
        super().__init__(message)
        self.session_id = session_id
        self.refusal = refusal
        self.detected_at_ms = detected_at_ms
        self.terminated_at_ms = terminated_at_ms
        self.stop_answer = stop_answer
        self.stop_confirmed = stop_confirmed
        #: False when the loser deliberately did not fire: the run's binding
        #: was already confirmed by the takeover writer, so a session-level
        #: stop could have killed the *winner's* adopted worker. The loser's
        #: possibly-rogue process is then an unresolved hazard this exception
        #: surfaces, never a termination that is claimed.
        self.stop_attempted = stop_attempted

    @property
    def termination_latency_ms(self) -> int:
        """Detection to the provider's stop answer -- not a claim beyond it."""

        return self.terminated_at_ms - self.detected_at_ms


@dataclass(frozen=True)
class OrchestrationOutcome:
    """What one mediated start/recovery actually did, read back durably."""

    session_id: str
    #: ``started`` (fresh admission), ``respawned`` (recovery re-ran a spawn
    #: that never happened), ``resumed`` (recovery went through the provider's
    #: resume -- which itself adopts a surviving process rather than spawning).
    path: str
    binding: SessionBinding
    readout: SessionReadout


def default_identity_confirmation(readout: SessionReadout) -> bool:
    """Is this readout a *positive* identity read-back?

    Conservative on purpose. The C2 provider withholds ``OBSERVED`` until an
    event named the committed identity -- with one exception: a child that
    exited without emitting anything is reported as its process disposition
    (``exited-N``), which observed an exit, not an identity. Confirming on
    that word would put "the process died" into SQLite as "the identity read
    back", so it is excluded here. A provider whose vocabulary differs can be
    given a different policy at construction; withholding confirmation is the
    safe direction either way (the binding simply stays ``spawned``).
    """

    if readout.observation is not Observation.OBSERVED:
        return False
    state = readout.provider_state or ""
    return not state.startswith("exited-")


class SessionOrchestrator:
    """One run's lease-before-spawn walk, injectable end to end.

    Time is the caller's (``now_ms``), identity is the caller's
    (``session_uuid_factory`` -- the generated UUID itself is passed to the
    provider as ``StartRequest.session_id``, so the provider-neutral identity
    and the C2 ``--session-id`` value are one string and no C2 derivation
    leaks into this layer), and waiting is the caller's (``wait``).
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        provider: SessionProvider,
        *,
        run_id: str,
        holder: str,
        workspace: str,
        role: str,
        now_ms: Callable[[], int],
        session_uuid_factory: Callable[[], str],
        settings: Optional[Mapping[str, Any]] = None,
        provider_name: str = "claude-cli",
        ttl_ms: int = 30_000,
        resource: Optional[str] = None,
        identity_confirmed: Callable[[SessionReadout], bool] = (
            default_identity_confirmation
        ),
        readback_attempts: int = 50,
        wait: object = _DEFAULT_WAIT,
        attempt_id_factory: Optional[Callable[[], str]] = None,
        seam: Optional[Callable[[str], None]] = None,
    ) -> None:
        if readback_attempts < 1:
            raise ValueError("readback_attempts must be at least 1")
        self._connection = connection
        self._provider = provider
        self._run_id = run_id
        self._holder = holder
        self._workspace = workspace
        self._role = role
        self._now_ms = now_ms
        self._uuid_factory = session_uuid_factory
        self._settings = dict(settings or {})
        self._provider_name = provider_name
        self._ttl_ms = ttl_ms
        self._resource = resource if resource is not None else f"session-run:{run_id}"
        self._identity_confirmed = identity_confirmed
        self._readback_attempts = readback_attempts
        if wait is _DEFAULT_WAIT:
            # A real provider answers start() the instant Popen returns, long
            # before the child has emitted its identity; back-to-back polls
            # would exhaust every attempt against a healthy child. The default
            # is therefore paced -- the pace is IO pacing against a live
            # subprocess, never a timestamp and never a measured admission
            # figure (U34). Pass wait=None for a deterministic in-memory
            # provider, or your own callable for a different policy.
            self._wait: Optional[Callable[[], None]] = lambda: time.sleep(0.05)
        else:
            self._wait = wait  # type: ignore[assignment]
        self._attempt_id_factory = attempt_id_factory
        self._seam = seam
        self._gate_sequence = 0

    def _cross(self, seam_name: str) -> None:
        if self._seam is not None:
            self._seam(seam_name)

    # -- the fenced writes ---------------------------------------------------

    def _attempt_id(self) -> str | None:
        return self._attempt_id_factory() if self._attempt_id_factory else None

    def _acquire(self) -> Lease:
        return lease_module.acquire(
            self._connection,
            resource=self._resource,
            holder=self._holder,
            now_ms=self._now_ms(),
            ttl_ms=self._ttl_ms,
        )

    def _post_spawn_gate(self, lease: Lease, *, moment: str) -> None:
        """The fenced authority validation around the provider verb.

        Touches the ``run`` row (``updated_at_ms``): a real write, so the
        fence is evaluated atomically as part of it and a refusal lands as a
        durable ``action`` row -- never a read-then-decide (S6's rule that
        expiry discovery alone is insufficient).
        """

        self._gate_sequence += 1
        now = self._now_ms()
        statement = fenced_update(
            "run",
            set={"updated_at_ms": param("now_ms")},
            where=eq("run_id", param("run_id")),
            stamps_writer_epoch=False,
        )
        write = ProtectedWrite(
            kind=effect_kind(lease.resource, "post_spawn_gate"),
            idempotency_key=(
                f"post_spawn_gate:{self._run_id}:{moment}:{now}:{self._gate_sequence}"
            ),
            statement=statement,
            exactly_once_mechanism="transactional_with_record",
            params={"run_id": self._run_id, "now_ms": now},
            run_id=self._run_id,
        )
        protected_write(
            self._connection, lease, write, now_ms=now, attempt_id=self._attempt_id()
        )

    def _refuse_and_terminate(
        self, refusal: StaleWriterRefused, session_id: str
    ) -> "LoserTerminated":
        detected = self._now_ms()
        # A session-level stop cannot name a process generation, so firing it
        # blind could kill the *winner's* worker: a takeover writer that has
        # already completed its walk (the run's binding is confirmed) may have
        # adopted the very child this loser spawned. The loser therefore stops
        # only while no takeover writer has confirmed the binding; once one
        # has, the loser stands down and surfaces its possibly-rogue process
        # as an unresolved hazard instead -- coordinated with the holder,
        # never a blind kill and never a silent trust.
        #
        # The check-and-stop is serialised against the winner's confirm, not a
        # read-then-stop: the database write lock is held from before the read
        # until after the stop, so a winner cannot move the binding to
        # confirmed in between (its own confirm blocks on the same lock,
        # within SQLite's busy timeout -- the provider's stop_timeout must
        # stay under it, which the C2 default does). This is a coordination
        # lock, not a protected write; nothing is committed under it.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            binding = session_binding.binding_for_session(
                self._connection, session_id
            )
            winner_confirmed = (
                binding is not None
                and binding.released_at_ms is None
                and binding.binding_phase == PHASE_IDENTITY_CONFIRMED
            )
            if winner_confirmed:
                terminated = self._now_ms()
                return LoserTerminated(
                    f"claimant {self._holder!r} lost the lease on "
                    f"{self._resource!r} inside the spawn-admission critical "
                    f"section; the takeover writer has already confirmed the "
                    f"binding for session {session_id!r}, so no session-level "
                    "stop was fired (it could kill the winner's adopted "
                    "worker). Any process this claimant created is an "
                    "UNRESOLVED hazard the holder must reconcile",
                    session_id=session_id,
                    refusal=refusal,
                    detected_at_ms=detected,
                    terminated_at_ms=terminated,
                    stop_answer=None,
                    stop_confirmed=False,
                    stop_attempted=False,
                )
            stop_answer = self._provider.stop(session_id)
        finally:
            self._connection.rollback()
        terminated = self._now_ms()
        # The provider's own verdict, never assumed: an Ok is the post-stop
        # readout of a session the provider reports stopped; a Failure means
        # the child may still be live, and saying otherwise here would put a
        # fabricated termination into the very record the residual is read
        # out of.
        stop_confirmed = isinstance(stop_answer, Ok)
        outcome = (
            f"was terminated ({terminated - detected} ms after detection)"
            if stop_confirmed
            else (
                f"was ordered stopped but the stop is NOT confirmed "
                f"({terminated - detected} ms after detection): {stop_answer!r}"
            )
        )
        return LoserTerminated(
            f"claimant {self._holder!r} lost the lease on "
            f"{self._resource!r} inside the spawn-admission critical section; "
            f"the process for session {session_id!r} {outcome}",
            session_id=session_id,
            refusal=refusal,
            detected_at_ms=detected,
            terminated_at_ms=terminated,
            stop_answer=stop_answer,
            stop_confirmed=stop_confirmed,
        )

    def _validate_after_spawn(self, lease: Lease, session_id: str, *, moment: str) -> None:
        """Post-spawn half of the critical section: refuse-and-terminate."""

        try:
            self._post_spawn_gate(lease, moment=moment)
        except StaleWriterRefused as refusal:
            raise self._refuse_and_terminate(refusal, session_id) from refusal

    def _commit_readback(
        self, lease: Lease, session_id: str, readout: SessionReadout
    ) -> None:
        """The walk's final step is always a fenced write, whatever the phase.

        The naive shape here -- read the phase, skip the commit when it is
        already ``identity_confirmed`` -- is an unfenced read-then-decide, and
        it is wrong in exactly the case that matters: the one writer that
        finds the phase already confirmed *without having confirmed it* is a
        stale claimant whose binding was moved by the takeover. So when the
        confirm itself has nothing left to write, the walk still ends in a
        fenced gate write: a live holder passes, and a stale one is refused,
        recorded, and its process terminated -- never returned as success.
        """

        current = session_binding.binding_for_session(self._connection, session_id)
        try:
            if current is not None and current.binding_phase == PHASE_SPAWNED:
                session_binding.confirm_identity(
                    self._connection,
                    lease,
                    session_id=session_id,
                    run_id=self._run_id,
                    provider_state=readout.provider_state or "",
                    now_ms=self._now_ms(),
                    attempt_id=self._attempt_id(),
                )
            else:
                self._post_spawn_gate(lease, moment="readback-final")
        except StaleWriterRefused as refusal:
            raise self._refuse_and_terminate(refusal, session_id) from refusal
        self._cross(SEAM_AFTER_READBACK_COMMIT)

    # -- provider answers ----------------------------------------------------

    def _unwrap(self, verb: str, answer: object) -> SessionReadout:
        if isinstance(answer, Ok):
            return answer.value
        if isinstance(answer, Failure):
            raise ProviderStartFailed(
                f"provider {verb} failed: {answer.kind.value}: {answer.detail}",
                answer,
            )
        raise ProviderStartFailed(  # pragma: no cover - contract violation
            f"provider {verb} returned neither Ok nor Failure: {answer!r}",
            Failure(
                kind=FailureKind.UNINTERPRETABLE_RESPONSE,
                detail=f"unexpected {verb} answer {answer!r}",
            ),
        )

    def _await_identity(self, lease: Lease, session_id: str) -> SessionReadout:
        """Poll ``read_state`` until the committed identity reads back.

        Never confirms on trust: exhausting the attempts raises, the binding
        stays ``spawned``, and the last answer rides on the exception. The
        exhaustion path still ends in a fenced write first -- a claimant whose
        lease was taken over during a fruitless poll must leave as a refused
        stale writer (with its child handled), not as a quiet timeout.
        """

        last_answer: object = None
        for attempt in range(self._readback_attempts):
            answer = self._provider.read_state(session_id)
            last_answer = answer
            if (
                isinstance(answer, Ok)
                # The read-back must positively name the committed identity
                # (D-0027): a readout about some other id -- however healthy
                # -- confirms nothing about this binding.
                and answer.value.session_id == session_id
                and self._identity_confirmed(answer.value)
            ):
                return answer.value
            if attempt + 1 < self._readback_attempts and self._wait is not None:
                self._wait()
        try:
            self._post_spawn_gate(lease, moment="readback-exhausted")
        except StaleWriterRefused as refusal:
            raise self._refuse_and_terminate(refusal, session_id) from refusal
        raise IdentityUnconfirmed(
            f"the identity committed for session {session_id!r} did not read "
            f"back within {self._readback_attempts} attempts; the binding is "
            "left at 'spawned' rather than confirmed on trust",
            last_answer,
        )

    # -- the walks -----------------------------------------------------------

    def start(self) -> OrchestrationOutcome:
        """Fresh admission: lease, commit, spawn, read back, confirm.

        :raises LeaseHeld: another claimant's lease is live; nothing written,
            nothing spawned.
        :raises StaleWriterRefused: the token went stale before the spawn; the
            refusal is durable and no process was created.
        :raises LoserTerminated: the token went stale inside the critical
            section; the just-created process was terminated, measured.
        """

        lease = self._acquire()
        session_id = self._uuid_factory()
        self._cross(SEAM_BEFORE_ADMISSION_COMMIT)
        # The fence's now_ms is captured *after* the seam: the seam is an
        # arbitrary external delay, and a timestamp taken before it would let
        # a claimant stopped across its own expiry pass the fence's liveness
        # test with a stale clock.
        now = self._now_ms()
        session_binding.prepare_binding(
            self._connection,
            lease,
            session_id=session_id,
            run_id=self._run_id,
            provider=self._provider_name,
            now_ms=now,
            attempt_id=self._attempt_id(),
        )
        return self._spawn_and_confirm(lease, session_id, path="started", marked=False)

    def _spawn_and_confirm(
        self, lease: Lease, session_id: str, *, path: str, marked: bool
    ) -> OrchestrationOutcome:
        if not marked:
            # The write-ahead mark: committed under the fence *before* the
            # provider verb, so 'prepared' durably means "no spawn attempted"
            # and a stale claimant is refused before it can create a process.
            session_binding.mark_spawned(
                self._connection,
                lease,
                session_id=session_id,
                run_id=self._run_id,
                now_ms=self._now_ms(),
                attempt_id=self._attempt_id(),
            )
        self._cross(SEAM_AFTER_ADMISSION_BEFORE_SPAWN)
        answer = self._provider.start(
            StartRequest(
                session_id=session_id,
                workspace=self._workspace,
                role=self._role,
                settings=self._settings,
            )
        )
        self._cross(SEAM_AFTER_SPAWN_BEFORE_READBACK_COMMIT)
        # The fenced validation runs before the provider's answer is even
        # interpreted: a Failure does not prove no process was created (the
        # C2 provider can fail the *readout* after a successful Popen), so a
        # claimant that lost its lease during the verb must be refused --
        # and its possible child handled -- whatever the verb said.
        self._validate_after_spawn(lease, session_id, moment="after-start")
        self._unwrap("start", answer)
        readout = self._await_identity(lease, session_id)
        self._commit_readback(lease, session_id, readout)
        return self._outcome(session_id, path, readout)

    def recover(self) -> OrchestrationOutcome:
        """Re-identify after a crash: exactly one session for the run.

        The lease is taken first (raising the epoch -- the previous claimant's
        token is dead from this instant), then the binding row decides the
        path; the provider's own record decides whether a spawn actually
        happened, and a surviving process is resolved before any verb that
        could create a second one.
        """

        lease = self._acquire()
        binding = session_binding.active_binding(self._connection, self._run_id)
        if binding is None:
            # Nothing was admitted before the crash (the kill landed before
            # the binding commit). This is a fresh admission, not an adoption:
            # any provider-side leftovers under other identities belong to
            # other runs or to no run, and are deliberately not adopted here
            # (no orphan is adopted into a run its binding does not name).
            session_id = self._uuid_factory()
            self._cross(SEAM_BEFORE_ADMISSION_COMMIT)
            session_binding.prepare_binding(
                self._connection,
                lease,
                session_id=session_id,
                run_id=self._run_id,
                provider=self._provider_name,
                now_ms=self._now_ms(),
                attempt_id=self._attempt_id(),
            )
            return self._spawn_and_confirm(
                lease, session_id, path="started", marked=False
            )

        session_id = binding.session_id
        if binding.binding_phase == PHASE_PREPARED:
            # The write-ahead mark never committed, so the provider verb was
            # never reached; continue the walk from the mark.
            return self._spawn_and_confirm(
                lease, session_id, path="respawned", marked=False
            )

        known = self._provider.read_state(session_id)
        if isinstance(known, Failure) and known.kind is FailureKind.UNKNOWN_SESSION:
            # The provider commits its own durable record before it creates a
            # process, so "unknown session" means the spawn never happened --
            # the mark is a write-ahead, not a receipt. Re-run the spawn under
            # the same committed identity (no fresh identity is minted: the
            # binding row is the identity).
            return self._spawn_and_confirm(
                lease, session_id, path="respawned", marked=True
            )

        # The provider knows the session: recovery goes through resume, never
        # a fresh --session-id claim (U28). The provider resolves a surviving
        # process first -- a live child is adopted, not respawned -- so no
        # second process is created on this id through the mediated path. The
        # gate write brackets the verb exactly as it brackets a spawn.
        self._post_spawn_gate(lease, moment="before-resume")
        answer = self._provider.resume(session_id)
        # Fence first, interpret second -- same reasoning as the start walk: a
        # resume Failure does not prove no process was created.
        self._validate_after_spawn(lease, session_id, moment="after-resume")
        self._unwrap("resume", answer)
        readout = self._await_identity(lease, session_id)
        self._commit_readback(lease, session_id, readout)
        return self._outcome(session_id, "resumed", readout)

    def _outcome(
        self, session_id: str, path: str, readout: SessionReadout
    ) -> OrchestrationOutcome:
        binding = session_binding.active_binding(self._connection, self._run_id)
        # "Exactly one" is the index's at-most-one plus this non-empty read:
        # recovery that ends without an active, confirmed binding for the very
        # session it drove did not re-identify anything.
        if binding is None or binding.session_id != session_id:
            raise AssertionError(
                f"run {self._run_id!r} ended its walk without an active binding "
                f"for session {session_id!r}: {binding!r}"
            )
        if binding.binding_phase != PHASE_IDENTITY_CONFIRMED:
            raise AssertionError(  # pragma: no cover - confirm precedes this
                f"binding for {session_id!r} left at {binding.binding_phase!r}"
            )
        return OrchestrationOutcome(
            session_id=session_id, path=path, binding=binding, readout=readout
        )
