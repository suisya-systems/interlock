"""The two-phase barrier protocol itself, and the process hygiene around it.

Design sections 3 and 8.2. These are the portable-lane tests: no case matrix, no
component behaviour, just the machinery the whole harness rests on. They run on
every OS because a barrier that only works on Linux is a harness that only works
on Linux.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.fault_injection import conformance, contract
from tests.fault_injection.contract import (
    ArmedAnchor,
    ContractViolation,
    Handshake,
    PROTOCOL_VERSION,
)
from tests.fault_injection.controller import BarrierTimeout, Controller, epoch_regressions
from tests.fault_injection.spike_driver import SPIKE_ADAPTER

_POSIX = os.name == "posix"


# ---------------------------------------------------------------------------
# arming vocabulary
# ---------------------------------------------------------------------------

def test_an_armed_anchor_round_trips_through_its_wire_form() -> None:
    """Operation, anchor and occurrence survive the CLI, because all three matter.

    A loop passes the same point repeatedly, so the occurrence index is part of
    the arming rather than an afterthought (design 3.1).
    """

    anchor = ArmedAnchor(
        anchor=contract.CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD,
        occurrence=3,
        operation=contract.OPERATION_ATTEMPT,
    )
    assert anchor.wire() == "attempt@after_effect_before_record:3"
    assert ArmedAnchor.parse(anchor.wire()) == anchor
    assert ArmedAnchor.parse("lease-acquired") == ArmedAnchor(anchor="lease-acquired")


def test_an_anchor_outside_the_contract_is_refused() -> None:
    with pytest.raises(ContractViolation, match="not an armable anchor"):
        ArmedAnchor(anchor="somewhere_near_the_write")
    with pytest.raises(ContractViolation, match="1-based"):
        ArmedAnchor(anchor=contract.SYNC_LEASE_ACQUIRED, occurrence=0)


def test_the_handshake_refuses_a_version_mismatch() -> None:
    """Controller and driver refuse a mismatch rather than guessing (design 6.2)."""

    good = Handshake(
        protocol_version=PROTOCOL_VERSION,
        contract_version=contract.FAULT_RUNNER_CONTRACT_VERSION,
        role=contract.ROLE_DISPATCHER,
        case_id="c",
        restart_generation=0,
    )
    good.check()
    with pytest.raises(ContractViolation, match="protocol"):
        Handshake(
            protocol_version=PROTOCOL_VERSION + 1,
            contract_version=contract.FAULT_RUNNER_CONTRACT_VERSION,
            role=contract.ROLE_DISPATCHER,
            case_id="c",
            restart_generation=0,
        ).check()
    with pytest.raises(ContractViolation, match="fault-runner contract"):
        Handshake(
            protocol_version=PROTOCOL_VERSION,
            contract_version=contract.FAULT_RUNNER_CONTRACT_VERSION + 1,
            role=contract.ROLE_DISPATCHER,
            case_id="c",
            restart_generation=0,
        ).check()


# ---------------------------------------------------------------------------
# the barrier
# ---------------------------------------------------------------------------

def _controller(tmp_path: Path, case: Any) -> Controller:
    return Controller(
        workdir=tmp_path,
        adapter=SPIKE_ADAPTER,
        case=case,
        suite_seed=1,
        barrier_timeout_s=15.0,
        case_timeout_s=60.0,
    )


def test_an_unarmed_checkpoint_costs_no_round_trip(tmp_path: Path) -> None:
    """A case perturbs the timing of nothing it is not about (design 3.1).

    Determinism comes from the barrier, not from timing -- so the windows a case
    does not name must be free, and the way to see that is that a run with
    nothing armed emits no checkpoint event at all while still doing the work.
    """

    case = conformance.synthetic_case(
        case_id="protocol-unarmed", role=contract.ROLE_DISPATCHER, arms={}
    )
    with _controller(tmp_path, case) as controller:
        controller.bootstrap()
        controller.spawn(contract.ROLE_DISPATCHER, armed=())
        controller.run_to_completion(contract.ROLE_DISPATCHER)
        trace = controller.traces()[contract.ROLE_DISPATCHER]

    kinds = [event["event"] for event in trace]
    assert contract.EVENT_CHECKPOINT not in kinds
    assert contract.EVENT_SYNC not in kinds
    assert contract.EVENT_DONE in kinds
    assert any(event.get("operation") == contract.OPERATION_ATTEMPT for event in trace)


def test_an_armed_checkpoint_holds_the_process_until_it_is_released(tmp_path: Path) -> None:
    """The hook writes one line and blocks reading one. It never raises.

    S7's ``checkpoint`` callable was designed so that raising from it kills a
    delivery. That is fine for S7's unit tests and disqualifying here: an
    exception unwinds the stack, runs ``finally`` clauses and closes the SQLite
    connection in an orderly way. None of that happens in a crash.
    """

    anchor = f"{contract.OPERATION_ATTEMPT}@{contract.CHECKPOINT_BEFORE_DURABLE_WRITE}:1"
    case = conformance.synthetic_case(
        case_id="protocol-armed",
        role=contract.ROLE_DISPATCHER,
        arms={contract.ROLE_DISPATCHER: [anchor]},
    )
    with _controller(tmp_path, case) as controller:
        controller.bootstrap()
        process = controller.spawn(
            contract.ROLE_DISPATCHER, armed=[ArmedAnchor.parse(anchor)]
        )
        event = controller.wait_at_anchor(contract.ROLE_DISPATCHER)
        assert event["name"] == contract.CHECKPOINT_BEFORE_DURABLE_WRITE
        assert event["occurrence"] == 1

        # Still alive, and still inside the window, a moment later.
        time.sleep(0.2)
        assert process.popen.poll() is None

        controller.release(contract.ROLE_DISPATCHER)
        controller.run_to_completion(contract.ROLE_DISPATCHER)


def test_the_occurrence_index_selects_which_pass_through_the_loop(tmp_path: Path) -> None:
    """The second delivery, not the first: a loop needs the index to be exact."""

    anchor = f"{contract.OPERATION_ATTEMPT}@{contract.CHECKPOINT_BEFORE_DURABLE_WRITE}:2"
    case = conformance.synthetic_case(
        case_id="protocol-occurrence",
        role=contract.ROLE_DISPATCHER,
        arms={contract.ROLE_DISPATCHER: [anchor]},
        messages=2,
    )
    with _controller(tmp_path, case) as controller:
        controller.bootstrap()
        controller.spawn(contract.ROLE_DISPATCHER, armed=[ArmedAnchor.parse(anchor)])
        event = controller.wait_at_anchor(contract.ROLE_DISPATCHER)
        assert event["occurrence"] == 2
        # The first message is already delivered when the second stops.
        earlier = [
            item
            for item in controller.traces()[contract.ROLE_DISPATCHER]
            if item["event"] == contract.EVENT_STEP
            and item.get("operation") == contract.OPERATION_ATTEMPT
        ]
        assert len(earlier) == 1
        controller.release(contract.ROLE_DISPATCHER)
        controller.run_to_completion(contract.ROLE_DISPATCHER)


def test_a_barrier_that_is_never_reached_becomes_an_attributable_failure(
    tmp_path: Path,
) -> None:
    """A CI hang is converted into a named failure, never a wedged job (design 8.2)."""

    anchor = f"{contract.OPERATION_ATTEMPT}@{contract.CHECKPOINT_BEFORE_DURABLE_WRITE}:9"
    case = conformance.synthetic_case(
        case_id="protocol-timeout",
        role=contract.ROLE_DISPATCHER,
        arms={contract.ROLE_DISPATCHER: [anchor]},
    )
    controller = Controller(
        workdir=tmp_path,
        adapter=SPIKE_ADAPTER,
        case=case,
        suite_seed=1,
        barrier_timeout_s=2.0,
        case_timeout_s=10.0,
    )
    with controller:
        controller.bootstrap()
        controller.spawn(contract.ROLE_DISPATCHER, armed=[ArmedAnchor.parse(anchor)])
        with pytest.raises(BarrierTimeout):
            controller.wait_at_anchor(contract.ROLE_DISPATCHER)


# ---------------------------------------------------------------------------
# process hygiene -- design 8.2
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _POSIX, reason="POSIX sessions and process groups")
def test_every_role_process_gets_its_own_session_and_group(tmp_path: Path) -> None:
    """So a stray shell cannot be confused with it and the group signals as a unit."""

    case = conformance.synthetic_case(
        case_id="protocol-session", role=contract.ROLE_DISPATCHER, arms={}
    )
    with _controller(tmp_path, case) as controller:
        controller.bootstrap()
        process = controller.spawn(contract.ROLE_DISPATCHER, armed=())
        assert os.getpgid(process.pid) == process.pid
        assert process.pgid == process.pid
        controller.run_to_completion(contract.ROLE_DISPATCHER)


def test_teardown_runs_on_the_unhappy_path_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    """Unconditional, layered, and reaps last: pass, fail and error alike."""

    anchor = f"{contract.OPERATION_ATTEMPT}@{contract.CHECKPOINT_BEFORE_DURABLE_WRITE}:1"
    case = conformance.synthetic_case(
        case_id="protocol-teardown",
        role=contract.ROLE_DISPATCHER,
        arms={contract.ROLE_DISPATCHER: [anchor]},
    )
    controller = _controller(tmp_path, case)
    with pytest.raises(RuntimeError):
        with controller:
            controller.bootstrap()
            process = controller.spawn(
                contract.ROLE_DISPATCHER, armed=[ArmedAnchor.parse(anchor)]
            )
            controller.wait_at_anchor(contract.ROLE_DISPATCHER)
            raise RuntimeError("the case blew up while a role was at a barrier")

    assert process.reaped
    assert process.popen.poll() is not None


@pytest.mark.skipif(not _POSIX, reason="SIGKILL exit status is POSIX-only")
def test_a_kill_is_a_signal_and_the_exit_status_says_so(tmp_path: Path) -> None:
    """A role process that exited any other way fails the case as a harness error."""

    anchor = f"{contract.OPERATION_ATTEMPT}@{contract.CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT}:1"
    case = conformance.synthetic_case(
        case_id="protocol-kill",
        role=contract.ROLE_DISPATCHER,
        arms={contract.ROLE_DISPATCHER: [anchor]},
    )
    with _controller(tmp_path, case) as controller:
        controller.bootstrap()
        controller.spawn(contract.ROLE_DISPATCHER, armed=[ArmedAnchor.parse(anchor)])
        controller.wait_at_anchor(contract.ROLE_DISPATCHER)
        status = controller.kill(contract.ROLE_DISPATCHER, assert_exit_status=True)
    assert status == -signal.SIGKILL


@pytest.mark.skipif(not _POSIX, reason="SIGKILL exit status is POSIX-only")
def test_a_process_that_was_not_killed_fails_the_case_as_a_harness_error(
    tmp_path: Path,
) -> None:
    """The exit-status check has to be able to fire, or it is decoration.

    This is the check that stands between "the case injected a crash" and "the
    case ran a process that finished normally and reported PASS". It is asserted
    here against a process that really did exit 0, because a check nobody has
    ever seen fail is indistinguishable from one that cannot.
    """

    case = conformance.synthetic_case(
        case_id="protocol-not-killed", role=contract.ROLE_DISPATCHER, arms={}
    )
    with _controller(tmp_path, case) as controller:
        controller.bootstrap()
        controller.spawn(contract.ROLE_DISPATCHER, armed=())
        controller.run_to_completion(contract.ROLE_DISPATCHER)
        with pytest.raises(ContractViolation, match="not -SIGKILL"):
            controller.kill(contract.ROLE_DISPATCHER, assert_exit_status=True)


# ---------------------------------------------------------------------------
# the linear-history reader
# ---------------------------------------------------------------------------

def test_the_epoch_regression_reader_ignores_refusals_and_reads_insertion_order() -> None:
    """A refusal is evidence the fence held, not evidence that it did not."""

    history = [
        {"status": "applied", "writer_epoch": 1},
        {"status": "refused", "writer_epoch": 1},
        {"status": "applied", "writer_epoch": 2},
    ]
    assert epoch_regressions(history) == []

    interleaved = [
        {"status": "applied", "writer_epoch": 2},
        {"status": "applied", "writer_epoch": 1},
    ]
    assert len(epoch_regressions(interleaved)) == 1
