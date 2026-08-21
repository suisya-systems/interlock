"""Collection-time policy for the harness: lanes, profiles, budgets, seeds.

Design sections 8.1 (lanes) and 9 (the CI budget). Two things happen here and
nowhere else:

* **What runs where is enumerable, never silent.** A case declares its lane in
  the manifest; a lane that cannot run on this OS produces a *skip with the
  lane named*, so "what did not run" is readable off the report.
* **The budgets are mechanical.** The per-case and suite watchdogs carry the
  profile's numbers, and a manifest whose case count exceeds the profile bound
  fails collection -- so growth in I-11's matrix forces an explicit budget diff
  instead of silent CI creep.

Markers are registered here rather than in ``pyproject.toml`` deliberately: the
repository has no pytest configuration section, and adding one changes the
behaviour of all nine CI jobs for the sake of this package.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from tests.fault_injection import manifest as manifest_module
from tests.fault_injection.spike_driver import SPIKE_ADAPTER

#: The environment variable a re-run supplies the suite seed through (design
#: 4.4). One suite seed per run; from CI it is fixed and recorded in the run
#: header, and a local run may pass any value.
SUITE_SEED_ENV = "S9_SUITE_SEED"
PROFILE_ENV = "S9_PROFILE"

#: A fixed default rather than a random one: an unreproducible default seed
#: would make every red build a new investigation.
DEFAULT_SUITE_SEED = 20_260_820


#: Wall-clock seconds spent inside this package's own tests, accumulated by the
#: report hook below.
_SPENT_S = {"total": 0.0}

_PACKAGE = "tests/fault_injection"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "fault_injection: a case from the S9 manifest (Issue #15)"
    )
    config.addinivalue_line(
        "markers", "linux_lane: needs the Linux conformance lane (design 8.1)"
    )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Charge only this package's time to this package's budget.

    The suite budget (design 9) is a budget for the fault-injection suite. CI
    runs the whole repository in one session, so measuring from the first case to
    session teardown would charge every unrelated test -- and every slow
    subprocess test that happens to sort after this package -- to the S9 number,
    turning an unrelated slowdown into a red S9 build.
    """

    if report.nodeid.startswith(_PACKAGE):
        _SPENT_S["total"] += float(getattr(report, "duration", 0.0) or 0.0)


def suite_seed() -> int:
    raw = os.environ.get(SUITE_SEED_ENV)
    return int(raw) if raw else DEFAULT_SUITE_SEED


def profile_name() -> str:
    return os.environ.get(PROFILE_ENV, "fast")


def active_lanes() -> tuple[str, ...]:
    """The portable lane runs everywhere; the conformance lane is Linux only.

    macOS *would* run the signal cases, and deliberately does not: keeping the
    conformance claim single-lane means a macOS scheduler flake can never block
    the gate (design 8.1).
    """

    if sys.platform.startswith("linux"):
        return (manifest_module.LANE_PORTABLE, manifest_module.LANE_LINUX)
    return (manifest_module.LANE_PORTABLE,)


@pytest.fixture(scope="session")
def manifest() -> Mapping[str, Any]:
    return manifest_module.load_manifest()


@pytest.fixture(scope="session")
def adapter() -> Any:
    """The default adapter. A case routes itself via :func:`adapter_for`."""

    return SPIKE_ADAPTER


def adapter_for(case: Mapping[str, Any]) -> Any:
    """Resolve the adapter a manifest case declares.

    Routing is manifest data (the ``adapter`` key, validated at collection);
    resolving it is policy, and it lives here so the durable half never
    imports an implementation module (``test_import_graph``).
    """

    from tests.fault_injection.session_driver import SESSION_ADAPTER

    by_name = {SPIKE_ADAPTER.name: SPIKE_ADAPTER, SESSION_ADAPTER.name: SESSION_ADAPTER}
    return by_name[case["adapter"]]


@pytest.fixture(scope="session")
def profile(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    name = profile_name()
    if name not in manifest["profiles"]:
        raise pytest.UsageError(
            f"{PROFILE_ENV}={name!r} is not a manifest profile; "
            f"choose one of {sorted(manifest['profiles'])}"
        )
    return dict(manifest["profiles"][name], name=name)


@pytest.fixture(scope="session", autouse=True)
def _suite_budget(profile: Mapping[str, Any]) -> Any:
    """The outermost of the three budgets (design 9).

    It is a budget check and not a hang detector, deliberately: a hang is caught
    by the per-barrier and per-case deadlines inside the controller, which run on
    host monotonic time and convert a wedged case into an attributable failure
    with its trace attached. This one exists so that *growth* -- a matrix that
    creeps past its runtime allowance without ever hanging -- becomes an explicit
    budget diff.
    """

    yield
    elapsed = _SPENT_S["total"]
    budget = float(profile["suite_timeout_s"])
    if elapsed > budget:
        pytest.fail(
            f"the S9 {profile['name']} profile spent {elapsed:.0f}s in "
            f"{_PACKAGE}, over its {budget:.0f}s budget (design 9): prune the "
            "matrix or raise the budget in an explicit diff"
        )


def profile_selected_cases() -> list[dict]:
    """Every case this profile declares, on every lane.

    Lane selection is deliberately **not** applied here. Design section 8.1 asks
    for a ``pytest`` skip elsewhere, "so what does not run on an OS is
    enumerable, never silent" -- and a case filtered out before parametrisation
    produces no test id at all, which reads in a report exactly like a case that
    passed. Every profile case therefore becomes a test item; the ones this OS
    cannot run skip with their lane named.
    """

    loaded = manifest_module.load_manifest()
    name = profile_name()
    return [case for case in loaded["cases"] if name in case["profiles"]]


def lane_skip_reason(case: Mapping[str, Any]) -> str | None:
    """Why this OS does not run ``case``, or ``None`` if it does."""

    lanes = active_lanes()
    if case["lane"] in lanes:
        return None
    return (
        f"case is on the {case['lane']} lane; this host ({sys.platform}) runs "
        f"{'/'.join(lanes)}. macOS would run the signal cases and deliberately "
        "does not: a single-lane conformance claim means a macOS scheduler "
        "flake can never block the gate (design 8.1)"
    )
