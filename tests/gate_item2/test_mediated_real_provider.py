"""The mediated proof over the real C2 provider (fake CLI, real subprocesses).

Same shapes as ``test_orchestrator_walk``, now driven through
``ClaudeCliSessionProvider`` over the S2 fake CLI so the assertions reach the
provider's own durable artifacts: the spawn log (which argv was ever executed),
the per-session ``record.json``, and the captured event streams (the C2 stand-in
for the transcript). The fake CLI honours whatever identity it is told to claim
and refuses nothing -- the mediated outcome is Interlock's doing.

Major-1 separation (the review's three kill shapes) is explicit here:

- supervisor-only kill: the child survives (its own process group) and is
  adopted, uniquely, without a second spawn;
- supervisor+child kill: only the binding and the provider record remain, and
  recovery resumes -- never re-claims -- the bound identity;
- claimant dies pre-admission with the retry inside the window is tier 1's
  (``test_the_u27_shape_through_interlock_spawns_only_the_winner``) and the
  fault-injection harness's, where the kill is a real SIGKILL at an armed
  anchor.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from claude_org_runtime.session.claude_cli_provider import ClaudeCliSessionProvider
from claude_org_runtime.session.provider import Ok
from claude_org_runtime.supervisor import SessionOrchestrator

from .conftest import RUN_ID, TTL_MS, Clock, active_rows
from tests.session.test_claude_cli_provider import _FAKE_CLI

UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

# ---------------------------------------------------------------------------
# Platform sweep, stated once. The provider *fails closed* wherever a pid's
# liveness or identity cannot be proven (#17): recovery around an orphan
# record refuses to adopt, signal or resume rather than guess. That refusal
# is design, not breakage -- so the shapes below are provable only where
# their proof surface exists, and are skipped (with the dependency named)
# everywhere else. The CI matrix's Linux jobs run every one of them.
#
# - Orphan liveness at all (kill-0 probe): POSIX only. On Windows an orphan
#   record's liveness is unknowable and every recovery around it is refused.
# - A *live* orphan's identity (pid-recycling guard): needs the pid's command
#   line, i.e. /proc. macOS is POSIX without /proc: dead orphans resolve
#   (kill-0), live ones refuse.
#
# Everything else in this file (fresh-start walks, identity mismatch, the
# race with the refusal absent) drives only this instance's own children and
# is platform-free. tests/test_orchestrator_walk.py is in-memory throughout;
# test_session_driver_harness.py and the fault-injection session cases are
# Linux-lane by declaration (real SIGKILL, /proc observer).
# ---------------------------------------------------------------------------
_POSIX = os.name == "posix"
HAS_PROC = Path("/proc").is_dir()


@pytest.fixture
def fake_cli(tmp_path: Path) -> tuple[str, ...]:
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLI, encoding="utf-8")
    return (sys.executable, str(script))


@pytest.fixture
def spawn_log(tmp_path: Path, monkeypatch) -> Path:
    log = tmp_path / "spawns.jsonl"
    monkeypatch.setenv("FAKE_SPAWN_LOG", str(log))
    return log


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def make_provider(fake_cli, state_root):
    made: list[ClaudeCliSessionProvider] = []

    def factory() -> ClaudeCliSessionProvider:
        provider = ClaudeCliSessionProvider(
            state_root, claude_command=fake_cli, stop_timeout=2.0
        )
        made.append(provider)
        return provider

    yield factory
    for provider in made:
        listed = provider.list_sessions()
        if isinstance(listed, Ok):
            for readout in listed.value:
                provider.stop(readout.session_id)


@pytest.fixture
def make_real_orchestrator(cp, clock, tmp_path):
    def factory(provider, holder: str, session_id: str = UUID_A) -> SessionOrchestrator:
        return SessionOrchestrator(
            cp,
            provider,
            run_id=RUN_ID,
            holder=holder,
            workspace=str(tmp_path / "workspace"),
            role="worker",
            now_ms=clock.now_ms,
            session_uuid_factory=lambda: session_id,
            settings={"prompt": "reply with ok", "resume_prompt": "resume"},
            ttl_ms=TTL_MS,
            readback_attempts=200,
            wait=lambda: time.sleep(0.02),
        )

    return factory


def _spawns(spawn_log: Path) -> list[dict]:
    if not spawn_log.exists():
        return []
    return [
        json.loads(line)
        for line in spawn_log.read_text(encoding="utf-8").splitlines()
    ]


def _event_session_ids(state_root: Path, session_id: str) -> set[str]:
    """Every identity any captured event stream ever named for this session."""

    names: set[str] = set()
    for events_file in sorted((state_root / session_id).glob("events-*.jsonl")):
        for line in events_file.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("session_id"):
                names.add(event["session_id"])
    return names


def test_the_walk_commits_the_exact_identity_the_provider_is_told_to_claim(
    cp, spawn_log, state_root, make_provider, make_real_orchestrator, monkeypatch
):
    """One string end to end: binding row, --session-id argv, event stream."""

    monkeypatch.setenv("FAKE_MODE", "ok")
    provider = make_provider()
    outcome = make_real_orchestrator(provider, "sup-1").start()

    assert outcome.session_id == UUID_A
    spawns = _spawns(spawn_log)
    assert len(spawns) == 1
    argv = spawns[0]["argv"]
    assert argv[argv.index("--session-id") + 1] == UUID_A
    # The provider's captured stream -- the C2 transcript stand-in -- names
    # exactly the committed identity and no other writer's.
    assert _event_session_ids(state_root, UUID_A) == {UUID_A}
    assert [tuple(row) for row in active_rows(cp)] == [
        (UUID_A, "identity_confirmed", "observed")
    ]


def test_a_reported_identity_that_disagrees_is_never_confirmed(
    cp, spawn_log, make_provider, make_real_orchestrator, monkeypatch
):
    """The U27 failure shape at the read-back: impounded, not adopted."""

    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setenv("FAKE_REPORT_ID", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    provider = make_provider()
    orchestrator = make_real_orchestrator(provider, "sup-1")
    from claude_org_runtime.supervisor import IdentityUnconfirmed

    with pytest.raises(IdentityUnconfirmed):
        orchestrator.start()
    # The binding never claimed a read-back that contradicted it.
    [(session_id, phase, observation)] = [tuple(r) for r in active_rows(cp)]
    assert (phase, observation) == ("spawned", "unobserved")


@pytest.mark.skipif(
    not HAS_PROC,
    reason=(
        "adoption requires confirming the surviving pid's command line via "
        "/proc; where /proc does not exist (macOS, Windows) the provider "
        "fails closed and refuses to adopt -- by design (#17), so the "
        "adoption shape is provable only where the proof surface exists"
    ),
)
def test_supervisor_only_kill_the_surviving_child_is_adopted_not_respawned(
    cp, clock, spawn_log, state_root, make_provider, make_real_orchestrator, monkeypatch
):
    """Major-1 shape 1: the child outlives its supervisor (own process group).

    A new supervisor life recovers: it re-identifies the run's one session
    from SQLite, finds the child alive through the provider's own durable
    record, and adopts -- the spawn log proves no second process was ever
    created for the id.
    """

    monkeypatch.setenv("FAKE_MODE", "events-then-hang")
    monkeypatch.setenv("FAKE_SLEEP", "60")
    first_supervisor = make_provider()
    outcome = make_real_orchestrator(first_supervisor, "sup-1").start()
    assert outcome.binding.binding_phase == "identity_confirmed"

    # The supervisor dies; the child does not (start_new_session=True). A new
    # supervisor process is a new provider instance over the same state root.
    del first_supervisor
    clock.advance_past_expiry()
    second_supervisor = make_provider()
    recovered = make_real_orchestrator(second_supervisor, "sup-2").recover()

    assert recovered.session_id == UUID_A
    assert recovered.path == "resumed"
    # Adoption, not respawn: still exactly one spawned process, ever.
    assert len(_spawns(spawn_log)) == 1
    assert _event_session_ids(state_root, UUID_A) == {UUID_A}
    assert len(active_rows(cp)) == 1


@pytest.mark.skipif(
    not _POSIX,
    reason=(
        "resuming an orphan record requires determining the recorded pid's "
        "liveness (the kill-0 probe); on Windows that is unknowable and the "
        "provider fails closed, refusing to adopt, signal or resume around "
        "it -- by design (#17), so the resume shape is provable only on POSIX"
    ),
)
def test_supervisor_and_child_kill_recovery_resumes_the_bound_identity(
    cp, clock, spawn_log, state_root, make_provider, make_real_orchestrator, monkeypatch
):
    """Major-1 shape 3: binding and record remain; recovery goes through
    --resume with the committed identity, never a fresh --session-id claim."""

    monkeypatch.setenv("FAKE_MODE", "ok")
    first_supervisor = make_provider()
    outcome = make_real_orchestrator(first_supervisor, "sup-1").start()
    # The 'ok' child has already exited by the time the walk confirms (its
    # readout is the result event); the supervisor now dies too.
    del first_supervisor

    clock.advance_past_expiry()
    second_supervisor = make_provider()
    recovered = make_real_orchestrator(second_supervisor, "sup-2").recover()

    assert recovered.session_id == UUID_A
    spawns = _spawns(spawn_log)
    assert len(spawns) == 2
    first_argv, second_argv = spawns[0]["argv"], spawns[1]["argv"]
    assert first_argv[first_argv.index("--session-id") + 1] == UUID_A
    assert "--session-id" not in second_argv  # never a fresh claim (U28)
    assert second_argv[second_argv.index("--resume") + 1] == UUID_A
    assert _event_session_ids(state_root, UUID_A) == {UUID_A}
    assert [tuple(row) for row in active_rows(cp)] == [
        (UUID_A, "identity_confirmed", "observed")
    ]


def test_the_provider_refusal_is_not_the_mechanism(
    cp, clock, spawn_log, make_provider, make_real_orchestrator, monkeypatch
):
    """The mediated single-writer outcome survives the refusal being absent.

    Two supervisors race for one run with the provider's ``already in use``
    refusal switched off entirely (the fake admits everything, exactly as U27
    measures inside the window). The second claimant still never spawns --
    because the lease refused it, before any argv existed.
    """

    monkeypatch.setenv("FAKE_MODE", "ok")
    provider = make_provider()
    make_real_orchestrator(provider, "sup-1").start()

    from claude_org_runtime.control_plane.lease import LeaseHeld

    rival = make_provider()
    with pytest.raises(LeaseHeld):
        make_real_orchestrator(rival, "sup-2", session_id=UUID_A).start()
    assert len(_spawns(spawn_log)) == 1  # the loser produced no process
