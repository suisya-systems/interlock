"""The controller: spawn, barrier, kill, restart, cleanup.

Design sections 3 (the two-phase barrier), 5 (combination semantics), 7 (the
injected clock) and 8 (OS policy, signal hygiene and cleanup). **Durable**: it
speaks only the fault-runner contract and holds an ``Adapter``; it never
imports an implementation module.

The one paragraph worth reading before the code: **the kill is always a real
signal from outside the process.** Phase one is the driver announcing that it
is inside the named window and blocking on its control pipe; phase two is
``os.kill(pid, SIGKILL)`` (or ``Popen.kill()`` on the portable lane). No reply
is ever written for a kill case -- the blocked read is torn down by the kill --
and the controller then asserts the exit status, because a role process that
exited any other way failed the case as a *harness* error and must be
attributable as one.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from tests.fault_injection import contract
from tests.fault_injection.contract import (
    ArmedAnchor,
    ContractViolation,
    EVENT_CHECKPOINT,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_HELLO,
    EVENT_RECOVERY_COMPLETE,
    EVENT_SYNC,
    Handshake,
)

__all__ = [
    "BarrierTimeout",
    "CaseFailure",
    "CaseTimeout",
    "Controller",
    "RoleProcess",
    "TEARDOWN_GRACE_S",
    "repro_line",
]

#: How long the teardown ladder waits between ``SIGTERM`` and ``SIGKILL``
#: (design 8.2). Host monotonic time, never the injected clock.
TEARDOWN_GRACE_S = 2.0

_POSIX = os.name == "posix"


class CaseFailure(AssertionError):
    """A case failed, with the reproduction line attached.

    Raised *instead of* re-instantiating whatever the original exception was:
    the harness can surface exceptions whose constructors take more than a
    message, and rebuilding one of those from a string turns a real failure into
    a ``TypeError`` about reporting it. The original is always the chained
    cause.
    """


class BarrierTimeout(AssertionError):
    """An armed barrier was never reached. A harness fault, not a component one."""


class CaseTimeout(AssertionError):
    """A case outran its budget (design 9). Converted from a CI hang."""


#: The node id template a reproduction line hands the reader.
#:
#: A node id and not ``-k <case_id>``: ``-k`` is a substring match and the
#: manifest's grammar makes case ids substrings of one another
#: (``disp__attempt__before_durable_write__sigkill`` is inside both
#: ``...__occ2`` and ``sup+disp__attempt__...``), so the documented "re-run this
#: one case" would quietly run three. The node id selects exactly one.
CASE_NODE_ID = "tests/fault_injection/test_cases.py::test_manifest_case[{case_id}]"


def repro_line(
    *,
    case_id: str,
    suite_seed: int,
    manifest_version: int,
    contract_version: int = contract.FAULT_RUNNER_CONTRACT_VERSION,
    resolved_skew_ms: int | None = None,
    profile: str | None = None,
) -> str:
    """The single reproduction line a failing case prints (design 4.4).

    It carries the profile as well as the seed, because the profile is a third
    input to *selection*: two thirds of the matrix is ``full``-only, and the
    re-run of a nightly failure under the default ``fast`` profile would collect
    no tests at all and report success by collecting nothing.

    Same case id, same suite seed, same manifest version give the same armed
    windows, the same payloads and the same schedule decisions.
    """

    # Spelled for the shell of the host that printed it. The line exists to be
    # pasted, and POSIX ``VAR=value cmd`` is a syntax error in both PowerShell
    # and cmd.exe -- so on the Windows jobs the advertised way to reproduce a
    # failure would not run.
    node_id = CASE_NODE_ID.format(case_id=case_id)
    if os.name == "nt":  # pragma: no cover - Windows jobs only
        command = (
            f"$env:S9_PROFILE='{profile or 'fast'}'; "
            f"$env:S9_SUITE_SEED='{suite_seed}'; "
            f'python -m pytest "{node_id}"'
        )
    else:
        command = (
            f"S9_PROFILE={profile or 'fast'} S9_SUITE_SEED={suite_seed} "
            f"python -m pytest '{node_id}'"
        )
    return (
        f"S9-REPRO case_id={case_id} suite_seed={suite_seed} "
        f"manifest_version={manifest_version} contract_version={contract_version} "
        f"resolved_skew_ms={resolved_skew_ms} profile={profile or 'fast'}\n"
        f"S9-RERUN {command}"
    )


@dataclass
class RoleProcess:
    """One role process: an independent PID with an independent connection."""

    role: str
    popen: subprocess.Popen
    generation: int
    stderr_path: Path
    events: "queue.Queue[Mapping[str, Any] | None]"
    reader: threading.Thread
    pgid: int | None
    trace: list[Mapping[str, Any]] = field(default_factory=list)
    reaped: bool = False
    stopped: bool = False

    @property
    def pid(self) -> int:
        return self.popen.pid

    # -- protocol ----------------------------------------------------------

    def send(self, command: Mapping[str, Any]) -> None:
        """Write one command line to the driver's control pipe."""

        assert self.popen.stdin is not None
        try:
            self.popen.stdin.write(json.dumps(command, sort_keys=True) + "\n")
            self.popen.stdin.flush()
        except (BrokenPipeError, ValueError):
            # The driver is gone. For a kill case that is the expected shape and
            # the caller's own exit-status assertion is the authority.
            pass

    def next_event(self, timeout_s: float) -> Mapping[str, Any]:
        try:
            event = self.events.get(timeout=timeout_s)
        except queue.Empty:
            raise BarrierTimeout(
                f"{self.role}: no protocol event within {timeout_s:.1f}s; "
                f"trace so far: {json.dumps(self.trace)}"
            ) from None
        if event is None:
            raise BarrierTimeout(
                f"{self.role}: the event pipe closed while an event was "
                f"expected; stderr is at {self.stderr_path}"
            )
        self.trace.append(event)
        return event

    def wait_for_event(self, kinds: Iterable[str], timeout_s: float) -> Mapping[str, Any]:
        """Consume events until one of ``kinds`` arrives."""

        wanted = set(kinds)
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BarrierTimeout(
                    f"{self.role}: waited {timeout_s:.1f}s for one of "
                    f"{sorted(wanted)}; trace: {json.dumps(self.trace)}"
                )
            event = self.next_event(remaining)
            if event.get("event") in wanted:
                return event
            if event.get("event") == EVENT_ERROR:
                raise AssertionError(
                    f"{self.role}: the driver reported {event.get('type')!r}; "
                    f"stderr is at {self.stderr_path}"
                )

    def drain(self, timeout_s: float) -> None:
        """Read to end of stream, so the trace is complete for a report."""

        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                event = self.events.get(timeout=remaining)
            except queue.Empty:
                return
            if event is None:
                return
            self.trace.append(event)


def _reader_thread(
    stream: Any, sink: "queue.Queue[Mapping[str, Any] | None]"
) -> threading.Thread:
    def run() -> None:
        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    sink.put(json.loads(line))
                except json.JSONDecodeError:
                    sink.put({"event": EVENT_ERROR, "type": "MalformedEvent"})
        finally:
            sink.put(None)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class _JobObject:
    """Windows process containment (design 8.2).

    POSIX sessions, ``SIGCONT``/``SIGTERM`` and ``killpg`` do not exist on
    Windows and ``start_new_session`` has no effect there, so the contract has
    an explicit branch rather than a best-effort mapping: role processes are
    assigned to a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, and
    closing the handle kills the whole tree atomically at the kernel -- the
    handle playing the un-reusable-id role the un-reaped pgid plays on POSIX.
    Where the API is unavailable the fallback is ``taskkill /T /F``.
    """

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":  # pragma: no cover - POSIX takes the other branch
            return
        try:  # pragma: no cover - exercised on the Windows jobs only
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return

            class _LIMIT(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _IO(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class _EXTENDED(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _LIMIT),
                    ("IoInfo", _IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            information = _EXTENDED()
            information.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(information), ctypes.sizeof(information)
            )
            self.handle = handle
            self._kernel32 = kernel32
        except Exception:
            self.handle = None

    def assign(self, pid: int) -> None:  # pragma: no cover - Windows only
        if self.handle is None:
            return
        try:
            import ctypes

            process = self._kernel32.OpenProcess(0x1F0FFF, False, pid)
            if process:
                try:
                    self._kernel32.AssignProcessToJobObject(self.handle, process)
                finally:
                    self._kernel32.CloseHandle(process)
        except Exception:
            pass

    def close(self) -> None:  # pragma: no cover - Windows only
        if self.handle is None:
            return
        try:
            self._kernel32.CloseHandle(self.handle)
        finally:
            self.handle = None


class Controller:
    """Spawn / barrier / kill / restart / cleanup for one case.

    Every timeout below runs on the **host monotonic clock** and is never
    skewed: the injected clock models the wall clock, which is what lease expiry
    arithmetic uses, and a harness that skewed its own watchdogs would have no
    way of noticing it had hung (design 7).
    """

    def __init__(
        self,
        *,
        workdir: Path,
        adapter: Any,
        case: Mapping[str, Any],
        suite_seed: int,
        barrier_timeout_s: float,
        case_timeout_s: float,
        profile: str | None = None,
    ) -> None:
        self.profile = profile
        self.workdir = Path(workdir)
        self.adapter = adapter
        self.case = dict(case)
        self.suite_seed = int(suite_seed)
        self.barrier_timeout_s = float(barrier_timeout_s)
        self.case_timeout_s = float(case_timeout_s)
        self.db_path = self.workdir / "control-plane.sqlite3"
        self.processes: dict[str, RoleProcess] = {}
        self._spawned: list[RoleProcess] = []
        self._job = _JobObject()
        self._deadline = time.monotonic() + self.case_timeout_s
        self.workdir.mkdir(parents=True, exist_ok=True)

    # -- lifecycle ---------------------------------------------------------

    def bootstrap(self) -> None:
        self._check_deadline()
        self.adapter.bootstrap(
            self.db_path,
            roles=tuple(self.case["targets"]),
            now_ms=int(self.case["clock_base_ms"]),
        )

    def _check_deadline(self) -> None:
        if time.monotonic() > self._deadline:
            raise CaseTimeout(
                f"{self.case['case_id']} outran its {self.case_timeout_s:.0f}s "
                "budget (design 9); teardown ran and the trace is attached"
            )

    def _remaining(self, want_s: float) -> float:
        return max(0.1, min(want_s, self._deadline - time.monotonic()))

    def spawn(
        self,
        role: str,
        *,
        armed: Sequence[ArmedAnchor] = (),
        generation: int = 0,
        clock_offset_ms: int = 0,
        key: str | None = None,
        extra_arguments: Sequence[str] = (),
    ) -> RoleProcess:
        """Start one role process: separate OS process, separate session.

        ``key`` names the process slot. It defaults to the role, and differs
        only for a second process of the same role -- the claimant a lease
        takeover needs, which is the same script under a different holder
        identity. Which component may hold which resource is ``Q-0001`` and
        stays open, so holder identities are per-case data here too.
        """

        self._check_deadline()
        command = [
            sys.executable,
            "-m",
            self.adapter.driver_module,
            "--role",
            role,
            "--db",
            str(self.db_path),
            "--case-id",
            str(self.case["case_id"]),
            "--suite-seed",
            str(self.suite_seed),
            "--armed",
            ",".join(anchor.wire() for anchor in armed),
            "--clock-base-ms",
            str(self.case["clock_base_ms"]),
            "--clock-offset-ms",
            str(clock_offset_ms),
            "--restart-generation",
            str(generation),
            "--control-fd",
            "0",
            "--event-fd",
            "1",
        ]
        command.extend(
            self.adapter.role_arguments(role, case=self.case, workdir=self.workdir)
        )
        # Appended last so a repeated option wins: this is how a claimant gets a
        # different holder identity without the adapter growing a second code
        # path for it.
        command.extend(extra_arguments)

        slot = key or role
        stderr_path = self.workdir / f"{slot}-g{generation}.stderr"
        stderr = stderr_path.open("w", encoding="utf-8")
        popen = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=_child_env(),
            text=True,
            bufsize=1,
            # Its own session and process group, so a stray grandchild cannot be
            # confused with it and the group can be signalled as a unit.
            start_new_session=_POSIX,
        )
        stderr.close()
        if not _POSIX:  # pragma: no cover - Windows jobs only
            self._job.assign(popen.pid)

        sink: "queue.Queue[Mapping[str, Any] | None]" = queue.Queue()
        reader = _reader_thread(popen.stdout, sink)
        process = RoleProcess(
            role=role,
            popen=popen,
            generation=generation,
            stderr_path=stderr_path,
            events=sink,
            reader=reader,
            pgid=popen.pid if _POSIX else None,
        )
        self.processes[slot] = process
        self._spawned.append(process)

        hello = process.wait_for_event({EVENT_HELLO}, self._remaining(self.barrier_timeout_s))
        Handshake(
            protocol_version=int(hello["protocol_version"]),
            contract_version=int(hello["contract_version"]),
            role=str(hello["role"]),
            case_id=str(hello["case_id"]),
            restart_generation=int(hello["restart_generation"]),
        ).check(
            # Versions and vocabulary membership are not enough. A driver that
            # answered with a *valid* but different role, case or generation
            # would still be recorded under the slot the controller asked for,
            # so the harness would drive one role while reporting another -- or
            # run generation 0 twice and call the second one a restart. The
            # handshake is the only place that can catch it, because every later
            # event is correlated by the slot rather than by the wire.
            expect_role=role,
            expect_case_id=str(self.case["case_id"]),
            expect_generation=generation,
        )
        return process

    def spawn_claimant(
        self, role: str, *, holder_suffix: str, clock_offset_ms: int
    ) -> RoleProcess:
        """A second claimant on the same resource, under its own clock.

        This is the shape ``ACCEPTANCE.md`` section 2's lease row actually asks
        for: a claimant whose clock has crossed the holder's expiry takes the
        lease over while the holder is frozen. Per-role offsets, never a global
        shift, and the host clock is untouched (design 7).
        """

        holder = f"{self.adapter.holder_of(role)}-{holder_suffix}"
        return self.spawn(
            role,
            armed=(),
            clock_offset_ms=clock_offset_ms,
            key=_claimant_key(role),
            extra_arguments=("--holder", holder),
        )

    # -- barrier -----------------------------------------------------------

    def wait_at_anchor(self, role: str) -> Mapping[str, Any]:
        """Wait until ``role`` reports it is blocked inside its armed window."""

        process = self.processes[role]
        return process.wait_for_event(
            {EVENT_CHECKPOINT, EVENT_SYNC}, self._remaining(self.barrier_timeout_s)
        )

    def barrier_aligned(self, roles: Sequence[str]) -> Mapping[str, Mapping[str, Any]]:
        """Aligned mode (design 5): wait until *every* target is blocked.

        No kill is issued before the barrier is complete, so the kill set is
        applied to a system in a known joint state -- which is what "in
        combination" means here, rather than a race between siblings.
        """

        return {role: self.wait_at_anchor(role) for role in roles}

    def release(self, role: str) -> None:
        """Phase two, pass-through case: reply ``continue`` and let it proceed."""

        self.processes[role].send({"cmd": contract.CMD_CONTINUE})

    def set_clock_offset(self, role: str, offset_ms: int) -> Mapping[str, Any]:
        """Move one role's injected clock while it is blocked at a barrier.

        Per-role by construction (design 7): skew between roles is two offsets,
        never a global shift, and the host clock is never touched.
        """

        process = self.processes[role]
        process.send({"cmd": contract.CMD_SET_CLOCK_OFFSET, "offset_ms": int(offset_ms)})
        return process.wait_for_event(
            {contract.EVENT_CLOCK_OFFSET}, self._remaining(self.barrier_timeout_s)
        )

    # -- faults ------------------------------------------------------------

    def kill(self, role: str, *, assert_exit_status: bool = _POSIX) -> int:
        """Phase two: a real, uncatchable kill of a process inside its window.

        The exit status is asserted to be ``-SIGKILL`` **wherever the platform
        can report it**, which is every POSIX host regardless of the case's
        lane. Keying that check on the lane instead would have left the twelve
        (role x window) cells that gate item 4 is read from -- all on the
        portable lane -- accepting a role process that exited any other way,
        including one that was never killed at all. The lane governs where
        *gate evidence* is read from (design 8.1); it does not govern whether
        the harness checks its own work. Windows produces the same no-unwind
        crash through ``TerminateProcess`` and reports no signal, so there the
        invariants are the whole assertion.
        """

        process = self.processes[role]
        if process.reaped:
            # Already exited and reaped: its pid may have been recycled, and
            # signalling a recycled id is the one thing design section 8.2
            # forbids outright. The recorded exit status is the answer, and for
            # a kill case it is the wrong one -- which is the point.
            status = process.popen.returncode
            if assert_exit_status and status != -signal.SIGKILL:
                raise ContractViolation(
                    f"{role} exited {status}, not -SIGKILL: the case did not "
                    "inject the crash it claims to have injected"
                )
            return status
        if _POSIX:
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # It died on its own between the barrier and the kill. Not an
                # error here: the exit-status assertion below is what decides
                # whether the case still means anything.
                pass
            # The leader is dead but deliberately **not yet reaped**, so its pid
            # -- and therefore the group's pgid -- cannot have been recycled.
            # That is the only window in which it is safe to sweep the group,
            # and sweeping it is not optional: a role process that forked a
            # child (which is exactly what the I-12/I-14 real-component adapters
            # will do) would otherwise leave that grandchild running, holding
            # the controller's pipe open, after every kill case. The reap stays
            # last (design 8.2).
            if process.pgid is not None:
                try:
                    os.killpg(process.pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        else:  # pragma: no cover - Windows jobs only
            process.popen.kill()
        status = process.popen.wait(timeout=self._remaining(self.barrier_timeout_s))
        process.reaped = True
        if assert_exit_status and status != -signal.SIGKILL:
            raise ContractViolation(
                f"{role} exited {status}, not -SIGKILL: the case did not inject "
                "the crash it claims to have injected"
            )
        self._unwedge(role)
        return status

    def sigstop(self, role: str) -> None:
        """Pause a holder so its lease can lapse under it (Linux lane only)."""

        if not _POSIX:  # pragma: no cover - the manifest keeps these off Windows
            raise ContractViolation("SIGSTOP cases are Linux-lane only (design 8.1)")
        process = self.processes[role]
        os.kill(process.pid, signal.SIGSTOP)
        process.stopped = True

    def assert_no_progress_while_stopped(self, role: str, *, settle_s: float = 0.5) -> None:
        """A stopped process consumes nothing. Checked, not assumed.

        The controller has already written ``continue`` to the control pipe. A
        running process would answer it within microseconds; a stopped one
        cannot answer it at all until ``SIGCONT``. If an event arrives here the
        pause did not take, and the case's whole determinism argument -- that
        pause / takeover / return is a sequence rather than a race -- is void.
        """

        process = self.processes[role]
        if not process.stopped:
            raise ContractViolation(f"{role} was never stopped")
        try:
            event = process.events.get(timeout=settle_s)
        except queue.Empty:
            return
        raise ContractViolation(
            f"{role} produced {event!r} while stopped: SIGSTOP did not take, so "
            "the pause/takeover/return order was a scheduling accident"
        )

    def sigcont(self, role: str) -> None:
        process = self.processes[role]
        if process.stopped:
            os.kill(process.pid, signal.SIGCONT)
            process.stopped = False

    def restart(self, role: str, *, armed: Sequence[ArmedAnchor] = ()) -> RoleProcess:
        """Re-execute the same command line with the next restart generation.

        There is no warm state handed across a restart: the command line and the
        database file are the whole input, and the entrypoint must recover
        before it proceeds. The controller waits for the recovery-complete event
        before returning, which is what makes ``restart_order`` sequential and
        each case's intermediate state pinned (design 5).
        """

        previous = self.processes[role]
        process = self.spawn(role, armed=armed, generation=previous.generation + 1)
        process.wait_for_event(
            {EVENT_RECOVERY_COMPLETE}, self._remaining(self.case_timeout_s)
        )
        return process

    def run_to_completion(self, role: str) -> Mapping[str, Any]:
        """Let a role finish its script and exit cleanly."""

        process = self.processes[role]
        event = process.wait_for_event(
            {EVENT_DONE}, self._remaining(self.case_timeout_s)
        )
        status = process.popen.wait(timeout=self._remaining(self.barrier_timeout_s))
        process.reaped = True
        if status != 0:
            raise AssertionError(
                f"{role} exited {status} after reporting done; stderr is at "
                f"{process.stderr_path}"
            )
        return event

    def _unwedge(self, role: str) -> None:
        observer = self.adapter.observer(self.workdir, role)
        unwedge = getattr(observer, "unwedge", None)
        if callable(unwedge):
            unwedge()

    # -- evidence ----------------------------------------------------------

    def observer(self, role: str) -> Any:
        return self.adapter.observer(self.workdir, role)

    def query(self, name: str, **params: Any) -> list[dict]:
        """Run a named invariant query (design 6.2) against the store."""

        queries = self.adapter.invariant_queries()
        if name not in queries:
            raise ContractViolation(
                f"{name!r} is not an invariant this adapter binds; the contract "
                f"names {sorted(contract.SQL_INVARIANTS)}"
            )
        import sqlite3

        store = self.adapter.store_path(
            name, control_plane=self.db_path, workdir=self.workdir
        )
        if not Path(store).exists():
            return []
        connection = sqlite3.connect(store)
        try:
            cursor = connection.execute(queries[name], params)
            columns = [column[0] for column in cursor.description or ()]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            connection.close()

    def last_reported_now_ms(self, *, default: int) -> int:
        """The latest instant any role reported, in the injected frame."""

        instants = [
            int(event["now_ms"])
            for entry in self.all_traces()
            for event in entry["trace"]
            if isinstance(event.get("now_ms"), int)
        ]
        return max(instants) if instants else default

    def traces(self) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        return {role: tuple(process.trace) for role, process in self.processes.items()}

    def all_traces(self) -> Sequence[Mapping[str, Any]]:
        """Every generation's trace, in spawn order -- the failure report body."""

        return tuple(
            {"role": process.role, "generation": process.generation, "trace": tuple(process.trace)}
            for process in self._spawned
        )

    # -- teardown ----------------------------------------------------------

    def teardown(self) -> None:
        """Unconditional, layered, and reaps last (design 8.2).

        The ordering is the point. An exited-but-unreaped leader is a zombie,
        and a zombie's pid -- and therefore its pgid -- cannot be reused until it
        is reaped, so every ``killpg`` in the ladder is guaranteed to address the
        group we created even when the leader died at the first step while
        grandchildren survived. The reap is deliberately last.
        """

        for process in self._spawned:
            try:
                self._teardown_one(process)
            except Exception:  # pragma: no cover - teardown never raises
                pass
        if not _POSIX:  # pragma: no cover - Windows jobs only
            self._job.close()

    def _teardown_one(self, process: RoleProcess) -> None:
        if process.reaped:
            # Already reaped: the id may have been reused and signalling it is
            # forbidden. Nothing left to do but close the pipes.
            self._close_pipes(process)
            return
        if _POSIX and process.pgid is not None:
            for signal_number in (signal.SIGCONT, signal.SIGTERM):
                # A stopped process ignores SIGTERM until it is continued, so
                # SIGCONT leads the ladder.
                try:
                    os.killpg(process.pgid, signal_number)
                except (ProcessLookupError, PermissionError):
                    pass
            _grace(process, TEARDOWN_GRACE_S)
            try:
                os.killpg(process.pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        else:  # pragma: no cover - Windows jobs only
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                )
            except Exception:
                pass
        try:
            process.popen.wait(timeout=5)
        except Exception:  # pragma: no cover - a wedged kernel, not our bug
            pass
        process.reaped = True
        self._close_pipes(process)

    @staticmethod
    def _close_pipes(process: RoleProcess) -> None:
        for stream in (process.popen.stdin, process.popen.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def __enter__(self) -> "Controller":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.teardown()


def _grace(process: RoleProcess, seconds: float) -> None:
    """Wait out the grace period **without reaping** (design 8.2).

    ``Popen.poll()`` and ``waitpid(WNOHANG)`` are forbidden here: both reap on
    success, which would release the pgid mid-ladder and let a later ``killpg``
    address a recycled group. ``waitid`` with ``WNOWAIT`` reports the exit
    without consuming it; where it is unavailable there is no exit polling at
    all and the grace is a plain sleep.
    """

    deadline = time.monotonic() + seconds
    waitid = getattr(os, "waitid", None)
    while time.monotonic() < deadline:
        if waitid is None:  # pragma: no cover - platforms without waitid
            time.sleep(0.05)
            continue
        try:
            result = waitid(
                os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG
            )
        except (ChildProcessError, OSError):  # pragma: no cover
            return
        if result is not None:
            return
        time.sleep(0.05)


def _child_env() -> Mapping[str, str]:
    """Inherit the environment and prepend the paths the driver needs.

    Inheriting rather than hand-building is load-bearing on Windows: an
    interpreter started without ``SystemRoot`` and without the ``PATH`` its DLLs
    are found on never reaches ``main()``. The repository root goes on
    ``PYTHONPATH`` because the driver is spawned by dotted module path, and
    ``src`` because a worktree may not have the package installed.
    """

    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    prefix = [str(root), str(root / "src")]
    existing = environment.get("PYTHONPATH")
    if existing:
        prefix.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(prefix)
    # Hash randomisation cannot reach the harness's determinism (design 4.3
    # derives every seed by sha256), but a fixed value keeps a child's own
    # incidental iteration order stable too.
    environment["PYTHONHASHSEED"] = "0"
    return environment


# ---------------------------------------------------------------------------
# executing one manifest case -- design 5, 7, 8
# ---------------------------------------------------------------------------

def epoch_regressions(history: Sequence[Mapping[str, Any]]) -> list[tuple[dict, dict]]:
    """Applied writes whose epoch went backwards -- the forbidden interleaving.

    ``ACCEPTANCE.md`` section 2's single-writer row: the state item's history is
    a linear sequence with no interleaving from the rejected writer. Refused
    rows are ignored on purpose; a refusal is the evidence that the fence held,
    not evidence that it did not. The history arrives in the database's own
    insertion order, never in the caller's -- under injected skew a timestamp
    ordering would manufacture regressions and hide real ones.
    """

    applied = [row for row in history if row.get("status") == "applied"]
    return [
        (dict(first), dict(second))
        for first, second in zip(applied, applied[1:])
        if (second.get("writer_epoch") or 0) < (first.get("writer_epoch") or 0)
    ]


#: How many times the destination must have been asked, per fault kind.
#:
#: ``drop-delivery``: the refused attempt and the resend that followed it.
#: ``dup-delivery``: both copies of the message, under one key.
#: ``lost-ack``: the first delivery and the re-delivery the missing ack caused.
#: ``recipient-unavailable``: the refused attempts plus the one that landed.
#:     Counting fewer would accept a run in which the recipient was never
#:     actually unavailable, which is the failure mode this table exists for.
#: ``late-ack``: the first delivery and the re-delivery the missing ack caused.
_ATTEMPT_FLOOR: Mapping[str, int] = {
    "drop-delivery": 2,
    "dup-delivery": 2,
    "lost-ack": 2,
    "recipient-unavailable": 4,
    "late-ack": 2,
}

#: How high the **outbox row's own** ``retry_count`` must have climbed, per
#: fault kind.
#:
#: Deliberately a second table and not a reuse of the one above, because the two
#: count different things. ``_ATTEMPT_FLOOR`` counts attempts at a destination
#: *key*, and ``dup-delivery`` reaches its floor of two with two different
#: messages sharing one key -- each row attempted exactly once. Only a fault
#: whose repeat lands on the *same row* raises that row's retry count, so only
#: those appear here. Reusing the other table would fail ``dup-delivery`` for
#: doing precisely what it is supposed to do.
#:
#: This exists because a floor of one says no more than "an attempt happened":
#: an outbox that incremented once and never again, or lost the count across a
#: restart, would satisfy it while breaking ACCEPTANCE.md section 2's
#: "monotonically increasing, restart-surviving retry count".
_RETRY_COUNT_FLOOR: Mapping[str, int] = {
    "drop-delivery": 2,           # the refused attempt and the resend
    "lost-ack": 2,                # the delivery and the re-delivery
    "recipient-unavailable": 4,   # the refused attempts and the one that landed
}


def _armed(case: Mapping[str, Any], role: str) -> list[ArmedAnchor]:
    return [ArmedAnchor.parse(wire) for wire in case["arms"].get(role, ())]


def _claimant_key(role: str) -> str:
    return f"{role}#claimant"


def execute_case(controller: "Controller", case: Mapping[str, Any]) -> dict:
    """Drive one case to its end state and return what the assertions need.

    The shape of a case is entirely manifest data: which roles are armed where,
    whether the barrier is aligned or staggered, the kill and restart orders,
    the clock programme. Nothing here decides anything a case did not declare --
    that is what makes ``case_id + manifest_version`` denote one fully-specified
    case (design 4.1).

    Two observations are taken *during* the run and returned, because they
    cannot be reconstructed afterwards:

    ``at_kill``
        the destination's effect count for each target, read between phase two
        and the restart. This is what proves the window was the window: a kill
        at ``after_effect_before_record`` must find the effect already present,
        and a kill at ``before_durable_write`` must find it absent. Read only at
        the end of the case, both look identical -- recovery has re-attempted by
        then -- and the harness would be certifying the third ACCEPTANCE.md
        section 2 window without ever having entered it.

    ``unresolved_at_kill``
        the outbox rows still unacked at the same moment, so a restart case can
        assert that recovery had something to recover.
    """

    targets: Sequence[str] = case["targets"]
    fault = case["fault"]
    barrier_mode = case["barrier"]
    resolved_skew_ms: int | None = None

    controller.bootstrap()
    for role in targets:
        controller.spawn(role, armed=_armed(case, role))

    if barrier_mode == contract.BARRIER_STAGGERED:
        # Kills are not barrier-simultaneous: A dies at its checkpoint, B keeps
        # operating against the survivor state, then B dies at a later armed
        # checkpoint (design 5). Dispatch is on the *barrier mode*, which is the
        # field design section 5 defines combination semantics on -- not on the
        # fault string, which would leave the declared mode unread.
        for step in case["staggered"]:
            controller.wait_at_anchor(step["wait"])
            controller.kill(step["kill"])
    elif fault in ("sigkill", "staggered-sigkill", "recipient-unavailable", "late-ack"):
        # Aligned mode: every target is frozen inside its window before any kill
        # is issued, so the kill set is applied to a known joint state.
        #
        # ``recipient-unavailable`` and ``late-ack`` are kill-shaped too, and
        # deliberately so. Both invariants ACCEPTANCE.md section 2 names for them
        # are about *surviving a restart* -- "retry count is durable across
        # restarts" and "deliver the ack after the sender has restarted" -- so a
        # case with no kill in it could not observe either. What distinguishes
        # them from a plain ``sigkill`` is the behaviour the driver carries, not
        # the shape of the kill.
        controller.barrier_aligned(targets)
        for role in case["kill_order"]:
            controller.kill(role)
    elif fault in ("sigkill-expire", "resumed-writer-race"):
        # ACCEPTANCE.md section 2's lease row: "kill the lease holder without
        # release", and its single-writer row: "a write is attempted
        # concurrently from a resumed process and its replacement".
        #
        # The two are one mechanic anchored at two different points. The holder
        # is killed at its armed anchor and never releases, so its lease row is
        # left behind with a live expiry and an epoch nobody holds. A claimant
        # whose clock has crossed that expiry then takes the resource over --
        # that is the replacement. Finally the killed holder is restarted: it
        # comes back with *no epoch in memory*, re-runs its script from the top,
        # and meets the claimant's live lease.
        #
        # That last step is why the driver records a refusal at ``acquire``. A
        # SIGKILLed process cannot "return as a stale writer" the way a
        # SIGSTOPped one does -- it has no token left to present -- so the
        # refusal it does earn is the one at the resource boundary, and that is
        # the refusal this case asserts. The stale-token-write half of the row
        # is proved by ``sigstop-expire``, where the holder really does keep its
        # epoch across the takeover.
        role = targets[0]
        controller.barrier_aligned(targets)
        for killed in case["kill_order"]:
            controller.kill(killed)
        claimant = case["claimant"]
        if claimant is not None:
            resolved_skew_ms = contract.resolve_skew_ms(
                claimant["clock"], ttl_ms=case["ttl_ms"], elapsed_ms=case["ttl_ms"]
            )
            controller.spawn_claimant(
                claimant["role"],
                holder_suffix=claimant["holder_suffix"],
                clock_offset_ms=resolved_skew_ms,
            )
            controller.run_to_completion(_claimant_key(claimant["role"]))
        del role
    elif fault == "writer-race":
        # "Two writers race for the same state item." They cannot both be live
        # writers: ``acquire``'s upsert only replaces a lapsed row, so the second
        # claimant on one resource is refused at the resource boundary rather
        # than merged into the history. That refusal *is* section 2's invariant
        # ("a stale writer is rejected, not merged"), and the ledger row it
        # leaves is the durable observable.
        #
        # The incumbent is held at its barrier for the whole race, so the racer
        # provably meets a live lease rather than a lapsed one -- the ordering is
        # a barrier, never a sleep.
        role = targets[0]
        controller.wait_at_anchor(role)
        racer = case["claimant"]
        if racer is not None:
            controller.spawn_claimant(
                racer["role"],
                holder_suffix=racer["holder_suffix"],
                # No skew: the point is that the incumbent's lease is *live*.
                clock_offset_ms=0,
            )
            controller.run_to_completion(_claimant_key(racer["role"]))
        controller.release(role)
        controller.run_to_completion(role)
    elif fault in ("clock-fwd", "clock-back", "sigstop-expire"):
        role = targets[0]
        controller.wait_at_anchor(role)
        if fault == "sigstop-expire":
            # Only while the holder is provably blocked at its sync point:
            # already holding its lease and between operations.
            controller.sigstop(role)
        claimant = case["claimant"]
        if claimant is not None:
            resolved_skew_ms = contract.resolve_skew_ms(
                claimant["clock"], ttl_ms=case["ttl_ms"], elapsed_ms=case["ttl_ms"]
            )
            controller.spawn_claimant(
                claimant["role"],
                holder_suffix=claimant["holder_suffix"],
                clock_offset_ms=resolved_skew_ms,
            )
            controller.run_to_completion(_claimant_key(claimant["role"]))
        if fault == "sigstop-expire":
            # The design's determinism argument for this case is that a stopped
            # process *cannot consume the continue until it is resumed*, so the
            # pause / takeover / return order is a sequence and not a scheduling
            # accident. That argument is only worth anything if it is checked:
            # the release is issued while the holder is still stopped, and the
            # holder must make no progress on it. Without this the signal is
            # decoration -- the process was already blocked on its control pipe.
            controller.release(role)
            controller.assert_no_progress_while_stopped(role)
            controller.sigcont(role)
        else:
            skew = case["skew"]
            if skew is not None:
                # Same-role skew: the offset lands while the process is blocked
                # and the *next* operation observes it.
                resolved_skew_ms = contract.resolve_skew_ms(
                    skew["direction"],
                    ttl_ms=case["ttl_ms"],
                    elapsed_ms=case["ttl_ms"],
                )
                controller.set_clock_offset(role, resolved_skew_ms)
            controller.release(role)
        controller.run_to_completion(role)
    elif fault in (
        "drop-delivery",
        "dup-delivery",
        "lost-ack",
        "dup-ack",
        "re-ack",
        "incident-repeat",
        "incident-replay",
        "observation-outage",
    ):
        # Surface faults: the injection is in what the script does, not in what
        # happens to the process. The barrier is a pass-through here, used to
        # pin the moment rather than to kill -- the ack-multiplicity injections,
        # the repeated incident condition and the broken observation seam all
        # need the script to keep running past the anchor to be observable at
        # all.
        role = targets[0]
        controller.wait_at_anchor(role)
        if not case["release_after_barrier"]:  # pragma: no cover - all seeds release
            raise ContractViolation(
                f"{case['case_id']}: a delivery-surface fault anchors at a "
                "pass-through barrier and must declare release_after_barrier"
            )
        controller.release(role)
        controller.run_to_completion(role)
    else:  # pragma: no cover - FAULT_KINDS is closed and validated
        raise ContractViolation(f"unknown fault kind {fault!r}")

    at_kill = {
        role: {
            key: controller.observer(role).effect_count(key)
            for key in controller.adapter.effect_keys(role, case)
        }
        for role in targets
    }
    unresolved_at_kill = {
        role: _recoverable_state(controller, role, case) for role in targets
    }

    if case["restart_after"]:
        order = case["restart_order"]
        if order == "concurrent":  # pragma: no cover - no case declares it yet
            raise ContractViolation(
                "concurrent restart is a distinct manifest value and no seed "
                "case declares it (design 5)"
            )
        for role in order:
            # Sequential by contract: target N+1 starts only after target N's
            # entrypoint has signalled recovery-complete, so each case pins which
            # component recovers into which intermediate state.
            controller.restart(role, armed=())
            controller.run_to_completion(role)

    return {
        "resolved_skew_ms": resolved_skew_ms,
        "at_kill": at_kill,
        "unresolved_at_kill": unresolved_at_kill,
    }


def _recoverable_state(
    controller: "Controller", role: str, case: Mapping[str, Any]
) -> list[dict]:
    """Durable state the kill left mid-flight, for this role.

    Deliberately wider than the outbox. A kill inside ``bind`` leaves no unacked
    message but does leave a lease row held by a process that no longer exists
    and a half-written binding -- state a restart has to reconcile. Counting
    only outbox rows would call that case "nothing to recover" and let its
    recovery assertion pass on an empty set, which is the same vacuity in a new
    place.
    """

    now_ms = controller.last_reported_now_ms(default=int(case["clock_base_ms"]))
    params = controller.adapter.query_parameters(role, now_ms=now_ms)
    state: list[dict] = []
    state.extend(
        dict(row, evidence="outbox")
        for row in controller.query(
            contract.INVARIANT_RETRY_COUNT_DURABLE,
            holder_prefix=params["holder_prefix"],
        )
        if row.get("status") != "acked"
    )
    state.extend(
        dict(row, evidence="action")
        for row in controller.query(
            contract.INVARIANT_NO_PENDING_ACTION, scope=params["scope"]
        )
    )
    state.extend(
        dict(row, evidence="lease")
        for row in controller.query(
            contract.INVARIANT_LEASE_SINGLE_HOLDER, now_ms=now_ms
        )
        if row["resource"] == params["resource"]
    )
    # Gate item 4 says the work a restart resumes is "unresolved incidents"
    # (D-0001), so an incident still open at the kill is recoverable state in
    # exactly the sense this function means. Before the matrix wrote incident
    # rows the omission cost nothing; once it does, a case that killed with an
    # incident open would otherwise be judged to have had nothing to recover.
    state.extend(
        dict(row, evidence="incident")
        for row in controller.query(
            contract.INVARIANT_UNRESOLVED_INCIDENTS, scope=params["scope"]
        )
    )
    return state


def _assert_incident_collapse(
    case: Mapping[str, Any],
    role: str,
    rows: Sequence[Mapping[str, Any]],
    fail: Any,
) -> None:
    """A repeated incident condition is collapsed under its dedup key.

    ``ACCEPTANCE.md`` section 2's dedup row is explicit that the Issue fixes the
    *fields* and not the semantics: whether a repeat increments ``retry count``
    on the existing incident or opens a linked one is ``Q-0002``, as is the
    re-notification window in absolute time, and "tests must parameterise both
    rather than hard-code either". So this function asserts *the rule the case
    declared* and has no opinion of its own. What it does assert unconditionally
    is the part the Issue does fix (D-0007): a repeat is collapsed under the
    dedup key rather than producing an unbounded stream of unrelated incidents,
    and ``dedup_key`` and ``retry_count`` are present on every row.
    """

    parameters = case.get("incident_params") or {}
    collapse = parameters.get("collapse")
    repeats = int(parameters.get("repeats") or 0)
    expect_collapse = parameters.get("expect_collapse")
    if not rows:
        fail(f"{role}: no incident was raised, so the collapse rule asserts nothing")
        return

    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_key.setdefault(str(row["dedup_key"]), []).append(row)

    for row in rows:
        if not row.get("dedup_key"):
            fail(f"{role}: incident {row['incident_id']!r} has no dedup key (D-0007)")
        if row.get("retry_count") is None:
            fail(f"{role}: incident {row['incident_id']!r} has no retry count (D-0007)")

    if collapse is None:
        # No rule declared: the case is not making a Q-0002 claim, so only the
        # fields are checked. This is the shape every S9 seed case has.
        return

    if expect_collapse is False:
        # The raises fell outside the case's own re-notification window, so
        # there is nothing to collapse *under either rule*: each raise is its
        # own condition as far as the window is concerned. Asserting this is
        # what makes the window a real parameter rather than one that is
        # carried and ignored -- and it is deliberately checked before the rule
        # branch, because outside the window the rule does not apply at all.
        for key, group in by_key.items():
            if len(group) != repeats:
                fail(
                    f"{role}: dedup key {key!r} produced {len(group)} incident(s) "
                    f"for {repeats} raise(s) outside its re-notification window; "
                    "outside the window nothing is collapsed"
                )
            linked = [row for row in group if row["related_incident_id"] is not None]
            if linked:
                fail(
                    f"{role}: dedup key {key!r} linked {len(linked)} incident(s) "
                    "although the raises fell outside its window"
                )
        return

    for key, group in by_key.items():
        if collapse == "increment-in-place":
            if len(group) != 1:
                fail(
                    f"{role}: dedup key {key!r} opened {len(group)} incidents "
                    "under the increment-in-place rule, which collapses onto one"
                )
                continue
            if int(group[0]["retry_count"]) != repeats - 1:
                fail(
                    f"{role}: dedup key {key!r} was raised {repeats} time(s) but "
                    f"its incident carries retry_count={group[0]['retry_count']}, "
                    f"not {repeats - 1}"
                )
        elif collapse == "open-linked":
            if len(group) != repeats:
                fail(
                    f"{role}: dedup key {key!r} opened {len(group)} incidents "
                    f"under the open-linked rule, which opens one per repeat "
                    f"({repeats})"
                )
                continue
            root = [row for row in group if row["related_incident_id"] is None]
            if len(root) != 1:
                fail(
                    f"{role}: dedup key {key!r} has {len(root)} unlinked "
                    "incidents; a linked chain has exactly one root"
                )
                continue
            linked = {row["related_incident_id"] for row in group} - {None}
            if linked and linked != {root[0]["incident_id"]}:
                fail(
                    f"{role}: dedup key {key!r} links to {sorted(linked)}, not "
                    f"to its own chain root {root[0]['incident_id']!r}"
                )
        else:  # pragma: no cover - the vocabulary is validated in the manifest
            fail(f"{role}: {collapse!r} is not a collapse rule this harness implements")


def _assert_observation_classified(
    case: Mapping[str, Any],
    role: str,
    rows: Sequence[Mapping[str, Any]],
    fail: Any,
) -> None:
    """The outage is classified, and classified as exactly one thing.

    ``ACCEPTANCE.md`` section 2's observation row, and D-0006 behind it. The
    assertion is deliberately **not** a disjunction over the two
    non-anomaly states: a harness that classified a genuine read failure as
    ``NO_ACTIVITY_EVIDENCE`` would pass a disjunction while committing the exact
    conflation D-0006 exists to forbid. Each observation mode names one fact
    state and the case asserts that one.

    Nothing here reads a fact state's *meaning* -- Q-0012 is open and this is a
    check that the reader's outcome was named correctly, not that the name
    implies anything.
    """

    observation = case.get("observation") or {}
    mode = observation.get("mode")
    if not rows:
        fail(f"{role}: the observation produced no incident row to classify")
        return
    for row in rows:
        state = row["fact_state"]
        if state not in contract.FACT_STATES:
            fail(
                f"{role}: incident {row['incident_id']!r} carries fact state "
                f"{state!r}, which is outside the closed set (D-0005)"
            )
        if not row.get("detector_version"):
            fail(
                f"{role}: incident {row['incident_id']!r} carries no detector "
                "version; a fact state without one cannot be replayed (D-0007)"
            )
    if mode is None:
        return
    wanted = contract.OBSERVATION_FACT_STATES[mode]
    wrong = [row for row in rows if row["fact_state"] != wanted]
    if wrong:
        fail(
            f"{role}: the observation path was made {mode!r}, which is "
            f"{wanted}; it was classified "
            f"{sorted({row['fact_state'] for row in wrong})}. Collapsing a read "
            "failure and a quiet worker into one state is what D-0006 forbids"
        )


def assert_invariants(
    controller: "Controller",
    case: Mapping[str, Any],
    *,
    resolved_skew_ms: int | None,
    at_kill: Mapping[str, Mapping[str, int]] | None = None,
    unresolved_at_kill: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> None:
    """Assert exactly what the case declared, by name, and nothing else.

    Every failure carries the reproduction line, because a case that cannot be
    re-run alone is not a case (design 4.4).
    """

    repro = repro_line(
        case_id=case["case_id"],
        suite_seed=controller.suite_seed,
        manifest_version=case["manifest_version"],
        resolved_skew_ms=resolved_skew_ms,
        profile=controller.profile,
    )

    def fail(message: str) -> None:
        raise AssertionError(f"{message}\n{repro}\ntraces: {json.dumps(controller.all_traces())}")

    # The instant the final state is read at, in the injected frame: the latest
    # ``now_ms`` any participant reported.
    #
    # A fixed ``base + 4 * ttl`` was the obvious choice and it is wrong: it sits
    # past every lease's expiry, so ``lease-single-holder`` returns nothing on
    # every case and ``no-unowned-outbox``'s liveness arm is false for every row
    # -- two invariants that can then only ever pass. Reading at the last instant
    # the run actually reached keeps both meaningful, and it is exactly the
    # instant a recovering process would see.
    now_ms = controller.last_reported_now_ms(default=int(case["clock_base_ms"]))
    expected = case["expected"]

    for role in case["targets"]:
        params = controller.adapter.query_parameters(role, now_ms=now_ms)

        for name in expected["queries"]:
            wanted = contract.INVARIANT_PARAMETERS[name]
            rows = controller.query(name, **{key: params[key] for key in wanted})

            if name == contract.INVARIANT_NO_UNOWNED_OUTBOX:
                # ACCEPTANCE.md section 2: no outbox row remains in a state with
                # no owner after recovery.
                if rows:
                    fail(f"{role}: {len(rows)} outbox row(s) left unowned: {rows}")
            elif name == contract.INVARIANT_RETRY_COUNT_DURABLE:
                if not rows:
                    fail(f"{role}: no outbox rows at all; the script wrote nothing")
                floor = max(1, _RETRY_COUNT_FLOOR.get(case["fault"], 1))
                for row in rows:
                    if row["retry_count"] < floor:
                        fail(
                            f"{role}: {row['message_id']} carries "
                            f"retry_count={row['retry_count']}; a "
                            f"{case['fault']} case injected at least {floor} "
                            "attempt(s), so a lower durable count means the "
                            "count was not kept across them"
                        )
                    if row["status"] != "acked":
                        fail(f"{role}: {row['message_id']} ended {row['status']!r}, not acked")
            elif name == contract.INVARIANT_SINGLE_ACKED_STATE:
                for row in rows:
                    # Message identity shows exactly one acked state regardless
                    # of ack multiplicity; a duplicate delivery is a second row
                    # under the same dedup key and still one effect.
                    if row["acked_rows"] != row["rows_total"]:
                        fail(f"{role}: dedup key {row['dedup_key']!r} is half-acked: {row}")
            elif name == contract.INVARIANT_LINEAR_WRITER_HISTORY:
                # Non-vacuity first. "No epoch regression" over an empty history
                # is true of a database nobody ever wrote to, and a query that
                # silently matches nothing would report exactly that -- which is
                # how this invariant was vacuous before the scope parameter
                # existed. A history that cannot see a write cannot see an
                # interleaving either.
                if not rows:
                    fail(
                        f"{role}: the write history is empty, so 'no interleaving' "
                        "asserts nothing; the query is not seeing this role's writes"
                    )
                regressions = epoch_regressions(rows)
                if regressions:
                    fail(f"{role}: a rejected writer interleaved: {regressions}")
            elif name == contract.INVARIANT_RECORDED_REFUSALS:
                if case["fault"] == "clock-back":
                    # The backward-skew row of ACCEPTANCE.md section 2 is about
                    # the refusal, not about the absence of a symptom: a renewal
                    # whose new expiry lands at or before its own acquisition is
                    # refused outright rather than silently clamped, and that
                    # refusal is what this case exists to observe.
                    skew_refusals = [
                        row for row in rows if row["refusal"] == "ClockSkewRefused"
                    ]
                    if not skew_refusals:
                        fail(
                            f"{role}: no ClockSkewRefused was recorded, so the "
                            "backward skew never reached the expiry boundary"
                        )
                if case["fault"] in ("dup-ack", "re-ack"):
                    # The ack-multiplicity injections leave no trace in the
                    # control plane by construction -- an idempotent ack changes
                    # nothing, which is the invariant. So the evidence that the
                    # *second* ack happened at all is the ignored-ack row, and
                    # without checking for it the case would pass identically on
                    # a driver that stopped issuing the duplicate.
                    ignored = [
                        row for row in rows if row["refusal"] == "AckAlreadyRecorded"
                    ]
                    if not ignored:
                        fail(
                            f"{role}: no ack was ever ignored as already-recorded, "
                            f"so this {case['fault']} case never issued the second "
                            "ack it claims to inject"
                        )
                # The returning holder's write attempt is refused and that
                # refusal is recorded, not silently dropped. This is a SQL query
                # over a persisted row on purpose: an event-trace line would only
                # prove the harness saw an exception (design 5).
                if not rows:
                    fail(f"{role}: the stale writer's refusal was never recorded")
            elif name == contract.INVARIANT_NO_PENDING_ACTION:
                if rows:
                    fail(f"{role}: recovery left {len(rows)} action(s) pending: {rows}")
            elif name == contract.INVARIANT_LEASE_SINGLE_HOLDER:
                held = [row for row in rows if row["resource"] == params["resource"]]
                if not held and case["fault"] != "sigstop-expire":
                    # Same non-vacuity rule: "at most one live holder" over a
                    # resource nobody holds is a statement about nothing. A
                    # Secretary case is the one exception -- its script ends by
                    # releasing, which is the point of the release step.
                    if contract.OPERATION_LEASE_RELEASE not in contract.ROLE_SCRIPTS[role]:
                        fail(
                            f"{role}: no live holder on {params['resource']!r} at "
                            f"now_ms={now_ms}, so the single-holder assertion is vacuous"
                        )
                if len(held) > 1:
                    fail(f"{role}: {len(held)} live holders on one resource: {held}")
                if case["fault"] == "clock-back" and held:
                    # A holder whose clock ran backwards never gains authority:
                    # a renewal landing at or before the acquisition is refused
                    # outright, and one that is accepted only ever shortens. So
                    # the expiry may never exceed what the acquisition itself
                    # bought (docs/lease-fencing.md, "Holder's clock slow on
                    # renewal"). The ceiling is measured against the row's own
                    # acquisition rather than against the clock base, because the
                    # acquisition instant is what the TTL was added to.
                    ceiling = int(held[0]["acquired_at_ms"]) + int(case["ttl_ms"])
                    if held[0]["expires_at_ms"] > ceiling:
                        fail(
                            f"{role}: a backward-skewed renewal extended the lease "
                            f"to {held[0]['expires_at_ms']} (> {ceiling})"
                        )
            elif name == contract.INVARIANT_INCIDENT_COLLAPSE:
                _assert_incident_collapse(case, role, rows, fail)
            elif name == contract.INVARIANT_UNRESOLVED_INCIDENTS:
                # "Work resumes from unresolved incidents" (gate item 4). After
                # the restart the incident the case opened must still be
                # readable from SQLite alone -- the packet is in the row, not in
                # anyone's context (D-0003, D-0007) -- and it must carry the two
                # fields D-0007 makes mandatory.
                if not rows:
                    fail(
                        f"{role}: no unresolved incident survived, so 'work "
                        "resumes from unresolved incidents' asserts nothing"
                    )
                for row in rows:
                    if not row.get("dedup_key"):
                        fail(f"{role}: incident {row['incident_id']!r} carries no dedup key")
                    if row.get("retry_count") is None:
                        fail(f"{role}: incident {row['incident_id']!r} carries no retry count")
            elif name == contract.INVARIANT_OBSERVATION_CLASSIFIED:
                _assert_observation_classified(case, role, rows, fail)
            elif name == contract.INVARIANT_NO_ANOMALY_ESCALATION:
                # The query is a COUNT, so it always has exactly one row and
                # "none were produced" is a pass rather than an empty result.
                if not rows:  # pragma: no cover - a COUNT always returns a row
                    fail(f"{role}: the escalation count returned no row at all")
                escalations = int(rows[0]["escalations"])
                if escalations:
                    fail(
                        f"{role}: {escalations} termination/restart "
                        "recommendation(s) were produced from an observation "
                        "outage. D-0006: observation-unavailable and "
                        "no-activity-evidence are not anomalies"
                    )
            else:  # pragma: no cover - guarded by the vocabulary check below
                raise ContractViolation(
                    f"{name!r} is a named invariant with no assertion behind it. "
                    "The chain above has no default arm on purpose: a case that "
                    "declared this name would otherwise run its SQL, assert "
                    "nothing, and report coverage it does not have"
                )

        observer = controller.observer(role)
        claimant = case["claimant"]
        # Two different shapes wear the same manifest field. In a takeover the
        # claimant wins and the incumbent is the superseded one; in a race the
        # incumbent is alive and holding, so the *claimant* is the writer that
        # was rejected. Reading them the same way would assert that whichever
        # writer actually won produced no effect at all.
        superseded = (
            claimant is not None
            and claimant["role"] == role
            and case["fault"] in contract.TAKEOVER_FAULTS
        )
        rejected_claimant = (
            claimant is not None
            and claimant["role"] == role
            and case["fault"] not in contract.TAKEOVER_FAULTS
        )
        for name in expected["destination"]:
            keys = controller.adapter.effect_keys(role, case)
            if name == contract.INVARIANT_ONE_EFFECT_PER_KEY:
                # The counterparty's own record, read out of process, after the
                # kill: SQLite alone cannot prove this (ACCEPTANCE.md section 2).
                if superseded:
                    # In a takeover case the effect belongs to the epoch that
                    # won. The interesting half is the other one: the fenced-out
                    # holder came back and reached the destination *zero* times,
                    # which is the destination-side statement of "a stale writer
                    # is rejected, not merged".
                    winner = controller.adapter.effect_keys(
                        role, case, holder_suffix=claimant["holder_suffix"]
                    )
                    for key in winner:
                        count = observer.effect_count(key)
                        if count != 1:
                            fail(f"{role}: claimant key {key!r} produced {count} effects, not one")
                    for key in keys:
                        count = observer.effect_count(key)
                        if count != 0:
                            fail(
                                f"{role}: the superseded holder's key {key!r} "
                                f"reached the destination {count} time(s)"
                            )
                    continue
                for key in keys:
                    count = observer.effect_count(key)
                    if count != 1:
                        fail(f"{role}: {key!r} produced {count} effects, not one")
                if rejected_claimant:
                    # The loser of the race never got a lease, so it never
                    # reached the destination. That zero is the
                    # destination-side statement of "a stale writer is
                    # rejected, not merged" -- the control-plane half is the
                    # recorded refusal.
                    loser = controller.adapter.effect_keys(
                        role, case, holder_suffix=claimant["holder_suffix"]
                    )
                    for key in loser:
                        count = observer.effect_count(key)
                        if count != 0:
                            fail(
                                f"{role}: the rejected writer's key {key!r} "
                                f"reached the destination {count} time(s); it "
                                "was refused and should have reached it none"
                            )
            elif name == contract.INVARIANT_DELIVERED_IMPLIES_EFFECT:
                # A delivery-surface fault is *about* the repeat: the resend
                # after a drop, the second copy of a duplicate, the re-delivery
                # after a lost ack. Counting one attempt would accept a run in
                # which the repeat never happened -- the case would then be
                # reporting coverage of a fault it did not inject. The floor is
                # therefore stated per fault kind and read from the
                # destination's own attempt log.
                floor = _ATTEMPT_FLOOR.get(case["fault"], 1)
                for key in keys:
                    attempts = observer.attempt_count(key)
                    if attempts < floor:
                        fail(
                            f"{role}: {key!r} was attempted {attempts} time(s) at "
                            f"the destination; a {case['fault']} case requires at "
                            f"least {floor}, because the repeat is the evidence"
                        )
                # One effect *record* per delivery dedup key, counted over the
                # destination's whole store and not only over the keys we
                # expected: a per-key existence test cannot see an extra effect
                # published under a key nobody asked about.
                published = list(getattr(observer, "effects", lambda: ())())
                if published and len(published) != len(set(keys)) and not superseded:
                    fail(
                        f"{role}: the destination holds {len(published)} effect "
                        f"records for {len(set(keys))} dedup key(s): {published}"
                    )
                for key in keys:
                    if observer.attempt_count(key) < 1:
                        fail(f"{role}: {key!r} was never attempted at the destination")
                    if observer.effect_count(key) < 1:
                        fail(f"{role}: {key!r} is delivered in our rows but absent at the destination")

    # -- the window was the window ----------------------------------------
    #
    # ACCEPTANCE.md section 2 calls the after-effect window "the one that proves
    # idempotency rather than luck". Proving it requires knowing the effect was
    # already at the destination when the process died -- which is only
    # observable between the kill and the restart, because recovery re-attempts
    # and both windows look identical afterwards.
    if at_kill is not None and case["fault"] in contract.KILL_FAULTS:
        for role in case["targets"]:
            anchors = [ArmedAnchor.parse(wire) for wire in case["arms"].get(role, ())]
            counted = at_kill.get(role, {})
            if not counted or not anchors:
                continue
            anchor = anchors[0].anchor
            if anchor not in contract.CHECKPOINTS:
                continue
            if anchors[0].operation != contract.OPERATION_ATTEMPT:
                # Only the delivery path has effect windows. A kill armed on
                # ``ack`` sits *after* that role's delivery by construction, so
                # counting effects against the anchor's name would be reading a
                # window that operation does not have.
                continue
            occurrence = anchors[0].occurrence
            present = sum(counted.values())
            # Occurrence N means N-1 earlier deliveries have already completed,
            # so the expected count is stated against the occurrence rather than
            # against zero -- otherwise the ``occ2`` variant, which exists
            # precisely to arm a later pass through the loop, would look like a
            # kill that landed too late.
            expected_effects = (
                occurrence
                if anchor in contract.EFFECT_BEARING_CHECKPOINTS
                else occurrence - 1
            )
            if present != expected_effects:
                fail(
                    f"{role}: killed at occurrence {occurrence} of {anchor}, "
                    f"where the destination should hold {expected_effects} "
                    f"effect(s); it held {present}. The kill did not land inside "
                    "the window this case claims to prove"
                )

    owner = expected["recovery_owner"]
    if case["restart_after"] and owner is not None:
        # "Somebody recovered it" is not an assertion (design 5). Two things are
        # checked, because the recovery-complete event alone is emitted by every
        # restart and would be tautological: the named role signalled it, *and*
        # there was unfinished work at the moment of the kill for its recovery to
        # have driven to resolution. A case that left nothing unresolved proves
        # nothing about recovery and is a manifest error, not a pass.
        recovered = [
            entry
            for entry in controller.all_traces()
            if entry["role"] == owner
            and entry["generation"] > 0
            and any(event.get("event") == EVENT_RECOVERY_COMPLETE for event in entry["trace"])
        ]
        if not recovered:
            fail(f"{owner} never signalled recovery-complete after its restart")

        if unresolved_at_kill is not None:
            left_behind = [
                row
                for role in case["targets"]
                for row in unresolved_at_kill.get(role, ())
            ]
            if not left_behind:
                fail(
                    "the kill left no durable state -- no unacked message, no "
                    "pending action, no held lease -- so the restart recovered "
                    f"nothing and naming {owner!r} as the recovery owner "
                    "asserts recovery vacuously"
                )

    if case["restart_after"] and owner is None and unresolved_at_kill is not None:
        # The other direction, so the rule cannot be satisfied by simply
        # declining to name an owner: a case that *did* leave work behind must
        # say whose recovery resolved it.
        left_behind = [
            row
            for role in case["targets"]
            for row in unresolved_at_kill.get(role, ())
        ]
        if left_behind:
            fail(
                f"the kill left {len(left_behind)} piece(s) of durable state "
                "behind but the case names no recovery owner; 'somebody "
                "recovered it' is not an assertion (design 5)"
            )
