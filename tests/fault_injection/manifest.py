"""The case matrix: an explicit, checked-in enumeration (design 4).

Injection points are never sampled. Issue ``#15`` reads as if the seed selected
them; it does not, and design section 4 says why: if it did, adding a case,
reordering an enumeration or a different Python hash seed would silently change
what every seed means. The matrix is a frozen literal in ``manifest.json``,
:func:`build_cases` is the generator that must reproduce it exactly, and
``test_manifest.py`` asserts the two agree -- so adding or pruning a case is
always a reviewable diff and never a side effect of an enumeration change.

The seed's authority is payload and schedule only (design 4.3).

**What S9 ships here** is the schema, the seed set that proves the harness (at
least one case per fault kind, per checkpoint and per lane, plus the section 5
combination seed set) and the generator-freeze test. Populating the full
``ACCEPTANCE.md`` section 2 matrix is I-11's deliverable, on this schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from tests.fault_injection import contract
from tests.fault_injection.contract import (
    ARMABLE_ANCHORS,
    ArmedAnchor,
    BARRIER_ALIGNED,
    BARRIER_STAGGERED,
    CHECKPOINTS,
    CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD,
    CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT,
    CHECKPOINT_BEFORE_DURABLE_WRITE,
    CHECKPOINT_DELIVERED_BEFORE_ACK,
    ContractViolation,
    DESTINATION_INVARIANTS,
    EFFECT_BEARING_CHECKPOINTS,
    LANE_LINUX,
    LANE_PORTABLE,
    OPERATION_ACK,
    OPERATION_ATTEMPT,
    OPERATION_BIND,
    OPERATION_ENQUEUE,
    OPERATION_LEASE_ACQUIRE,
    OPERATION_LEASE_RENEW,
    OPERATION_OBSERVE,
    ROLES,
    ROLE_DISPATCHER,
    ROLE_SECRETARY,
    ROLE_SUPERVISOR,
    SQL_INVARIANTS,
    SYNC_LEASE_ACQUIRED,
    SYNC_OBSERVED,
)

__all__ = [
    "MANIFEST_PATH",
    "MANIFEST_VERSION",
    "PROFILES",
    "PRUNING_RULE",
    "build_cases",
    "build_manifest",
    "load_manifest",
    "profile_cases",
    "validate_case",
    "validate_manifest",
]

MANIFEST_PATH = Path(__file__).with_name("manifest.json")

#: Bumped on any semantic change to the matrix. A failure report carries it
#: alongside the contract version (design 4.2, 4.4).
#:
#: 1 -> 2 (I-11, Issue ``#16``): the matrix grows from S9's seed set to the whole
#: ``ACCEPTANCE.md`` section 2 table. The bump is not cosmetic -- the version is
#: stamped into every case entry and mixed into every per-case seed, so it also
#: re-rolls the payload bytes and schedule jitter of the 35 seed cases. That is
#: the intended meaning of a semantic change to the matrix: the old evidence was
#: recorded against manifest 1 and is not silently re-labelled as manifest 2.
MANIFEST_VERSION = 2

#: A fixed constant, not a wall-clock reading: the injected clock's base
#: (design 7). It is the same instant the S6/S7 suites use.
CLOCK_BASE_MS = 1_700_000_000_000

#: Lease geometry the boundary-relative skew magnitudes are resolved against.
TTL_MS = 30_000

#: The harness engineering budgets (design 9). These are **not** acceptance
#: thresholds -- they are enforced CI budgets, revisable by an ordinary reviewed
#: diff, and reading one *as* gate evidence would be a ruling and goes to the
#: secretary.
PROFILES: Mapping[str, Mapping[str, Any]] = {
    "fast": {
        "runs_on": "every PR push, Linux job only (plus the portable lane everywhere)",
        "max_cases": 25,
        "per_case_timeout_s": 15,
        "combination_case_timeout_s": 15,
        "suite_timeout_s": 240,
        "barrier_timeout_s": 10,
    },
    "full": {
        "runs_on": "nightly and gate runs (I-11/I-13/I-15), Linux conformance lane",
        "max_cases": 200,
        "per_case_timeout_s": 30,
        "combination_case_timeout_s": 60,
        "suite_timeout_s": 1500,
        "barrier_timeout_s": 20,
    },
}

#: Recorded rather than left implicit (design 5): scale is controlled by policy,
#: not by product, and anything pruned is listed.
#: The two collapse rules ACCEPTANCE.md section 2 names without choosing between
#: them (Q-0002). The matrix must cover both; this file expresses no preference.
COLLAPSE_RULES = ("increment-in-place", "open-linked")

#: The faults whose subject is the incident packet rather than the delivery.
INCIDENT_FAULTS = ("incident-repeat", "incident-replay")

PRUNING_RULE = (
    "Aligned combination cases cover the multi-role subsets against a curated "
    "set of (operation, checkpoint) pairs -- the delivery loop's windows where "
    "roles genuinely interact -- and not the full cross-product. Pruned "
    "deliberately: (a) the 7-subset x 4-checkpoint x 7-operation product, of "
    "which only the pairs listed here are kept; (b) single-role sigkill cases "
    "on operations other than the three named non-attempt seeds; (c) staggered "
    "sequences beyond the two the acceptance surface cares most about. I-11 "
    "extends this set deliberately, never by product."
)


def _case(
    *,
    targets: Sequence[str],
    operation: str,
    checkpoint: str,
    fault: str,
    variant: str | None = None,
    lane: str,
    profiles: Sequence[str],
    arms: Mapping[str, Sequence[str]],
    barrier: str = BARRIER_ALIGNED,
    kill_order: Sequence[str] | None = None,
    restart_order: Sequence[str] | None = None,
    expected: Mapping[str, Any],
    messages: int = 1,
    behaviours: Sequence[str] = (),
    claimant: Mapping[str, Any] | None = None,
    skew: Mapping[str, Any] | None = None,
    release_after_barrier: bool = False,
    restart_after: bool = True,
    staggered: Sequence[Mapping[str, Any]] | None = None,
    incident_params: Mapping[str, Any] | None = None,
    observation: Mapping[str, Any] | None = None,
    unavailable_attempts: int | None = None,
) -> dict:
    """One manifest entry. Every field a case needs that the id does not carry.

    ``case_id + manifest_version`` denotes exactly one fully-specified case,
    which is what the re-run and failure-report contracts rely on (design 4.1).
    """

    segments = ["+".join(targets), operation, checkpoint, fault]
    if variant:
        segments.append(variant)
    case_id = "__".join(segments)
    entry = {
        "case_id": case_id,
        "targets": list(targets),
        "operation": operation,
        "checkpoint": checkpoint,
        "fault": fault,
        "variant": variant,
        "lane": lane,
        "profiles": list(profiles),
        "barrier": barrier,
        "arms": {role: list(anchors) for role, anchors in arms.items()},
        "kill_order": list(kill_order if kill_order is not None else targets),
        # Design section 5: "restart_order -- explicit ordered list (default:
        # same as kill_order)". Defaulting to targets instead would silently
        # give a case with an explicit kill order a restart order its author
        # never declared.
        "restart_order": list(
            restart_order
            if restart_order is not None
            else (kill_order if kill_order is not None else targets)
        ),
        "expected": {
            "queries": list(expected.get("queries", ())),
            "destination": list(expected.get("destination", ())),
            "recovery_owner": expected.get("recovery_owner"),
        },
        "messages": messages,
        "behaviours": list(behaviours),
        "claimant": (
            {
                key: (list(value) if isinstance(value, tuple) else value)
                for key, value in claimant.items()
            }
            if claimant
            else None
        ),
        "skew": dict(skew) if skew else None,
        "release_after_barrier": release_after_barrier,
        "restart_after": restart_after,
        "staggered": [dict(step) for step in staggered] if staggered else None,
        "incident_params": dict(incident_params) if incident_params else None,
        # Normalised to JSON's own types, so the frozen literal and the
        # generator compare equal: a tuple survives ``dict()`` unchanged but
        # reads back from JSON as a list.
        "observation": (
            {
                "mode": observation["mode"],
                "escalate_on": list(observation.get("escalate_on", ())),
            }
            if observation
            else None
        ),
        "unavailable_attempts": unavailable_attempts,
        "ttl_ms": TTL_MS,
        "clock_base_ms": CLOCK_BASE_MS,
        "manifest_version": MANIFEST_VERSION,
    }
    return entry


_KILL_QUERIES = (
    contract.INVARIANT_NO_UNOWNED_OUTBOX,
    contract.INVARIANT_RETRY_COUNT_DURABLE,
    contract.INVARIANT_LINEAR_WRITER_HISTORY,
    contract.INVARIANT_NO_PENDING_ACTION,
)
_KILL_DESTINATION = (
    contract.INVARIANT_ONE_EFFECT_PER_KEY,
    contract.INVARIANT_DELIVERED_IMPLIES_EFFECT,
)
_TAKEOVER_QUERIES = (
    contract.INVARIANT_LINEAR_WRITER_HISTORY,
    contract.INVARIANT_RECORDED_REFUSALS,
    contract.INVARIANT_LEASE_SINGLE_HOLDER,
)


def build_cases() -> list[dict]:
    """Produce the candidate matrix. The frozen literal is the authority.

    A test asserts this equals ``manifest.json``'s ``cases``; the generator is a
    convenience for producing a diff, never a collection-time enumeration.
    """

    cases: list[dict] = []

    # -- kill at each of the four windows, for each role separately ---------
    #
    # Gate item 4 requires all three ACCEPTANCE.md section 2 windows for *each*
    # of the three components, plus the fourth the outbox rows add. Every role
    # reaches all four through its own ``attempt``-driven action, which is why
    # the Supervisor and Secretary scripts carry one (design 2.1).
    for role in ROLES:
        for checkpoint in CHECKPOINTS:
            fast = role == ROLE_DISPATCHER
            cases.append(
                _case(
                    targets=(role,),
                    operation=OPERATION_ATTEMPT,
                    checkpoint=checkpoint,
                    fault="sigkill",
                    lane=LANE_PORTABLE,
                    profiles=("full",) if not fast else ("fast", "full"),
                    arms={role: (f"{OPERATION_ATTEMPT}@{checkpoint}:1",)},
                    expected={
                        "queries": _KILL_QUERIES,
                        "destination": _KILL_DESTINATION,
                        "recovery_owner": role,
                    },
                )
            )

    # -- kill on the operations that are not the delivery loop --------------
    for role, operation, checkpoint in (
        (ROLE_SUPERVISOR, OPERATION_BIND, CHECKPOINT_BEFORE_DURABLE_WRITE),
        (ROLE_SECRETARY, OPERATION_ENQUEUE, CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT),
        (ROLE_DISPATCHER, OPERATION_LEASE_ACQUIRE, CHECKPOINT_BEFORE_DURABLE_WRITE),
    ):
        # A kill at the first step of a script leaves nothing durable behind, so
        # it names no recovery owner: what it proves is that a restart from a
        # cold start is clean, not that recovery repaired anything.
        from_cold = operation == contract.ROLE_SCRIPTS[role][0] and (
            checkpoint == CHECKPOINT_BEFORE_DURABLE_WRITE
        )
        cases.append(
            _case(
                targets=(role,),
                operation=operation,
                checkpoint=checkpoint,
                fault="sigkill",
                lane=LANE_LINUX,
                profiles=("full",),
                arms={role: (f"{operation}@{checkpoint}:1",)},
                expected={
                    "queries": _KILL_QUERIES,
                    "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                    "recovery_owner": None if from_cold else role,
                },
            )
        )

    # -- the same checkpoint, a later occurrence ----------------------------
    #
    # A loop passes the same point repeatedly, so the occurrence index is part
    # of the arming and gets its own variant slug (design 4.1).
    cases.append(
        _case(
            targets=(ROLE_DISPATCHER,),
            operation=OPERATION_ATTEMPT,
            checkpoint=CHECKPOINT_BEFORE_DURABLE_WRITE,
            fault="sigkill",
            variant="occ2",
            lane=LANE_LINUX,
            profiles=("full",),
            arms={ROLE_DISPATCHER: (f"{OPERATION_ATTEMPT}@{CHECKPOINT_BEFORE_DURABLE_WRITE}:2",)},
            messages=2,
            expected={
                "queries": _KILL_QUERIES,
                "destination": _KILL_DESTINATION,
                "recovery_owner": ROLE_DISPATCHER,
            },
        )
    )

    # -- clock skew, both directions, across the expiry boundary ------------
    #
    # Two supported shapes only (design 7): cross-role skew, where the target is
    # blocked while a *sibling's* offset moves and the sibling acts under its
    # new clock; and same-role skew, observed by the script's *next* operation.
    # A case whose expectation depends on an in-flight call seeing a mid-call
    # skew is invalid by construction and refused at validation.
    # Backward skew is observed by a *renewal* being refused, so a backward case
    # is only meaningful for a role whose script renews. The Secretary's does
    # not -- it releases instead -- so it carries a forward case.
    for role, direction, profiles in (
        (ROLE_SUPERVISOR, "backward", ("fast", "full")),
        (ROLE_DISPATCHER, "forward", ("fast", "full")),
        (ROLE_DISPATCHER, "backward", ("full",)),
        (ROLE_SECRETARY, "forward", ("full",)),
    ):
        forward = direction == "forward"
        cases.append(
            _case(
                targets=(role,),
                operation=OPERATION_LEASE_ACQUIRE,
                checkpoint=SYNC_LEASE_ACQUIRED,
                fault="clock-fwd" if forward else "clock-back",
                lane=LANE_PORTABLE,
                profiles=profiles,
                arms={role: (f"{OPERATION_LEASE_ACQUIRE}@{SYNC_LEASE_ACQUIRED}:1",)},
                restart_after=False,
                release_after_barrier=not forward,
                # Forward skew is the cross-role shape: a claimant on the same
                # resource whose clock has crossed the holder's expiry takes the
                # lease over while the holder is frozen at its barrier.
                claimant=(
                    {"role": role, "holder_suffix": "b", "clock": "forward",
                     "observation": "sibling"}
                    if forward
                    else None
                ),
                # Backward skew is the same-role shape: the offset lands while
                # the process is blocked, and the *next* operation observes it.
                skew=(
                    None
                    if forward
                    else {"role": role, "direction": "backward", "observation": "next-operation"}
                ),
                expected={
                    "queries": _TAKEOVER_QUERIES if forward else (
                        contract.INVARIANT_LEASE_SINGLE_HOLDER,
                        contract.INVARIANT_LINEAR_WRITER_HISTORY,
                        # The backward direction's whole observable: the renewal
                        # that would have landed at or before its own
                        # acquisition is refused, and the refusal is recorded.
                        contract.INVARIANT_RECORDED_REFUSALS,
                    ),
                    "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                    "recovery_owner": None,
                },
            )
        )

    # -- SIGSTOP: pause a holder, let its lease lapse, resume it ------------
    #
    # Anchored at a sync point, never at a bare sleep: the controller sends
    # SIGSTOP only while the holder is provably blocked at that barrier, already
    # holding its lease and between operations. Being stopped, it cannot consume
    # the ``continue`` until it is resumed, so pause / takeover / return is a
    # deterministic sequence rather than a scheduling accident (design 4.1).
    for role, profiles in ((ROLE_DISPATCHER, ("fast", "full")), (ROLE_SUPERVISOR, ("full",))):
        cases.append(
            _case(
                targets=(role,),
                operation=OPERATION_LEASE_ACQUIRE,
                checkpoint=SYNC_LEASE_ACQUIRED,
                fault="sigstop-expire",
                lane=LANE_LINUX,
                profiles=profiles,
                arms={role: (f"{OPERATION_LEASE_ACQUIRE}@{SYNC_LEASE_ACQUIRED}:1",)},
                restart_after=False,
                claimant={
                    "role": role,
                    "holder_suffix": "b",
                    "clock": "forward",
                    "observation": "sibling",
                },
                expected={
                    "queries": _TAKEOVER_QUERIES,
                    "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                    "recovery_owner": None,
                },
            )
        )

    # -- delivery-surface faults --------------------------------------------
    #
    # Anchored like every other fault, but the fault is at the delivery surface
    # rather than at the process: the barrier is a pass-through here, used to
    # pin the moment rather than to kill.
    for role, checkpoint, fault, behaviour, messages in (
        (ROLE_DISPATCHER, CHECKPOINT_BEFORE_DURABLE_WRITE, "drop-delivery", "drop-delivery", 1),
        (ROLE_SECRETARY, CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT, "dup-delivery", "dup-delivery", 2),
        (ROLE_SUPERVISOR, CHECKPOINT_DELIVERED_BEFORE_ACK, "lost-ack", "lost-ack", 1),
    ):
        cases.append(
            _case(
                targets=(role,),
                operation=OPERATION_ATTEMPT,
                checkpoint=checkpoint,
                fault=fault,
                lane=LANE_PORTABLE,
                profiles=("fast", "full"),
                arms={role: (f"{OPERATION_ATTEMPT}@{checkpoint}:1",)},
                messages=messages,
                behaviours=(behaviour,),
                release_after_barrier=True,
                expected={
                    "queries": (
                        contract.INVARIANT_NO_UNOWNED_OUTBOX,
                        contract.INVARIANT_RETRY_COUNT_DURABLE,
                        contract.INVARIANT_SINGLE_ACKED_STATE,
                    ),
                    "destination": _KILL_DESTINATION,
                    "recovery_owner": role,
                },
                # Q-0002 (incident collapse semantics) and Q-0003 (reconcile
                # interval) stay open: the schema carries the parameters and S9
                # fixes no value (design 10).
                incident_params={"collapse": None, "reconcile_interval_ms": None},
            )
        )

    # -- staggered kills -----------------------------------------------------
    #
    # Not barrier-simultaneous: A is killed at its checkpoint, B keeps operating
    # against the survivor state, then B is killed at a later armed checkpoint.
    # Strictly enumerated, each naming its full sequence (design 5).
    cases.append(
        _case(
            targets=(ROLE_DISPATCHER, ROLE_SECRETARY),
            operation=OPERATION_ATTEMPT,
            checkpoint=CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD,
            fault="staggered-sigkill",
            variant="killorder-ds",
            lane=LANE_LINUX,
            profiles=("full",),
            barrier=BARRIER_STAGGERED,
            arms={
                ROLE_DISPATCHER: (
                    f"{OPERATION_ATTEMPT}@{CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD}:1",
                ),
                ROLE_SECRETARY: (f"{OPERATION_ACK}@{CHECKPOINT_BEFORE_DURABLE_WRITE}:1",),
            },
            kill_order=(ROLE_DISPATCHER, ROLE_SECRETARY),
            restart_order=(ROLE_DISPATCHER, ROLE_SECRETARY),
            staggered=(
                {"wait": ROLE_DISPATCHER, "kill": ROLE_DISPATCHER},
                {"wait": ROLE_SECRETARY, "kill": ROLE_SECRETARY},
            ),
            expected={
                "queries": _KILL_QUERIES,
                "destination": _KILL_DESTINATION,
                "recovery_owner": ROLE_SECRETARY,
            },
        )
    )
    cases.append(
        _case(
            targets=(ROLE_SUPERVISOR, ROLE_DISPATCHER),
            operation=OPERATION_LEASE_ACQUIRE,
            checkpoint=CHECKPOINT_BEFORE_DURABLE_WRITE,
            fault="staggered-sigkill",
            variant="killorder-sd",
            lane=LANE_LINUX,
            profiles=("full",),
            barrier=BARRIER_STAGGERED,
            arms={
                ROLE_SUPERVISOR: (
                    f"{OPERATION_LEASE_ACQUIRE}@{CHECKPOINT_BEFORE_DURABLE_WRITE}:1",
                ),
                ROLE_DISPATCHER: (f"{OPERATION_ENQUEUE}@{CHECKPOINT_BEFORE_DURABLE_WRITE}:1",),
            },
            kill_order=(ROLE_SUPERVISOR, ROLE_DISPATCHER),
            restart_order=(ROLE_SUPERVISOR, ROLE_DISPATCHER),
            staggered=(
                {"wait": ROLE_SUPERVISOR, "kill": ROLE_SUPERVISOR},
                {"wait": ROLE_DISPATCHER, "kill": ROLE_DISPATCHER},
            ),
            expected={
                "queries": _KILL_QUERIES,
                "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                "recovery_owner": ROLE_DISPATCHER,
            },
        )
    )

    # =====================================================================
    # I-11 (Issue #16): the rest of the ACCEPTANCE.md section 2 table.
    #
    # Everything above is S9's seed set -- one case per fault kind, per
    # checkpoint, per lane, plus the combination seeds. Everything below closes
    # the gap between that set and the six-row table, and each block names the
    # row and the injected phrase it discharges so the table can be checked
    # against this file without opening the manifest.
    # =====================================================================

    # -- Lease row: "kill the lease holder without release" -----------------
    #
    # The holder dies mid-script without ever releasing, a claimant whose clock
    # has crossed the expiry takes the resource over, and the restarted holder
    # comes back to find the lease gone. It is refused at ``acquire`` and the
    # refusal is recorded -- which is the observable this row asks for, obtained
    # at the point a SIGKILLed process can actually be refused. (A killed
    # process keeps no epoch in memory, so it cannot present a stale token the
    # way the SIGSTOP cases' holder does; that half of the row is theirs.)
    for role, operation, checkpoint, profiles in (
        (ROLE_DISPATCHER, OPERATION_LEASE_RENEW, CHECKPOINT_BEFORE_DURABLE_WRITE,
         ("fast", "full")),
        (ROLE_SUPERVISOR, OPERATION_LEASE_RENEW, CHECKPOINT_BEFORE_DURABLE_WRITE,
         ("full",)),
    ):
        cases.append(
            _case(
                targets=(role,),
                operation=operation,
                checkpoint=checkpoint,
                fault="sigkill-expire",
                lane=LANE_LINUX,
                profiles=profiles,
                arms={role: (f"{operation}@{checkpoint}:1",)},
                claimant={
                    "role": role,
                    "holder_suffix": "b",
                    "clock": "forward",
                    "observation": "sibling",
                },
                expected={
                    "queries": _TAKEOVER_QUERIES,
                    "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                    "recovery_owner": role,
                },
            )
        )

    # -- Single-writer row: "two writers race for the same state item" ------
    #
    # They cannot both be live writers, and that is the finding rather than a
    # limitation: ``acquire`` only replaces a lapsed row, so the second claimant
    # is refused at the resource boundary. "A stale writer is rejected, not
    # merged" is observed exactly there. The incumbent is held at a barrier for
    # the whole race so the racer provably meets a *live* lease.
    for role, profiles in ((ROLE_DISPATCHER, ("fast", "full")), (ROLE_SECRETARY, ("full",))):
        cases.append(
            _case(
                targets=(role,),
                operation=OPERATION_LEASE_ACQUIRE,
                checkpoint=SYNC_LEASE_ACQUIRED,
                fault="writer-race",
                lane=LANE_LINUX,
                profiles=profiles,
                arms={role: (f"{OPERATION_LEASE_ACQUIRE}@{SYNC_LEASE_ACQUIRED}:1",)},
                restart_after=False,
                claimant={
                    "role": role,
                    "holder_suffix": "race",
                    "clock": "forward",
                    "observation": "sibling",
                    # Refused at acquire *and then carrying on* with a token the
                    # lease row rejects. Without this the racer contributes no
                    # write at all, and section 2's "the state item's history is
                    # a linear sequence with no interleaving from the rejected
                    # writer" would be true of every run -- including a run in
                    # which atomic fencing had stopped working.
                    "behaviours": ("stale-writer",),
                },
                expected={
                    "queries": (
                        contract.INVARIANT_LINEAR_WRITER_HISTORY,
                        contract.INVARIANT_RECORDED_REFUSALS,
                        contract.INVARIANT_LEASE_SINGLE_HOLDER,
                    ),
                    "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                    "recovery_owner": None,
                },
            )
        )

    # -- Single-writer row: "a write is attempted concurrently from a resumed
    #    process and its replacement" ---------------------------------------
    #
    # Same mechanic as the lease row above, anchored mid-write instead of at the
    # lease boundary: the resumed process is the restarted generation and the
    # replacement is the claimant that took the resource over while it was gone.
    cases.append(
        _case(
            targets=(ROLE_DISPATCHER,),
            operation=OPERATION_ENQUEUE,
            checkpoint=CHECKPOINT_BEFORE_DURABLE_WRITE,
            fault="resumed-writer-race",
            lane=LANE_LINUX,
            profiles=("fast", "full"),
            arms={
                ROLE_DISPATCHER: (
                    f"{OPERATION_ENQUEUE}@{CHECKPOINT_BEFORE_DURABLE_WRITE}:1",
                )
            },
            # The *resumed* process is the stale writer here: it comes back with
            # no epoch, is refused at acquire, and carries on believing it holds
            # the lease -- which is what makes its writes race the replacement's
            # rather than simply not happening. Inert in the first generation,
            # which acquires cleanly, and inert for the replacement, which is
            # never refused.
            behaviours=("stale-writer",),
            claimant={
                "role": ROLE_DISPATCHER,
                "holder_suffix": "b",
                "clock": "forward",
                "observation": "sibling",
                # The replacement is held here, still alive and still holding,
                # while the resumed process comes back and writes. "A write is
                # attempted concurrently from a resumed process and its
                # replacement" needs both to exist at once; a replacement that
                # had already exited would leave the resumed process racing a
                # lease row rather than a writer.
                "arms": (f"{OPERATION_ATTEMPT}@{CHECKPOINT_DELIVERED_BEFORE_ACK}:1",),
            },
            expected={
                "queries": _TAKEOVER_QUERIES,
                "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                "recovery_owner": ROLE_DISPATCHER,
            },
        )
    )

    # -- Outbox-resend row: "hold the recipient unavailable across several
    #    retry attempts" ---------------------------------------------------
    #
    # The invariant this row names is that the retry count is "monotonically
    # increasing, restart-surviving", so the case has to contain a restart --
    # a run without one could not observe the surviving half at all. The
    # refusal budget lives in the destination's own attempt log, so it keeps
    # counting across the kill instead of starting again.
    for role, profiles in ((ROLE_DISPATCHER, ("fast", "full")), (ROLE_SECRETARY, ("full",))):
        cases.append(
            _case(
                targets=(role,),
                operation=OPERATION_ATTEMPT,
                checkpoint=CHECKPOINT_DELIVERED_BEFORE_ACK,
                fault="recipient-unavailable",
                lane=LANE_LINUX,
                profiles=profiles,
                arms={
                    role: (f"{OPERATION_ATTEMPT}@{CHECKPOINT_DELIVERED_BEFORE_ACK}:1",)
                },
                behaviours=("recipient-unavailable",),
                unavailable_attempts=3,
                expected={
                    "queries": (
                        contract.INVARIANT_NO_UNOWNED_OUTBOX,
                        contract.INVARIANT_RETRY_COUNT_DURABLE,
                        contract.INVARIANT_SINGLE_ACKED_STATE,
                        contract.INVARIANT_NO_PENDING_ACTION,
                    ),
                    "destination": _KILL_DESTINATION,
                    "recovery_owner": role,
                },
            )
        )

    # -- Ack row: "duplicate the ack", "ack an already-acked message",
    #    "deliver the ack after the sender has restarted" -------------------
    #
    # All three used to happen in every case and therefore in no case: the ack
    # step acked twice unconditionally, so a regression in either shape had
    # nowhere to show. The repeat is behaviour-driven now and these are the
    # cases that ask for it. The observable is section 2's own: "message
    # identity in SQLite shows exactly one acked state regardless of ack
    # multiplicity; the recipient's effect count is one".
    for role, fault, behaviour, profiles in (
        (ROLE_DISPATCHER, "dup-ack", "dup-ack", ("fast", "full")),
        (ROLE_SECRETARY, "dup-ack", "dup-ack", ("full",)),
        (ROLE_SUPERVISOR, "re-ack", "re-ack", ("fast", "full")),
        (ROLE_DISPATCHER, "re-ack", "re-ack", ("full",)),
    ):
        cases.append(
            _case(
                targets=(role,),
                operation=OPERATION_ACK,
                checkpoint=CHECKPOINT_BEFORE_DURABLE_WRITE,
                fault=fault,
                lane=LANE_LINUX,
                profiles=profiles,
                arms={role: (f"{OPERATION_ACK}@{CHECKPOINT_BEFORE_DURABLE_WRITE}:1",)},
                release_after_barrier=True,
                restart_after=False,
                expected={
                    "queries": (
                        contract.INVARIANT_SINGLE_ACKED_STATE,
                        contract.INVARIANT_NO_UNOWNED_OUTBOX,
                        contract.INVARIANT_NO_PENDING_ACTION,
                        # Without this the case would pass whether or not the
                        # second ack was ever issued: an idempotent ack leaves
                        # the state it found, so "exactly one acked state" reads
                        # the same after one ack as after two. The ignored ack's
                        # ledger row is the evidence that the injection happened.
                        contract.INVARIANT_RECORDED_REFUSALS,
                    ),
                    "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                    "recovery_owner": None,
                },
                behaviours=(behaviour,),
                incident_params={
                    "collapse": None,
                    "reconcile_interval_ms": None,
                },
            )
        )

    # The late ack: the sender dies after delivery and before the ack is
    # recorded, and the ack lands only in the generation that comes back. "A
    # lost ack causes a resend (safe), never a lost message", and the late ack
    # changes nothing.
    for role, profiles in ((ROLE_DISPATCHER, ("fast", "full")), (ROLE_SUPERVISOR, ("full",))):
        cases.append(
            _case(
                targets=(role,),
                operation=OPERATION_ATTEMPT,
                checkpoint=CHECKPOINT_DELIVERED_BEFORE_ACK,
                fault="late-ack",
                lane=LANE_LINUX,
                profiles=profiles,
                arms={
                    role: (f"{OPERATION_ATTEMPT}@{CHECKPOINT_DELIVERED_BEFORE_ACK}:1",)
                },
                expected={
                    "queries": (
                        contract.INVARIANT_NO_UNOWNED_OUTBOX,
                        contract.INVARIANT_SINGLE_ACKED_STATE,
                        contract.INVARIANT_RETRY_COUNT_DURABLE,
                        contract.INVARIANT_NO_PENDING_ACTION,
                    ),
                    "destination": _KILL_DESTINATION,
                    "recovery_owner": role,
                },
            )
        )

    # -- Dedup row: "raise the same incident condition repeatedly within a
    #    window", "replay a persisted incident packet" ---------------------
    #
    # This is the block Q-0002 governs, and the shape is dictated by what §2
    # asks for rather than by what would be convenient: "tests must parameterise
    # both rather than hard-code either". So every case names its collapse rule
    # and its re-notification window, the driver implements both rules and is
    # told which to apply, and `_validate_incident_parameterisation` refuses a
    # matrix that has drifted onto one rule or one window. Nothing here decides
    # Q-0002; the matrix covers it.
    #
    # The window is made to *do* something, too. A parameter that is carried but
    # never changes an outcome is indistinguishable from a hard-coded one, so one
    # case declares a window its own raises fall outside of and expects no
    # collapse at all.
    #
    # ``expect_collapse`` is declared rather than derived. Deriving it would mean
    # comparing the window against this harness's step interval inside the
    # assertion, which bakes an implementation detail of the driver into the
    # thing that is supposed to be checking the driver.
    for fault, collapse, window_ms, expect_collapse, profiles in (
        ("incident-repeat", "increment-in-place", 5_000, True, ("fast", "full")),
        ("incident-repeat", "open-linked", 60_000, True, ("full",)),
        # The window is too small for the second raise to fall inside it, so the
        # repeat is not a repeat *within a window* and nothing is collapsed.
        ("incident-repeat", "open-linked", 10, False, ("full",)),
        ("incident-replay", "increment-in-place", 60_000, True, ("fast", "full")),
        ("incident-replay", "open-linked", 5_000, True, ("full",)),
    ):
        replay = fault == "incident-replay"
        variant = f"{collapse}-{'in' if expect_collapse else 'out'}"
        cases.append(
            _case(
                targets=(ROLE_SUPERVISOR,),
                operation=OPERATION_OBSERVE,
                checkpoint=SYNC_OBSERVED,
                fault=fault,
                variant=variant,
                lane=LANE_LINUX,
                profiles=profiles,
                arms={ROLE_SUPERVISOR: (f"{OPERATION_OBSERVE}@{SYNC_OBSERVED}:1",)},
                release_after_barrier=True,
                restart_after=False,
                behaviours=("incident-replay",) if replay else (),
                observation={"mode": contract.OBSERVATION_SILENT, "escalate_on": ()},
                incident_params={
                    # Case data, never composed by the driver: Q-0002 asks what
                    # composes an incident dedup key, and the two spellings
                    # below differ in composition on purpose so no case can be
                    # relying on one shape.
                    "dedup_key": (
                        f"observe/{fault}/{collapse}"
                        if expect_collapse
                        else f"{fault}:{collapse}:outside"
                    ),
                    "repeats": 2,
                    "collapse": collapse,
                    "renotify_window_ms": window_ms,
                    "expect_collapse": expect_collapse,
                    # Q-0003, not Q-0002. Named so it is visibly unset rather
                    # than absent, and refused a value by validation.
                    "reconcile_interval_ms": None,
                },
                expected={
                    "queries": (
                        contract.INVARIANT_INCIDENT_COLLAPSE,
                        contract.INVARIANT_UNRESOLVED_INCIDENTS,
                        contract.INVARIANT_OBSERVATION_CLASSIFIED,
                        contract.INVARIANT_NO_ANOMALY_ESCALATION,
                    ),
                    "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                    "recovery_owner": None,
                },
            )
        )

    # -- Observation-outage row (D-0006) ------------------------------------
    #
    # "Make the observation path fail or return nothing while the worker is
    # genuinely healthy." Two distinct injections, because the whole of D-0006
    # is that they are distinct: a read that *fails* is
    # ``OBSERVATION_UNAVAILABLE`` and a read that *returns nothing* is
    # ``NO_ACTIVITY_EVIDENCE``, and neither is an anomaly.
    #
    # Each case also declares an escalation policy naming the very state the
    # injection produces. That is what makes the second half of the row
    # falsifiable: the driver is *asked* to escalate and must refuse, so
    # ``no-anomaly-escalation`` counts a row a broken driver would have written
    # rather than a row nothing in the tree can write.
    for mode, lane, profiles in (
        # The off-Linux add-on has a 20-case budget of its own (design 9) and
        # this fills the last slot deliberately: the read-failure injection is
        # the row's headline, it touches no signal, and D-0006 is a
        # control-plane property worth proving on every OS. The silent-read
        # injection runs on the conformance lane, which is where I-11's gate
        # evidence is read from in any case.
        (contract.OBSERVATION_UNREADABLE, LANE_PORTABLE, ("fast", "full")),
        (contract.OBSERVATION_SILENT, LANE_LINUX, ("fast", "full")),
    ):
        cases.append(
            _case(
                targets=(ROLE_SUPERVISOR,),
                operation=OPERATION_OBSERVE,
                checkpoint=SYNC_OBSERVED,
                fault="observation-outage",
                variant=mode,
                lane=lane,
                profiles=profiles,
                arms={ROLE_SUPERVISOR: (f"{OPERATION_OBSERVE}@{SYNC_OBSERVED}:1",)},
                release_after_barrier=True,
                restart_after=False,
                observation={
                    "mode": mode,
                    "escalate_on": (contract.OBSERVATION_FACT_STATES[mode],),
                },
                expected={
                    "queries": (
                        contract.INVARIANT_OBSERVATION_CLASSIFIED,
                        contract.INVARIANT_NO_ANOMALY_ESCALATION,
                        contract.INVARIANT_RECORDED_REFUSALS,
                        # The packet is in the row and not in anyone's context
                        # (D-0003, D-0007), so it is readable from SQLite alone
                        # after the fact -- which is what gate item 4's "work
                        # resumes from unresolved incidents" rests on.
                        contract.INVARIANT_UNRESOLVED_INCIDENTS,
                    ),
                    "destination": (contract.INVARIANT_ONE_EFFECT_PER_KEY,),
                    "recovery_owner": None,
                },
            )
        )

    # -- aligned combinations ------------------------------------------------
    for targets in (
        (ROLE_SUPERVISOR, ROLE_DISPATCHER),
        (ROLE_DISPATCHER, ROLE_SECRETARY),
        (ROLE_SUPERVISOR, ROLE_SECRETARY),
        (ROLE_SUPERVISOR, ROLE_DISPATCHER, ROLE_SECRETARY),
    ):
        for checkpoint in (
            CHECKPOINT_BEFORE_DURABLE_WRITE,
            CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD,
        ):
            cases.append(
                _case(
                    targets=targets,
                    operation=OPERATION_ATTEMPT,
                    checkpoint=checkpoint,
                    fault="sigkill",
                    lane=LANE_LINUX,
                    profiles=("full",),
                    arms={
                        role: (f"{OPERATION_ATTEMPT}@{checkpoint}:1",) for role in targets
                    },
                    expected={
                        "queries": _KILL_QUERIES,
                        "destination": _KILL_DESTINATION,
                        "recovery_owner": targets[-1],
                    },
                )
            )

    return cases


def build_manifest() -> dict:
    return {
        "manifest_version": MANIFEST_VERSION,
        "contract_version": contract.FAULT_RUNNER_CONTRACT_VERSION,
        "clock_base_ms": CLOCK_BASE_MS,
        "ttl_ms": TTL_MS,
        "clock_guard_ms": contract.CLOCK_GUARD_MS,
        "pruning_rule": PRUNING_RULE,
        "profiles": {name: dict(values) for name, values in PROFILES.items()},
        "cases": build_cases(),
    }


def load_manifest() -> dict:
    """Read the frozen matrix. Validation runs at collection, never at run."""

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    return manifest


def profile_cases(manifest: Mapping[str, Any], *, profile: str, lanes: Sequence[str]) -> list[dict]:
    """The cases a run of ``profile`` on ``lanes`` executes."""

    if profile not in manifest["profiles"]:
        raise ContractViolation(f"{profile!r} is not a manifest profile")
    return [
        case
        for case in manifest["cases"]
        if profile in case["profiles"] and case["lane"] in lanes
    ]


# ---------------------------------------------------------------------------
# validation -- refused at collection, never as a timeout in CI
# ---------------------------------------------------------------------------

def validate_case(case: Mapping[str, Any]) -> None:
    """Every rule design section 4/5/6/7 states as manifest-enforced."""

    case_id = case.get("case_id", "<unnamed>")

    targets = tuple(case["targets"])
    if not targets:
        raise ContractViolation(f"{case_id}: a case names at least one target")
    if any(role not in ROLES for role in targets):
        raise ContractViolation(f"{case_id}: {targets} is not a subset of {ROLES}")
    if list(targets) != [role for role in ROLES if role in targets]:
        raise ContractViolation(
            f"{case_id}: targets are written in the contract's role order so a "
            "subset has one canonical spelling"
        )

    if case["lane"] not in contract.LANES:
        raise ContractViolation(f"{case_id}: {case['lane']!r} is not a lane")
    if case["fault"] not in contract.FAULT_KINDS:
        raise ContractViolation(f"{case_id}: {case['fault']!r} is not a fault kind")
    if case["barrier"] not in contract.BARRIER_MODES:
        raise ContractViolation(f"{case_id}: {case['barrier']!r} is not a barrier mode")

    # SIGSTOP is Linux-only, and what does not run on an OS is enumerable
    # (design 8.1) -- so the lane is checked against the fault, not inferred.
    if case["fault"] == "sigstop-expire" and case["lane"] != LANE_LINUX:
        raise ContractViolation(
            f"{case_id}: SIGSTOP cases are Linux-lane only (design 8.1)"
        )
    if case["fault"] == "staggered-sigkill" and case["barrier"] != BARRIER_STAGGERED:
        raise ContractViolation(f"{case_id}: a staggered kill declares staggered mode")
    if case["barrier"] == BARRIER_STAGGERED and not case["staggered"]:
        # The controller dispatches on the barrier mode, so a case that declares
        # staggered without naming its sequence would have no sequence to run.
        raise ContractViolation(
            f"{case_id}: staggered mode names its full sequence (design 5)"
        )
    if case["staggered"] and case["barrier"] != BARRIER_STAGGERED:
        raise ContractViolation(
            f"{case_id}: a staggered sequence is only run in staggered mode"
        )

    for role, anchors in case["arms"].items():
        if role not in ROLES:
            raise ContractViolation(f"{case_id}: {role!r} is not a role")
        if not anchors:
            raise ContractViolation(
                f"{case_id}: {role} is armed with nothing; every fault is "
                "anchored (design 4.1)"
            )
        for wire in anchors:
            armed = ArmedAnchor.parse(wire)
            if armed.anchor in contract.SYNC_POINTS:
                continue
            operation = armed.operation
            if operation is None:
                raise ContractViolation(
                    f"{case_id}: a checkpoint arming names its operation, so "
                    "the applicability matrix can be checked"
                )
            applicable = contract.CHECKPOINT_APPLICABILITY[operation]
            if armed.anchor not in applicable:
                raise ContractViolation(
                    f"{case_id}: {operation} has no {armed.anchor} window "
                    f"(it has {applicable}); a barrier that cannot be reached "
                    "is a manifest error, not a CI timeout"
                )

    if set(case["arms"]) != set(targets) and case["claimant"] is None:
        raise ContractViolation(f"{case_id}: every target is armed and only targets are")
    if any(role not in targets for role in case["kill_order"]):
        raise ContractViolation(f"{case_id}: kill_order names a non-target")
    if case["restart_order"] != "concurrent" and any(
        role not in targets for role in case["restart_order"]
    ):
        raise ContractViolation(f"{case_id}: restart_order names a non-target")

    expected = case["expected"]
    for name in expected["queries"]:
        if name not in SQL_INVARIANTS:
            raise ContractViolation(f"{case_id}: {name!r} is not a SQL invariant")
    for name in expected["destination"]:
        if name not in DESTINATION_INVARIANTS:
            raise ContractViolation(f"{case_id}: {name!r} is not a destination invariant")
    if not expected["queries"] and not expected["destination"]:
        raise ContractViolation(f"{case_id}: a case asserts something")

    # ACCEPTANCE.md section 2: a case that asserts exactly-once for an external
    # effect using only our own rows does not pass. So a case anchored inside or
    # after an effect window must name a destination assertion.
    anchored_in_effect_window = case["checkpoint"] in EFFECT_BEARING_CHECKPOINTS or any(
        ArmedAnchor.parse(wire).anchor in EFFECT_BEARING_CHECKPOINTS
        for anchors in case["arms"].values()
        for wire in anchors
    )
    if anchored_in_effect_window and not expected["destination"]:
        raise ContractViolation(
            f"{case_id}: armed inside or after an effect window, where SQLite "
            "alone cannot tell a completed effect from one that never started "
            "-- name a destination assertion. The check reads the armed anchors "
            "and not only the case-id classification, because in a combination "
            "case a secondary role can be the one armed in the effect window"
        )

    if expected["recovery_owner"] is not None and expected["recovery_owner"] not in ROLES:
        raise ContractViolation(f"{case_id}: {expected['recovery_owner']!r} is not a role")
    # A kill at the very first durable write of a role's script leaves nothing
    # behind: no lease row, no message, no action. Such a case is worth having --
    # it proves a restart from a cold start is clean -- but it has no recovery to
    # name, and naming one would be an assertion satisfied by an empty set. So
    # the rule runs both ways.
    nothing_was_written = (
        len(targets) == 1
        and case["checkpoint"] == contract.CHECKPOINT_BEFORE_DURABLE_WRITE
        and case["operation"] == contract.ROLE_SCRIPTS[targets[0]][0]
    )
    if case["restart_after"] and expected["recovery_owner"] is None and not nothing_was_written:
        raise ContractViolation(
            f"{case_id}: a case that restarts names the role whose recovery it "
            "asserts; 'somebody recovered it' is not an assertion (design 5)"
        )
    if case["restart_after"] and expected["recovery_owner"] is not None and nothing_was_written:
        raise ContractViolation(
            f"{case_id}: the kill lands before this role's first durable write, "
            "so the restart has nothing to recover and the case may not name a "
            "recovery owner"
        )

    skew = case["skew"]
    if skew is not None and skew.get("observation") != "next-operation":
        raise ContractViolation(
            f"{case_id}: a same-role skew is observed by the script's next "
            "operation; an expectation that depends on an in-flight call seeing "
            "a mid-call skew is invalid by construction (design 7)"
        )
    claimant = case["claimant"]
    if claimant is not None and claimant.get("observation") != "sibling":
        raise ContractViolation(
            f"{case_id}: a cross-role skew is observed by the sibling acting "
            "under its new clock (design 7)"
        )

    if case["fault"] in INCIDENT_FAULTS:
        parameters = case["incident_params"]
        if parameters is None:
            raise ContractViolation(
                f"{case_id}: an incident case carries its Q-0002 parameters"
            )
        # The relaxation, and its exact scope (I-11, Issue `#16`).
        #
        # S9 refused any value in ``incident_params`` on the grounds that S9
        # fixes none, which was right for S9: it shipped no incident case, so a
        # value there could only have been a decision taken by accident.
        # ACCEPTANCE.md section 2 asks something different of the *matrix*:
        # "tests must parameterise both rather than hard-code either". A case
        # therefore carries a value, and the discipline moves up one level -- to
        # :func:`_validate_incident_parameterisation`, which refuses a matrix
        # that has quietly settled on one rule or one window. Neither this
        # function nor that one expresses a preference between the rules, and
        # ``Q-0002`` stays open.
        if parameters.get("collapse") not in COLLAPSE_RULES:
            raise ContractViolation(
                f"{case_id}: {parameters.get('collapse')!r} is not one of the "
                f"collapse rules {COLLAPSE_RULES}; an incident case names the "
                "rule it runs under so the matrix can cover both"
            )
        window = parameters.get("renotify_window_ms")
        if not isinstance(window, int) or window <= 0:
            raise ContractViolation(
                f"{case_id}: the re-notification window is the *other* half of "
                "Q-0002 and is stated in absolute time, so a case names a "
                f"positive value; got {window!r}"
            )
        if not isinstance(parameters.get("repeats"), int) or parameters["repeats"] < 2:
            raise ContractViolation(
                f"{case_id}: 'raise the same condition repeatedly' needs at "
                "least two raises"
            )
        if not isinstance(parameters.get("expect_collapse"), bool):
            raise ContractViolation(
                f"{case_id}: a case says whether its raises fall inside its own "
                "window; deriving it from the window would bake this harness's "
                "step interval into the assertion"
            )
        if not parameters.get("dedup_key"):
            raise ContractViolation(
                f"{case_id}: the dedup key is case data. Q-0002 asks what "
                "composes it, and a driver-side formula would answer that by "
                "inertia -- exactly as a role-to-resource table would answer "
                "Q-0001"
            )
        if "reconcile_interval_ms" not in parameters:
            raise ContractViolation(
                f"{case_id}: incident_params omits 'reconcile_interval_ms'"
            )
        if parameters["reconcile_interval_ms"] is not None:
            raise ContractViolation(
                f"{case_id}: reconcile_interval_ms is Q-0003, not Q-0002, and "
                "nothing in this task settles it; leave it unset"
            )

    if case["fault"] in ("drop-delivery", "dup-delivery", "lost-ack"):
        if not case["release_after_barrier"]:
            raise ContractViolation(
                f"{case_id}: a delivery-surface fault anchors at a pass-through "
                "barrier and declares release_after_barrier"
            )
        if case["incident_params"] is None:
            raise ContractViolation(
                f"{case_id}: the dedup row of ACCEPTANCE.md section 2 requires "
                "both Q-0002 (collapse semantics) and Q-0003 (reconcile "
                "interval) to be parameterised rather than hard-coded; carry "
                "them as manifest fields, unset"
            )
        for key in ("collapse", "reconcile_interval_ms"):
            if key not in case["incident_params"]:
                raise ContractViolation(f"{case_id}: incident_params omits {key!r}")
            if case["incident_params"][key] is not None:
                raise ContractViolation(
                    f"{case_id}: {key!r} is fixed to "
                    f"{case['incident_params'][key]!r}; S9 fixes no value for an "
                    "open question (design 10)"
                )

    if case["ttl_ms"] <= 0 or case["clock_base_ms"] <= 0:
        raise ContractViolation(f"{case_id}: the lease geometry is positive")
    for profile in case["profiles"]:
        if profile not in PROFILES:
            raise ContractViolation(f"{case_id}: {profile!r} is not a profile")


def _validate_incident_parameterisation(manifest: Mapping[str, Any]) -> None:
    """Q-0002 is parameterised by the matrix, not answered by it.

    ``ACCEPTANCE.md`` section 2's dedup row is explicit that the Issue fixes the
    incident *fields* and not the semantics -- whether a repeat increments the
    retry count on the existing incident or opens a linked one is unresolved,
    "as is the re-notification window in absolute time -- both are Q-0002" --
    and that "tests must parameterise both rather than hard-code either".

    Per-case validation lets a case name a rule. This is what stops the matrix
    as a whole from having quietly picked one: every rule in the vocabulary must
    appear, and more than one window must appear, so no single value can be
    load-bearing on a pass. A matrix that drifted onto one rule fails at
    collection with the drift named, rather than passing and reading as though
    the question were settled.

    Q-0003 is a different question and stays out of it: ``reconcile_interval_ms``
    is refused a value by :func:`validate_case`.
    """

    incident_cases = [
        case for case in manifest["cases"] if case["fault"] in INCIDENT_FAULTS
    ]
    if not incident_cases:
        raise ContractViolation(
            "the dedup row of ACCEPTANCE.md section 2 names two injections -- a "
            "repeated incident condition and a replayed packet -- and the "
            "matrix has no case for either"
        )
    rules = {case["incident_params"]["collapse"] for case in incident_cases}
    if rules != set(COLLAPSE_RULES):
        raise ContractViolation(
            f"the incident cases run under {sorted(rules)}; Q-0002 is open, so "
            f"the matrix covers every rule in {sorted(COLLAPSE_RULES)} rather "
            "than settling on one by omission"
        )
    windows = {case["incident_params"]["renotify_window_ms"] for case in incident_cases}
    if len(windows) < 2:
        raise ContractViolation(
            f"every incident case declares the same re-notification window "
            f"({sorted(windows)}); one window cannot show that the assertion "
            "does not depend on its value, which is what parameterising it means"
        )
    if not any(
        case["incident_params"]["expect_collapse"] is False for case in incident_cases
    ):
        raise ContractViolation(
            "no incident case declares a window its own raises fall outside of, "
            "so the window is carried but never does anything -- an inert "
            "parameter is indistinguishable from a hard-coded one"
        )


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Whole-matrix rules: identity, versions and the profile budgets."""

    if manifest["contract_version"] != contract.FAULT_RUNNER_CONTRACT_VERSION:
        raise ContractViolation(
            f"the manifest targets fault-runner contract "
            f"{manifest['contract_version']}, this build is "
            f"{contract.FAULT_RUNNER_CONTRACT_VERSION}"
        )

    seen: set[str] = set()
    for case in manifest["cases"]:
        if case["case_id"] in seen:
            # A duplicate fails the run before any case executes, because the
            # case id is the re-run key, the manifest key and the failure-report
            # key all at once (design 4.1).
            raise ContractViolation(f"duplicate case_id {case['case_id']!r}")
        seen.add(case["case_id"])
        validate_case(case)

    # Growth in I-11's matrix forces an explicit budget diff instead of silent
    # CI creep (design 9).
    for name, profile in manifest["profiles"].items():
        count = len([case for case in manifest["cases"] if name in case["profiles"]])
        if count > profile["max_cases"]:
            raise ContractViolation(
                f"profile {name!r} holds {count} cases, over its "
                f"{profile['max_cases']}-case budget: raise the budget in an "
                "explicit diff or prune the matrix"
            )

    # The off-Linux add-on is its own budget (design 9).
    portable = len([case for case in manifest["cases"] if case["lane"] == LANE_PORTABLE])
    if portable > 20:
        raise ContractViolation(
            f"the portable lane holds {portable} cases, over its 20-case "
            "off-Linux budget"
        )

    _validate_incident_parameterisation(manifest)

    # Coverage the design requires S9 to seed: one case per fault kind, per
    # checkpoint, per lane.
    faults = {case["fault"] for case in manifest["cases"]}
    if faults != set(contract.FAULT_KINDS):
        raise ContractViolation(
            f"the seed set misses fault kinds {sorted(set(contract.FAULT_KINDS) - faults)}"
        )
    anchors = {case["checkpoint"] for case in manifest["cases"]}
    if not set(CHECKPOINTS) <= anchors:
        raise ContractViolation(
            f"the seed set misses checkpoints {sorted(set(CHECKPOINTS) - anchors)}"
        )
    lanes = {case["lane"] for case in manifest["cases"]}
    if lanes != set(contract.LANES):
        raise ContractViolation(f"the seed set misses lanes {sorted(set(contract.LANES) - lanes)}")
