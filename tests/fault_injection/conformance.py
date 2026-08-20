"""The adapter conformance battery (design 6.3).

Run against **every** adapter, present and future. An adapter that has not
passed it cannot contribute matrix results -- this is the mechanical form of
design section 2.2's "stays valid" claim, and it is what the I-12/I-14 adapters
will be built against.

It asserts the contract itself, not any component's behaviour:

1. every checkpoint is reachable, and an armed one blocks;
2. the barrier round-trip works -- ``continue`` releases and the script finishes;
3. SIGKILL at each checkpoint yields exit ``-SIGKILL`` and a database the
   invariant queries can still be run against;
4. the restart entrypoint emits recovery-complete and is idempotent -- restarting
   twice changes nothing;
5. the injected clock is honoured -- ``set_clock_offset`` visibly moves the
   driver's reported ``now_ms``;
6. two runs of one case with one seed produce identical event traces;
7. the contract's checkpoint names equal the adapter's own vocabulary;
8. the driver CLI accepts every option the contract names;
9. the driver never reads the host clock.

So "the harness ran" can never silently mean "the adapter faked it".
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from tests.fault_injection import contract
from tests.fault_injection.controller import Controller
from tests.fault_injection.contract import ArmedAnchor, ContractViolation

__all__ = [
    "BATTERY",
    "synthetic_case",
    "check_barrier_round_trip",
    "check_checkpoint_blocks",
    "check_clock_is_injected",
    "check_driver_cli",
    "check_identical_traces",
    "check_invariant_queries_are_not_vacuous",
    "check_no_host_clock",
    "check_restart_is_idempotent",
    "check_sigkill_exit_status",
    "check_vocabulary_matches",
]

CONFORMANCE_CLOCK_BASE_MS = 1_700_000_000_000
CONFORMANCE_TTL_MS = 30_000
CONFORMANCE_SEED = 4_242


def synthetic_case(
    *,
    case_id: str,
    role: str,
    arms: Mapping[str, Sequence[str]],
    messages: int = 1,
    behaviours: Sequence[str] = (),
) -> dict:
    """A minimal case for the battery. Never part of the matrix.

    Deliberately built here rather than borrowed from the manifest: the battery
    must be runnable against an adapter before that adapter has any manifest
    cases at all.
    """

    return {
        "case_id": case_id,
        "targets": [role],
        "operation": contract.OPERATION_ATTEMPT,
        "checkpoint": contract.CHECKPOINT_BEFORE_DURABLE_WRITE,
        "fault": "sigkill",
        "variant": None,
        "lane": contract.LANE_PORTABLE,
        "profiles": ["full"],
        "barrier": contract.BARRIER_ALIGNED,
        "arms": {key: list(value) for key, value in arms.items()},
        "kill_order": [role],
        "restart_order": [role],
        "expected": {"queries": [], "destination": [], "recovery_owner": None},
        "messages": messages,
        "behaviours": list(behaviours),
        "claimant": None,
        "skew": None,
        "release_after_barrier": False,
        "restart_after": False,
        "staggered": None,
        "incident_params": None,
        "ttl_ms": CONFORMANCE_TTL_MS,
        "clock_base_ms": CONFORMANCE_CLOCK_BASE_MS,
        "manifest_version": 0,
    }


def _controller(adapter: Any, workdir: Path, case: Mapping[str, Any]) -> Controller:
    return Controller(
        workdir=workdir,
        adapter=adapter,
        case=case,
        suite_seed=CONFORMANCE_SEED,
        barrier_timeout_s=15.0,
        case_timeout_s=60.0,
    )


# ---------------------------------------------------------------------------
# 1 and 2 -- reachable, blocking, and released by the round-trip
# ---------------------------------------------------------------------------

def check_checkpoint_blocks(
    adapter: Any, workdir: Path, *, role: str, operation: str, checkpoint: str
) -> Mapping[str, Any]:
    """The named window is reached, announced, and the process holds there."""

    case = synthetic_case(
        case_id=f"conformance-{role}-{operation}-{checkpoint}",
        role=role,
        arms={role: [f"{operation}@{checkpoint}:1"]},
    )
    with _controller(adapter, workdir, case) as controller:
        controller.bootstrap()
        controller.spawn(role, armed=[ArmedAnchor.parse(f"{operation}@{checkpoint}:1")])
        event = controller.wait_at_anchor(role)
        if event["name"] != checkpoint or event["operation"] != operation:
            raise ContractViolation(
                f"asked for {operation}@{checkpoint}, the driver stopped at "
                f"{event['operation']}@{event['name']}"
            )
        process = controller.processes[role]
        if process.popen.poll() is not None:
            raise ContractViolation(
                "the driver exited at the barrier instead of blocking in it"
            )
        return dict(event)


def check_barrier_round_trip(adapter: Any, workdir: Path, *, role: str) -> None:
    """``continue`` releases the barrier and the script runs to a clean exit."""

    checkpoint = contract.CHECKPOINT_BEFORE_DURABLE_WRITE
    case = synthetic_case(
        case_id=f"conformance-round-trip-{role}",
        role=role,
        arms={role: [f"{contract.OPERATION_ATTEMPT}@{checkpoint}:1"]},
    )
    with _controller(adapter, workdir, case) as controller:
        controller.bootstrap()
        controller.spawn(
            role,
            armed=[ArmedAnchor.parse(f"{contract.OPERATION_ATTEMPT}@{checkpoint}:1")],
        )
        controller.wait_at_anchor(role)
        controller.release(role)
        controller.run_to_completion(role)


# ---------------------------------------------------------------------------
# 3 -- a real kill, and a database that survives it
# ---------------------------------------------------------------------------

def check_sigkill_exit_status(
    adapter: Any, workdir: Path, *, role: str, checkpoint: str, assert_exit_status: bool
) -> None:
    """The kill is a signal, and afterwards the store is still queryable.

    The second half matters as much as the first: a SIGKILL takes down a SQLite
    connection mid-transaction, and the invariant queries running against the
    reopened file is the evidence that the journal recovered rather than that
    nothing was going on.
    """

    case = synthetic_case(
        case_id=f"conformance-kill-{role}-{checkpoint}",
        role=role,
        arms={role: [f"{contract.OPERATION_ATTEMPT}@{checkpoint}:1"]},
    )
    with _controller(adapter, workdir, case) as controller:
        controller.bootstrap()
        controller.spawn(
            role,
            armed=[ArmedAnchor.parse(f"{contract.OPERATION_ATTEMPT}@{checkpoint}:1")],
        )
        controller.wait_at_anchor(role)
        controller.kill(role, assert_exit_status=assert_exit_status)
        for name in contract.SQL_INVARIANTS:
            wanted = contract.INVARIANT_PARAMETERS[name]
            params = adapter.query_parameters(role, now_ms=CONFORMANCE_CLOCK_BASE_MS)
            controller.query(name, **{key: params[key] for key in wanted})


# ---------------------------------------------------------------------------
# 4 -- the restart entrypoint recovers, and recovering twice changes nothing
# ---------------------------------------------------------------------------

def check_restart_is_idempotent(adapter: Any, workdir: Path, *, role: str) -> None:
    checkpoint = contract.CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT
    case = synthetic_case(
        case_id=f"conformance-restart-{role}",
        role=role,
        arms={role: [f"{contract.OPERATION_ATTEMPT}@{checkpoint}:1"]},
    )
    with _controller(adapter, workdir, case) as controller:
        controller.bootstrap()
        controller.spawn(
            role,
            armed=[ArmedAnchor.parse(f"{contract.OPERATION_ATTEMPT}@{checkpoint}:1")],
        )
        controller.wait_at_anchor(role)
        controller.kill(role, assert_exit_status=False)

        controller.restart(role)
        controller.run_to_completion(role)
        first = _snapshot(controller, adapter, role)
        if not first[contract.INVARIANT_LINEAR_WRITER_HISTORY] or not first[
            contract.INVARIANT_RETRY_COUNT_DURABLE
        ]:
            raise ContractViolation(
                f"{adapter.driver_module}: the idempotence snapshot is empty, so "
                "'restarting twice changes nothing' compares nothing to nothing"
            )

        controller.restart(role)
        controller.run_to_completion(role)
        second = _snapshot(controller, adapter, role)

    if first != second:
        raise ContractViolation(
            "restarting a recovered role changed durable state; recovery is "
            f"not idempotent\nfirst:  {json.dumps(first, sort_keys=True)}\n"
            f"second: {json.dumps(second, sort_keys=True)}"
        )


def _snapshot(controller: Controller, adapter: Any, role: str) -> dict:
    """Everything a restart could change, so "changes nothing" means something.

    The write history and the retry state are in here, not only the "is anything
    unfinished" queries: an adapter whose restart appended another applied
    action, or bumped a retry count, or re-attempted an already-acked message,
    would leave every unfinished-work query empty and pass an idempotence check
    that never looked at what it actually mutated.

    The lease row is deliberately excluded -- a restart renews, and an expiry
    that moves is the correct behaviour, not a durable change.
    """

    now_ms = controller.last_reported_now_ms(default=CONFORMANCE_CLOCK_BASE_MS)
    params = adapter.query_parameters(role, now_ms=now_ms)
    snapshot: dict[str, Any] = {}
    for name in (
        contract.INVARIANT_NO_UNOWNED_OUTBOX,
        contract.INVARIANT_SINGLE_ACKED_STATE,
        contract.INVARIANT_NO_PENDING_ACTION,
        contract.INVARIANT_LINEAR_WRITER_HISTORY,
        contract.INVARIANT_RETRY_COUNT_DURABLE,
    ):
        wanted = contract.INVARIANT_PARAMETERS[name]
        snapshot[name] = controller.query(
            name, **{key: params[key] for key in wanted}
        )
    observer = controller.observer(role)
    snapshot["effects"] = {
        key: (observer.effect_count(key), observer.attempt_count(key))
        for key in adapter.effect_keys(role, controller.case)
    }
    return snapshot


# ---------------------------------------------------------------------------
# 5 -- the clock is injected, not read
# ---------------------------------------------------------------------------

def check_clock_is_injected(adapter: Any, workdir: Path, *, role: str) -> None:
    """A ``set_clock_offset`` at a barrier visibly moves the reported ``now_ms``."""

    anchor = f"{contract.OPERATION_LEASE_ACQUIRE}@{contract.SYNC_LEASE_ACQUIRED}:1"
    case = synthetic_case(
        case_id=f"conformance-clock-{role}", role=role, arms={role: [anchor]}
    )
    offset = contract.resolve_skew_ms(
        "forward", ttl_ms=CONFORMANCE_TTL_MS, elapsed_ms=0
    )
    with _controller(adapter, workdir, case) as controller:
        controller.bootstrap()
        controller.spawn(role, armed=[ArmedAnchor.parse(anchor)])
        at_barrier = controller.wait_at_anchor(role)
        moved = controller.set_clock_offset(role, offset)
        if int(moved["offset_ms"]) != offset:
            raise ContractViolation(
                f"the driver reported offset {moved['offset_ms']}, not {offset}"
            )
        if int(moved["now_ms"]) - int(at_barrier["now_ms"]) != offset:
            raise ContractViolation(
                "the driver's reported now_ms did not move by the injected "
                f"offset: {at_barrier['now_ms']} -> {moved['now_ms']}"
            )
        controller.release(role)
        controller.run_to_completion(role)


#: Reading any of these is reading the host clock. The check is over the parsed
#: syntax tree, not over the source text: a prose mention of ``time.time()`` in a
#: docstring is not a call, and a checker that cannot tell the two apart teaches
#: people to stop writing the docstring.
FORBIDDEN_CLOCK_CALLS = frozenset(
    {
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
    }
)

FORBIDDEN_CLOCK_MODULES = frozenset({"time", "datetime"})


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def check_no_host_clock(adapter: Any) -> None:
    """The driver may not read the host clock -- not as a base, not as a fallback.

    Asserted over the module's own syntax tree rather than by trusting a
    docstring: it is the property the identical-trace requirement rests on, and
    a single ``time.time()`` fallback would make a re-run on another day differ
    while every test still passed. The clock a role process has is the injected
    one; the *controller's* watchdogs run on host monotonic time and are never
    skewed, which is a deliberate asymmetry (design 7) and is why only the
    driver module is scanned.
    """

    module = importlib.import_module(adapter.driver_module)
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))

    offenders: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in FORBIDDEN_CLOCK_CALLS:
                offenders.add(name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_CLOCK_MODULES:
                    offenders.add(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_CLOCK_MODULES:
                offenders.add(f"from {node.module} import ...")

    if offenders:
        raise ContractViolation(
            f"{adapter.driver_module} reaches the host clock "
            f"({', '.join(sorted(offenders))}); the injected clock is the only "
            "clock a role process has (design 7)"
        )


# ---------------------------------------------------------------------------
# 6 -- same case, same seed, identical trace
# ---------------------------------------------------------------------------

def check_identical_traces(adapter: Any, workdir: Path, *, role: str) -> None:
    """Two runs of one case with one seed produce identical event traces.

    This is the whole determinism claim made testable (design 4.4). It holds
    only because the clock is virtual and because no identifier the driver puts
    on the wire is randomly generated.
    """

    traces = []
    for run in range(2):
        checkpoint = contract.CHECKPOINT_DELIVERED_BEFORE_ACK
        case = synthetic_case(
            case_id="conformance-determinism",
            role=role,
            arms={role: [f"{contract.OPERATION_ATTEMPT}@{checkpoint}:1"]},
            messages=2,
        )
        with _controller(adapter, workdir / f"run{run}", case) as controller:
            controller.bootstrap()
            controller.spawn(
                role,
                armed=[ArmedAnchor.parse(f"{contract.OPERATION_ATTEMPT}@{checkpoint}:1")],
            )
            controller.wait_at_anchor(role)
            controller.release(role)
            controller.run_to_completion(role)
            traces.append(json.dumps(controller.traces()[role], sort_keys=True))
    if traces[0] != traces[1]:
        raise ContractViolation(
            "the same case with the same seed produced different traces:\n"
            f"{traces[0]}\n{traces[1]}"
        )


# ---------------------------------------------------------------------------
# 7 and 8 -- vocabulary and CLI
# ---------------------------------------------------------------------------

def check_vocabulary_matches(adapter: Any) -> None:
    """The contract's four names are the adapter's four names.

    Today they are textually equal to S7's constants. When S7 is discarded the
    contract's names survive and the next adapter maps its internals onto them --
    this assertion is what makes that mapping mandatory rather than optional.
    """

    vocabulary = tuple(adapter.checkpoint_vocabulary())
    if vocabulary != contract.CHECKPOINTS:
        raise ContractViolation(
            f"{adapter.driver_module} names its windows {vocabulary}; the "
            f"contract names them {contract.CHECKPOINTS}"
        )


def check_driver_cli(adapter: Any) -> None:
    """The driver accepts every option the contract names, and says so.

    Checked by running ``--help`` in a real subprocess, which also smoke-tests
    that the module is executable at all and that its help text encodes cleanly
    on the console encoding of the platform running it.
    """

    from tests.fault_injection.controller import _child_env

    result = subprocess.run(
        [sys.executable, "-m", adapter.driver_module, "--help"],
        capture_output=True,
        text=True,
        env=dict(_child_env()),
    )
    if result.returncode != 0:
        raise ContractViolation(
            f"{adapter.driver_module} --help exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    missing = [
        option
        for option in contract.driver_cli_arguments()
        if option not in result.stdout
    ]
    if missing:
        raise ContractViolation(
            f"{adapter.driver_module} does not accept {missing}; the contract's "
            f"CLI is {contract.driver_cli_arguments()}"
        )


def check_invariant_queries_bind_the_contract_parameters(adapter: Any) -> None:
    """Every named invariant is bound, and binds exactly its named parameters."""

    queries = adapter.invariant_queries()
    missing = [name for name in contract.SQL_INVARIANTS if name not in queries]
    if missing:
        raise ContractViolation(f"{adapter.driver_module} binds no SQL for {missing}")
    for name, sql in queries.items():
        declared = set(contract.INVARIANT_PARAMETERS[name])
        used = set(re.findall(r":([a-z_]+)", sql))
        # Equality, not containment. A subset check only catches the harmless
        # direction: an adapter that *omitted* a parameter would pass, and the
        # omission is the dangerous one -- a ``lease-single-holder`` query
        # without ``:now_ms`` reads expired leases as live and reports a
        # single-holder violation that is not there, or misses one that is.
        if used != declared:
            raise ContractViolation(
                f"{name} binds {sorted(used)}; the contract names "
                f"{sorted(declared)}. Missing: {sorted(declared - used)}; "
                f"unexpected: {sorted(used - declared)}"
            )


def check_invariant_queries_are_not_vacuous(adapter: Any, workdir: Path, *, role: str) -> None:
    """Every named query can actually see the rows its role wrote.

    The failure this exists for is the quietest one a harness has. A query whose
    scoping does not match the schema returns zero rows on every run, and an
    invariant of the shape "this result set is empty" then passes forever --
    including on the day the property it names is violated. It is not a test
    failure, it is the *absence* of one.

    So the battery runs a clean case and asserts the positive direction: the
    write history is non-empty, the lease is held at the instant the run
    reached, and the role's outbox rows are visible. An adapter that cannot show
    these has not bound the invariants, whatever its SQL says.
    """

    case = synthetic_case(
        case_id=f"conformance-vacuity-{role}", role=role, arms={}
    )
    with _controller(adapter, workdir, case) as controller:
        controller.bootstrap()
        controller.spawn(role, armed=())
        controller.run_to_completion(role)

        now_ms = controller.last_reported_now_ms(default=CONFORMANCE_CLOCK_BASE_MS)
        params = adapter.query_parameters(role, now_ms=now_ms)

        def rows(name: str) -> list:
            wanted = contract.INVARIANT_PARAMETERS[name]
            return controller.query(name, **{key: params[key] for key in wanted})

        history = rows(contract.INVARIANT_LINEAR_WRITER_HISTORY)
        if not history:
            raise ContractViolation(
                f"{adapter.driver_module}: linear-writer-history sees none of "
                f"{role}'s writes, so 'no epoch regression' would pass over an "
                "empty set forever"
            )
        outbox_rows = rows(contract.INVARIANT_RETRY_COUNT_DURABLE)
        if not outbox_rows:
            raise ContractViolation(
                f"{adapter.driver_module}: retry-count-durable sees none of "
                f"{role}'s outbox rows"
            )
        if contract.OPERATION_LEASE_RELEASE not in contract.ROLE_SCRIPTS[role]:
            held = [
                row
                for row in rows(contract.INVARIANT_LEASE_SINGLE_HOLDER)
                if row["resource"] == params["resource"]
            ]
            if not held:
                raise ContractViolation(
                    f"{adapter.driver_module}: no live holder on "
                    f"{params['resource']!r} at now_ms={now_ms}, so "
                    "'at most one live holder' would assert nothing"
                )

        observer = controller.observer(role)
        for key in adapter.effect_keys(role, case):
            if observer.effect_count(key) != 1:
                raise ContractViolation(
                    f"{adapter.driver_module}: the destination observer cannot "
                    f"see the effect for {key!r}"
                )


def check_refusal_ids_are_unique(adapter: Any, workdir: Path, *, role: str) -> None:
    """No two refusals recorded in one case share an ``action_id``.

    A refusal's ``action_id`` is whatever the driver passed as ``attempt_id``,
    and it is the primary key of the row. A harness cannot use a uuid there --
    a uuid in the evidence is a re-run that cannot be compared -- so the ids are
    composed, and a composed id collides the moment the same writer is refused
    twice on the same operation. The collision does not surface as a duplicate
    row: it surfaces as an ``IntegrityError`` raised from inside the write's own
    transaction, *instead of* the refusal exception, which rolls the refusal
    back. The record ACCEPTANCE.md section 2 requires to be durable is precisely
    the thing that is lost.

    S7 hit this and randomises its own bare-refusal ids; this check is how the
    harness keeps the property without being able to.
    """

    case = synthetic_case(
        case_id=f"conformance-refusal-ids-{role}", role=role, arms={}
    )
    with _controller(adapter, workdir, case) as controller:
        controller.bootstrap()
        controller.spawn(role, armed=())
        controller.run_to_completion(role)

        now_ms = controller.last_reported_now_ms(default=CONFORMANCE_CLOCK_BASE_MS)
        params = adapter.query_parameters(role, now_ms=now_ms)
        history = controller.query(
            contract.INVARIANT_LINEAR_WRITER_HISTORY, scope=params["scope"]
        )
        ids = [row["action_id"] for row in history]
        duplicates = sorted({name for name in ids if ids.count(name) > 1})
        if duplicates:
            raise ContractViolation(
                f"{adapter.driver_module}: {role} wrote action rows sharing "
                f"{duplicates}; a refusal id that repeats loses the refusal it "
                "was supposed to record"
            )


#: The battery, as data, so a report can name what ran.
BATTERY = (
    "checkpoint-blocks",
    "barrier-round-trip",
    "sigkill-exit-status",
    "restart-is-idempotent",
    "clock-is-injected",
    "no-host-clock",
    "identical-traces",
    "vocabulary-matches",
    "driver-cli",
    "invariant-queries",
    "invariant-queries-not-vacuous",
    "refusal-ids-unique",
)
