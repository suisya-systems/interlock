"""S2 -- the C2 provider, exercised hermetically against a fake CLI.

Every test here runs against a small Python stand-in for the ``claude``
executable, for two reasons that matter more than realism:

* the real CLI spends a billed model turn per spawn and does not exist on the
  CI matrix at all, so a suite that needed it would be a suite that never
  runs where regressions are caught (the real CLI is exercised by
  ``tests/gate_item11``, whose S2 rows bind live sessions on machines that
  carry it);
* the failure shapes issue ``#17`` is actually about -- a wrong identity read
  back, a refusal that exists only on stderr, a child that answers garbage,
  ``is_error`` alongside exit 0 -- are exactly the shapes a live healthy CLI
  will not produce on demand.

The fake renders the *public surface the probes recorded* (``--version``,
``--help`` flag text, stream-json events with ``session_id`` in ``init`` and
``terminal_reason``/``is_error``/``subtype`` in ``result``; i01 §3.2-§3.4)
and nothing else. Where a fact is provider-shaped and measured rather than
contractual -- the U27 admission-window width, say -- nothing here asserts
it, per the design-review directive to keep #6's probe findings from
hardening into test assumptions.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from claude_org_runtime.session import claude_cli_provider as s2
from claude_org_runtime.session.claude_cli_provider import (
    ClaudeCliSessionProvider,
    claude_session_uuid,
)
from claude_org_runtime.session.provider import (
    REQUIRED_CAPABILITIES,
    Failure,
    FailureKind,
    Observation,
    Ok,
    SpawnRefused,
    StartRequest,
    WorkspaceDecision,
    WorkspaceVerdict,
)

IS_POSIX = os.name == "posix"
HAS_PROC = Path("/proc").is_dir()

FAKE_VERSION = "9.9.9-fake (Claude Code)"

#: The fake CLI. One file, driven by environment variables so the *provider
#: under test* is byte-identical across scenarios -- only the backend's
#: behaviour changes, which is the situation the provider exists to survive.
_FAKE_CLI = f"""
import json, os, sys, time

args = sys.argv[1:]

if "--version" in args:
    print({FAKE_VERSION!r})
    sys.exit(0)

if "--help" in args:
    omitted = set(os.environ.get("FAKE_HELP_OMIT", "").split())
    lines = [
        "  -p, --print                Print response and exit",
        "  --session-id <uuid>        Use a specific session ID",
        "  -r, --resume [value]       Resume a conversation by session ID",
        "  --output-format <format>   Output format (json | stream-json)",
        "  --verbose                  Override verbose mode",
        "  --model <model>            Model for the current session",
    ]
    print("Usage: claude [options] [command] [prompt]")
    for line in lines:
        if not any(flag in line for flag in omitted):
            print(line)
    sys.exit(0)

log = os.environ.get("FAKE_SPAWN_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"argv": args, "cwd": os.getcwd()}}) + "\\n")

def value_of(flag):
    return args[args.index(flag) + 1] if flag in args else None

claimed = value_of("--session-id") or value_of("--resume")
mode = os.environ.get("FAKE_MODE", "ok")
sleep_for = float(os.environ.get("FAKE_SLEEP", "60"))

if mode == "refuse-in-use":
    print("Error: Session ID " + str(claimed) + " is already in use.", file=sys.stderr)
    sys.exit(1)

if mode == "silent":
    time.sleep(sleep_for)
    sys.exit(0)

reported = os.environ.get("FAKE_REPORT_ID", claimed)

def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()

emit({{"type": "system", "subtype": "init", "session_id": reported,
      "unknown_field": {{"nested": ["tolerated"]}}}})

if mode == "garbage-then-hang":
    sys.stdout.write("this complete line is not JSON\\n")
    sys.stdout.flush()
    time.sleep(sleep_for)
    sys.exit(0)

emit({{"type": "unheard_of_event", "session_id": reported, "payload": 123}})

if mode == "events-then-hang":
    time.sleep(sleep_for)
    sys.exit(0)

if os.environ.get("FAKE_RESULT_BARE") == "1":
    emit({{"type": "result", "session_id": reported}})
else:
    emit({{"type": "result",
          "subtype": os.environ.get("FAKE_SUBTYPE", "success"),
          "is_error": os.environ.get("FAKE_IS_ERROR") == "1",
          "terminal_reason": os.environ.get("FAKE_TERMINAL_REASON", "completed"),
          "session_id": reported,
          "another_unknown_field": True}})
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


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
def provider(fake_cli, tmp_path: Path) -> ClaudeCliSessionProvider:
    instance = ClaudeCliSessionProvider(
        tmp_path / "state", claude_command=fake_cli, stop_timeout=2.0
    )
    yield instance
    listed = instance.list_sessions()
    if isinstance(listed, Ok):
        for readout in listed.value:
            instance.stop(readout.session_id)


def _request(tmp_path: Path, session_id: str = "sess-1", **settings) -> StartRequest:
    return StartRequest(
        session_id=session_id,
        workspace=str(tmp_path / "workspaces" / session_id),
        role="worker",
        settings=settings,
    )


def _spawned(spawn_log: Path) -> list[dict]:
    if not spawn_log.exists():
        return []
    return [json.loads(line) for line in spawn_log.read_text(encoding="utf-8").splitlines()]


def _wait_for_spawns(spawn_log: Path, count: int, timeout: float = 10.0) -> list[dict]:
    """The fake writes its log after it starts executing, which is after
    ``Popen`` returns -- so arrival is waited for, never assumed."""

    deadline = time.monotonic() + timeout
    while True:
        spawned = _spawned(spawn_log)
        if len(spawned) >= count:
            return spawned
        assert time.monotonic() < deadline, f"saw {len(spawned)} spawns, wanted {count}"
        time.sleep(0.02)


def _recorded_generation(tmp_path: Path, session_id: str) -> int:
    record = json.loads(
        (tmp_path / "state" / session_id / "record.json").read_text(encoding="utf-8")
    )
    return record["generation"]


def _wait_for_state(provider, session_id: str, state: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while True:
        result = provider.read_state(session_id)
        if isinstance(result, Ok) and result.value.provider_state == state:
            return result.value
        assert time.monotonic() < deadline, f"never reached {state!r}: {result!r}"
        time.sleep(0.02)


def _wait_for_exit(provider, session_id: str, timeout: float = 10.0) -> None:
    session = provider._sessions[session_id]
    assert session.process is not None
    session.process.wait(timeout=timeout)


# --------------------------------------------------------------------------
# The identity: derived before the spawn, as a pure function
# --------------------------------------------------------------------------


def test_a_uuid_session_id_is_honoured_verbatim():
    chosen = "4C3A9A0E-D6E5-4D90-AEE0-0ED948DD8631"
    assert claude_session_uuid(chosen) == chosen.lower()


def test_a_non_uuid_session_id_derives_the_same_uuid_every_time():
    """Committable ahead of the process: no spawn is consulted to know it."""

    first = claude_session_uuid("item11-bound-session")
    assert first == claude_session_uuid("item11-bound-session")
    assert uuid.UUID(first).version == 5
    assert claude_session_uuid("another-session") != first


# --------------------------------------------------------------------------
# The capability probe (D-0010)
# --------------------------------------------------------------------------


def test_the_probe_reports_the_clis_own_version_and_every_capability(provider):
    result = provider.probe_capabilities()
    assert isinstance(result, Ok)
    report = result.value
    assert report.provider_version == FAKE_VERSION
    assert report.supported >= REQUIRED_CAPABILITIES
    # The raw version answer is in the report, which is where D-0010's record
    # of "the capability probe's raw output" travels.
    assert FAKE_VERSION in report.detail


def test_a_missing_flag_is_a_missing_capability_and_refuses_the_spawn(
    fake_cli, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_HELP_OMIT", "--resume")
    provider = ClaudeCliSessionProvider(tmp_path / "state", claude_command=fake_cli)
    result = provider.probe_capabilities()
    assert isinstance(result, Ok)
    assert "session.resume" in result.value.missing
    with pytest.raises(SpawnRefused) as refusal:
        provider.start(_request(tmp_path))
    assert "session.resume" in str(refusal.value)


def test_an_absent_cli_is_a_failure_that_refuses_the_spawn(tmp_path):
    provider = ClaudeCliSessionProvider(
        tmp_path / "state", claude_command=str(tmp_path / "no-such-claude")
    )
    result = provider.probe_capabilities()
    assert isinstance(result, Failure)
    assert result.kind is FailureKind.BACKEND_UNREACHABLE
    with pytest.raises(SpawnRefused):
        provider.start(_request(tmp_path))


# --------------------------------------------------------------------------
# start: the readout before the child has spoken (R4's reachable case)
# --------------------------------------------------------------------------


def test_a_fresh_start_is_could_not_observe_with_a_reason(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "silent")
    result = provider.start(_request(tmp_path))
    assert isinstance(result, Ok)
    readout = result.value
    assert readout.observation is Observation.COULD_NOT_OBSERVE
    assert readout.could_not_observe_reason
    assert readout.provider_state is None


def test_the_identity_is_durably_recorded_before_it_is_ever_read_back(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "silent")
    provider.start(_request(tmp_path))
    record = json.loads(
        (tmp_path / "state" / "sess-1" / "record.json").read_text(encoding="utf-8")
    )
    assert record["claude_session_uuid"] == claude_session_uuid("sess-1")
    assert record["pid"] is not None


def test_a_session_id_that_escapes_the_state_root_is_refused(provider, tmp_path):
    result = provider.start(
        StartRequest(session_id="../evil", workspace=str(tmp_path), role="worker")
    )
    assert isinstance(result, Failure)
    assert result.kind is FailureKind.REFUSED_BY_PROVIDER


def test_a_session_id_is_never_reused(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "silent")
    assert isinstance(provider.start(_request(tmp_path)), Ok)
    again = provider.start(_request(tmp_path))
    assert isinstance(again, Failure)
    assert again.kind is FailureKind.REFUSED_BY_PROVIDER


def test_unknown_settings_keys_belong_to_someone_else_and_are_ignored(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "silent")
    result = provider.start(_request(tmp_path, announce_after=3600, some_other_key="x"))
    assert isinstance(result, Ok)


def test_cli_args_from_settings_reach_the_spawn_verbatim(
    provider, tmp_path, spawn_log, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "silent")
    provider.start(
        _request(tmp_path, cli_args=["--settings", "/some/role.json", "--permission-mode", "plan"])
    )
    (spawned,) = _wait_for_spawns(spawn_log, 1)
    argv = spawned["argv"]
    assert argv[argv.index("--settings") + 1] == "/some/role.json"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--session-id") + 1] == claude_session_uuid("sess-1")
    assert spawned["cwd"] == str((tmp_path / "workspaces" / "sess-1").resolve())


@pytest.mark.parametrize(
    "settings",
    [
        {"cli_args": "--model haiku"},
        {"cli_args": ["--set\x00tings"]},
        {"prompt": ""},
        {"prompt": 42},
        {"resume_prompt": "   "},
    ],
    ids=["bare-string-args", "nul-in-args", "empty-prompt", "non-string-prompt", "blank-resume"],
)
def test_unusable_settings_are_refused_with_a_reason_before_any_spawn(
    provider, tmp_path, spawn_log, settings
):
    result = provider.start(_request(tmp_path, **settings))
    assert isinstance(result, Failure)
    assert result.kind is FailureKind.REFUSED_BY_PROVIDER
    assert result.detail.strip()
    assert _spawned(spawn_log) == []


def test_a_vetoed_workspace_creation_refuses_the_start(provider, tmp_path):
    class _Vetoer:
        def on_workspace_transition(self, transition):
            return WorkspaceDecision(WorkspaceVerdict.VETO, "unsaved artifacts present")

    provider.register_workspace_observer(_Vetoer())
    result = provider.start(_request(tmp_path))
    assert isinstance(result, Failure)
    assert "vetoed" in result.detail


# --------------------------------------------------------------------------
# read_state: the child's own words, and exit codes never as evidence
# --------------------------------------------------------------------------


def test_the_readout_carries_the_childs_own_terminal_word(provider, tmp_path):
    provider.start(_request(tmp_path))
    readout = _wait_for_state(provider, "sess-1", "completed")
    assert readout.observation is Observation.OBSERVED
    assert readout.provider_detail["is_error"] is False


def test_exit_zero_is_not_taken_as_evidence_of_success(
    provider, tmp_path, monkeypatch
):
    """i01 §3.4: a SIGINT'd run exits 0 with ``is_error: true``. The readout
    must carry the child's own word for that, not an invented success."""

    monkeypatch.setenv("FAKE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_TERMINAL_REASON", "aborted_streaming")
    monkeypatch.setenv("FAKE_EXIT", "0")
    provider.start(_request(tmp_path))
    _wait_for_exit(provider, "sess-1")
    readout = _wait_for_state(provider, "sess-1", "aborted_streaming")
    assert readout.provider_detail["is_error"] is True
    assert readout.provider_detail["returncode"] == 0


def test_unknown_event_types_are_carried_uninterpreted(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "events-then-hang")
    provider.start(_request(tmp_path))
    readout = _wait_for_state(provider, "sess-1", "unheard_of_event")
    assert readout.observation is Observation.OBSERVED


def test_a_complete_line_that_is_not_json_fails_loudly(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "garbage-then-hang")
    provider.start(_request(tmp_path))
    deadline = time.monotonic() + 10.0
    while True:
        result = provider.read_state("sess-1")
        if isinstance(result, Failure):
            break
        assert time.monotonic() < deadline, f"never failed loudly: {result!r}"
        time.sleep(0.02)
    assert result.kind is FailureKind.UNINTERPRETABLE_RESPONSE
    assert "not JSON" in result.detail


def test_a_result_that_names_no_outcome_fails_loudly(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_RESULT_BARE", "1")
    provider.start(_request(tmp_path))
    _wait_for_exit(provider, "sess-1")
    result = provider.read_state("sess-1")
    assert isinstance(result, Failure)
    assert result.kind is FailureKind.UNINTERPRETABLE_RESPONSE


def test_the_stderr_only_refusal_is_captured_and_surfaced(
    provider, tmp_path, monkeypatch
):
    """i01 §3.3: the ``already in use`` refusal exists only on stderr, with an
    empty stdout. The readout is the exit disposition with that stderr
    attached -- carried verbatim, never interpreted as a lock (U27/U38)."""

    monkeypatch.setenv("FAKE_MODE", "refuse-in-use")
    provider.start(_request(tmp_path))
    _wait_for_exit(provider, "sess-1")
    readout = _wait_for_state(provider, "sess-1", "exited-1")
    assert "already in use" in readout.provider_detail["stderr_tail"]


def test_an_unknown_session_is_a_typed_failure(provider):
    for verb in (provider.read_state, provider.stop, provider.resume):
        result = verb("never-started")
        assert isinstance(result, Failure)
        assert result.kind is FailureKind.UNKNOWN_SESSION


def test_zero_sessions_is_a_fact_not_a_failure(provider):
    assert provider.list_sessions() == Ok(())


# --------------------------------------------------------------------------
# Identity read-back: a mismatch is an incident, not a warning
# --------------------------------------------------------------------------


def test_a_wrong_identity_read_back_is_an_incident(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_REPORT_ID", str(uuid.uuid4()))
    provider.start(_request(tmp_path))
    _wait_for_exit(provider, "sess-1")
    result = provider.read_state("sess-1")
    assert isinstance(result, Failure)
    assert result.kind is FailureKind.UNINTERPRETABLE_RESPONSE
    assert "identity incident" in result.detail
    assert result.provider_detail["expected"] == claude_session_uuid("sess-1")


def test_an_identity_incident_survives_a_supervisor_restart(
    fake_cli, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_REPORT_ID", str(uuid.uuid4()))
    first = ClaudeCliSessionProvider(tmp_path / "state", claude_command=fake_cli)
    first.start(_request(tmp_path))
    first._sessions["sess-1"].process.wait(timeout=10)
    assert isinstance(first.read_state("sess-1"), Failure)

    # A new supervisor life over the same state root: the incident is in the
    # durable record, so the session still answers as impounded rather than
    # reading as healthy.
    second = ClaudeCliSessionProvider(tmp_path / "state", claude_command=fake_cli)
    result = second.read_state("sess-1")
    assert isinstance(result, Failure)
    assert "identity incident" in result.detail
    resumed = second.resume("sess-1")
    assert isinstance(resumed, Failure)
    assert "identity incident" in resumed.detail


# --------------------------------------------------------------------------
# stop: the process group, and the readout taken after the exit
# --------------------------------------------------------------------------


def test_stop_terminates_a_running_child_and_reports_what_is_left(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "silent")
    provider.start(_request(tmp_path))
    result = provider.stop("sess-1")
    assert isinstance(result, Ok)
    readout = result.value
    assert readout.observation is Observation.OBSERVED
    assert readout.provider_state.startswith("exited-")
    # The record and captured output stay on disk: the disposition of what
    # the child left behind is that it is kept, not swept.
    assert (tmp_path / "state" / "sess-1" / "record.json").exists()


def test_stop_of_an_already_exited_child_is_a_readout_not_an_error(
    provider, tmp_path
):
    provider.start(_request(tmp_path))
    _wait_for_exit(provider, "sess-1")
    result = provider.stop("sess-1")
    assert isinstance(result, Ok)
    assert result.value.provider_state == "completed"


# --------------------------------------------------------------------------
# resume: adopt-or-spawn, in the order that cannot mint a second writer
# --------------------------------------------------------------------------


def test_resume_of_a_live_child_adopts_it_and_spawns_nothing(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "silent")
    provider.start(_request(tmp_path))
    result = provider.resume("sess-1")
    assert isinstance(result, Ok)
    # A resume that spawned would have bumped the durable generation before
    # the spawn -- the record, not a race against the child's own log, is the
    # evidence nothing was spawned.
    assert _recorded_generation(tmp_path, "sess-1") == 0


def test_resume_of_an_exited_session_spawns_dash_dash_resume(
    provider, tmp_path, spawn_log
):
    provider.start(_request(tmp_path))
    _wait_for_exit(provider, "sess-1")
    result = provider.resume("sess-1")
    assert isinstance(result, Ok)
    spawned = _wait_for_spawns(spawn_log, 2)
    argv = spawned[1]["argv"]
    assert argv[argv.index("--resume") + 1] == claude_session_uuid("sess-1")
    # Never a fresh claim: U28 shows the dead session still holds it.
    assert "--session-id" not in argv
    _wait_for_state(provider, "sess-1", "completed")


def test_resume_persists_its_generation_so_the_next_life_reads_the_right_output(
    provider, tmp_path
):
    provider.start(_request(tmp_path))
    _wait_for_exit(provider, "sess-1")
    provider.resume("sess-1")
    record = json.loads(
        (tmp_path / "state" / "sess-1" / "record.json").read_text(encoding="utf-8")
    )
    assert record["generation"] == 1
    assert (tmp_path / "state" / "sess-1" / "events-001.jsonl").exists()


@pytest.mark.skipif(not IS_POSIX, reason="orphan liveness is resolved via POSIX signals")
def test_an_orphans_record_is_detected_by_the_next_supervisor_life(
    fake_cli, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "silent")
    first = ClaudeCliSessionProvider(tmp_path / "state", claude_command=fake_cli)
    first.start(_request(tmp_path, session_id="orphaned"))
    try:
        second = ClaudeCliSessionProvider(tmp_path / "state", claude_command=fake_cli)
        listed = second.list_sessions()
        assert isinstance(listed, Ok)
        assert [r.session_id for r in listed.value] == ["orphaned"]
    finally:
        first.stop("orphaned")


@pytest.mark.skipif(
    not HAS_PROC, reason="adoption requires confirming the pid's command line via /proc"
)
def test_a_live_orphan_is_adopted_not_resumed_around(fake_cli, tmp_path, monkeypatch):
    """The reclaim order issue #17 fixes: the surviving process is resolved
    first, because a ``--resume`` issued while it runs is the second live
    writer the provider will not refuse (U32)."""

    monkeypatch.setenv("FAKE_MODE", "silent")
    first = ClaudeCliSessionProvider(tmp_path / "state", claude_command=fake_cli)
    first.start(_request(tmp_path, session_id="orphaned"))
    try:
        second = ClaudeCliSessionProvider(
            tmp_path / "state", claude_command=fake_cli, stop_timeout=2.0
        )
        result = second.resume("orphaned")
        assert isinstance(result, Ok)
        assert _recorded_generation(tmp_path, "orphaned") == 0, (
            "resume spawned next to a live orphan"
        )
        # And the adopting life can stop what it adopted.
        stopped = second.stop("orphaned")
        assert isinstance(stopped, Ok)
    finally:
        first.stop("orphaned")


@pytest.mark.skipif(
    not HAS_PROC, reason="the pid-reuse guard reads the pid's command line via /proc"
)
def test_a_recycled_pid_is_never_trusted_signalled_or_adopted(
    fake_cli, tmp_path, monkeypatch
):
    """A record whose pid now names a stranger (here: this very test process)
    must be read as "the child is gone" -- the stranger is left untouched and
    the session is re-entered via ``--resume`` (i02 §3.3)."""

    log = tmp_path / "spawns.jsonl"
    monkeypatch.setenv("FAKE_SPAWN_LOG", str(log))
    provider = ClaudeCliSessionProvider(tmp_path / "state", claude_command=fake_cli)
    session_dir = tmp_path / "state" / "stale"
    session_dir.mkdir(parents=True)
    workspace = tmp_path / "workspaces" / "stale"
    workspace.mkdir(parents=True)
    record = {
        "session_id": "stale",
        "claude_session_uuid": claude_session_uuid("stale"),
        "workspace": str(workspace),
        "role": "worker",
        "resume_prompt": "continue",
        "cli_args": [],
        "generation": 0,
        "argv": ["claude", "-p", "x"],
        "pid": os.getpid(),
        "pgid": os.getpid(),
        "incident": None,
    }
    (session_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")

    result = provider.resume("stale")
    assert isinstance(result, Ok), f"resume refused a reclaimable session: {result!r}"
    (spawned,) = _wait_for_spawns(log, 1)
    assert spawned["argv"][spawned["argv"].index("--resume") + 1] == claude_session_uuid("stale")
    _wait_for_state(provider, "stale", "completed")


# --------------------------------------------------------------------------
# The stated assumptions: mechanically present, next to the code they bind
# --------------------------------------------------------------------------

S2_SOURCE = Path(s2.__file__).read_text(encoding="utf-8")


def test_the_refusal_is_stated_not_to_be_a_lock_next_to_the_spawn_path():
    """Issue #17: 'State this assumption in the code, next to the spawn
    path.' Checked mechanically so deleting the sentence fails the build."""

    assert "never relied on as a lock" in S2_SOURCE
    assert "U27" in S2_SOURCE
    start_doc = ClaudeCliSessionProvider._start_session.__doc__ or ""
    assert "never relied on as a lock" in start_doc


def test_resume_says_it_is_unguarded_and_names_the_lease_as_the_gate():
    resume_doc = ClaudeCliSessionProvider.resume.__doc__ or ""
    assert "U32" in resume_doc
    assert "lease" in resume_doc


def test_the_provider_imports_nothing_from_the_control_plane():
    """D-0009's contract separation, asserted on the module rather than
    trusted to review: the provider that cannot name the lease cannot borrow
    it, and cannot be borrowed by it."""

    assert "control_plane" not in S2_SOURCE


def test_the_cli_version_written_against_is_recorded():
    assert "2.1.234" in s2.CLI_VERSION_WRITTEN_AGAINST or "2.1.237" in s2.CLI_VERSION_WRITTEN_AGAINST
