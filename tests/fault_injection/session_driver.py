"""The #18 adapter: the real orchestration walk under the fault harness.

Where ``spike_driver`` binds the harness to the S6/S7 spike surface, this
module binds it to the *real* crash-window components: the
``SessionOrchestrator`` (supervisor join layer), the staged ``session``
binding, and ``ClaudeCliSessionProvider`` over a deterministic fake CLI. It is
the second member of ``ADAPTER_MODULES`` -- the only files in this package
allowed to import ``claude_org_runtime`` -- and it exists so the four
injection points of gate item 2 are exercised by a real ``SIGKILL`` against a
real process at an armed anchor, not by an in-process simulation.

One role (``sup``), one operation (``session-start``). The orchestrator's own
seams (:data:`~claude_org_runtime.supervisor.session_orchestrator.SEAMS`) are
mapped onto the barrier's anchors, so the kill lands exactly where the case
says: before the binding commit, between the commit and the spawn, between the
spawn and the read-back's commit, or after the read-back's commit.

Two deliberate departures from the spike driver's rules, stated rather than
hidden:

- ``time.sleep`` appears here (and only for the read-back poll). Every
  *timestamp* still comes from the injected :class:`Clock` -- the wall wait is
  IO pacing against a real subprocess, never a figure that reaches a row, and
  never a measured admission-window width (U34).
- The driver is not in the conformance battery's ``ADAPTERS`` tuple: the full
  battery presupposes a three-role delivery loop this adapter deliberately
  does not have. Its own reachability/kill/recovery checks live in
  ``tests/gate_item2/test_session_driver_harness.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from claude_org_runtime.control_plane import schema
from claude_org_runtime.control_plane.lease import LeaseHeld
from claude_org_runtime.session.claude_cli_provider import ClaudeCliSessionProvider
from claude_org_runtime.supervisor.session_orchestrator import (
    SEAM_AFTER_ADMISSION_BEFORE_SPAWN,
    SEAM_AFTER_READBACK_COMMIT,
    SEAM_AFTER_SPAWN_BEFORE_READBACK_COMMIT,
    SEAM_BEFORE_ADMISSION_COMMIT,
    SessionOrchestrator,
)

from tests.fault_injection import contract
from tests.fault_injection.contract import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_HELLO,
    EVENT_RECOVERY_COMPLETE,
    EVENT_SYNC,
)
from tests.fault_injection.spike_driver import (
    _INVARIANT_QUERIES,
    Barrier,
    Clock,
    RESTART_CLOCK_ADVANCE_MS,
)

__all__ = ["SESSION_ADAPTER", "SessionAdapter", "DRIVER_MODULE", "main"]

DRIVER_MODULE = "tests.fault_injection.session_driver"

RUN_ID = "run-session-start"
RESOURCE = f"session-run:{RUN_ID}"
HOLDER = "sup-session"

#: The orchestrator's seams, mapped onto the contract's anchors. The first
#: three are checkpoint windows; the fourth is the sync point (there is no
#: further write for a checkpoint to sit in front of).
_SEAM_ANCHORS: Mapping[str, tuple[str, str]] = {
    SEAM_BEFORE_ADMISSION_COMMIT: (
        contract.CHECKPOINT_BEFORE_DURABLE_WRITE,
        contract.EVENT_CHECKPOINT,
    ),
    SEAM_AFTER_ADMISSION_BEFORE_SPAWN: (
        contract.CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
        contract.EVENT_CHECKPOINT,
    ),
    SEAM_AFTER_SPAWN_BEFORE_READBACK_COMMIT: (
        contract.CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD,
        contract.EVENT_CHECKPOINT,
    ),
    SEAM_AFTER_READBACK_COMMIT: (
        contract.SYNC_IDENTITY_READBACK_COMMITTED,
        EVENT_SYNC,
    ),
}

#: The deterministic fake CLI. Same discipline as the S2 test harness: it
#: honours whatever identity it is told to claim and refuses nothing (U27/U32
#: assumed absent by construction), logs every spawn, and emits the minimal
#: stream-json walk (init -> result) so the identity read-back is positive.
_FAKE_CLI = """
import json, os, sys, time

args = sys.argv[1:]

if "--version" in args:
    print("9.9.9-fake (Claude Code)")
    sys.exit(0)

if "--help" in args:
    print("Usage: claude [options] [command] [prompt]")
    print("  -p, --print                Print response and exit")
    print("  --session-id <uuid>        Use a specific session ID")
    print("  -r, --resume [value]       Resume a conversation by session ID")
    print("  --output-format <format>   Output format (json | stream-json)")
    print("  --verbose                  Override verbose mode")
    sys.exit(0)

def value_of(flag):
    return args[args.index(flag) + 1] if flag in args else None

claimed = value_of("--session-id") or value_of("--resume")

# The destination's own effect record: one start line at spawn, one exit line
# at exit, each stamped with the destination's wall clock. The observer reads
# concurrent liveness back out of these intervals, so two overlapping
# processes on one id are detectable even after both have exited. (This is
# the fake's clock, not the driver's -- no injected-clock rule applies to the
# destination's own ledger.)
log = os.environ.get("SESSION_DRIVER_SPAWN_LOG")
def ledger(event):
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, "uuid": claimed,
                                     "pid": os.getpid(), "t": time.time(),
                                     "argv": args}) + "\\n")

ledger("start")

def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()

emit({"type": "system", "subtype": "init", "session_id": claimed})
emit({"type": "result", "subtype": "success", "terminal_reason": "completed",
      "session_id": claimed})
ledger("exit")
sys.exit(0)
"""


def fake_cli_path(workdir: Path) -> Path:
    return Path(workdir) / "fake_claude.py"


def spawn_log_path(workdir: Path) -> Path:
    return Path(workdir) / "session-spawns.jsonl"


def state_root_path(workdir: Path) -> Path:
    return Path(workdir) / "session-state"


def session_uuid_for(case_id: str, generation: int) -> str:
    """Deterministic identity per (case, generation) -- never ``uuid4``.

    Deterministic so two runs of one case produce comparable traces; distinct
    per generation so a recovery that finds no binding mints a genuinely fresh
    identity rather than colliding with a half-dead claim.
    """

    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"interlock-i18:{case_id}:{generation}")
    )


# ---------------------------------------------------------------------------
# the driver process
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=DRIVER_MODULE,
        description=(
            "#18 role driver: the real session-start walk under the fault "
            "controller. Spawned by the harness; not useful by hand."
        ),
    )
    parser.add_argument("--role", required=True, choices=[contract.ROLE_SUPERVISOR])
    parser.add_argument("--db", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--suite-seed", type=int, required=True)
    parser.add_argument("--armed", default="")
    parser.add_argument("--clock-base-ms", type=int, required=True)
    parser.add_argument("--clock-offset-ms", type=int, default=0)
    parser.add_argument("--restart-generation", type=int, default=0)
    parser.add_argument("--control-fd", type=int, default=0)
    parser.add_argument("--event-fd", type=int, default=1)
    # adapter-specific
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--ttl-ms", type=int, default=30_000)
    return parser


def _run_walk(
    *,
    connection,
    workdir: Path,
    case_id: str,
    generation: int,
    ttl_ms: int,
    clock: Clock,
    barrier: Barrier,
    armed: Sequence[contract.ArmedAnchor],
    emit: Callable[[Mapping[str, Any]], None],
) -> None:
    os.environ["SESSION_DRIVER_SPAWN_LOG"] = str(spawn_log_path(workdir))
    provider = ClaudeCliSessionProvider(
        state_root_path(workdir),
        claude_command=(sys.executable, str(fake_cli_path(workdir))),
        stop_timeout=2.0,
    )
    effect_window_armed = any(
        anchor.anchor == contract.CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD
        for anchor in armed
    )

    def seam(name: str) -> None:
        anchor, kind = _SEAM_ANCHORS[name]
        if (
            anchor == contract.CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD
            and effect_window_armed
        ):
            # "After the effect" must be true *at the barrier*, not merely
            # after Popen returned: the controller samples the destination's
            # ledger between the kill and the restart, and a child that had
            # not yet written its start line would make a genuinely-entered
            # window read as never entered. IO pacing only -- the ledger is
            # the destination's own record, and no timestamp from here
            # reaches a row.
            log = spawn_log_path(workdir)
            for _ in range(1_000):
                if log.exists() and '"start"' in log.read_text(encoding="utf-8"):
                    break
                time.sleep(0.01)
        barrier.hit(anchor, operation=contract.OPERATION_SESSION_START, kind=kind)

    orchestrator = SessionOrchestrator(
        connection,
        provider,
        run_id=RUN_ID,
        holder=f"{HOLDER}-g{generation}",
        workspace=str(Path(workdir) / "workspace"),
        role="worker",
        now_ms=clock.now_ms,
        session_uuid_factory=lambda: session_uuid_for(case_id, generation),
        settings={"prompt": "reply with ok", "resume_prompt": "resume"},
        ttl_ms=ttl_ms,
        resource=RESOURCE,
        readback_attempts=400,
        # IO pacing against a real subprocess; no timestamp is ever read from
        # the host clock (the Clock above supplies every now_ms).
        wait=lambda: time.sleep(0.01),
        attempt_id_factory=_attempt_ids(case_id, generation),
        seam=seam,
    )

    if generation == 0:
        orchestrator.start()
        return

    # Recovery: the predecessor was SIGKILLed holding the lease, and a lease
    # cannot tell dead from slow -- the retry waits out the TTL. The wait is
    # the injected clock's, never the wall's.
    for attempt in range(3):
        try:
            orchestrator.recover()
            break
        except LeaseHeld:
            clock.advance(ttl_ms + 1_000)
    else:  # pragma: no cover - two advances always clear one TTL
        raise AssertionError("the dead claimant's lease never expired")
    emit({"event": EVENT_RECOVERY_COMPLETE, "now_ms": clock.now_ms()})


def _attempt_ids(case_id: str, generation: int) -> Callable[[], str]:
    counter = {"n": 0}

    def next_id() -> str:
        counter["n"] += 1
        return f"{case_id}:g{generation}:attempt-{counter['n']}"

    return next_id


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(argv) if argv is not None else None)

    event_fd = os.dup(arguments.event_fd)
    if arguments.event_fd == 1:
        os.dup2(2, 1)
    events = os.fdopen(event_fd, "w", encoding="utf-8", newline="\n")
    control = os.fdopen(arguments.control_fd, "r", encoding="utf-8")

    def emit(message: Mapping[str, Any]) -> None:
        events.write(json.dumps(message, sort_keys=True) + "\n")
        events.flush()

    emit(
        {
            "event": EVENT_HELLO,
            "protocol_version": contract.PROTOCOL_VERSION,
            "contract_version": contract.FAULT_RUNNER_CONTRACT_VERSION,
            "role": arguments.role,
            "case_id": arguments.case_id,
            "restart_generation": arguments.restart_generation,
            "adapter": SessionAdapter.name,
        }
    )

    armed = tuple(
        contract.ArmedAnchor.parse(item)
        for item in arguments.armed.split(",")
        if item.strip()
    )
    clock = Clock(
        base_ms=arguments.clock_base_ms
        + arguments.restart_generation * RESTART_CLOCK_ADVANCE_MS,
        offset_ms=arguments.clock_offset_ms,
    )
    barrier = Barrier(armed=armed, emit=emit, control=control, clock=clock)

    connection = schema.open_control_plane(arguments.db)
    try:
        _run_walk(
            connection=connection,
            workdir=Path(arguments.workdir),
            case_id=arguments.case_id,
            generation=arguments.restart_generation,
            ttl_ms=arguments.ttl_ms,
            clock=clock,
            barrier=barrier,
            armed=armed,
            emit=emit,
        )
        emit({"event": EVENT_DONE, "now_ms": clock.now_ms()})
        return 0
    except BaseException as error:  # noqa: BLE001 - the driver reports, never hides
        emit({"event": EVENT_ERROR, "type": type(error).__name__})
        import traceback

        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        try:
            connection.close()
        except Exception:  # pragma: no cover - closing a dead connection
            pass


# ---------------------------------------------------------------------------
# the destination observer and the adapter object
# ---------------------------------------------------------------------------


class _SessionObserver:
    """The destination's own record: real processes and captured streams.

    A spawned process is the external effect of ``session-start``, so both
    reports are read from outside the killed role -- ``/proc`` for liveness,
    the provider's captured ``events-*.jsonl`` for the transcript stand-in --
    never inferred from control-plane rows (``ACCEPTANCE.md`` section 2).
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = Path(workdir)

    # -- the two #18 reports ------------------------------------------------

    def _session_uuids(self) -> list[str]:
        root = state_root_path(self._workdir)
        if not root.is_dir():
            return []
        return sorted(entry.name for entry in root.iterdir() if entry.is_dir())

    def _ledger(self) -> list[dict]:
        log = spawn_log_path(self._workdir)
        if not log.exists():
            return []
        entries = []
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def _live_now(self, session_uuid: str) -> int:
        """Processes live right now on this id -- filtered to this workdir,
        so a concurrent run of the same case elsewhere is never counted."""

        count = 0
        marker = str(fake_cli_path(self._workdir))
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if session_uuid in cmdline and marker in cmdline:
                count += 1
        return count

    def live_process_report(self) -> Mapping[str, int]:
        """``{session_uuid: max concurrently-live process count}``.

        Read from the destination's own start/exit ledger, not from our rows:
        each spawn writes a start line and (on a normal exit) an exit line, so
        the maximum interval overlap per id is answerable *after* every child
        has exited -- a post-hoc /proc scan alone would report 0 for a
        violation whose two processes both finished (the U27 shape does
        exactly that). A start with no exit line is treated as still open if
        the process is live now, and as an unknowable point otherwise (a
        SIGKILLed child writes no exit; its interval is not invented).
        """

        report: dict[str, int] = {uuid_: 0 for uuid_ in self._session_uuids()}
        intervals: dict[str, list[tuple[float, float]]] = {}
        opens: dict[tuple[str, int], float] = {}
        for entry in self._ledger():
            uuid_ = entry.get("uuid")
            pid = entry.get("pid")
            if not uuid_:
                continue
            report.setdefault(uuid_, 0)
            if entry.get("event") == "start":
                opens[(uuid_, pid)] = float(entry["t"])
            elif entry.get("event") == "exit":
                started = opens.pop((uuid_, pid), None)
                if started is not None:
                    intervals.setdefault(uuid_, []).append((started, float(entry["t"])))
        for (uuid_, _pid), started in opens.items():
            live_now = self._live_now(uuid_)
            if live_now:
                intervals.setdefault(uuid_, []).append((started, float("inf")))
        for uuid_ in report:
            spans = sorted(intervals.get(uuid_, ()))
            peak = 0
            for index, (start, end) in enumerate(spans):
                overlap = 1 + sum(
                    1
                    for other_start, other_end in spans[:index]
                    if other_end > start
                )
                peak = max(peak, overlap)
            report[uuid_] = max(peak, self._live_now(uuid_))
        return report

    def transcript_report(self) -> Mapping[str, Mapping[str, Any]]:
        """Per session: the identities its streams name, and doubled turns.

        A stream (one ``events-NNN.jsonl``) belongs to one child process. Two
        writers into one stream would double its ``init``/``result`` events;
        a foreign writer would put a second identity into it. Both are the
        interleaving item 2 forbids.
        """

        report: dict[str, dict[str, Any]] = {}
        for session_uuid in self._session_uuids():
            distinct: set[str] = set()
            duplicates = 0
            directory = state_root_path(self._workdir) / session_uuid
            for stream in sorted(directory.glob("events-*.jsonl")):
                seen: dict[str, int] = {}
                for line in stream.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("session_id"):
                        distinct.add(event["session_id"])
                    kind = event.get("type")
                    if kind in ("system", "result"):
                        seen[kind] = seen.get(kind, 0) + 1
                duplicates += sum(count - 1 for count in seen.values() if count > 1)
            report[session_uuid] = {
                "distinct_ids": sorted(distinct),
                "duplicate_turn_ids": duplicates,
            }
        return report

    # -- the generic observer surface ---------------------------------------

    def effect_count(self, key: str) -> int:
        """Spawns on one session id, counted from the destination's ledger.

        The key shape is ``spawn:<session_uuid>``. This is what lets the
        controller's window-landing gate prove the kill really fell where the
        case claims: a kill before the effect window must find zero spawn
        records, and one after it exactly one.
        """

        if not key.startswith("spawn:"):
            return 0
        session_uuid = key[len("spawn:"):]
        return sum(
            1
            for entry in self._ledger()
            if entry.get("event") == "start" and entry.get("uuid") == session_uuid
        )

    def attempt_count(self, key: str) -> int:
        return self.effect_count(key)

    def unwedge(self) -> None:  # pragma: no cover - no delivery surface
        return None


class SessionAdapter:
    """Binds the harness seam to the real #18 components."""

    name = "session"
    driver_module = DRIVER_MODULE

    def bootstrap(self, db_path: Any, *, roles: Sequence[str], now_ms: int) -> None:
        connection = schema.create_control_plane(db_path)
        try:
            connection.execute(
                "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
                " VALUES (?, 'running', ?, ?)",
                (RUN_ID, int(now_ms), int(now_ms)),
            )
            connection.commit()
        finally:
            connection.close()
        workdir = Path(db_path).parent
        fake_cli_path(workdir).write_text(_FAKE_CLI, encoding="utf-8")
        (workdir / "workspace").mkdir(exist_ok=True)

    def role_arguments(
        self, role: str, *, case: Mapping[str, Any], workdir: Any
    ) -> Sequence[str]:
        return (
            "--workdir",
            str(workdir),
            "--ttl-ms",
            str(case.get("ttl_ms", 30_000)),
        )

    def observer(self, workdir: Any, role: str) -> _SessionObserver:
        return _SessionObserver(Path(workdir))

    def invariant_queries(self) -> Mapping[str, str]:
        # The store is the same spike control-plane schema, so the spike
        # adapter's SQL binds unchanged -- re-spelled here would drift.
        return dict(_INVARIANT_QUERIES)

    def store_path(self, name: str, *, control_plane: Any, workdir: Any) -> Path:
        return Path(control_plane)

    def query_parameters(self, role: str, *, now_ms: int) -> Mapping[str, Any]:
        return {
            "resource": RESOURCE,
            "holder": HOLDER,
            "holder_prefix": f"{HOLDER}-m%",
            "scope": RUN_ID,
            "now_ms": int(now_ms),
        }

    def effect_keys(
        self,
        role: str,
        case: Mapping[str, Any],
        *,
        holder_suffix: str | None = None,
    ) -> tuple[str, ...]:
        # One key: the generation-0 spawn. The controller samples its count
        # between the kill and the restart, which is what proves the kill
        # landed inside the claimed window (zero spawns before the effect
        # window, exactly one after it). The whole-store reports
        # (live_process_report / transcript_report) ride beside it.
        return (f"spawn:{session_uuid_for(case['case_id'], 0)}",)

    def holder_of(self, role: str) -> str:
        return HOLDER

    def checkpoint_vocabulary(self) -> Sequence[str]:
        return tuple(contract.CHECKPOINTS)


SESSION_ADAPTER = SessionAdapter()


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
