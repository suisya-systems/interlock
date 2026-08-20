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


class BarrierTimeout(AssertionError):
    """An armed barrier was never reached. A harness fault, not a component one."""


class CaseTimeout(AssertionError):
    """A case outran its budget (design 9). Converted from a CI hang."""


def repro_line(
    *,
    case_id: str,
    suite_seed: int,
    manifest_version: int,
    contract_version: int = contract.FAULT_RUNNER_CONTRACT_VERSION,
    resolved_skew_ms: int | None = None,
) -> str:
    """The single reproduction line a failing case prints (design 4.4).

    Re-run with ``pytest tests/fault_injection -k <case_id>`` and the suite seed
    in ``S9_SUITE_SEED``. Same case id, same suite seed, same manifest version
    give the same armed windows, the same payloads and the same schedule.
    """

    return (
        f"S9-REPRO case_id={case_id} suite_seed={suite_seed} "
        f"manifest_version={manifest_version} contract_version={contract_version} "
        f"resolved_skew_ms={resolved_skew_ms}"
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
    ) -> None:
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
        ).check()
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

    def kill(self, role: str, *, assert_exit_status: bool) -> int:
        """Phase two: a real, uncatchable kill of a process inside its window.

        On the Linux conformance lane the exit status is asserted to be
        ``-SIGKILL``: a role process that exited any other way did not
        experience the crash the case is about, and that is a harness error.
        On the portable lane the same effect is produced by ``Popen.kill()``
        (``TerminateProcess`` on Windows) and only the invariants are asserted.
        """

        process = self.processes[role]
        if _POSIX:
            os.kill(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows jobs only
            process.popen.kill()
        # The reap is what releases the pid, and therefore the pgid, for reuse;
        # nothing may signal this group afterwards (design 8.2).
        status = process.popen.wait(timeout=self._remaining(self.barrier_timeout_s))
        process.reaped = True
        if assert_exit_status and _POSIX and status != -signal.SIGKILL:
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
    """

    targets: Sequence[str] = case["targets"]
    fault = case["fault"]
    linux_lane = case["lane"] == contract.LANE_LINUX
    resolved_skew_ms: int | None = None

    controller.bootstrap()
    for role in targets:
        controller.spawn(role, armed=_armed(case, role))

    if fault == "staggered-sigkill":
        # Kills are not barrier-simultaneous: A dies at its checkpoint, B keeps
        # operating against the survivor state, then B dies at a later armed
        # checkpoint (design 5).
        for step in case["staggered"]:
            controller.wait_at_anchor(step["wait"])
            controller.kill(step["kill"], assert_exit_status=linux_lane)
    elif fault == "sigkill":
        # Aligned mode: every target is frozen inside its window before any kill
        # is issued, so the kill set is applied to a known joint state.
        controller.barrier_aligned(targets)
        for role in case["kill_order"]:
            controller.kill(role, assert_exit_status=linux_lane)
    elif fault in ("clock-fwd", "clock-back", "sigstop-expire"):
        role = targets[0]
        controller.wait_at_anchor(role)
        if fault == "sigstop-expire":
            # Only while the holder is provably blocked at its sync point:
            # already holding its lease and between operations. Being stopped it
            # cannot consume the ``continue`` until it is resumed, which is what
            # makes pause / takeover / return a sequence rather than a race.
            controller.sigstop(role)
        claimant = case["claimant"]
        if claimant is not None:
            resolved_skew_ms = contract.resolve_skew_ms(
                "forward", ttl_ms=case["ttl_ms"], elapsed_ms=0
            )
            controller.spawn_claimant(
                claimant["role"],
                holder_suffix=claimant["holder_suffix"],
                clock_offset_ms=resolved_skew_ms,
            )
            controller.run_to_completion(_claimant_key(claimant["role"]))
        if fault == "sigstop-expire":
            controller.sigcont(role)
        skew = case["skew"]
        if skew is not None:
            # Same-role skew: the offset lands while the process is blocked and
            # the *next* operation observes it. An expectation depending on an
            # in-flight call seeing a mid-call skew is refused at validation.
            resolved_skew_ms = contract.resolve_skew_ms(
                skew["direction"],
                ttl_ms=case["ttl_ms"],
                elapsed_ms=case["ttl_ms"],
            )
            controller.set_clock_offset(role, resolved_skew_ms)
        controller.release(role)
        controller.run_to_completion(role)
    elif fault in ("drop-delivery", "dup-delivery", "lost-ack"):
        role = targets[0]
        controller.wait_at_anchor(role)
        controller.release(role)
        controller.run_to_completion(role)
    else:  # pragma: no cover - FAULT_KINDS is closed and validated
        raise ContractViolation(f"unknown fault kind {fault!r}")

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

    return {"resolved_skew_ms": resolved_skew_ms}


def assert_invariants(
    controller: "Controller", case: Mapping[str, Any], *, resolved_skew_ms: int | None
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
    )

    def fail(message: str) -> None:
        raise AssertionError(f"{message}\n{repro}\ntraces: {json.dumps(controller.all_traces())}")

    now_ms = int(case["clock_base_ms"]) + int(case["ttl_ms"]) * 4
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
                for row in rows:
                    if row["retry_count"] < 1:
                        fail(f"{role}: {row['message_id']} never recorded an attempt: {row}")
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
                regressions = epoch_regressions(rows)
                if regressions:
                    fail(f"{role}: a rejected writer interleaved: {regressions}")
            elif name == contract.INVARIANT_RECORDED_REFUSALS:
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
                if len(held) > 1:
                    fail(f"{role}: {len(held)} live holders on one resource: {held}")
                if case["fault"] == "clock-back" and held:
                    # A holder whose clock ran backwards shortens its own lease:
                    # its authority ends earlier, never later (docs/lease-fencing.md).
                    ceiling = int(case["clock_base_ms"]) + int(case["ttl_ms"])
                    if held[0]["expires_at_ms"] >= ceiling:
                        fail(
                            f"{role}: a backward-skewed renewal extended the lease "
                            f"to {held[0]['expires_at_ms']} (>= {ceiling})"
                        )

        observer = controller.observer(role)
        claimant = case["claimant"]
        superseded = claimant is not None and claimant["role"] == role
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
            elif name == contract.INVARIANT_DELIVERED_IMPLIES_EFFECT:
                for key in keys:
                    if observer.attempt_count(key) < 1:
                        fail(f"{role}: {key!r} was never attempted at the destination")
                    if observer.effect_count(key) < 1:
                        fail(f"{role}: {key!r} is delivered in our rows but absent at the destination")

    owner = expected["recovery_owner"]
    if case["restart_after"] and owner is not None:
        # "Somebody recovered it" is not an assertion (design 5): the case names
        # which restarted role's recovery it holds responsible, and the evidence
        # is that role's own recovery-complete event in a later generation.
        recovered = [
            entry
            for entry in controller.all_traces()
            if entry["role"] == owner
            and entry["generation"] > 0
            and any(event.get("event") == EVENT_RECOVERY_COMPLETE for event in entry["trace"])
        ]
        if not recovered:
            fail(f"{owner} never signalled recovery-complete after its restart")
