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
import re
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


#: The characters a session id may use once this provider has to name a file
#: after it. An allow-list rather than a list of things to reject: a stub that
#: tried to canonicalise arbitrary ids would be reimplementing path semantics
#: it has no need for, and each platform has its own way for a "harmless" id to
#: land somewhere else -- a separator on POSIX, a drive qualifier such as
#: ``C:foo`` on Windows, an embedded NUL on both.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9._-]+")


def _is_one_path_component(session_id: str) -> bool:
    """True when the id is safe to use, whole, as a file name on any platform.

    A session id is the caller's to choose (S1), and this provider turns it
    into a file name, so an id that escapes the state root would let a caller
    pick which file the provider deletes and rewrites.
    """

    return bool(_SAFE_SESSION_ID.fullmatch(session_id)) and session_id not in {".", ".."}


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
        # Resolved, because the child is started with its workspace as cwd:
        # a relative root would name one directory to this process and a
        # different one to the child, and every session would then look
        # permanently unobservable for a reason nothing reports.
        self._state_root = Path(state_root).resolve()
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

        if not _is_one_path_component(request.session_id):
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"session id {request.session_id!r} is not usable as a single "
                "file name; this provider names a state file after the session "
                "and will not let an id reach outside its state root",
            )
        if request.session_id in self._sessions:
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"session {request.session_id!r} already exists; this provider "
                "does not reuse a session id",
            )

        try:
            workspace = Path(request.workspace)
            workspace_exists = workspace.is_dir()
        except ValueError as exc:
            # A NUL in the path, say. S1 only requires the workspace to be a
            # non-empty string, so an unusable one is caller input this verb
            # refuses with a reason rather than an exception.
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"the workspace configured for session {request.session_id!r} "
                f"is not a usable path: {exc}",
            )
        if not workspace_exists:
            refusal = self._create_workspace(request, workspace)
            if refusal is not None:
                return refusal

        state_file = self._state_file(request.session_id)
        try:
            self._state_root.mkdir(parents=True, exist_ok=True)
            # A stale file from an earlier session of the same name would be
            # read as this child's word.
            state_file.unlink(missing_ok=True)
        except OSError as exc:
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"the state file for session {request.session_id!r} could not "
                f"be prepared under {self._state_root}: {exc}",
                {"errno": exc.errno},
            )

        environment = dict(os.environ)
        environment[STATE_FILE_ENV] = str(state_file)
        announce_after = request.settings.get("announce_after")
        if announce_after is not None:
            environment[ANNOUNCE_AFTER_ENV] = str(announce_after)

        try:
            command = self._child_command(request)
        except (TypeError, ValueError) as exc:
            # The caller's settings are unusable -- the wrong shape, empty, or
            # carrying a NUL. All of them are the same answer, and it is reached
            # before any spawn is attempted so that no platform's idea of which
            # exception to raise can change it.
            #
            # Echoing the setting back is shape-checked for the same reason
            # ``_child_command`` checks it: ``settings`` is opaque, so the value
            # may be an int, or a bare string whose iteration would report its
            # characters as arguments. Anything that is not already a sequence
            # of arguments is reported as itself, not taken apart.
            raw = request.settings.get("command")
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"the child command configured for session "
                f"{request.session_id!r} is unusable: {exc}",
                {"command": list(raw) if isinstance(raw, (list, tuple)) else repr(raw)},
            )
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
        except (ValueError, IndexError) as exc:
            # A backstop. The two cases that used to arrive here -- an empty
            # command and one carrying a NUL -- are now refused by
            # ``_child_command`` before any spawn is attempted, precisely so
            # that the answer does not depend on which platform's layer rejects
            # them. Anything else ``Popen`` refuses without reaching the
            # operating system is still the caller's settings being unusable,
            # which the contract says is a reason-bearing Failure and not an
            # exception at the caller.
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"the child command configured for session "
                f"{request.session_id!r} is unusable: {exc}",
                {"command": list(command)},
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
            self._close_child_input(session)
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
        if not isinstance(command, (list, tuple)):
            # ``settings`` is opaque, so anything at all can arrive here. A
            # bare string is rejected along with the rest: iterating it would
            # spawn its first character.
            raise TypeError(
                f"a child command must be a list or tuple of arguments, got {command!r}"
            )
        argv = [str(part) for part in command]

        # The contents are checked HERE rather than by letting ``Popen`` reject
        # them, because *which layer rejects them is platform-dependent and the
        # classification must not be*.
        #
        # On POSIX an empty argv raises ``IndexError`` and an embedded NUL
        # raises ``ValueError``, both before the operating system is involved.
        # On Windows an empty argv reaches ``CreateProcess``, which fails with
        # ``OSError`` (``WinError 87``, ``errno`` 22) -- indistinguishable at the
        # call site from a genuine spawn failure, and so classified as
        # ``BACKEND_UNREACHABLE``. That inverted the answer the contract owes
        # the caller: unusable *settings* say "fix your configuration"
        # (``REFUSED_BY_PROVIDER``), while an unreachable backend says "the
        # child could not be started" and invites a retry that cannot succeed.
        #
        # Deciding it before the spawn makes the verdict a property of the
        # request rather than of the platform.
        if not argv:
            raise ValueError("a child command must name at least one argument")
        for index, part in enumerate(argv):
            if "\x00" in part:
                raise ValueError(
                    f"argument {index} of the child command contains a NUL, "
                    "which no operating system can carry in an argv"
                )
        return argv

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
        except ValueError as exc:
            # A path the operating system is never even asked about -- one
            # carrying a NUL, say. ``is_dir()`` answers False for it rather
            # than raising, so this is where an unusable workspace surfaces.
            return Failure(
                FailureKind.REFUSED_BY_PROVIDER,
                f"the workspace configured for session {request.session_id!r} "
                f"is not a usable path: {exc}",
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
            # An exited session stays in the table -- read_state on it is a
            # legitimate question -- so the pipe held open for its child is
            # released here rather than only in stop(), which a child that
            # exited on its own is never asked for.
            self._close_child_input(session)
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
        except UnicodeDecodeError as exc:
            # A child may write whatever it likes. Bytes that are not a word
            # are a state this provider could not observe, with the reason --
            # not an exception, and not a state invented on the child's behalf.
            return SessionReadout(
                session_id=session_id,
                observation=Observation.COULD_NOT_OBSERVE,
                could_not_observe_reason=(
                    f"the child is running but wrote a state that is not UTF-8: {exc}"
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
