"""The conformance battery, run against every adapter this build ships.

Today that is one adapter (the S6/S7 spike driver). When I-12 and I-14 land,
their adapters are added to ``ADAPTERS`` and everything below runs against them
unchanged -- which is the point: an adapter that has not passed the battery
cannot contribute matrix results (design 6.3).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from tests.fault_injection import conformance, contract
from tests.fault_injection.spike_driver import SPIKE_ADAPTER

ADAPTERS = (SPIKE_ADAPTER,)
_ADAPTER_IDS = [adapter.driver_module.rsplit(".", 1)[-1] for adapter in ADAPTERS]

_LINUX = sys.platform.startswith("linux")


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
@pytest.mark.parametrize("role", contract.ROLES)
@pytest.mark.parametrize("checkpoint", contract.CHECKPOINTS)
def test_every_checkpoint_is_reachable_and_blocks(
    adapter: Any, role: str, checkpoint: str, tmp_path: Path
) -> None:
    """All four windows, for all three roles.

    Gate item 4 requires all three ACCEPTANCE.md section 2 kill windows for each
    of the three components, and the two mid-call windows exist only on a
    record -> effect -> result path. That is why the Supervisor and Secretary
    scripts each carry one externally-effecting action: without it, I-11 could
    arm a required (role, window) pair and hit a manifest-validation dead end.
    """

    conformance.check_checkpoint_blocks(
        adapter,
        tmp_path,
        role=role,
        operation=contract.OPERATION_ATTEMPT,
        checkpoint=checkpoint,
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
@pytest.mark.parametrize(
    "operation",
    [
        contract.OPERATION_LEASE_ACQUIRE,
        contract.OPERATION_LEASE_RENEW,
        contract.OPERATION_ENQUEUE,
        contract.OPERATION_ACK,
    ],
)
def test_the_non_delivery_operations_expose_their_windows(
    adapter: Any, operation: str, tmp_path: Path
) -> None:
    """A barrier the applicability matrix advertises can actually be reached."""

    conformance.check_checkpoint_blocks(
        adapter,
        tmp_path,
        role=contract.ROLE_DISPATCHER,
        operation=operation,
        checkpoint=contract.CHECKPOINT_BEFORE_DURABLE_WRITE,
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
def test_the_barrier_round_trip_releases_the_process(adapter: Any, tmp_path: Path) -> None:
    conformance.check_barrier_round_trip(
        adapter, tmp_path, role=contract.ROLE_DISPATCHER
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
@pytest.mark.parametrize("checkpoint", contract.CHECKPOINTS)
def test_a_kill_at_each_window_is_a_signal_and_leaves_a_readable_database(
    adapter: Any, checkpoint: str, tmp_path: Path
) -> None:
    """The exit-status half of the assertion is lane-conditional (design 8.1)."""

    conformance.check_sigkill_exit_status(
        adapter,
        tmp_path,
        role=contract.ROLE_DISPATCHER,
        checkpoint=checkpoint,
        assert_exit_status=_LINUX,
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
def test_the_restart_entrypoint_recovers_and_is_idempotent(
    adapter: Any, tmp_path: Path
) -> None:
    conformance.check_restart_is_idempotent(
        adapter, tmp_path, role=contract.ROLE_DISPATCHER
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
def test_the_injected_clock_is_honoured(adapter: Any, tmp_path: Path) -> None:
    conformance.check_clock_is_injected(
        adapter, tmp_path, role=contract.ROLE_SUPERVISOR
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
def test_the_driver_never_reads_the_host_clock(adapter: Any) -> None:
    conformance.check_no_host_clock(adapter)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
def test_one_case_and_one_seed_give_identical_traces(adapter: Any, tmp_path: Path) -> None:
    conformance.check_identical_traces(
        adapter, tmp_path, role=contract.ROLE_SECRETARY
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
def test_the_checkpoint_vocabulary_is_the_contracts(adapter: Any) -> None:
    conformance.check_vocabulary_matches(adapter)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
def test_the_driver_accepts_the_contract_cli(adapter: Any) -> None:
    conformance.check_driver_cli(adapter)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ADAPTER_IDS)
def test_the_invariant_queries_bind_the_contract_parameters(adapter: Any) -> None:
    conformance.check_invariant_queries_bind_the_contract_parameters(adapter)
