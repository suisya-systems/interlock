"""Interlock's own fail-closed spawn precondition.

D-0023 part 2: "Interlock validates the rendered per-role configuration and
refuses to spawn on a broken one". The obligation is Interlock's under D-0017
*regardless of provider*, which is why this file survived the C1 -> C2 switch
unchanged in intent while #8 did not survive at all.

The shape that matters is negative: on a broken configuration the spawner
callable is **never invoked**. Not invoked with a narrowed fence, not invoked
with a warning logged -- not invoked. A downgraded spawn is the failure mode
the criterion names, and it is the one a "best effort" renderer produces.

Three brokenness classes are named by issue #9 and each has a test:

===================================  =========================================
broken configuration                 caught by
===================================  =========================================
config deleted                       ``document-unreadable`` / ``role-absent``
hook path unresolvable               ``hook-unresolvable``
sandbox profile absent               ``sandbox-profile-absent``
===================================  =========================================

A fourth is caught here rather than in the renderer: a fence that renders
cleanly but whose own breach battery does not deny every rule. That is a
self-check, and it refuses the spawn too -- shipping a fence Interlock cannot
itself prove is the same class of error as shipping no fence.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .battery import BatteryReport, ProbeSynthesisError, run_battery
from .renderer import FenceContext, FenceRefusal, RefusalReason, render_fence
from .rules import Fence
from .state import write_fence

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

def _fsync_dir(path: Path) -> None:
    """Best effort: not every platform lets a directory be opened for fsync."""

    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(fd)


EVENT_ADMITTED = "spawn-admitted"
EVENT_REFUSED = "spawn-refused"
EVENT_BATTERY = "battery-run"

REASON_BATTERY_INCOMPLETE = "battery-incomplete"
REASON_PROBE_UNSYNTHESIZABLE = "probe-unsynthesizable"


def default_hook_script() -> Path:
    """The deny hook's own file, as an absolute path."""

    return Path(__file__).resolve().with_name("hook.py")


class FenceLedger:
    """Append-only JSONL record of spawn admissions and refusals.

    "Recorded durably" is taken literally: every event is flushed and
    ``fsync``ed before the caller is told anything, because a refusal lost on
    crash is a refusal that was not recorded -- and the crash is precisely the
    moment the record is wanted.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._thread_lock = threading.RLock()

    @contextlib.contextmanager
    def transaction(self):
        with self._thread_lock:
            if fcntl is None:  # pragma: no cover - Windows
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + ".lock")
            with open(lock_path, "a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def append(self, event: str, **payload: Any) -> dict[str, Any]:
        entry = {"event": event, "at": self._clock(), **payload}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # ``fsync`` on a *newly created* file does not promise its directory
        # entry survives a power loss -- the bytes would be on disk under a
        # pathname that no longer exists. The parent is synced on creation so
        # a refusal recorded seconds before a crash is still there afterwards.
        is_new = not self.path.exists()
        line = json.dumps(entry, sort_keys=True) + "\n"
        # ``newline=""`` pins the JSONL record separator to the ``\n`` written
        # above rather than a platform-dependent CRLF, matching
        # ``curator.ledger.ApprovalLedger``.
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if is_new:
            _fsync_dir(self.path.parent)
        return entry

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def refusals(self) -> list[dict[str, Any]]:
        return [entry for entry in self.events() if entry["event"] == EVENT_REFUSED]


@dataclass(frozen=True)
class SpawnPlan:
    """What a spawner is handed once, and only once, the fence is sound."""

    role: str
    fence: Fence
    settings_path: Path
    fence_path: Path
    context: FenceContext

    def cli_args(self) -> list[str]:
        """The public-CLI flags this fence renders to (D-0010).

        ``--permission-mode`` is passed explicitly rather than left to the
        settings file, because i01 §3.9 showed ``permissionMode`` is the one
        part of the fence the provider reads back -- so it is the one part a
        restart can be checked against directly.
        """

        return [
            "--settings",
            str(self.settings_path),
            "--permission-mode",
            self.fence.permission_mode,
        ]


@dataclass(frozen=True)
class SpawnOutcome:
    admitted: bool
    role: str
    fence: Fence | None = None
    plan: SpawnPlan | None = None
    result: Any = None
    reasons: tuple[tuple[str, str], ...] = ()
    battery: BatteryReport | None = None

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(code for code, _ in self.reasons)


@dataclass
class FencedSpawner:
    """Renders, validates, publishes and only then spawns.

    ``spawner`` is injected so the precondition is testable without a real
    ``claude -p`` child; the live probe that exercises the real one lives in
    ``investigation/i04_pretooluse_probe.py``.
    """

    ledger: FenceLedger
    document: Mapping[str, Any] | None = None
    settings_name: str = "settings.local.json"
    _last_battery: BatteryReport | None = field(default=None, init=False, repr=False)

    def spawn(
        self,
        role: str,
        ctx: FenceContext,
        spawner: Callable[[SpawnPlan], Any],
    ) -> SpawnOutcome:
        """Admit or refuse, then -- only if admitted -- start the child.

        The child is started **outside** the ledger transaction. A synchronous
        spawner (``subprocess.run`` on a ``claude -p`` session) would otherwise
        hold the cross-process lock for the entire session, and every other
        role would block on it -- including roles trying to record a *refusal*,
        which is the one thing that must never wait.
        """

        outcome = self._admit(role, ctx)
        if not outcome.admitted:
            return outcome
        return SpawnOutcome(
            admitted=True,
            role=outcome.role,
            fence=outcome.fence,
            plan=outcome.plan,
            result=spawner(outcome.plan),
            battery=outcome.battery,
        )

    def _admit(self, role: str, ctx: FenceContext) -> SpawnOutcome:
        with self.ledger.transaction():
            try:
                fence = render_fence(role, ctx, document=self.document)
            except FenceRefusal as refusal:
                return self._refuse(role, refusal.reasons)

            try:
                battery = run_battery(fence)
            except ProbeSynthesisError as exc:
                # A rule the battery cannot aim a probe at is a rule nothing
                # observes. Letting this escape would skip the durable record
                # entirely, so it refuses like any other unprovable fence.
                return self._refuse(role, [(REASON_PROBE_UNSYNTHESIZABLE, str(exc))])
            self.ledger.append(
                EVENT_BATTERY,
                role=role,
                probes=len(battery.results),
                all_denied=battery.all_denied,
            )
            if not battery.all_denied:
                unproven = [result.probe.rule_id for result in battery.breaches]
                return self._refuse(
                    role,
                    [
                        (
                            REASON_BATTERY_INCOMPLETE,
                            "fence rendered but did not deny its own probes: "
                            + ", ".join(unproven),
                        )
                    ],
                    battery=battery,
                )

            # Publication is all-or-nothing. A fence left on disk by a spawn
            # that was then refused would be read by the hook on the next
            # start and enforced as though it had been admitted -- the refusal
            # invariant says nothing is published, and half of something is
            # not nothing.
            # A fence may already be live at this path from an earlier
            # admitted session. Unlinking the replacement on failure would
            # leave that session with no fence at all -- every hook call
            # denying until the next successful publication -- so the previous
            # bytes are kept and restored.
            previous = (
                ctx.fence_path.read_bytes() if Path(ctx.fence_path).is_file() else None
            )
            fence_path = None
            try:
                fence_path = write_fence(fence, ctx.fence_path)
                settings_path = self._write_settings(fence, ctx)
            except OSError as exc:
                if fence_path is not None:
                    try:
                        if previous is None:
                            fence_path.unlink()
                        else:
                            fence_path.write_bytes(previous)
                    except OSError:
                        # The rollback itself failed, so the refusal must say
                        # so: an operator has a stale fence to remove by hand.
                        return self._refuse(
                            role,
                            [
                                (
                                    RefusalReason.DOCUMENT_UNREADABLE,
                                    f"cannot publish fence: {exc}; and the partially "
                                    f"published fence at {fence_path} could not be "
                                    f"rolled back -- restore it before the next spawn",
                                )
                            ],
                        )
                return self._refuse(
                    role, [(RefusalReason.DOCUMENT_UNREADABLE, f"cannot publish fence: {exc}")]
                )

            plan = SpawnPlan(
                role=role,
                fence=fence,
                settings_path=settings_path,
                fence_path=fence_path,
                context=ctx,
            )
            self.ledger.append(
                EVENT_ADMITTED,
                role=role,
                rules=len(fence.rules),
                permission_mode=fence.permission_mode,
                fence_path=str(fence_path),
                settings_path=str(settings_path),
            )
            return SpawnOutcome(
                admitted=True, role=role, fence=fence, plan=plan, battery=battery
            )

    def _write_settings(self, fence: Fence, ctx: FenceContext) -> Path:
        path = Path(ctx.fence_path).parent / self.settings_name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        body = json.dumps(fence.settings, sort_keys=True, indent=2) + "\n"
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return path

    def _refuse(
        self,
        role: str,
        reasons: list[tuple[str, str]],
        *,
        battery: BatteryReport | None = None,
    ) -> SpawnOutcome:
        self.ledger.append(
            EVENT_REFUSED,
            role=role,
            reasons=[{"code": code, "detail": detail} for code, detail in reasons],
        )
        return SpawnOutcome(
            admitted=False, role=role, reasons=tuple(reasons), battery=battery
        )
