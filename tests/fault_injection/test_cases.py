"""The cases: every manifest entry, executed and asserted by name.

This module is deliberately thin. A case's meaning lives in the manifest and its
assertions live in ``controller.assert_invariants``, keyed by the contract's
invariant names -- so when the S5-S7 implementations are discarded (D-0026) the
adapter is re-bound and nothing here is rewritten.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from tests.fault_injection import conftest as policy
from tests.fault_injection.controller import (
    CaseFailure,
    Controller,
    assert_invariants,
    execute_case,
    repro_line,
)

_CASES = policy.profile_selected_cases()


def _timeout(case: Mapping[str, Any], profile: Mapping[str, Any]) -> float:
    combination = len(case["targets"]) > 1 or case["fault"] == "staggered-sigkill"
    key = "combination_case_timeout_s" if combination else "per_case_timeout_s"
    return float(profile[key])


@pytest.mark.fault_injection
@pytest.mark.parametrize("case", _CASES, ids=[case["case_id"] for case in _CASES])
def test_manifest_case(
    case: Mapping[str, Any],
    tmp_path: Path,
    profile: Mapping[str, Any],
) -> None:
    """Run one manifest case and assert exactly what it declared.

    Every failure -- an invariant, a barrier that was never reached, a case that
    outran its budget, a role that exited some way other than by the signal --
    carries the ``S9-REPRO`` line and the ``S9-RERUN`` command that reproduces
    exactly this case. The harness-fault paths need it most: they are the ones
    that happen on a runner nobody has a shell on.
    """

    reason = policy.lane_skip_reason(case)
    if reason is not None:
        pytest.skip(reason)

    with Controller(
        workdir=tmp_path / "case",
        adapter=policy.adapter_for(case),
        case=case,
        suite_seed=policy.suite_seed(),
        barrier_timeout_s=float(profile["barrier_timeout_s"]),
        case_timeout_s=_timeout(case, profile),
        profile=str(profile["name"]),
    ) as controller:
        try:
            outcome = execute_case(controller, case)
            assert_invariants(
                controller,
                case,
                resolved_skew_ms=outcome["resolved_skew_ms"],
                at_kill=outcome["at_kill"],
                unresolved_at_kill=outcome["unresolved_at_kill"],
            )
        except Exception as error:
            # Only ``Exception``: a skip, a keyboard interrupt or pytest's own
            # control-flow exceptions must pass through untouched.
            #
            # And a *new* exception chained from the original rather than the
            # original's type re-instantiated with a longer message: several
            # exceptions the harness can raise have structured constructors
            # (``subprocess.TimeoutExpired`` takes cmd and timeout), so
            # rebuilding them from a string raises ``TypeError`` and replaces
            # the real failure with a failure about reporting the failure. The
            # cause is chained, so the original traceback is still what the
            # reader sees first.
            line = repro_line(
                case_id=case["case_id"],
                suite_seed=policy.suite_seed(),
                manifest_version=case["manifest_version"],
                profile=str(profile["name"]),
            )
            if line.splitlines()[0] in str(error):
                raise
            raise CaseFailure(f"{type(error).__name__}: {error}\n{line}") from error


def test_what_this_os_does_not_run_is_enumerable() -> None:
    """What does not run on an OS is listed, never silent (design 8.1).

    Every profile case is collected as a test item on every OS. The ones this
    host cannot run skip with the lane named in the reason, so a gate reader can
    tell "did not run here" from "passed" by reading the report alone. This test
    asserts the *rule*: nothing is ever excluded for a reason other than a lane.
    """

    for case in _CASES:
        reason = policy.lane_skip_reason(case)
        if reason is None:
            continue
        assert case["lane"] == "linux", (
            f"{case['case_id']} is skipped for a reason other than its lane"
        )
        assert case["lane"] in reason and "design 8.1" in reason
