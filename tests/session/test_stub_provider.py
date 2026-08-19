"""S3 -- the stub provider over local child processes.

These tests exercise the stub as the control-plane suite will: through the S1
verbs only, including the degraded paths (a refused spawn, a session that
cannot be observed yet, a re-entry that is refused), since those are the ones
item 11's re-run leans on and the ones a happy-path-only suite would let rot.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from claude_org_runtime.session import provider as s1
from claude_org_runtime.session.stub_provider import (
    ANNOUNCE_AFTER_ENV,
    DEFAULT_CHILD_STATE,
    STATE_FILE_ENV,
    LocalProcessSessionProvider,
)

#: Long enough that a test reading straight after the spawn always lands inside
#: the window in which the child has not reported yet.
NEVER_ANNOUNCES = 3600


@pytest.fixture
def provider(tmp_path: Path):
    """A provider whose children are always stopped, test outcome regardless."""

    instance = LocalProcessSessionProvider(tmp_path / "state")
    try:
        yield instance
    finally:
        for readout in instance.list_sessions().value:
            instance.stop(readout.session_id)


def _request(tmp_path: Path, session_id: str = "s-1", **settings) -> s1.StartRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return s1.StartRequest(
        session_id=session_id,
        workspace=str(workspace),
        role="worker",
        settings=settings,
    )


def _wait_until_observed(provider, session_id: str, timeout: float = 10.0):
    """Poll ``read_state`` until the child has reported, or give up loudly."""

    deadline = time.monotonic() + timeout
    readout = provider.read_state(session_id).value
    while readout.observation is s1.Observation.COULD_NOT_OBSERVE:
        assert time.monotonic() < deadline, f"child never reported: {readout}"
        time.sleep(0.02)
        readout = provider.read_state(session_id).value
    return readout


# -- it is a provider at all ----------------------------------------------


def test_the_stub_satisfies_the_s1_contract(tmp_path: Path):
    """Concrete on every abstract member, and it did not override the gate."""

    instance = LocalProcessSessionProvider(tmp_path)
    assert isinstance(instance, s1.SessionProvider)
    assert not LocalProcessSessionProvider.__abstractmethods__
    for gate in ("start", "require_spawnable"):
        assert getattr(LocalProcessSessionProvider, gate) is s1.SessionProvider.__dict__[gate]


def test_no_claude_cli_and_no_network(tmp_path: Path, provider):
    """The scope's two hard constraints, checked rather than asserted in prose."""

    source = Path(
        sys.modules[LocalProcessSessionProvider.__module__].__file__
    ).read_text(encoding="utf-8")
    for forbidden in ("socket", "urllib", "requests", "http://", "https://"):
        assert forbidden not in source.lower(), forbidden

    started = provider.start(_request(tmp_path))
    command = started.value.provider_detail["command"]
    assert command[0] == sys.executable


def test_no_verb_writes_to_a_child(provider):
    """D-0009: delivery is MessageBus's, and the stub grows no path to it."""

    delivery_shaped = [
        name
        for name in dir(LocalProcessSessionProvider)
        if not name.startswith("_")
        and any(
            word in name.lower()
            for word in ("deliver", "send", "write", "notify", "input", "message", "prompt")
        )
    ]
    assert not delivery_shaped, delivery_shaped


# -- the capability probe and its fail-closed spawn (D-0010) ---------------


def test_the_probe_reports_a_build_and_every_required_capability(provider):
    probe = provider.probe_capabilities()
    assert isinstance(probe, s1.Ok)
    report = probe.value
    assert report.provider_version.startswith("python ")
    assert report.compatible
    assert not report.missing


def test_an_unusable_interpreter_fails_the_probe_and_refuses_the_spawn(tmp_path: Path):
    """The degraded path: nothing is spawned on a provider that does not answer."""

    broken = LocalProcessSessionProvider(
        tmp_path / "state", python_executable=str(tmp_path / "no-such-interpreter")
    )
    probe = broken.probe_capabilities()
    assert isinstance(probe, s1.Failure)
    assert probe.kind is s1.FailureKind.BACKEND_UNREACHABLE

    with pytest.raises(s1.SpawnRefused):
        broken.start(_request(tmp_path))
    assert broken.list_sessions().value == ()


# -- start, list, read_state ----------------------------------------------


def test_a_started_session_reports_the_childs_own_state_word(tmp_path: Path, provider):
    assert isinstance(provider.start(_request(tmp_path)), s1.Ok)
    readout = _wait_until_observed(provider, "s-1")
    assert readout.observation is s1.Observation.OBSERVED
    assert readout.provider_state == DEFAULT_CHILD_STATE
    assert readout.provider_detail["pid"] > 0


def test_a_child_that_has_not_reported_yet_is_could_not_observe(tmp_path: Path, provider):
    """The case R4 and D-0006 exist for: alive, unobservable, and it says why."""

    provider.start(_request(tmp_path, announce_after=NEVER_ANNOUNCES))
    result = provider.read_state("s-1")
    assert isinstance(result, s1.Ok)
    readout = result.value
    assert readout.observation is s1.Observation.COULD_NOT_OBSERVE
    assert readout.provider_state is None
    assert "not reported" in readout.could_not_observe_reason


def test_no_sessions_is_an_empty_success_not_a_failure(provider):
    result = provider.list_sessions()
    assert isinstance(result, s1.Ok)
    assert result.value == ()


def test_list_sessions_carries_one_readout_per_started_session(tmp_path: Path, provider):
    provider.start(_request(tmp_path, "s-1"))
    provider.start(_request(tmp_path, "s-2"))
    listed = provider.list_sessions().value
    assert sorted(readout.session_id for readout in listed) == ["s-1", "s-2"]


def test_a_reused_session_id_is_refused(tmp_path: Path, provider):
    provider.start(_request(tmp_path))
    again = provider.start(_request(tmp_path))
    assert isinstance(again, s1.Failure)
    assert again.kind is s1.FailureKind.REFUSED_BY_PROVIDER


def test_reading_an_unknown_session_is_a_failure_not_an_empty_readout(provider):
    result = provider.read_state("never-started")
    assert isinstance(result, s1.Failure)
    assert result.kind is s1.FailureKind.UNKNOWN_SESSION


def test_a_child_that_cannot_be_spawned_is_a_failure(tmp_path: Path, provider):
    result = provider.start(
        _request(tmp_path, command=[str(tmp_path / "no-such-child")])
    )
    assert isinstance(result, s1.Failure)
    assert result.kind is s1.FailureKind.BACKEND_UNREACHABLE


# -- stop ------------------------------------------------------------------


def test_stop_reports_the_state_after_the_child_is_gone(tmp_path: Path, provider):
    provider.start(_request(tmp_path))
    _wait_until_observed(provider, "s-1")
    stopped = provider.stop("s-1")
    assert isinstance(stopped, s1.Ok)
    assert stopped.value.observation is s1.Observation.OBSERVED
    assert stopped.value.provider_state.startswith("exited-")
    assert provider.read_state("s-1").value.provider_state.startswith("exited-")


def test_stopping_an_unknown_session_is_a_failure(provider):
    result = provider.stop("never-started")
    assert isinstance(result, s1.Failure)
    assert result.kind is s1.FailureKind.UNKNOWN_SESSION


# -- resume ----------------------------------------------------------------


def test_resuming_a_live_session_returns_its_current_readout(tmp_path: Path, provider):
    provider.start(_request(tmp_path))
    _wait_until_observed(provider, "s-1")
    resumed = provider.resume("s-1")
    assert isinstance(resumed, s1.Ok)
    assert resumed.value.provider_state == DEFAULT_CHILD_STATE


def test_resuming_an_exited_session_is_refused_with_a_reason(tmp_path: Path, provider):
    provider.start(_request(tmp_path))
    provider.stop("s-1")
    resumed = provider.resume("s-1")
    assert isinstance(resumed, s1.Failure)
    assert resumed.kind is s1.FailureKind.REFUSED_BY_PROVIDER
    assert "re-entered" in resumed.detail


def test_resuming_an_unknown_session_is_a_failure(provider):
    result = provider.resume("never-started")
    assert isinstance(result, s1.Failure)
    assert result.kind is s1.FailureKind.UNKNOWN_SESSION


# -- the workspace lifecycle surface (gate item 7) -------------------------


class _Recorder:
    """An observer that records every transition and answers as told."""

    def __init__(self, decision: s1.WorkspaceDecision):
        self.decision = decision
        self.seen: list[s1.WorkspaceTransition] = []

    def on_workspace_transition(self, transition):
        self.seen.append(transition)
        return self.decision


def test_creating_a_workspace_is_announced_before_it_is_made(tmp_path: Path, provider):
    observer = _Recorder(s1.WorkspaceDecision(s1.WorkspaceVerdict.ALLOW))
    provider.register_workspace_observer(observer)
    workspace = tmp_path / "fresh"
    request = s1.StartRequest(
        session_id="s-1", workspace=str(workspace), role="worker", settings={}
    )

    assert isinstance(provider.start(request), s1.Ok)
    assert [t.kind for t in observer.seen] == ["create-workspace"]
    assert workspace.is_dir()


def test_a_vetoed_workspace_is_neither_created_nor_started(tmp_path: Path, provider):
    observer = _Recorder(
        s1.WorkspaceDecision(s1.WorkspaceVerdict.VETO, "outside the approved root")
    )
    provider.register_workspace_observer(observer)
    workspace = tmp_path / "forbidden"
    request = s1.StartRequest(
        session_id="s-1", workspace=str(workspace), role="worker", settings={}
    )

    result = provider.start(request)
    assert isinstance(result, s1.Failure)
    assert result.kind is s1.FailureKind.REFUSED_BY_PROVIDER
    assert "outside the approved root" in result.detail
    assert not workspace.exists()
    assert provider.list_sessions().value == ()


def test_an_existing_workspace_announces_nothing(tmp_path: Path, provider):
    """The stub only announces transitions it actually makes."""

    observer = _Recorder(s1.WorkspaceDecision(s1.WorkspaceVerdict.ALLOW))
    provider.register_workspace_observer(observer)
    provider.start(_request(tmp_path))
    assert observer.seen == []


# -- the child contract a caller may substitute into ------------------------


def test_a_caller_supplied_child_is_read_through_the_same_readout(
    tmp_path: Path, provider
):
    """Nothing in the readout is special-cased for the default child."""

    child = (
        "import os\n"
        f"path = os.environ[{STATE_FILE_ENV!r}]\n"
        "open(path, 'w', encoding='utf-8').write('its-own-word')\n"
        "import sys; sys.stdin.read()\n"
    )
    provider.start(_request(tmp_path, command=[sys.executable, "-c", child]))
    readout = _wait_until_observed(provider, "s-1")
    assert readout.provider_state == "its-own-word"


def test_the_announce_delay_is_passed_to_the_child_by_environment(tmp_path: Path, provider):
    """The knob the could-not-observe window depends on is the child's, not ours."""

    child = (
        "import os\n"
        f"path = os.environ[{STATE_FILE_ENV!r}]\n"
        f"open(path, 'w', encoding='utf-8').write(os.environ[{ANNOUNCE_AFTER_ENV!r}])\n"
        "import sys; sys.stdin.read()\n"
    )
    provider.start(
        _request(tmp_path, command=[sys.executable, "-c", child], announce_after=7)
    )
    readout = _wait_until_observed(provider, "s-1")
    assert readout.provider_state == "7"


# -- the state files the readout depends on ---------------------------------


@pytest.mark.parametrize(
    "session_id",
    ["../escape", "/absolute", "nested/id", "..", "back\\slash", "C:escape", "nul\x00id"],
)
def test_a_session_id_that_is_not_one_file_name_is_refused(
    tmp_path: Path, provider, session_id: str
):
    """The id names a state file, so it may not pick a file outside the root."""

    victim = tmp_path / "escape.state"
    victim.write_text("do not touch", encoding="utf-8")

    result = provider.start(_request(tmp_path, session_id))
    assert isinstance(result, s1.Failure)
    assert result.kind is s1.FailureKind.REFUSED_BY_PROVIDER
    assert victim.read_text(encoding="utf-8") == "do not touch"
    assert provider.list_sessions().value == ()


def test_a_relative_state_root_still_observes_its_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The child runs with the workspace as cwd, so the root must be absolute."""

    monkeypatch.chdir(tmp_path)
    instance = LocalProcessSessionProvider("state")
    try:
        assert isinstance(instance.start(_request(tmp_path)), s1.Ok)
        assert _wait_until_observed(instance, "s-1").provider_state == DEFAULT_CHILD_STATE
    finally:
        instance.stop("s-1")


def test_an_unusable_state_root_is_a_failure_not_an_exception(tmp_path: Path):
    """Ordinary provider-side trouble is returned, never raised at the caller."""

    blocked = tmp_path / "state"
    blocked.write_text("not a directory", encoding="utf-8")
    instance = LocalProcessSessionProvider(blocked)

    result = instance.start(_request(tmp_path))
    assert isinstance(result, s1.Failure)
    assert result.kind is s1.FailureKind.REFUSED_BY_PROVIDER
    assert instance.list_sessions().value == ()


def test_reading_an_exited_session_releases_its_child_pipe(tmp_path: Path, provider):
    """A child that exits on its own is never handed to stop()."""

    provider.start(_request(tmp_path, command=[sys.executable, "-c", "pass"]))
    deadline = time.monotonic() + 10.0
    while provider.read_state("s-1").value.observation is s1.Observation.COULD_NOT_OBSERVE:
        assert time.monotonic() < deadline, "child never exited"
        time.sleep(0.02)

    readout = provider.read_state("s-1").value
    assert readout.provider_state == "exited-0"
    assert provider._sessions["s-1"].process.stdin.closed


def test_an_unusable_child_command_is_a_failure_not_an_exception(
    tmp_path: Path, provider
):
    """Popen rejects these before the operating system sees them."""

    for command in ([], [sys.executable, "-c", "pass\x00"]):
        result = provider.start(_request(tmp_path, "s-1", command=command))
        assert isinstance(result, s1.Failure), command
        assert result.kind is s1.FailureKind.REFUSED_BY_PROVIDER
    assert provider.list_sessions().value == ()


def test_a_state_word_that_is_not_utf8_is_could_not_observe(tmp_path: Path, provider):
    """A child writes what it likes; unreadable bytes are not a state."""

    child = (
        "import os, sys\n"
        f"path = os.environ[{STATE_FILE_ENV!r}]\n"
        "open(path, 'wb').write(b'\\xff\\xfe')\n"
        "sys.stdin.read()\n"
    )
    provider.start(_request(tmp_path, command=[sys.executable, "-c", child]))
    deadline = time.monotonic() + 10.0
    while not (provider._sessions["s-1"].state_file.exists()):
        assert time.monotonic() < deadline, "child never wrote its state file"
        time.sleep(0.02)

    readout = provider.read_state("s-1").value
    assert readout.observation is s1.Observation.COULD_NOT_OBSERVE
    assert "not UTF-8" in readout.could_not_observe_reason
