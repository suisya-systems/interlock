"""S2 -- the C2 ``SessionProvider``, over Interlock-supervised ``claude -p``.

The implementation of the provisional S1 interface
(:mod:`claude_org_runtime.session.provider`) against the provider the
2026-08-18 ruling selected: **C2 -- Interlock-supervised ``claude -p``
subprocesses** (D-0025 part 2, D-0027). Interlock spawns the worker as a child
process it owns outright, and Interlock's own process supervision *is* the
session lifecycle. Throwaway under D-0026, and provider-specific by design.

What this module knows about the backend it drives, and where that knowledge
comes from (``investigation/i01-supervisor-probe.md``,
``investigation/i02-conversation-probe.md``,
``investigation/pre-spawn-fence-search.md``; CLI ``2.1.234`` probed,
``2.1.237`` smoke-checked at implementation time -- see
:data:`CLI_VERSION_WRITTEN_AGAINST`):

* **Exit 0 is never taken as evidence of anything** (i01 §3.4). A SIGINT'd run
  exits 0 with ``is_error: true``; a refused ``--session-id`` exits 1 with an
  *empty stdout*. The verdict, such as one exists, lives in the child's own
  structured output, so the readout here is built from the stream-json events
  the child wrote and the process disposition is only ever carried as detail.
* **The identity the child actually received is read back and reconciled**
  with the one this provider committed before the spawn. Under U27 two
  processes both exit 0 reporting the *same* ``session_id`` while only one of
  them can be the run's writer, so agreement of ids is checked positively and
  a disagreement is an **incident** -- persisted, and answered as a typed
  failure on every subsequent read -- not a warning.
* **stderr is captured separately and surfaced.** The ``already in use``
  refusal appears on stderr with stdout completely empty (i01 §3.3); a
  supervisor that read only stdout would be blind in the one case that
  matters most.
* **Each child gets its own process group** and is stopped by signalling the
  group, because the CLI does not reap MCP-server children of its own and a
  pid-targeted signal leaves them running (i01 §3.5, hazard H1).

Two assumptions this module states because the probes proved them, and
because the acceptance criteria of issue ``#17`` require them stated next to
the code they constrain:

**The provider's ``already in use`` refusal is never relied on as a lock**
(U27: a ~2-3 s admission window admitted two claimants to one id, both wrote
one transcript; U34: the width is a one-machine figure, not a constant; U38:
the claim is a file-existence check that deleting the transcript releases).
Where the refusal happens it is carried verbatim as the child's own outcome --
defence in depth, never exclusion. Every protected write goes through the
control plane's fencing token (issue ``#13``), and this module is written to
be correct with the refusal assumed absent.

**``--resume`` is treated as unguarded** (U32: two concurrent resumes of one
session were both admitted, simultaneously and at a 5 s stagger). Nothing in
the provider stops a second resume of the same session, and nothing here
pretends to either: :meth:`ClaudeCliSessionProvider.resume` performs identity
read-back and refusal only. Re-entry is gated by the control plane's lease --
which this module deliberately does not import (D-0009's contract separation);
the lease-before-resume orchestration and the commit-before-spawn crash-window
proof are issue ``#18``'s, not this module's.

What is *not* here is as deliberate as in the stub: no verb sends anything to
a running child. The one prompt a spawn carries is the argument ``claude -p``
requires to create or re-enter a session at all -- it is part of the spawn,
supplied through the opaque per-role ``settings``, and no method exists to
write to a session after it is running. Delivery is ``MessageBus``'s under
D-0009 and is built as S8.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence, Union

from .provider import (
    REQUIRED_CAPABILITIES,
    CapabilityReport,
    Failure,
    FailureKind,
    Observation,
    Ok,
    ProviderResult,
    SessionProvider,
    SessionReadout,
    SpawnRefused,
    StartRequest,
    WorkspaceTransition,
    WorkspaceVerdict,
)

#: The CLI this implementation was written against, recorded per issue ``#17``
#: and D-0010. The supervision and identity findings were probed on
#: ``2.1.234`` (``investigation/i01-supervisor-probe.md`` §2); the flag
#: surface, ``--session-id`` honouring and ``--resume`` identity read-back
#: were re-confirmed by smoke run on ``2.1.237`` while this module was
#: written. The capability probe records the *running* build's own raw answer
#: at probe time; this constant records what the code was written to.
CLI_VERSION_WRITTEN_AGAINST = "2.1.237 (Claude Code); probes ran on 2.1.234"

#: Namespace for deriving a ``claude``-acceptable session UUID from a
#: caller-chosen S1 session id. Fixed, so the derivation is a pure function of
#: the id alone: anyone -- the caller committing a binding row before the
#: spawn, or a supervisor restarted with nothing but the id -- derives the
#: same UUID without asking a process that may no longer exist.
SESSION_UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/suisya-systems/interlock/session-uuid"
)


def claude_session_uuid(session_id: str) -> str:
    """The ``--session-id`` value for one S1 session id, as a pure function.

    S1 lets the caller choose any non-empty string; ``claude`` accepts only a
    UUID. An id that already is a UUID is honoured verbatim (canonicalised),
    so a caller that wants to name the CLI session directly can; anything else
    is mapped through :data:`SESSION_UUID_NAMESPACE` deterministically. Being
    a pure function is the point: the identity is committable ahead of the
    process (the property C2 was chosen for), because it never depends on
    anything the spawn will say.
    """

    try:
        return str(uuid.UUID(session_id))
    except ValueError:
        return str(uuid.uuid5(SESSION_UUID_NAMESPACE, session_id))


# --------------------------------------------------------------------------
# The settings this provider reads, and the flags it renders
# --------------------------------------------------------------------------

#: ``settings`` key: the prompt the started session's one turn runs. A
#: ``claude -p`` process is one turn; without a prompt there is no process, so
#: a spawn without one uses :data:`DEFAULT_PROMPT`. This is spawn
#: configuration, not delivery: no verb can write to a session that is
#: already running (D-0009).
PROMPT_SETTING = "prompt"

#: ``settings`` key: the prompt a later :meth:`ClaudeCliSessionProvider.resume`
#: re-enters the session with. Persisted at start, because resume takes only a
#: session id (S1) and may run in a supervisor that has nothing else.
RESUME_PROMPT_SETTING = "resume_prompt"

#: ``settings`` key: extra CLI arguments appended verbatim to every spawn of
#: this session -- the seam through which per-role configuration from S10
#: arrives (``--settings <path> --permission-mode <mode>``, a ``--model``,
#: ...) without this module importing the layer that rendered it.
CLI_ARGS_SETTING = "cli_args"

DEFAULT_PROMPT = (
    "You are a supervised Interlock worker session. Confirm you are running "
    "and await instructions delivered separately."
)
DEFAULT_RESUME_PROMPT = (
    "The supervisor re-entered this session after a restart. Report the state "
    "of your current task and continue."
)

#: The provider's own word for the transition it makes when a start is asked
#: for a workspace that does not exist yet (gate item 7's surface). Same word
#: as the stub's on purpose: the transition is the same.
CREATE_WORKSPACE = "create-workspace"

#: The characters a session id may use once it has to name a state directory.
#: Same allow-list as the stub, for the same reason: an id that escapes the
#: state root would let a caller pick which directory this provider rewrites.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9._-]+")

#: The help-text flag whose presence renders each CLI-dependent capability.
#: ``session.list``, ``session.read-state`` and ``session.stop`` do not appear
#: here because under C2 they are rendered by Interlock's own supervision of
#: children it spawned -- the probe for those is that the CLI exists and
#: identifies itself at all.
_CAPABILITY_FLAGS: Mapping[str, tuple[str, ...]] = {
    "session.start": ("--print", "--session-id"),
    "session.resume": ("--resume",),
    "session.structured-readout": ("--output-format", "--verbose"),
}

#: Flags this provider renders itself. A per-role ``cli_args`` carrying one of
#: these would be appended *after* the provider's own and could override the
#: committed identity or the structured-output invocation -- ``--session-id``
#: from a role configuration would start writing another conversation before
#: identity read-back notices. Refused at settings validation, before any
#: spawn. ``--continue``/``-c`` are on the list although this provider never
#: passes them: they re-enter whatever conversation is most recent, which is
#: an identity chosen by nobody.
_PROVIDER_OWNED_FLAGS = (
    "-p",
    "--print",
    "-r",
    "--resume",
    "-c",
    "--continue",
    "--session-id",
    "--output-format",
    "--verbose",
)

#: How much of a session's captured stderr a readout carries. A tail, because
#: the messages that matter (the refusal, a fatal startup error) are last, and
#: a readout that embedded megabytes of stderr would itself be unreadable.
_STDERR_TAIL_CHARS = 2000


def _is_one_path_component(session_id: str) -> bool:
    return bool(_SAFE_SESSION_ID.fullmatch(session_id)) and session_id not in {".", ".."}


# --------------------------------------------------------------------------
# The durable per-session record
# --------------------------------------------------------------------------

#: Name of the record file inside a session's state directory. The record is
#: what makes an orphan *detectable* after a supervisor restart (issue #17's
#: reclaim criterion): the CLI has no public surface that lists ``-p``
#: children (i01 §3.6), so the only roster that can exist is the one this
#: provider writes itself.
_RECORD_NAME = "record.json"


@dataclass(frozen=True)
class _SessionRecord:
    """Everything about one session that must survive this process.

    Written **before** the child is spawned (with :attr:`pid` still unset) and
    updated with the pid immediately after, so the identity is on disk ahead
    of the process that will carry it. This is the mechanical half of
    commit-before-spawn; *proving* the crash window around it is issue #18's.
    """

    session_id: str
    claude_session_uuid: str
    workspace: str
    role: str
    resume_prompt: str
    cli_args: tuple[str, ...]
    generation: int
    argv: tuple[str, ...]
    pid: int | None = None
    #: On POSIX the child is its own session leader (``start_new_session``),
    #: so its process group id equals its pid; recorded separately anyway so a
    #: reader of the file does not need to know that.
    pgid: int | None = None
    #: A persisted identity incident. Once set it never clears: the one thing
    #: worse than a session whose identity broke is one whose identity broke
    #: and then read as healthy after a restart.
    incident: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "session_id": self.session_id,
                "claude_session_uuid": self.claude_session_uuid,
                "workspace": self.workspace,
                "role": self.role,
                "resume_prompt": self.resume_prompt,
                "cli_args": list(self.cli_args),
                "generation": self.generation,
                "argv": list(self.argv),
                "pid": self.pid,
                "pgid": self.pgid,
                "incident": self.incident,
            },
            indent=2,
        )

    @staticmethod
    def from_json(text: str) -> "_SessionRecord":
        """Parse, and validate the shape while parsing.

        Every field is checked here rather than trusted, because a record
        that decodes but carries the wrong types would not fail until the
        field is *used* -- a ``generation`` of ``null`` blowing up inside a
        path format, say -- which is a crash where the broken-record readout
        should have been. Raises :class:`ValueError` on any wrong shape, so
        every caller's broken-record handling covers it.
        """

        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError(f"a session record must be an object, got {type(raw).__name__}")

        def _text(key: str) -> str:
            value = raw.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"record field {key!r} must be a non-empty string, got {value!r}")
            return value

        def _strings(key: str) -> tuple[str, ...]:
            value = raw.get(key)
            if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
                raise ValueError(f"record field {key!r} must be a list of strings, got {value!r}")
            return tuple(value)

        def _optional_int(key: str) -> int | None:
            value = raw.get(key)
            if value is not None and not isinstance(value, int):
                raise ValueError(f"record field {key!r} must be an integer or null, got {value!r}")
            return value

        generation = raw.get("generation")
        if not isinstance(generation, int) or generation < 0:
            raise ValueError(
                f"record field 'generation' must be a non-negative integer, got {generation!r}"
            )
        incident = raw.get("incident")
        if incident is not None and not isinstance(incident, str):
            raise ValueError(f"record field 'incident' must be a string or null, got {incident!r}")
        return _SessionRecord(
            session_id=_text("session_id"),
            claude_session_uuid=_text("claude_session_uuid"),
            workspace=_text("workspace"),
            role=_text("role"),
            resume_prompt=_text("resume_prompt"),
            cli_args=_strings("cli_args"),
            generation=generation,
            argv=_strings("argv"),
            pid=_optional_int("pid"),
            pgid=_optional_int("pgid"),
            incident=incident,
        )


@dataclass
class _Supervised:
    """One session as this provider currently holds it.

    :attr:`process` is the ``Popen`` of a child *this* provider instance
    spawned, or ``None`` for a session known only through its durable record
    -- an orphan of an earlier supervisor life, observed through the record's
    pid and the files the child writes.
    """

    record: _SessionRecord
    process: subprocess.Popen[bytes] | None = None
    provider_detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _BrokenRecord:
    """A session whose durable record exists but cannot be read.

    Not "no session" (a record is right there) and not a readable one either.
    The verbs split on it the same way they split on
    :class:`_Uninterpretable`: the reading verbs report the session as
    explicitly unobservable with this reason (R4 -- it must not vanish from
    the roster), and the acting verbs refuse, because signalling or resuming
    a child whose identity cannot be read is acting on a guess.
    """

    session_id: str
    reason: str


@dataclass(frozen=True)
class _Uninterpretable:
    """A session whose own output could not be read as a readout.

    Not a :class:`SessionReadout` and not a :class:`Failure`, because which of
    the two it becomes depends on the verb: ``read_state`` owes the caller a
    loud typed failure, while ``list_sessions`` owes a roster in which this
    session still appears -- as explicitly unobservable, with this reason --
    rather than a roster that failed wholesale because one child wrote
    garbage.
    """

    detail: str
    provider_detail: Mapping[str, Any] = field(default_factory=dict)


_ReadoutOrProblem = Union[SessionReadout, _Uninterpretable]


class ClaudeCliSessionProvider(SessionProvider):
    """The five verbs and the capability probe, over ``claude -p`` children.

    Args:
        state_root: directory for the per-session durable records and captured
            output. Required and never defaulted, exactly as for the stub: two
            providers silently sharing a directory would adopt each other's
            children.
        claude_command: the CLI to run -- a single executable name/path, or a
            full command prefix (so a test can supply
            ``(sys.executable, fake_cli)`` without pretending a script is
            directly executable on every platform).
        base_cli_args: arguments appended to **every** spawn this provider
            makes, before the per-session :data:`CLI_ARGS_SETTING`. The seam
            for provider-wide choices (a pinned ``--model``, say) that are not
            per-role configuration.
        stop_timeout: seconds :meth:`stop` waits after a terminate before
            escalating to a kill.
        probe_timeout: the bound on each capability-probe subprocess. Its own
            knob because the two costs are unrelated: a probe is a CLI
            answering ``--version``, whose slowness says nothing about how
            long a terminated child should be given to die.
    """

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        claude_command: str | Sequence[str] = "claude",
        base_cli_args: Sequence[str] = (),
        stop_timeout: float = 5.0,
        probe_timeout: float = 10.0,
    ) -> None:
        super().__init__()
        # Resolved for the same reason the stub resolves it: children run with
        # their workspace as cwd, and a relative state root would name a
        # different directory to every reader.
        self._state_root = Path(state_root).resolve()
        if isinstance(claude_command, (str, os.PathLike)):
            self._command: tuple[str, ...] = (str(claude_command),)
        else:
            self._command = tuple(str(part) for part in claude_command)
            if not self._command:
                raise ValueError("claude_command must name at least one argument")
        self._base_cli_args = tuple(str(part) for part in base_cli_args)
        self._stop_timeout = stop_timeout
        self._probe_timeout = probe_timeout
        self._sessions: dict[str, _Supervised] = {}

    # -- the capability probe (D-0010) -------------------------------------

    def probe_capabilities(self) -> ProviderResult[CapabilityReport]:
        """Ask the CLI's public surface what it is and which flags it carries.

        Two invocations, both public and documented: ``--version`` identifies
        the build, and ``--help`` is scanned for the flags each capability is
        rendered with (:data:`_CAPABILITY_FLAGS`). There is no ``capabilities``
        subcommand to ask -- ``claude capabilities`` runs as a *billed model
        prompt* (i01 §2) -- so the help text is the honest surface. The raw
        version answer is carried in the report's detail so the record D-0010
        asks for exists wherever the report goes.
        """

        version_run = self._run_probe("--version")
        if isinstance(version_run, Failure):
            return version_run
        version = version_run.stdout.decode("utf-8", "replace").strip()
        if not version:
            return Failure(
                FailureKind.UNINTERPRETABLE_RESPONSE,
                f"{self._command[0]!r} answered the version probe with nothing",
            )

        help_run = self._run_probe("--help")
        if isinstance(help_run, Failure):
            return help_run
        help_text = help_run.stdout.decode("utf-8", "replace")

        supported = set(REQUIRED_CAPABILITIES) - set(_CAPABILITY_FLAGS)
        missing_flags: dict[str, list[str]] = {}
        for capability, flags in _CAPABILITY_FLAGS.items():
            absent = [flag for flag in flags if flag not in help_text]
            if absent:
                missing_flags[capability] = absent
            else:
                supported.add(capability)
        evidence = self._record_probe_evidence(version, help_text)
        return Ok(
            CapabilityReport(
                provider_version=version,
                supported=frozenset(supported),
                detail=(
                    f"version probe answered {version!r}; help text "
                    f"{'is missing ' + repr(missing_flags) if missing_flags else 'carries every required flag'}"
                    f"; raw probe output {evidence}"
                    f"; written against {CLI_VERSION_WRITTEN_AGAINST}"
                ),
            )
        )

    def _record_probe_evidence(self, version: str, help_text: str) -> str:
        """Keep the probe's raw answers, per D-0010's record requirement.

        The ``--help`` text is pages long, so the report's ``detail`` carries a
        pointer rather than the pages; the file under the state root is the
        durable record. Failing to write it degrades the record, not the probe
        -- and says so in the pointer instead of silently pointing at nothing.
        """

        path = self._state_root / "probe-evidence.txt"
        try:
            self._state_root.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".part")
            partial.write_text(
                f"$ {' '.join(self._command)} --version\n{version}\n\n"
                f"$ {' '.join(self._command)} --help\n{help_text}",
                encoding="utf-8",
            )
            os.replace(partial, path)
        except OSError as exc:
            return f"could not be recorded at {path}: {exc}"
        return f"recorded at {path}"

    def _run_probe(self, flag: str) -> "subprocess.CompletedProcess[bytes] | Failure":
        try:
            completed = subprocess.run(
                [*self._command, flag],
                capture_output=True,
                timeout=self._probe_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Failure(
                FailureKind.TIMED_OUT,
                f"{self._command[0]!r} did not answer {flag} within "
                f"{self._probe_timeout}s",
            )
        except OSError as exc:
            return Failure(
                FailureKind.BACKEND_UNREACHABLE,
                f"{self._command[0]!r} could not be executed: {exc}",
                {"errno": exc.errno},
            )
        if completed.returncode != 0:
            return Failure(
                FailureKind.BACKEND_UNREACHABLE,
                f"{self._command[0]!r} exited {completed.returncode} for {flag}",
                {"stderr": completed.stderr.decode("utf-8", "replace")},
            )
        return completed

    # -- the five verbs (D-0009) -------------------------------------------

    def _start_session(self, request: StartRequest) -> ProviderResult[SessionReadout]:
        """Spawn one ``claude -p`` child. Called by ``start`` after the gate.

        The identity is derived and durably recorded **before** the spawn
        (:func:`claude_session_uuid`, :class:`_SessionRecord`), and read back
        from the child's own structured output afterwards -- never inferred
        from the exit code.

        The CLI's own ``Session ID ... is already in use`` refusal, when it
        happens, arrives on the child's stderr with exit 1 and is carried
        verbatim in the readout. **It is never relied on as a lock**: U27
        measured a multi-second admission window inside which two claimants of
        one id were both admitted, and U38 shows the claim itself is a
        transcript-file existence check. Exclusion, where it exists, is the
        control plane's fencing token -- this code is written to be correct
        with the CLI's refusal assumed absent.
        """

        if not _is_one_path_component(request.session_id):
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"session id {request.session_id!r} is not usable as a single "
                "path component; this provider names a state directory after "
                "the session and will not let an id reach outside its state root",
            )
        if request.session_id in self._sessions or self._record_path(request.session_id).exists():
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"session {request.session_id!r} already exists in this "
                "provider's state root; this provider does not reuse a "
                "session id",
            )

        settings_or_refusal = self._read_settings(request)
        if isinstance(settings_or_refusal, Failure):
            return settings_or_refusal
        prompt, resume_prompt, cli_args = settings_or_refusal

        try:
            # Resolved before it is recorded: the record outlives this
            # process, and a relative path in it would name a different
            # directory to every future supervisor's working directory.
            workspace = Path(request.workspace).resolve()
            workspace_exists = workspace.is_dir()
        except (ValueError, OSError) as exc:
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"the workspace configured for session {request.session_id!r} "
                f"is not a usable path: {exc}",
            )
        if not workspace_exists:
            refusal = self._create_workspace(request, workspace)
            if refusal is not None:
                return refusal

        session_uuid = claude_session_uuid(request.session_id)
        holder = self._holder_of_uuid(session_uuid)
        if holder is not None and holder != request.session_id:
            # Reachable only by deliberately naming one identity twice -- an
            # id that *is* the UUID another id derives to -- but two S1
            # sessions sharing one provider identity is the U27 shape made at
            # home, and refusing it costs one directory scan per start.
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"session id {request.session_id!r} derives provider identity "
                f"{session_uuid}, which session {holder!r} already holds; two "
                "sessions must not share one identity",
            )
        record = _SessionRecord(
            session_id=request.session_id,
            claude_session_uuid=session_uuid,
            workspace=str(workspace),
            role=request.role,
            resume_prompt=resume_prompt,
            cli_args=cli_args,
            generation=0,
            argv=(
                *self._command,
                "-p",
                prompt,
                "--output-format",
                "stream-json",
                "--verbose",
                "--session-id",
                session_uuid,
                *self._base_cli_args,
                *cli_args,
            ),
        )
        return self._spawn(record, fresh=True)

    def list_sessions(self) -> ProviderResult[Sequence[SessionReadout]]:
        """Every session this provider supervises or holds a durable record of.

        Includes orphans: a record written by an earlier supervisor life names
        a session this instance never spawned, and leaving it off the roster
        would make the one copy of the truth about it invisible exactly when
        it matters (i01 §3.6: the CLI itself lists nothing). A session whose
        own output cannot be interpreted appears as explicitly unobservable
        with the reason -- one broken child must not fail the whole roster,
        and must not vanish from it either (R4).
        """

        try:
            discovered = self._discover_records()
        except OSError as exc:
            return Failure(
                FailureKind.BACKEND_UNREACHABLE,
                f"the state root {self._state_root} could not be read: {exc}",
                {"errno": getattr(exc, "errno", None)},
            )
        readouts = []
        for session in discovered:
            if isinstance(session, _BrokenRecord):
                readouts.append(
                    SessionReadout(
                        session_id=session.session_id,
                        observation=Observation.COULD_NOT_OBSERVE,
                        could_not_observe_reason=session.reason,
                    )
                )
                continue
            outcome = self._readout(session)
            if isinstance(outcome, _Uninterpretable):
                readouts.append(
                    SessionReadout(
                        session_id=session.record.session_id,
                        observation=Observation.COULD_NOT_OBSERVE,
                        could_not_observe_reason=outcome.detail,
                        provider_detail=outcome.provider_detail,
                    )
                )
            else:
                readouts.append(outcome)
        return Ok(tuple(readouts))

    def read_state(self, session_id: str) -> ProviderResult[SessionReadout]:
        """The session's state from its own structured output, or a loud failure.

        Tolerant of what it can be (unknown event types, unknown fields,
        events that have not arrived yet) and loud about what it cannot: a
        complete line that is not JSON, an init event that names no identity,
        or an identity that disagrees with the one committed before the spawn
        all come back as typed failures with the evidence attached, never as
        an empty or invented readout (R4).
        """

        session = self._find(session_id)
        if session is None:
            return Failure(
                FailureKind.UNKNOWN_SESSION,
                f"this provider holds no session {session_id!r} and its state "
                "root holds no record of one",
            )
        if isinstance(session, _BrokenRecord):
            # The session exists -- its record is right there -- but cannot be
            # read, which per S1 is a readout of "could not observe" with the
            # reason, not a failed call (R4).
            return Ok(
                SessionReadout(
                    session_id=session_id,
                    observation=Observation.COULD_NOT_OBSERVE,
                    could_not_observe_reason=session.reason,
                )
            )
        outcome = self._readout(session)
        if isinstance(outcome, _Uninterpretable):
            return Failure(
                FailureKind.UNINTERPRETABLE_RESPONSE, outcome.detail, outcome.provider_detail
            )
        return Ok(outcome)

    def stop(self, session_id: str) -> ProviderResult[SessionReadout]:
        """Signal the child's process group, confirm the exit, then report.

        The whole group (i01 §3.5): the CLI leaves MCP-server children of its
        own unreaped, and a pid-targeted signal would orphan them. The readout
        is taken after the exit is confirmed rather than assumed from the
        signal, and everything the session left behind -- its record, its
        captured output -- stays on disk under the state root as the
        disposition of the stop.
        """

        session = self._find(session_id)
        if session is None:
            return Failure(
                FailureKind.UNKNOWN_SESSION,
                f"this provider holds no session {session_id!r} and its state "
                "root holds no record of one",
            )
        if isinstance(session, _BrokenRecord):
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"refusing to stop session {session_id!r}: {session.reason} "
                "-- without a readable record there is no pid or process "
                "group this provider can be sure is the session's to signal",
            )
        refusal = self._terminate(session)
        if refusal is not None:
            return refusal
        outcome = self._readout(session)
        if isinstance(outcome, _Uninterpretable):
            return Failure(
                FailureKind.UNINTERPRETABLE_RESPONSE, outcome.detail, outcome.provider_detail
            )
        return Ok(outcome)

    def resume(self, session_id: str) -> ProviderResult[SessionReadout]:
        """Re-enter one session: adopt its live child, or spawn ``--resume``.

        **Identity read-back and refusal only.** ``--resume`` is unguarded --
        U32 admitted two concurrent resumes of one session, simultaneously and
        staggered -- so nothing here can make re-entry exclusive and nothing
        here pretends to: the single-writer property comes from the control
        plane's lease, which issue ``#18`` orchestrates *around* this call and
        which this module deliberately cannot import (D-0009).

        The reclaim order is the one issue ``#17`` fixes, because inverting it
        creates the second live writer U32 will not refuse:

        1. **Resolve the surviving process first.** A recorded child that is
           still alive -- confirmed as ours by its command line carrying this
           session's UUID, so a recycled pid is never trusted (i02 §3.3) --
           is *adopted*, not resumed around and not restarted.
        2. A recorded child that is gone has its exit **confirmed** before
           anything else happens.
        3. Only then is ``--resume`` spawned. Never a fresh ``--session-id``
           claim: U28 shows the dead session still holds the claim, the
           re-claim is refused, and a supervisor that read that refusal as
           fatal would fail to recover its own worker.
        """

        session = self._find(session_id)
        if session is None:
            return Failure(
                FailureKind.UNKNOWN_SESSION,
                f"this provider holds no session {session_id!r} and its state "
                "root holds no record of one",
            )
        if isinstance(session, _BrokenRecord):
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"refusing to resume session {session_id!r}: {session.reason} "
                "-- without a readable record neither the surviving process "
                "nor the identity to resume can be resolved, and resuming on "
                "a guess is how a second live writer is minted (U32)",
            )
        if session.record.incident is not None:
            return Failure(
                FailureKind.UNINTERPRETABLE_RESPONSE,
                f"identity incident: {session.record.incident}",
                {"session_id": session_id},
            )

        liveness = self._child_liveness(session)
        if isinstance(liveness, Failure):
            return liveness
        if liveness:
            # Step 1: the surviving process, adopted. ``_find`` already put an
            # orphan's record into the table; a live child of our own is
            # simply still ours. Either way the session is re-entered by
            # reading it, not by spawning a second writer next to it.
            outcome = self._readout(session)
            if isinstance(outcome, _Uninterpretable):
                return Failure(
                    FailureKind.UNINTERPRETABLE_RESPONSE,
                    outcome.detail,
                    outcome.provider_detail,
                )
            return Ok(outcome)

        # Step 2 is complete here: ``_child_liveness`` returned False only for
        # an exit it confirmed (a reaped child of ours, a recorded pid that no
        # longer exists, or a recycled pid whose command line proves it is a
        # stranger -- which is left untouched).
        try:
            self.require_spawnable()
        except SpawnRefused as exc:
            return Failure(
                FailureKind.INCOMPATIBLE_PROVIDER,
                f"resuming {session_id!r} would spawn a child, and the spawn "
                f"precondition refused it (D-0010): {exc}",
            )
        record = replace(
            session.record,
            generation=session.record.generation + 1,
            argv=(
                *self._command,
                "--resume",
                session.record.claude_session_uuid,
                "-p",
                session.record.resume_prompt,
                "--output-format",
                "stream-json",
                "--verbose",
                *self._base_cli_args,
                *session.record.cli_args,
            ),
            pid=None,
            pgid=None,
        )
        return self._spawn(record, fresh=False)

    # -- spawning and its record -------------------------------------------

    def _spawn(self, record: _SessionRecord, *, fresh: bool) -> ProviderResult[SessionReadout]:
        """Commit the record, start the child, read the identity back later.

        Shared by the start and resume paths so that both write the durable
        record *before* the process exists. (The crash-window proof around
        this ordering is issue #18's; what this module guarantees is only the
        mechanical order of its own writes.)
        """

        directory = self._session_dir(record.session_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._write_record(record)
        except OSError as exc:
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"the state for session {record.session_id!r} could not be "
                f"prepared under {self._state_root}: {exc}",
                {"errno": exc.errno},
            )

        events_path = self._events_path(record.session_id, record.generation)
        stderr_path = self._stderr_path(record.session_id, record.generation)
        popen_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            # Its own session, hence its own process group: the group is what
            # ``stop`` signals, because the CLI does not reap MCP children of
            # its own and a pid-targeted signal leaves them running (H1).
            popen_kwargs["start_new_session"] = True
        else:  # pragma: no cover - exercised only on Windows
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        try:
            with open(events_path, "wb") as events_file, open(
                stderr_path, "wb"
            ) as stderr_file:
                process = subprocess.Popen(
                    list(record.argv),
                    cwd=record.workspace,
                    stdin=subprocess.DEVNULL,
                    stdout=events_file,
                    stderr=stderr_file,
                    **popen_kwargs,
                )
        except OSError as exc:
            failure = Failure(
                FailureKind.BACKEND_UNREACHABLE,
                f"could not spawn a child for session {record.session_id!r}: {exc}",
                {"argv": list(record.argv), "errno": exc.errno},
            )
            if fresh:
                # A session that never had a process is not a session; leaving
                # its half-record behind would refuse the id forever and show
                # a phantom orphan on every roster.
                self._remove_session_dir(record.session_id)
            return failure

        # With ``start_new_session`` the child is its own group leader, so its
        # pgid is its pid by construction -- recorded without asking the
        # operating system, which could only race the child's exit.
        record = replace(
            record,
            pid=process.pid,
            pgid=process.pid if os.name == "posix" else None,
        )
        try:
            self._write_record(record)
        except OSError as exc:
            # The child is running; a record that still carries no pid is
            # degraded but honest, and killing a healthy child over it would
            # be the worse answer. The failure is surfaced on the readout
            # instead of silently dropped.
            detail: Mapping[str, Any] = {"record_update_failed": str(exc)}
        else:
            detail = {}
        session = _Supervised(
            record=record,
            process=process,
            provider_detail={"pid": process.pid, "generation": record.generation, **detail},
        )
        self._sessions[record.session_id] = session
        outcome = self._readout(session)
        if isinstance(outcome, _Uninterpretable):
            return Failure(
                FailureKind.UNINTERPRETABLE_RESPONSE, outcome.detail, outcome.provider_detail
            )
        return Ok(outcome)

    def _read_settings(
        self, request: StartRequest
    ) -> "tuple[str, str, tuple[str, ...]] | Failure":
        """The three settings this provider reads, validated before any spawn.

        ``settings`` is opaque per-role configuration in S1, so unknown keys
        are someone else's and are ignored; the keys this provider does read
        are checked here, before anything durable happens, so an unusable
        value is a reason-bearing refusal rather than a platform-dependent
        exception mid-spawn.
        """

        prompt = request.settings.get(PROMPT_SETTING, DEFAULT_PROMPT)
        resume_prompt = request.settings.get(RESUME_PROMPT_SETTING, DEFAULT_RESUME_PROMPT)
        for name, value in ((PROMPT_SETTING, prompt), (RESUME_PROMPT_SETTING, resume_prompt)):
            if not isinstance(value, str) or not value.strip():
                return Failure(
                    FailureKind.REFUSED_BY_PROVIDER,
                    f"settings[{name!r}] for session {request.session_id!r} "
                    f"must be a non-empty string, got {value!r}",
                )
            if "\x00" in value:
                return Failure(
                    FailureKind.REFUSED_BY_PROVIDER,
                    f"settings[{name!r}] for session {request.session_id!r} "
                    "contains a NUL, which no operating system can carry in an argv",
                )
            if value.lstrip().startswith("-"):
                # ``claude -p <prompt>`` takes the prompt positionally, so a
                # prompt that looks like a flag would be *parsed* as one --
                # silently changing the spawn's semantics rather than being
                # carried as text.
                return Failure(
                    FailureKind.REFUSED_BY_PROVIDER,
                    f"settings[{name!r}] for session {request.session_id!r} "
                    "begins with '-' and would be parsed as a CLI flag rather "
                    "than carried as a prompt",
                )
        raw_args = request.settings.get(CLI_ARGS_SETTING)
        if raw_args is None:
            cli_args: tuple[str, ...] = ()
        elif isinstance(raw_args, (list, tuple)):
            cli_args = tuple(str(part) for part in raw_args)
            for index, part in enumerate(cli_args):
                if "\x00" in part:
                    return Failure(
                        FailureKind.REFUSED_BY_PROVIDER,
                        f"settings[{CLI_ARGS_SETTING!r}][{index}] for session "
                        f"{request.session_id!r} contains a NUL, which no "
                        "operating system can carry in an argv",
                    )
                owned = next(
                    (
                        flag
                        for flag in _PROVIDER_OWNED_FLAGS
                        if part == flag or part.startswith(flag + "=")
                    ),
                    None,
                )
                if owned is not None:
                    return Failure(
                        FailureKind.REFUSED_BY_PROVIDER,
                        f"settings[{CLI_ARGS_SETTING!r}][{index}] for session "
                        f"{request.session_id!r} carries {owned!r}, which this "
                        "provider renders itself; per-role arguments must not "
                        "override the committed identity or the structured "
                        "readout",
                    )
        else:
            # A bare string is refused along with the rest: iterating it would
            # pass its characters as separate arguments.
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"settings[{CLI_ARGS_SETTING!r}] for session "
                f"{request.session_id!r} must be a list or tuple of arguments, "
                f"got {raw_args!r}",
            )
        return str(prompt), str(resume_prompt), cli_args

    def _create_workspace(
        self, request: StartRequest, workspace: Path
    ) -> Failure | None:
        """Announce the workspace transition, and make it unless vetoed."""

        transition = WorkspaceTransition(
            session_id=request.session_id,
            workspace=str(workspace),
            kind=CREATE_WORKSPACE,
            provider_detail={"role": request.role},
        )
        decision = self.evaluate_workspace_transition(transition)
        if decision.verdict is WorkspaceVerdict.VETO:
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"creating workspace {workspace} for session "
                f"{request.session_id!r} was vetoed: {decision.reason}",
                {"transition": CREATE_WORKSPACE},
            )
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"workspace {workspace} could not be created: {exc}",
                {"errno": exc.errno},
            )
        except ValueError as exc:
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"the workspace configured for session {request.session_id!r} "
                f"is not a usable path: {exc}",
            )
        return None

    # -- supervision: liveness, adoption, termination ----------------------

    def _child_liveness(self, session: _Supervised) -> "bool | Failure":
        """Is the recorded child alive -- resolved, never guessed.

        For a child of this instance the answer is ``poll()``. For an orphan
        the recorded pid alone is not evidence -- pids recycle -- so a live
        pid counts only when its command line still carries this session's
        UUID. A live pid whose identity cannot be read is a typed failure: on
        it this provider will neither adopt (it may be a stranger) nor spawn
        a resume (it may be the session's own writer, and a resume next to a
        live writer is the U32 failure), and it will certainly not signal it.
        """

        if session.process is not None:
            return session.process.poll() is None
        record = session.record
        if record.pid is None:
            # Recorded before the spawn and never updated: the previous
            # supervisor died inside the one window the record cannot cover.
            # There is no pid to resolve, so the child -- if it ever existed
            # -- is unfindable, and the only safe reading is "gone".
            return False
        if os.name != "posix":  # pragma: no cover - exercised via monkeypatch
            # No signal-0 probe and no /proc: the recorded child's liveness is
            # unknowable here, and unknowable must fail closed -- reading it
            # as "gone" would let resume spawn next to a possibly-live writer
            # (the U32 failure) and let stop report success over a running
            # child.
            return Failure(
                FailureKind.BACKEND_UNREACHABLE,
                f"pid {record.pid} recorded for session "
                f"{record.session_id!r} cannot have its liveness determined "
                "on this platform; refusing to adopt, signal or resume "
                "around it",
                {"pid": record.pid},
            )
        if not _pid_exists(record.pid):
            return False
        cmdline = _pid_cmdline(record.pid)
        if cmdline is None:
            return Failure(
                FailureKind.BACKEND_UNREACHABLE,
                f"pid {record.pid} recorded for session "
                f"{record.session_id!r} is alive, but its command line could "
                "not be read on this platform, so whether it is still that "
                "session's child is unknowable; refusing to adopt, signal or "
                "resume around it",
                {"pid": record.pid},
            )
        return record.claude_session_uuid in cmdline

    def _terminate(self, session: _Supervised) -> Failure | None:
        """Stop the child's whole process group and confirm the exit."""

        liveness = self._child_liveness(session)
        if isinstance(liveness, Failure):
            return liveness
        if not liveness:
            return None
        record = session.record
        if session.process is not None:
            process = session.process
            if os.name == "posix":
                _signal_group(record.pgid or process.pid, signal.SIGTERM)
            else:  # pragma: no cover - exercised only on Windows
                process.terminate()
            try:
                process.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    _signal_group(record.pgid or process.pid, signal.SIGKILL)
                else:  # pragma: no cover - exercised only on Windows
                    process.kill()
                try:
                    process.wait(timeout=self._stop_timeout)
                except subprocess.TimeoutExpired:
                    # A child that survives SIGKILL is stuck in the kernel;
                    # reporting the bound loudly beats blocking the caller on
                    # a wait that may never return.
                    return Failure(
                        FailureKind.TIMED_OUT,
                        f"the child (pid {process.pid}) of session "
                        f"{record.session_id!r} did not exit within "
                        f"{self._stop_timeout}s of SIGKILL",
                        {"pid": process.pid},
                    )
            # The leader's exit is not the group's (H1): an MCP child that
            # ignored the SIGTERM outlives a parent that honoured it, and
            # ``wait()`` returning says nothing about it. The group itself is
            # confirmed empty before the stop reports done.
            return self._reap_group_remnants(
                record.pgid or process.pid, record.session_id
            )
        # An adopted orphan is not a child of this process, so there is no
        # ``wait``; the exit is confirmed by the pid disappearing. The
        # identity was confirmed by ``_child_liveness`` above -- a stranger on
        # a recycled pid is never signalled.
        assert record.pid is not None  # liveness was True, so a pid existed
        _signal_group(record.pgid or record.pid, signal.SIGTERM)
        deadline = time.monotonic() + self._stop_timeout
        while _pid_running(record.pid):
            if time.monotonic() >= deadline:
                _signal_group(record.pgid or record.pid, signal.SIGKILL)
                break
            time.sleep(0.05)
        deadline = time.monotonic() + self._stop_timeout
        while _pid_running(record.pid):
            if time.monotonic() >= deadline:
                return Failure(
                    FailureKind.TIMED_OUT,
                    f"the recorded child (pid {record.pid}) of session "
                    f"{record.session_id!r} did not exit within "
                    f"{self._stop_timeout}s of SIGKILL",
                    {"pid": record.pid},
                )
            time.sleep(0.05)
        return self._reap_group_remnants(record.pgid or record.pid, record.session_id)

    def _reap_group_remnants(self, pgid: int, session_id: str) -> Failure | None:
        """Confirm the whole process group is gone, killing what remains (H1).

        The CLI does not reap MCP-server children of its own, and one that
        ignored the SIGTERM survives the leader -- so the leader's confirmed
        exit is where the sweep *starts*, not where the stop is done. Best
        effort on a platform without process groups.
        """

        if os.name != "posix":  # pragma: no cover - exercised only on Windows
            return None
        if not _group_has_live_members(pgid):
            return None
        _signal_group(pgid, signal.SIGKILL)
        deadline = time.monotonic() + self._stop_timeout
        while _group_has_live_members(pgid):
            if time.monotonic() >= deadline:
                return Failure(
                    FailureKind.TIMED_OUT,
                    f"process group {pgid} of session {session_id!r} still "
                    f"has members {self._stop_timeout}s after SIGKILL",
                    {"pgid": pgid},
                )
            time.sleep(0.05)
        return None

    # -- the readout ---------------------------------------------------------

    def _readout(self, session: _Supervised) -> _ReadoutOrProblem:
        """One session as its own output currently reports it.

        Built from the stream-json lines the child wrote, in this order of
        evidence:

        1. an **identity check** on every event that names one -- the C2 form
           of the roster read-back. Disagreement is persisted as an incident
           and every later read keeps failing;
        2. a ``result`` event, whose ``terminal_reason`` (falling back to its
           ``subtype``) is the child's own last word. The process exit code is
           carried in the detail and never consulted for the verdict;
        3. any other event, whose ``subtype`` or ``type`` is the child's own
           word for where it is;
        4. nothing parseable yet from a live child -- **could not observe**,
           with the reason (R4);
        5. an exit with no structured output at all, reported as the process
           disposition with the captured stderr surfaced -- which is where the
           CLI's refusals live (i01 §3.3).
        """

        record = session.record
        if record.incident is not None:
            return _Uninterpretable(
                f"identity incident: {record.incident}",
                {"session_id": record.session_id},
            )
        parsed = self._parse_events(session)
        if isinstance(parsed, _Uninterpretable):
            # The captured-output *file* could not be read. That is a failure
            # of the observation channel, not an answer in an uninterpretable
            # shape: per S1's read_state contract it is a readout of "could
            # not observe" with the reason, never a failed call.
            return SessionReadout(
                session_id=record.session_id,
                observation=Observation.COULD_NOT_OBSERVE,
                could_not_observe_reason=parsed.detail,
                provider_detail=parsed.provider_detail,
            )
        events, garbage = parsed
        base_detail: dict[str, Any] = {
            "pid": record.pid,
            "generation": record.generation,
            **dict(session.provider_detail),
        }
        stderr_tail = self._stderr_tail(record)
        if stderr_tail:
            base_detail["stderr_tail"] = stderr_tail

        for event in events:
            reported = event.get("session_id")
            if reported is not None and reported != record.claude_session_uuid:
                incident = (
                    f"session {record.session_id!r} committed identity "
                    f"{record.claude_session_uuid!r} before the spawn, but the "
                    f"child's own {event.get('type', '?')} event reports "
                    f"{reported!r}. Two processes reporting one id -- or one "
                    "process reporting another's -- is the U27 failure shape; "
                    "this session is impounded, not warned about."
                )
                self._record_incident(session, incident)
                return _Uninterpretable(
                    f"identity incident: {incident}",
                    {**base_detail, "expected": record.claude_session_uuid, "reported": reported},
                )

        if garbage is not None:
            # An uninterpretable line does not stop later, well-formed lines
            # from being read, but it is never silently dropped either: it
            # rides in the detail when a readout is still possible, and it is
            # the loud answer itself when nothing better exists (below).
            base_detail["uninterpretable_line"] = garbage

        # The read-back is positive, not merely non-contradictory: structured
        # output that never names the session's identity cannot be reconciled
        # with the one committed before the spawn, and accepting it anyway
        # would let schema drift quietly defeat the one check U27 makes
        # mandatory. A live child is given time (below); a finished one is
        # answered loudly.
        identity_read_back = any(event.get("session_id") is not None for event in events)
        result_event = next(
            (event for event in reversed(events) if event.get("type") == "result"), None
        )
        if result_event is not None:
            if not identity_read_back:
                return _Uninterpretable(
                    f"the child of session {record.session_id!r} finished "
                    "without any event naming a session identity, so the "
                    "identity committed before the spawn cannot be read back "
                    "and reconciled; its outcome is not accepted on trust",
                    {**base_detail, "expected": record.claude_session_uuid},
                )
            word = result_event.get("terminal_reason") or result_event.get("subtype")
            if not isinstance(word, str) or not word.strip():
                return _Uninterpretable(
                    f"the child of session {record.session_id!r} wrote a result "
                    "event carrying neither a terminal_reason nor a subtype; "
                    "a result that names no outcome cannot be read as one",
                    {**base_detail, "result_keys": sorted(result_event)},
                )
            return SessionReadout(
                session_id=record.session_id,
                observation=Observation.OBSERVED,
                provider_state=word,
                provider_detail={
                    **base_detail,
                    "is_error": result_event.get("is_error"),
                    "subtype": result_event.get("subtype"),
                    "terminal_reason": result_event.get("terminal_reason"),
                    "returncode": self._returncode(session),
                },
            )

        liveness = self._child_liveness(session)
        if isinstance(liveness, Failure):
            # Unknowable liveness is likewise an observation-channel failure:
            # the session is reported as itself, explicitly unobservable, with
            # the reason. The *acting* verbs (stop, resume) still consult
            # ``_child_liveness`` directly and keep failing closed on it.
            return SessionReadout(
                session_id=record.session_id,
                observation=Observation.COULD_NOT_OBSERVE,
                could_not_observe_reason=liveness.detail,
                provider_detail={**base_detail, **dict(liveness.provider_detail)},
            )
        if liveness:
            if garbage is not None:
                return _Uninterpretable(garbage, base_detail)
            if events and not identity_read_back:
                # The child is speaking but has not yet said who it is. The
                # identity may still arrive, so this is tolerated as an
                # explicit could-not-observe rather than either accepted as
                # an observed state or condemned as an incident.
                return SessionReadout(
                    session_id=record.session_id,
                    observation=Observation.COULD_NOT_OBSERVE,
                    could_not_observe_reason=(
                        "the child is emitting events, but none has named a "
                        "session identity yet; an observed state is withheld "
                        "until the committed identity reads back"
                    ),
                    provider_detail=base_detail,
                )
            if events:
                last = events[-1]
                word = last.get("subtype") or last.get("type")
                if not isinstance(word, str) or not word.strip():
                    return _Uninterpretable(
                        f"the child of session {record.session_id!r} wrote an "
                        "event carrying neither a subtype nor a type; an event "
                        "that names nothing cannot be read as a state",
                        {**base_detail, "event_keys": sorted(last)},
                    )
                return SessionReadout(
                    session_id=record.session_id,
                    observation=Observation.OBSERVED,
                    provider_state=word,
                    provider_detail=base_detail,
                )
            return SessionReadout(
                session_id=record.session_id,
                observation=Observation.COULD_NOT_OBSERVE,
                could_not_observe_reason=(
                    "the child is running but has not emitted anything "
                    "parseable yet"
                ),
                provider_detail=base_detail,
            )

        # The child is gone without a result event.
        if garbage is not None:
            return _Uninterpretable(garbage, base_detail)
        if events and not identity_read_back:
            return _Uninterpretable(
                f"the child of session {record.session_id!r} is gone after "
                "emitting events, none of which named a session identity; the "
                "identity committed before the spawn cannot be read back and "
                "reconciled",
                {**base_detail, "expected": record.claude_session_uuid},
            )
        returncode = self._returncode(session)
        if returncode is not None:
            # A child of ours: the operating system's word for its exit is a
            # fact this supervisor observed. What it is *not* is a verdict --
            # exit 0 with no result event says nothing about success, and the
            # word carries the number rather than an interpretation of it.
            return SessionReadout(
                session_id=record.session_id,
                observation=Observation.OBSERVED,
                provider_state=f"exited-{returncode}",
                provider_detail={**base_detail, "returncode": returncode},
            )
        return SessionReadout(
            session_id=record.session_id,
            observation=Observation.COULD_NOT_OBSERVE,
            could_not_observe_reason=(
                f"the recorded child (pid {record.pid}) is gone, wrote no "
                "result event, and was not a child of this supervisor, so its "
                "exit status was not observable"
            ),
            provider_detail=base_detail,
        )

    def _parse_events(
        self, session: _Supervised
    ) -> "tuple[list[dict[str, Any]], str | None] | _Uninterpretable":
        """The complete stream-json lines the child has written so far.

        Complete lines only: the CLI writes whole lines (i01 §3.3 observed no
        torn JSON even under SIGKILL), but the *file* can still be read
        mid-flush, so a trailing fragment without its newline is "not arrived
        yet", never corruption. A **complete** line that does not parse is the
        opposite case -- the child answered, in a shape this interface cannot
        interpret -- and is returned as the loud half rather than skipped
        (issue #17: fail loudly, never return an empty result).

        Unknown event types and unknown fields are tolerated by construction:
        an event is a dict, and nothing here enumerates which dicts exist.
        """

        record = session.record
        path = self._events_path(record.session_id, record.generation)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return [], None
        except OSError as exc:
            return _Uninterpretable(
                f"the captured output of session {record.session_id!r} at "
                f"{path} could not be read: {exc}",
                {"errno": getattr(exc, "errno", None)},
            )
        events: list[dict[str, Any]] = []
        garbage: str | None = None
        body, newline, _fragment = raw.rpartition(b"\n")
        if not newline:
            return [], None
        for index, line in enumerate(body.split(b"\n")):
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                garbage = (
                    f"line {index + 1} of session {record.session_id!r}'s "
                    f"captured output is complete but is not JSON: "
                    f"{line[:200]!r}"
                )
                continue
            if not isinstance(event, dict):
                garbage = (
                    f"line {index + 1} of session {record.session_id!r}'s "
                    f"captured output is JSON but not an object: {line[:200]!r}"
                )
                continue
            events.append(event)
        return events, garbage

    def _returncode(self, session: _Supervised) -> int | None:
        if session.process is None:
            return None
        return session.process.poll()

    def _stderr_tail(self, record: _SessionRecord) -> str:
        path = self._stderr_path(record.session_id, record.generation)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-_STDERR_TAIL_CHARS:].strip()

    # -- the durable record's plumbing --------------------------------------

    def _session_dir(self, session_id: str) -> Path:
        return self._state_root / session_id

    def _record_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / _RECORD_NAME

    def _events_path(self, session_id: str, generation: int) -> Path:
        return self._session_dir(session_id) / f"events-{generation:03d}.jsonl"

    def _stderr_path(self, session_id: str, generation: int) -> Path:
        return self._session_dir(session_id) / f"stderr-{generation:03d}.log"

    def _write_record(self, record: _SessionRecord) -> None:
        """Atomically, so no reader -- including a future supervisor life --
        ever sees half a record."""

        path = self._record_path(record.session_id)
        partial = path.with_suffix(".part")
        partial.write_text(record.to_json(), encoding="utf-8")
        os.replace(partial, path)

    def _record_incident(self, session: _Supervised, incident: str) -> None:
        session.record = replace(session.record, incident=incident)
        try:
            self._write_record(session.record)
        except OSError:
            # The in-memory impound still holds for this provider's life; a
            # record that cannot be written is degraded durability, not a
            # reason to let the incident read as healthy now.
            pass

    def _remove_session_dir(self, session_id: str) -> None:
        directory = self._session_dir(session_id)
        try:
            for child in directory.iterdir():
                child.unlink(missing_ok=True)
            directory.rmdir()
        except OSError:
            pass

    def _find(self, session_id: str) -> "_Supervised | _BrokenRecord | None":
        """The session, from this instance's table or from the durable record.

        A record found on disk is materialised into the table -- that is the
        *detection* half of orphan reclaim: a supervisor restarted over the
        same state root sees every session its predecessor recorded, whether
        or not the child survived.
        """

        session = self._sessions.get(session_id)
        if session is not None:
            return session
        if not _is_one_path_component(session_id):
            return None
        record_path = self._record_path(session_id)
        try:
            record = _SessionRecord.from_json(record_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # A record that exists but cannot be read is not "no session" --
            # a record is right there -- and it is not a readable one either.
            # Deliberately not cached: a record repaired or finished being
            # written between calls should start answering as itself.
            return _BrokenRecord(
                session_id=session_id,
                reason=(
                    f"a durable record for session {session_id!r} exists at "
                    f"{record_path} but could not be read as one: {exc!r}"
                ),
            )
        session = _Supervised(record=record, provider_detail={"adopted_from_record": True})
        self._sessions[session_id] = session
        return session

    def _holder_of_uuid(self, session_uuid: str) -> str | None:
        """Which recorded session, if any, already holds this provider identity."""

        try:
            discovered = self._discover_records()
        except OSError:
            # An unreadable state root cannot answer; the spawn path fails on
            # it moments later with its own reason.
            return None
        for entry in discovered:
            if isinstance(entry, _BrokenRecord):
                continue
            if entry.record.claude_session_uuid == session_uuid:
                return entry.record.session_id
        return None

    def _discover_records(self) -> "list[_Supervised | _BrokenRecord]":
        """Every supervised session plus every record on disk (orphans last).

        A directory whose record cannot be read is discovered as a
        :class:`_BrokenRecord` rather than dropped: dropping it is how a
        possibly-running child would lose its only roster entry exactly when
        a restarted supervisor needs it (R4).
        """

        discovered: list[_Supervised | _BrokenRecord] = list(self._sessions.values())
        known = set(self._sessions)
        if not self._state_root.is_dir():
            return discovered
        for entry in sorted(self._state_root.iterdir()):
            if entry.name in known or not (entry / _RECORD_NAME).is_file():
                continue
            session = self._find(entry.name)
            if session is not None:
                discovered.append(session)
        return discovered


# --------------------------------------------------------------------------
# Process liveness, without ever trusting a pid alone
# --------------------------------------------------------------------------


def _pid_exists(pid: int) -> bool:
    if os.name != "posix":  # pragma: no cover - exercised only on Windows
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_running(pid: int) -> bool:
    """Alive *and actually running* -- a zombie counts as exited.

    An orphan is normally reaped by init the moment it dies, but a process
    that is nobody's init (this one, in the tests; a subreaper, in some
    deployments) can be left holding an unreaped zombie whose pid still
    answers signal 0. Waiting on such a pid to disappear would wait forever
    for an exit that already happened.
    """

    if not _pid_exists(pid):
        return False
    stat = Path("/proc") / str(pid) / "stat"
    try:
        text = stat.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    fields = text.rpartition(")")[2].split()
    return not fields or fields[0] != "Z"


def _pid_cmdline(pid: int) -> str | None:
    """The process's command line, or ``None`` where it cannot be read.

    ``/proc`` where it exists (Linux); elsewhere the answer is honestly
    unknown, and the caller fails closed on unknown rather than adopting or
    signalling a process it cannot identify.
    """

    proc = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = proc.read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _group_has_live_members(pgid: int) -> bool:
    """Does the process group still hold anything that is actually running?

    ``killpg(pgid, 0)`` alone cannot answer it: an unreaped zombie holds its
    group open while being unkillable, and a sweep that waited on it would
    time out on an exit that already happened. Where ``/proc`` exists the
    group's members are read directly and zombies discounted; elsewhere the
    signal-0 probe is the best available answer.
    """

    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fields = text.rpartition(")")[2].split()
            # After the command name: state, ppid, pgrp, ...
            if len(fields) >= 3 and fields[2] == str(pgid) and fields[0] != "Z":
                return True
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, signum: int) -> None:
    """Signal a process group, tolerating one that is already gone."""

    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        # A recycled pgid owned by someone else: the group this session's
        # child led no longer exists, which for a stop is the desired state.
        pass
