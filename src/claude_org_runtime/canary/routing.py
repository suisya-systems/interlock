"""The run-start routing point -- item 10 rehearsal (Issue #23, D-0022).

.. warning::

   **Rehearsal artifact, throwaway by default (D-0026).** The property being
   rehearsed is the one that makes the canary cheap: **rollback is a routing
   change, not a data migration**. The discharge point is the canary itself,
   with live v1 as the counterparty (D-0022); nothing here discharges item 10.

**Where this boundary sits.** Above both systems, and above the
``SessionProvider``. The provider contract is five verbs about *sessions* and
carries no notion of which system owns a run; folding a system cutover into it
would put cutover semantics inside the interface item 11 proved swappable. So
the routing point is its own boundary, consulted **once per run, at run
start, before the first system-specific write or spawn**. It decides and
records; it does not start anything -- the caller takes the answer to the
owning system's own start path. That is also why this module imports nothing
from :mod:`claude_org_runtime.session` (or any other Interlock module): the
routing point has no provider dependency and survives a provider switch
untouched, which ``tests/canary/test_structural.py`` asserts rather than
describes.

**What a rollback is.** :meth:`RunStartRoutingPoint.route_new_runs_to` with
the previous owning system. There is no other rollback code path -- no
migration hook, no state converter, no "move these runs back" API -- and that
absence is the point, not an omission. Runs already in flight keep the owner
the ledger recorded for them, wherever the policy moves afterwards; what
*happens* to Interlock-started runs at a real rollback (drain? finish?
abort?) is part of Q-0005 and deliberately not decided -- or expressible --
here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from claude_org_runtime.canary.ledger import OWNING_SYSTEMS

__all__ = [
    "NoRoutingDecision",
    "OwnerChangeRefused",
    "RoutedRun",
    "RoutingDecision",
    "RoutingRefused",
    "RunStartRoutingPoint",
    "UnknownOwningSystem",
    "UnroutedRun",
]


class RoutingRefused(Exception):
    """The routing point refused. Nothing was routed and nothing was written."""


class NoRoutingDecision(RoutingRefused):
    """No routing decision has been taken yet.

    Deliberately not defaulted away: a routing point that assumes an owner
    when no one decided one is a cutover nobody decided on. The baseline
    ("new runs go to v1") is itself a recorded decision.
    """


class UnknownOwningSystem(RoutingRefused):
    """The named system is outside the closed vocabulary (see
    :data:`~claude_org_runtime.canary.ledger.OWNING_SYSTEMS`)."""


class OwnerChangeRefused(RoutingRefused):
    """A started run was asked to change owning system. No run changes owner
    mid-flight (gate item 10); the refusal leaves the ledger row untouched."""


class UnroutedRun(RoutingRefused):
    """The run has no ledger row: it was never routed through this point."""


@dataclass(frozen=True)
class RoutingDecision:
    """One appended row of the routing policy. The newest is the routing."""

    decision_seq: int
    owning_system: str
    decided_at_ms: int
    reason: str


@dataclass(frozen=True)
class RoutedRun:
    """One run's immutable ledger entry: which system owns it, and under
    which decision it was routed."""

    run_id: str
    owning_system: str
    decision_seq: int
    routed_at_ms: int


class RunStartRoutingPoint:
    """Decides, once per run at run start, which system owns the run.

    Constructed over an open routing-ledger connection (see
    :func:`~claude_org_runtime.canary.ledger.open_routing_ledger`). Every
    method is one transaction; time is the caller's throughout (``now_ms``),
    and order of authority among decisions is ``decision_seq``, never the
    clock.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def route_new_runs_to(self, owning_system: str, *, now_ms: int, reason: str) -> RoutingDecision:
        """Append a routing decision: new runs from now on belong to
        *owning_system*. **This method, with the previous owner, is the whole
        of a rollback.**

        Runs already started are untouched -- not by convention but because
        nothing here writes anywhere near them: the only statement is an
        INSERT into ``routing_decision``.
        """

        if owning_system not in OWNING_SYSTEMS:
            raise UnknownOwningSystem(
                f"{owning_system!r} is not one of {OWNING_SYSTEMS}; the "
                "rehearsal's owning-system vocabulary is closed"
            )
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO routing_decision (owning_system, decided_at_ms, reason) "
                "VALUES (:owning_system, :now_ms, :reason)",
                {"owning_system": owning_system, "now_ms": now_ms, "reason": reason},
            )
            seq = cursor.lastrowid
        assert seq is not None  # INTEGER PRIMARY KEY: SQLite always assigns one
        return RoutingDecision(
            decision_seq=seq, owning_system=owning_system, decided_at_ms=now_ms, reason=reason
        )

    def current_decision(self) -> RoutingDecision:
        """The newest routing decision -- the one a run starting now falls
        under.

        :raises NoRoutingDecision: if none has been taken.
        """

        row = self._connection.execute(
            "SELECT decision_seq, owning_system, decided_at_ms, reason "
            "  FROM routing_decision ORDER BY decision_seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise NoRoutingDecision(
                "no routing decision has been taken; the routing point does "
                "not assume an owner (record a baseline decision first)"
            )
        return RoutingDecision(*row)

    def route_run_start(self, run_id: str, *, now_ms: int) -> RoutedRun:
        """Record, under the current decision, which system owns *run_id*.

        Called once per run, before the first system-specific write. The
        caller then starts the run on the returned owner's own path; this
        method starts nothing.

        Idempotent against a crashed-and-retried router: routing an
        already-routed run to the **same** owner returns the existing row
        unchanged. Routing it to a **different** owner -- which is what a
        retry after a policy flip amounts to -- is refused: the run started
        under its recorded owner and keeps it (gate item 10).

        :raises NoRoutingDecision: if no decision has been taken.
        :raises OwnerChangeRefused: if *run_id* is already owned by another
            system.
        """

        decision = self.current_decision()
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO run_owner (run_id, owning_system, decision_seq, routed_at_ms) "
                    "VALUES (:run_id, :owning_system, :decision_seq, :now_ms)",
                    {
                        "run_id": run_id,
                        "owning_system": decision.owning_system,
                        "decision_seq": decision.decision_seq,
                        "now_ms": now_ms,
                    },
                )
        except sqlite3.IntegrityError as error:
            # Only a duplicate run_id is interpretable here; any other
            # integrity failure (a CHECK, say) is not an ownership question
            # and passes through as itself.
            try:
                existing = self.routed_run(run_id)
            except UnroutedRun:
                raise error
            if existing.owning_system == decision.owning_system:
                return existing
            raise OwnerChangeRefused(
                f"run {run_id!r} is owned by {existing.owning_system!r} "
                f"(decision {existing.decision_seq}); re-routing it to "
                f"{decision.owning_system!r} would change its owner mid-flight, "
                "which gate item 10 forbids"
            ) from None
        return RoutedRun(
            run_id=run_id,
            owning_system=decision.owning_system,
            decision_seq=decision.decision_seq,
            routed_at_ms=now_ms,
        )

    def routed_run(self, run_id: str) -> RoutedRun:
        """The ledger row for *run_id*.

        :raises UnroutedRun: if the run was never routed through this point.
        """

        row = self._connection.execute(
            "SELECT run_id, owning_system, decision_seq, routed_at_ms "
            "  FROM run_owner WHERE run_id = :run_id",
            {"run_id": run_id},
        ).fetchone()
        if row is None:
            raise UnroutedRun(f"run {run_id!r} has no ledger row; it was never routed")
        return RoutedRun(*row)
