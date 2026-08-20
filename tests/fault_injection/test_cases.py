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
    Controller,
    assert_invariants,
    execute_case,
)

_CASES = policy.selected_cases()
_SKIPPED = policy.skipped_cases()


def _timeout(case: Mapping[str, Any], profile: Mapping[str, Any]) -> float:
    combination = len(case["targets"]) > 1 or case["fault"] == "staggered-sigkill"
    key = "combination_case_timeout_s" if combination else "per_case_timeout_s"
    return float(profile[key])


@pytest.mark.fault_injection
@pytest.mark.parametrize("case", _CASES, ids=[case["case_id"] for case in _CASES])
def test_manifest_case(
    case: Mapping[str, Any],
    tmp_path: Path,
    adapter: Any,
    profile: Mapping[str, Any],
) -> None:
    """Run one manifest case and assert exactly what it declared.

    Re-run a single failure with ``pytest tests/fault_injection -k <case_id>``
    and the suite seed from the ``S9-REPRO`` line the failure prints.
    """

    with Controller(
        workdir=tmp_path / "case",
        adapter=adapter,
        case=case,
        suite_seed=policy.suite_seed(),
        barrier_timeout_s=float(profile["barrier_timeout_s"]),
        case_timeout_s=_timeout(case, profile),
    ) as controller:
        outcome = execute_case(controller, case)
        assert_invariants(
            controller, case, resolved_skew_ms=outcome["resolved_skew_ms"]
        )


def test_the_cases_this_os_skips_are_enumerable() -> None:
    """What does not run on an OS is listed, never silent (design 8.1).

    The assertion is not that the list is empty -- on macOS and Windows it is
    not -- but that every skipped case names the lane that excluded it, so a
    gate reader can tell "did not run here" from "passed".
    """

    for case in _SKIPPED:
        assert case["lane"] not in policy.active_lanes(), case["case_id"]
        assert case["lane"] == "linux", (
            f"{case['case_id']} was skipped for a reason other than the lane"
        )
