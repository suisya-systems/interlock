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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "fault_injection: a case from the S9 manifest (Issue #15)"
    )
    config.addinivalue_line(
        "markers", "linux_lane: needs the Linux conformance lane (design 8.1)"
    )


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
    """The one adapter S9 ships. I-12/I-14 add theirs beside it (design 2.2)."""

    return SPIKE_ADAPTER


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
def _suite_watchdog(profile: Mapping[str, Any]) -> Any:
    """The outermost of the three watchdogs (design 8.2), on monotonic time."""

    started = time.monotonic()
    yield
    elapsed = time.monotonic() - started
    budget = float(profile["suite_timeout_s"])
    if elapsed > budget:
        pytest.fail(
            f"the S9 {profile['name']} profile took {elapsed:.0f}s, over its "
            f"{budget:.0f}s budget (design 9): prune the matrix or raise the "
            "budget in an explicit diff"
        )


def selected_cases() -> list[dict]:
    """The cases this run executes, after profile and lane selection."""

    loaded = manifest_module.load_manifest()
    return manifest_module.profile_cases(
        loaded, profile=profile_name(), lanes=active_lanes()
    )


def skipped_cases() -> list[dict]:
    """Cases this OS cannot run, so what did not run stays enumerable."""

    loaded = manifest_module.load_manifest()
    lanes = active_lanes()
    return [
        case
        for case in loaded["cases"]
        if profile_name() in case["profiles"] and case["lane"] not in lanes
    ]
