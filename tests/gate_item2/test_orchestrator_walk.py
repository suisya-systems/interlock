"""The Interlock-mediated proof, layer by layer (issue #18).

Every case here runs a crash-and-retry shape *through* the control plane and
asserts the outcome the provider cannot supply: the losing claimant never
becomes a process, a second writer is refused durably, and re-identification
after a kill yields exactly one session for the run. The provider fixture
refuses nothing (see ``conftest``), so every pass here is a pass with the
provider's own refusal assumed absent -- it is defence in depth, not the
mechanism. No assertion reads an exit code; every one reads a durable row or
the provider's recorded call list.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.control_plane import session_binding
from claude_org_runtime.control_plane.lease import LeaseHeld, StaleWriterRefused, acquire
from claude_org_runtime.supervisor import (
    IdentityUnconfirmed,
    LoserTerminated,
    SessionOrchestrator,
)

from .conftest import RUN_ID, TTL_MS, active_rows, observed, refusals, take_over, unconfirmed

RESOURCE = f"session-run:{RUN_ID}"


# --------------------------------------------------------------------------
# the admission ordering itself
# --------------------------------------------------------------------------


def test_the_binding_is_committed_before_the_provider_is_asked_to_spawn(
    cp, provider, make_orchestrator
):
    """Commit-before-spawn, observed from inside the spawn (D-0024)."""

    seen: list[tuple[str, str]] = []

    def on_start(request):
        row = cp.execute(
            "SELECT binding_phase, observation FROM session WHERE session_id = ?"
            " AND released_at_ms IS NULL",
            (request.session_id,),
        ).fetchone()
        assert row is not None, "the spawn ran before the binding was committed"
        seen.append((row["binding_phase"], row["observation"]))
        return None

    provider.on_start = on_start
    outcome = make_orchestrator().start()

    # The write-ahead had already committed -- and honestly: the row said
    # 'spawned'/'unobserved', never claiming a read-back that had not happened.
    assert seen == [("spawned", "unobserved")]
    assert outcome.path == "started"
    assert outcome.binding.binding_phase == "identity_confirmed"
    assert outcome.binding.observation == "observed"


def test_the_readback_is_committed_not_assumed(cp, provider, make_orchestrator):
    """'After the read-back' means after the read-back's own commit."""

    outcome = make_orchestrator().start()
    row = cp.execute(
        "SELECT binding_phase, observation, provider_state FROM session"
        " WHERE session_id = ?",
        (outcome.session_id,),
    ).fetchone()
    assert (row["binding_phase"], row["observation"]) == ("identity_confirmed", "observed")
    assert row["provider_state"] == "running"


def test_an_identity_that_never_reads_back_is_never_confirmed(
    cp, provider, make_orchestrator
):
    """Exit codes and spawn success prove nothing (D-0027)."""

    provider.next_readouts = [unconfirmed("ignored")] * 10
    with pytest.raises(IdentityUnconfirmed):
        make_orchestrator().start()
    [(session_id, phase, observation)] = [tuple(r) for r in active_rows(cp)]
    assert phase == "spawned"
    assert observation == "unobserved"


# --------------------------------------------------------------------------
# the losing claimant never becomes a process
# --------------------------------------------------------------------------


def test_a_claimant_against_a_live_lease_never_reaches_the_provider(
    cp, provider, make_orchestrator
):
    make_orchestrator("sup-a").start()
    with pytest.raises(LeaseHeld):
        make_orchestrator("sup-b").start()
    # One spawn ever; the second claimant died at the lease, not at the
    # provider, and wrote nothing.
    assert len(provider.start_calls) == 1
    assert len(active_rows(cp)) == 1


def test_the_u27_shape_through_interlock_spawns_only_the_winner(
    cp, clock, provider, make_orchestrator, uuids
):
    """F3's crash window, mediated: claimant dies pre-admission, retry wins.

    The original claimant crashes *before its admission write commits* (the
    provider-side admission window has no Interlock admission yet), the retry
    lands while the provider would still admit both -- and through Interlock
    the dead claimant's token can admit nothing: the retry raises the epoch
    first, the loser never became a process, and no second binding exists.
    """

    stalled = make_orchestrator("sup-a")
    # sup-a acquires the lease and dies before prepare_binding commits: the
    # uuid factory is the seam between acquire and the admission write.
    def die(_calls=[]):
        raise KeyboardInterrupt("claimant killed inside the admission window")

    with pytest.raises(KeyboardInterrupt):
        SessionOrchestrator(
            cp,
            provider,
            run_id=RUN_ID,
            holder="sup-a",
            workspace="w",
            role="worker",
            now_ms=clock.now_ms,
            session_uuid_factory=die,
            ttl_ms=TTL_MS,
        ).start()
    assert provider.start_calls == []
    assert active_rows(cp) == []

    # The retry: through Interlock it must wait out the dead claimant's lease
    # (a lease cannot tell dead from slow), then it alone spawns.
    with pytest.raises(LeaseHeld):
        make_orchestrator("sup-a-retry").recover()
    clock.advance_past_expiry()
    outcome = make_orchestrator("sup-a-retry").recover()
    assert outcome.path == "started"
    assert [request.session_id for request in provider.start_calls] == [
        outcome.session_id
    ]
    assert [tuple(row)[0] for row in active_rows(cp)] == [outcome.session_id]


def test_a_stale_claimant_returning_before_its_admission_write_never_spawns(
    cp, clock, provider, make_orchestrator
):
    """SIGSTOP -> expiry -> takeover -> return, stopped before the admission.

    The returning claimant's first fenced write is refused, the refusal is
    durable, and the provider was never called by it.
    """

    def stop_and_lose():
        # The world moves while sup-a is stopped between its acquire and its
        # admission write: the lease expires and sup-b takes over (epoch up).
        take_over(cp, clock, "sup-b")
        return "11111111-1111-4111-8111-111111111111"

    with pytest.raises(StaleWriterRefused):
        make_orchestrator("sup-a", session_uuid_factory=stop_and_lose).start()

    assert provider.start_calls == []  # the loser never became a process
    assert active_rows(cp) == []
    recorded = refusals(cp)
    assert len(recorded) == 1
    assert "prepare_binding" in recorded[0]["kind"]


def test_a_claimant_stopped_inside_the_critical_section_is_terminated_measured(
    cp, clock, provider, make_orchestrator
):
    """SIGSTOP -> expiry -> takeover -> return, stopped *after* the admission.

    The admission write committed under a then-live token, the claimant
    stalled inside the critical section, and the takeover happened before its
    process existed for anyone to resolve. The post-spawn validation is where
    that claimant is caught: the refusal is recorded and the just-created
    process is terminated at once, with the latency measured rather than
    implied. (This window is exactly the residual the gate record states: an
    external exec cannot be made transactional with a SQLite commit.)
    """

    def takeover_inside(request):
        take_over(cp, clock, "sup-b")
        return None

    provider.on_start = takeover_inside
    with pytest.raises(LoserTerminated) as caught:
        make_orchestrator("sup-a").start()

    loser = caught.value
    # The process was created (that is the residual) -- and then terminated,
    # immediately and measurably, before any identity was confirmed.
    assert provider.stop_calls == [loser.session_id]
    assert loser.termination_latency_ms >= 0
    assert any("post_spawn_gate" in row["kind"] for row in refusals(cp))
    # The loser's binding never reached identity_confirmed.
    binding = session_binding.binding_for_session(cp, loser.session_id)
    assert binding is not None and binding.binding_phase == "spawned"


# --------------------------------------------------------------------------
# recovery: the four injection points, re-identified from SQLite alone
# --------------------------------------------------------------------------


def test_killed_before_the_binding_commit_recovery_starts_fresh(
    cp, clock, provider, make_orchestrator
):
    """Injection point 1: nothing durable exists; recovery is an admission."""

    clock.advance_past_expiry()  # any prior claimant's lease is history
    outcome = make_orchestrator("sup-2").recover()
    assert outcome.path == "started"
    assert len(provider.start_calls) == 1
    assert [tuple(row)[0] for row in active_rows(cp)] == [outcome.session_id]


def test_killed_between_commit_and_spawn_recovery_respawns_the_bound_identity(
    cp, clock, provider, make_orchestrator, uuids
):
    """Injection point 2a: 'prepared' committed, the mark did not."""

    lease = acquire(cp, resource=RESOURCE, holder="sup-1", now_ms=clock.now_ms(), ttl_ms=TTL_MS)
    session_id = uuids()
    session_binding.prepare_binding(
        cp, lease, session_id=session_id, run_id=RUN_ID, provider="scripted",
        now_ms=clock.now_ms(),
    )
    clock.advance_past_expiry()

    outcome = make_orchestrator("sup-2").recover()
    assert outcome.path == "respawned"
    assert outcome.session_id == session_id  # the committed identity, not a new one
    assert [request.session_id for request in provider.start_calls] == [session_id]
    assert provider.resume_calls == []
    assert outcome.binding.binding_phase == "identity_confirmed"


def test_killed_after_the_mark_but_before_the_spawn_ran_recovery_respawns(
    cp, clock, provider, make_orchestrator, uuids
):
    """Injection point 2b: 'spawned' committed, yet the provider never ran.

    The mark is a write-ahead, not a receipt: the provider commits its own
    record before creating a process, so 'unknown session' proves the spawn
    never happened and the recovery re-runs it -- with the same identity, and
    through start, not resume (there is nothing to resume).
    """

    lease = acquire(cp, resource=RESOURCE, holder="sup-1", now_ms=clock.now_ms(), ttl_ms=TTL_MS)
    session_id = uuids()
    session_binding.prepare_binding(
        cp, lease, session_id=session_id, run_id=RUN_ID, provider="scripted",
        now_ms=clock.now_ms(),
    )
    session_binding.mark_spawned(
        cp, lease, session_id=session_id, run_id=RUN_ID, now_ms=clock.now_ms()
    )
    clock.advance_past_expiry()

    outcome = make_orchestrator("sup-2").recover()
    assert outcome.path == "respawned"
    assert outcome.session_id == session_id
    assert [request.session_id for request in provider.start_calls] == [session_id]
    assert provider.resume_calls == []


def test_killed_between_spawn_and_readback_recovery_resumes_never_reclaims(
    cp, clock, provider, make_orchestrator, uuids
):
    """Injection point 3: the process existed; recovery goes through resume.

    U28: the claim is still held by the dead session, so a fresh
    ``--session-id`` claim would be refused -- recovery must resume. The
    provider records prove no second start was ever issued.
    """

    lease = acquire(cp, resource=RESOURCE, holder="sup-1", now_ms=clock.now_ms(), ttl_ms=TTL_MS)
    session_id = uuids()
    session_binding.prepare_binding(
        cp, lease, session_id=session_id, run_id=RUN_ID, provider="scripted",
        now_ms=clock.now_ms(),
    )
    session_binding.mark_spawned(
        cp, lease, session_id=session_id, run_id=RUN_ID, now_ms=clock.now_ms()
    )
    provider.plant(session_id, live=False)  # the provider knows the dead child
    clock.advance_past_expiry()

    outcome = make_orchestrator("sup-2").recover()
    assert outcome.path == "resumed"
    assert provider.start_calls == []
    assert provider.resume_calls == [session_id]
    assert outcome.binding.binding_phase == "identity_confirmed"


def test_killed_after_the_readback_commit_recovery_resumes_and_rewrites_nothing(
    cp, clock, provider, make_orchestrator
):
    """Injection point 4: the binding is already confirmed; recovery adopts."""

    outcome = make_orchestrator("sup-1").start()
    confirmed_at = cp.execute(
        "SELECT binding_phase FROM session WHERE session_id = ?",
        (outcome.session_id,),
    ).fetchone()[0]
    assert confirmed_at == "identity_confirmed"

    clock.advance_past_expiry()
    recovered = make_orchestrator("sup-2").recover()
    assert recovered.path == "resumed"
    assert recovered.session_id == outcome.session_id
    assert provider.resume_calls == [outcome.session_id]
    assert len(provider.start_calls) == 1  # still only the original spawn
    # Exactly one active binding, same identity, still confirmed.
    assert [tuple(row) for row in active_rows(cp)] == [
        (outcome.session_id, "identity_confirmed", "observed")
    ]


def test_every_recovery_ends_with_exactly_one_active_binding(
    cp, clock, provider, make_orchestrator
):
    """At-most-one is the index's; exactly-one is asserted non-empty here."""

    outcome = make_orchestrator("sup-1").start()
    for generation in range(2, 5):
        clock.advance_past_expiry()
        recovered = make_orchestrator(f"sup-{generation}").recover()
        assert recovered.session_id == outcome.session_id
        assert len(active_rows(cp)) == 1


# --------------------------------------------------------------------------
# the U32 shape, mediated; orphans; refusal durability
# --------------------------------------------------------------------------


def test_the_u32_shape_through_interlock_issues_no_second_resume(
    cp, clock, provider, make_orchestrator
):
    """Two recoveries race for one dead session; one resume ever happens.

    The provider would admit both resumes (U32 measures exactly that); through
    Interlock the second recovering claimant is refused at the lease and its
    resume is never issued.
    """

    outcome = make_orchestrator("sup-1").start()
    clock.advance_past_expiry()
    first = make_orchestrator("sup-2").recover()
    assert first.session_id == outcome.session_id
    with pytest.raises(LeaseHeld):
        make_orchestrator("sup-3").recover()
    assert provider.resume_calls == [outcome.session_id]  # exactly one, ever


def test_a_stale_recoverer_is_refused_before_its_resume(
    cp, clock, provider, make_orchestrator
):
    """The gate write precedes resume, so a stale recoverer never resumes."""

    outcome = make_orchestrator("sup-1").start()
    clock.advance_past_expiry()

    recoverer = make_orchestrator("sup-2")
    original_read_state = provider.read_state

    def lose_after_the_read(session_id):
        provider.read_state = original_read_state
        take_over(cp, clock, "sup-3")
        return original_read_state(session_id)

    provider.read_state = lose_after_the_read
    with pytest.raises(StaleWriterRefused):
        recoverer.recover()
    assert provider.resume_calls == []  # refused before the verb, not after
    assert any("post_spawn_gate" in row["kind"] for row in refusals(cp))
    del provider.read_state
    assert outcome.session_id  # the binding still names the one session


def test_an_orphan_the_binding_does_not_name_is_never_adopted(
    cp, clock, provider, make_orchestrator
):
    """No orphan session is adopted twice -- or into a run that never bound it."""

    orphan = "99999999-9999-4999-8999-999999999999"
    provider.plant(orphan, live=True)  # a leftover from some other life
    clock.advance_past_expiry()

    outcome = make_orchestrator("sup-2").recover()
    assert outcome.session_id != orphan
    assert provider.resume_calls == []  # the orphan was not resumed
    assert orphan not in [request.session_id for request in provider.start_calls]
    # The orphan is still enumerable -- unadopted, not erased.
    roster = {readout.session_id for readout in provider.list_sessions().value}
    assert orphan in roster


def test_a_claimant_that_stalls_in_the_readback_never_returns_success(
    cp, clock, provider, make_orchestrator
):
    """Takeover *during the read-back poll* -- after the last mid-walk fence.

    Claimant A passes the post-spawn gate, then stalls polling for its
    identity while its lease expires and a recovering claimant B resumes and
    confirms the same binding. A's walk must not end in an unfenced
    read-then-skip that returns success: its final step is a fenced write, so
    A is refused, recorded, and terminates its own child -- it never reports
    the session as its own alongside B.
    """

    stalled: dict = {}

    def takeover_during_wait():
        if "b" in stalled:
            return
        clock.advance_past_expiry()  # A's lease dies while A is stalled
        # From here the provider reports the session normally -- which is
        # exactly what a stale A sees on waking.
        for session in provider.sessions.values():
            session.readouts = []
        # B's full recovery, run inline while A is stalled: resume the
        # session and confirm the binding under B's (live) lease.
        stalled["b"] = make_orchestrator("sup-b").recover()

    # A's readouts stay unconfirmed until B has taken over.
    provider.next_readouts = [unconfirmed("pending")] * 4
    a = make_orchestrator("sup-a", wait=takeover_during_wait, readback_attempts=3)
    with pytest.raises(LoserTerminated) as caught:
        a.start()

    assert stalled["b"].session_id == caught.value.session_id
    # A stopped its own child and left a durable refusal; B's confirmed
    # binding is untouched and remains the run's single active one.
    assert caught.value.session_id in provider.stop_calls
    assert any("post_spawn_gate" in row["kind"] for row in refusals(cp))
    assert [tuple(row) for row in active_rows(cp)] == [
        (stalled["b"].session_id, "identity_confirmed", "observed")
    ]


def test_an_unconfirmed_stop_is_reported_as_unconfirmed(
    cp, clock, provider, make_orchestrator
):
    """A loser's stop that the provider cannot confirm is never dressed up."""

    from claude_org_runtime.session.provider import Failure, FailureKind

    def takeover_inside(request):
        take_over(cp, clock, "sup-b")
        return None

    provider.on_start = takeover_inside
    real_stop = provider.stop

    def failing_stop(session_id):
        provider.stop_calls.append(session_id)
        return Failure(FailureKind.TIMED_OUT, "child did not exit within 2s of SIGKILL")

    provider.stop = failing_stop
    with pytest.raises(LoserTerminated) as caught:
        make_orchestrator("sup-a").start()
    assert caught.value.stop_confirmed is False
    assert "NOT confirmed" in str(caught.value)
    provider.stop = real_stop


def test_refusals_are_rows_not_log_lines(cp, clock, provider, make_orchestrator):
    """Every refusal in these shapes is a durable action row (D-0001)."""

    def stop_and_lose():
        take_over(cp, clock, "sup-b")
        return "22222222-2222-4222-8222-222222222222"

    with pytest.raises(StaleWriterRefused):
        make_orchestrator("sup-a", session_uuid_factory=stop_and_lose).start()
    recorded = refusals(cp)
    assert recorded, "a refused admission left no durable record"
    assert all(row["refusal_reason"] for row in recorded)
