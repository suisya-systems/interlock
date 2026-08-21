"""The session driver's own battery: the checks its conformance absence owes.

The #18 adapter is deliberately not in the fault harness's ``ADAPTERS`` tuple
-- the full battery presupposes a three-role delivery loop it does not have --
so the properties that battery would have pinned are pinned here instead, and
in the default profile (the four manifest cases themselves are full-profile
only, so without this file a fast run would exercise the driver not at all):

- every anchor the four cases arm is actually reachable and blocks;
- a real SIGKILL at each anchor, followed by a restart, ends in
  re-identification (the full ``execute_case`` + ``assert_invariants`` path,
  including the window-landing and destination checks);
- two runs of one case produce identical protocol traces (the determinism
  the re-run contract relies on).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.fault_injection import manifest as manifest_module
from tests.fault_injection.controller import Controller, assert_invariants, execute_case
from tests.fault_injection.session_driver import SESSION_ADAPTER

_LINUX = sys.platform.startswith("linux")

pytestmark = pytest.mark.skipif(
    not _LINUX, reason="the session-start cases are Linux-lane (real SIGKILL + /proc)"
)


def _session_cases() -> list[dict]:
    return [
        case
        for case in manifest_module.load_manifest()["cases"]
        if case["adapter"] == SESSION_ADAPTER.name
    ]


def _run_case(case: dict, workdir: Path) -> tuple[dict, list[dict]]:
    with Controller(
        workdir=workdir,
        adapter=SESSION_ADAPTER,
        case=case,
        suite_seed=1,
        barrier_timeout_s=20.0,
        case_timeout_s=60.0,
        profile="full",
    ) as controller:
        outcome = execute_case(controller, case)
        assert_invariants(
            controller,
            case,
            resolved_skew_ms=outcome["resolved_skew_ms"],
            at_kill=outcome["at_kill"],
            unresolved_at_kill=outcome["unresolved_at_kill"],
        )
        return outcome, controller.all_traces()


def test_the_manifest_carries_all_four_injection_points():
    anchors = sorted(case["checkpoint"] for case in _session_cases())
    assert anchors == sorted(
        [
            "before_durable_write",
            "after_record_before_effect",
            "after_effect_before_record",
            "identity-readback-committed",
        ]
    )


@pytest.mark.parametrize(
    "case", _session_cases(), ids=[case["case_id"] for case in _session_cases()]
)
def test_each_anchor_is_reachable_killed_and_recovered(case: dict, tmp_path: Path):
    """The whole path, per anchor: reach, block, SIGKILL, restart, re-identify.

    ``execute_case`` asserts the kill's exit status; ``assert_invariants``
    asserts exactly-one confirmed binding, the window-landing spawn count,
    and the destination reports -- so an anchor that stopped being reached,
    a kill that stopped landing, or a recovery that stopped confirming all
    fail here, in the default profile.
    """

    outcome, traces = _run_case(case, tmp_path / "case")
    generations = {entry["generation"] for entry in traces}
    assert generations == {0, 1}
    killed = [entry for entry in traces if entry["generation"] == 0]
    assert any(
        event.get("event") in ("checkpoint", "sync")
        for entry in killed
        for event in entry["trace"]
    ), "generation 0 never announced the armed anchor"


def test_two_runs_of_one_case_produce_identical_traces(tmp_path: Path):
    case = _session_cases()[0]
    _, first = _run_case(case, tmp_path / "one")
    _, second = _run_case(case, tmp_path / "two")
    assert first == second, "the driver's protocol trace is not deterministic"
