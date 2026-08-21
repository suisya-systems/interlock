"""The registry's two per-provider predicates, pinned.

``unavailable`` decides whether a whole provider row runs on this machine at
all; ``disqualified`` decides whether the bound session's readout proves the
backend was really live. Both are the fixture package's own knowledge -- the
one place provider vocabulary is allowed to live -- and both fail the
measurement loudly rather than letting it green while measuring nothing.
"""

from __future__ import annotations

from claude_org_runtime.session.provider import Observation, SessionReadout

from . import registry


def _observed(state: str, **detail) -> SessionReadout:
    return SessionReadout(
        session_id="item11-bound-session",
        observation=Observation.OBSERVED,
        provider_state=state,
        provider_detail=detail,
    )


def test_every_entry_carries_both_predicates():
    for entry in registry.PROVIDERS.values():
        assert callable(entry.unavailable)
        assert callable(entry.disqualified)


def test_s3_is_available_and_qualified_everywhere():
    entry = registry.PROVIDERS["S3"]
    assert entry.unavailable() is None
    assert entry.disqualified(_observed("working")) is None


def test_s2_disqualifies_a_child_that_died_without_ever_speaking():
    """A broken-but-present install answers every probe and still cannot run a
    session: its child exits with the refusal on stderr and no structured
    output. That readout must abort the bound run, not green it."""

    entry = registry.PROVIDERS["S2"]
    reason = entry.disqualified(
        _observed("exited-1", stderr_tail="Invalid API key. Please run /login")
    )
    assert reason is not None
    assert "Invalid API key" in reason


def test_s2_accepts_any_state_the_child_itself_reported():
    entry = registry.PROVIDERS["S2"]
    for state in ("hook_started", "init", "assistant", "completed", "api_error"):
        assert entry.disqualified(_observed(state)) is None
    quiet = SessionReadout(
        session_id="item11-bound-session",
        observation=Observation.COULD_NOT_OBSERVE,
        could_not_observe_reason="the child is running but has not emitted anything parseable yet",
    )
    assert entry.disqualified(quiet) is None
