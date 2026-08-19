"""S3 -- the stub ``SessionProvider``, over local child processes.

A deliberately trivial implementation of the provisional S1 interface
(:mod:`claude_org_runtime.session.provider`) with **no Claude in the loop and
no network**: a "session" here is one ordinary local child process, and every
verb is rendered with the standard library alone.

Why it exists, and why it is written before the real provider (D-0020): the
control-plane suite is written against *a* provider, and whichever provider is
available while it is written is the one whose vocabulary leaks into it. With
this stub in place first, no Agent-View-shaped (or ``claude -p``-shaped)
assumption can enter the suite, so gate item 11 measures a structural property
rather than a retrofit. It is throwaway under D-0026, and it survives a C2
switch untouched.

**Deliberately trivial** is a requirement, not a caveat. Nothing here is
allowed to make a control-plane test pass for a reason the real provider would
not share, so:

* there is no retry, no reconnection, no cache of the capability probe -- the
  fail-closed probe (D-0010) runs on each spawn because that is what the
  contract says happens, not what is cheapest;
* the readout carries the **child's own** state word, read back from the file
  the child writes it to, rather than a word this module invents from
  ``poll()``. A provider's state vocabulary is the provider's (see
  :class:`~claude_org_runtime.session.provider.SessionReadout`), and a stub
  that invented one would be answering, in the stub, a question the real
  provider answers differently;
* the *could not observe* case is reached the way a real provider reaches it
  -- a child that is alive but has not yet said anything about itself -- and
  not by an injected fault. Item 11's re-run exercises the degraded paths, so
  the degraded paths have to be reachable without monkeypatching.

What is *not* here is as deliberate: no verb sends anything to a child. The
child reads its standard input and this module never writes to it. Delivery is
``MessageBus``'s under D-0009 and is built as S8; a stub that grew a delivery
path would make gate items 6 and 11 unmeasurable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

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
    StartRequest,
    WorkspaceTransition,
    WorkspaceVerdict,
)

#: The environment variable through which a child is told where to write its
#: own state word. Named rather than inlined so a caller supplying its own
#: child program can honour the same convention.
STATE_FILE_ENV = "INTERLOCK_STUB_STATE_FILE"

#: How long the default child waits before writing its state word, in seconds.
#: Its only purpose is to make the *could not observe* window reachable: a
#: child that is alive and has not reported yet is the case D-0006 requires the
#: system to tolerate, and a test cannot exercise a window that closes before
#: the spawn returns.
ANNOUNCE_AFTER_ENV = "INTERLOCK_STUB_ANNOUNCE_AFTER"

#: The state word the default child reports once it is up. It is the *child's*
#: word, not this module's: nothing here interprets it, ranks it, or maps it
#: onto anything.
DEFAULT_CHILD_STATE = "working"

#: The default child: it announces itself and then stays up until its standard
#: input closes. It is written to a temporary path and renamed into place so a
#: reader never sees half a word.
_DEFAULT_CHILD_PROGRAM = (
    "import os, sys, time\n"
    f"path = os.environ[{STATE_FILE_ENV!r}]\n"
    f"time.sleep(float(os.environ.get({ANNOUNCE_AFTER_ENV!r}, '0')))\n"
    "with open(path + '.part', 'w', encoding='utf-8') as handle:\n"
    f"    handle.write({DEFAULT_CHILD_STATE!r})\n"
    "os.replace(path + '.part', path)\n"
    "sys.stdin.read()\n"
)

#: The provider's own word for the transition it makes when a start is asked
#: for a workspace that does not exist yet (gate item 7's surface).
CREATE_WORKSPACE = "create-workspace"


@dataclass
class _Session:
    """One started session: the request that asked for it, and its child."""

    request: StartRequest
    process: subprocess.Popen[bytes]
    state_file: Path
    provider_detail: Mapping[str, Any] = field(default_factory=dict)


class LocalProcessSessionProvider(SessionProvider):
    """The five verbs and the capability probe, over local child processes.

    Args:
        state_root: directory this provider writes its per-session state files
            into. Required and never defaulted to a shared temporary location:
            two providers silently sharing a directory would read each other's
            children, and a stub whose sessions leak into another run's is a
            stub that makes the control-plane suite lie.
        python_executable: the interpreter used both as the thing the
            capability probe interrogates and as the default child. Defaults to
            the running interpreter, so the stub needs nothing installed.
        stop_timeout: seconds :meth:`stop` waits for a terminated child before
            killing it.
    """

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        python_executable: str = sys.executable,
        stop_timeout: float = 5.0,
    ) -> None:
        super().__init__()
        self._state_root = Path(state_root)
        self._python = python_executable
        self._stop_timeout = stop_timeout
        self._sessions: dict[str, _Session] = {}

    # -- the capability probe (D-0010) -------------------------------------

    def probe_capabilities(self) -> ProviderResult[CapabilityReport]:
        """Ask the interpreter, through its own CLI, what build it is.

        Public surface only: ``python -c`` and its exit status, nothing about
        how this process happens to be running. An interpreter that cannot be
        executed is reported as a :class:`Failure`, which is what refuses the
        next spawn -- the stub does not fall back to ``sys.version`` from
        in-process, because the point of the probe is to find out whether the
        thing that would be spawned works.
        """

        try:
            completed = subprocess.run(
                [self._python, "-c", "import sys; sys.stdout.write(sys.version.split()[0])"],
                capture_output=True,
                timeout=self._stop_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Failure(
                FailureKind.TIMED_OUT,
                f"the interpreter {self._python!r} did not answer the version "
                f"probe within {self._stop_timeout}s",
            )
        except OSError as exc:
            return Failure(
                FailureKind.BACKEND_UNREACHABLE,
                f"the interpreter {self._python!r} could not be executed: {exc}",
                {"errno": exc.errno},
            )
        if completed.returncode != 0:
            return Failure(
                FailureKind.BACKEND_UNREACHABLE,
                f"the interpreter {self._python!r} exited {completed.returncode} "
                "for the version probe",
                {"stderr": completed.stderr.decode("utf-8", "replace")},
            )
        version = completed.stdout.decode("utf-8", "replace").strip()
        if not version:
            return Failure(
                FailureKind.UNINTERPRETABLE_RESPONSE,
                f"the interpreter {self._python!r} answered the version probe "
                "with nothing",
            )
        return Ok(
            CapabilityReport(
                provider_version=f"python {version}",
                supported=REQUIRED_CAPABILITIES,
                detail="local child processes; no Claude CLI and no network",
            )
        )

    # -- the five verbs (D-0009) -------------------------------------------

    def _start_session(self, request: StartRequest) -> ProviderResult[SessionReadout]:
        """Spawn one child. Called by ``start`` only after the gate passes."""

        if request.session_id in self._sessions:
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"session {request.session_id!r} already exists; this provider "
                "does not reuse a session id",
            )

        workspace = Path(request.workspace)
        if not workspace.is_dir():
            refusal = self._create_workspace(request, workspace)
            if refusal is not None:
                return refusal

        self._state_root.mkdir(parents=True, exist_ok=True)
        state_file = self._state_file(request.session_id)
        # A stale file from an earlier session of the same name would be read
        # as this child's word.
        state_file.unlink(missing_ok=True)

        environment = dict(os.environ)
        environment[STATE_FILE_ENV] = str(state_file)
        announce_after = request.settings.get("announce_after")
        if announce_after is not None:
            environment[ANNOUNCE_AFTER_ENV] = str(announce_after)

        command = self._child_command(request)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return Failure(
                FailureKind.BACKEND_UNREACHABLE,
                f"could not spawn a child for session {request.session_id!r}: {exc}",
                {"command": list(command), "errno": exc.errno},
            )

        session = _Session(
            request=request,
            process=process,
            state_file=state_file,
            provider_detail={"pid": process.pid, "command": list(command)},
        )
        self._sessions[request.session_id] = session
        return Ok(self._readout(session))

    def list_sessions(self) -> ProviderResult[Sequence[SessionReadout]]:
        """Every session this provider started and has not forgotten.

        ``Ok(())`` when there are none: this provider is in-process, so being
        reachable is not in question, and an empty list is a fact about the
        provider rather than a failure to read it (R4).
        """

        return Ok(tuple(self._readout(session) for session in self._sessions.values()))

    def read_state(self, session_id: str) -> ProviderResult[SessionReadout]:
        """The child's current state, or an explicit could-not-observe."""

        session = self._sessions.get(session_id)
        if session is None:
            return Failure(
                FailureKind.UNKNOWN_SESSION,
                f"this provider holds no session {session_id!r}",
            )
        return Ok(self._readout(session))

    def stop(self, session_id: str) -> ProviderResult[SessionReadout]:
        """Terminate the child, then report what it looks like afterwards.

        The readout is taken *after* the wait rather than assumed from the
        terminate call, because a provider accepting a stop is not evidence
        that the session stopped.
        """

        session = self._sessions.get(session_id)
        if session is None:
            return Failure(
                FailureKind.UNKNOWN_SESSION,
                f"this provider holds no session {session_id!r}",
            )
        if session.process.poll() is None:
            session.process.terminate()
            try:
                session.process.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                session.process.kill()
                session.process.wait()
        self._close_child_input(session)
        return Ok(self._readout(session))

    def resume(self, session_id: str) -> ProviderResult[SessionReadout]:
        """Re-enter a session this provider still holds a child for.

        A local child process cannot be re-entered once it is gone, and this
        stub does not pretend otherwise: re-entering a session whose child has
        exited is refused with a reason rather than answered with a readout of
        something that is not running. Whether the real provider can do better
        is the real provider's business.
        """

        session = self._sessions.get(session_id)
        if session is None:
            return Failure(
                FailureKind.UNKNOWN_SESSION,
                f"this provider holds no session {session_id!r}",
            )
        if session.process.poll() is not None:
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"the child of session {session_id!r} has exited; a local child "
                "process cannot be re-entered",
                {"returncode": session.process.returncode},
            )
        return Ok(self._readout(session))

    # -- the parts the verbs are built from --------------------------------

    def _child_command(self, request: StartRequest) -> Sequence[str]:
        """The child to run: the caller's, if its settings name one.

        ``settings`` is opaque per-role configuration in S1, so a caller may
        supply its own child; whatever it supplies still gets
        :data:`STATE_FILE_ENV` and is still read through the same readout, so
        no verb behaves differently for the default child than for any other.
        """

        command = request.settings.get("command")
        if command is None:
            return [self._python, "-c", _DEFAULT_CHILD_PROGRAM]
        return [str(part) for part in command]

    def _state_file(self, session_id: str) -> Path:
        return self._state_root / f"{session_id}.state"

    def _create_workspace(
        self, request: StartRequest, workspace: Path
    ) -> Failure | None:
        """Announce the workspace transition, and make it unless vetoed.

        This is gate item 7's surface with a real producer behind it. The stub
        creates a workspace it was asked to start in and never removes one:
        announcing a transition it does not make would give the control-plane
        suite a veto to test that nothing acts on.
        """

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
        return None

    def _readout(self, session: _Session) -> SessionReadout:
        """What the provider currently reports for one session.

        Three cases, and the middle one is the case item 11 exercises:

        * the child has exited -- observed, and the state word is the child's
          exit disposition as the operating system reports it;
        * the child is alive and has not written its state word yet, or its
          state file cannot be read -- **could not observe**, with the reason,
          which is neither an error nor an observation of nothing (R4);
        * the child is alive and has reported -- observed, carrying the word
          the child itself wrote, uninterpreted.
        """

        session_id = session.request.session_id
        returncode = session.process.poll()
        if returncode is not None:
            return SessionReadout(
                session_id=session_id,
                observation=Observation.OBSERVED,
                provider_state=f"exited-{returncode}",
                provider_detail={**session.provider_detail, "returncode": returncode},
            )
        try:
            reported = session.state_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            # Not yet written is the ordinary shape of "has not reported": it
            # is the same case as an empty file, and splitting the two would
            # make a caller match on which one it got.
            reported = ""
        except OSError as exc:
            return SessionReadout(
                session_id=session_id,
                observation=Observation.COULD_NOT_OBSERVE,
                could_not_observe_reason=(
                    f"the child is running but its state file "
                    f"{session.state_file} could not be read: {exc}"
                ),
                provider_detail=session.provider_detail,
            )
        if not reported:
            return SessionReadout(
                session_id=session_id,
                observation=Observation.COULD_NOT_OBSERVE,
                could_not_observe_reason=(
                    "the child is running but has not reported a state yet"
                ),
                provider_detail=session.provider_detail,
            )
        return SessionReadout(
            session_id=session_id,
            observation=Observation.OBSERVED,
            provider_state=reported,
            provider_detail=session.provider_detail,
        )

    @staticmethod
    def _close_child_input(session: _Session) -> None:
        """Release the pipe held open for the child's standard input.

        Closing a pipe is not delivery: nothing is ever written to it. It is
        held open only so the default child has an input to block on, and
        released here so a stopped session leaks no file descriptor.
        """

        if session.process.stdin is not None and not session.process.stdin.closed:
            session.process.stdin.close()
