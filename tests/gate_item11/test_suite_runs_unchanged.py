"""Item 11, measured: the control-plane suite, run twice, differing only in the
provider fixture.

``ACCEPTANCE.md`` §1 item 11 and issue ``#20``:

    Even if the provider does not hold, only the ``SessionProvider`` need be
    swapped -- **demonstrated, not argued**. Zero test modifications required.

The demonstration is two subprocess runs of the *same* suite: one plain, one
with :mod:`tests.gate_item11.provider_plugin` binding a live provider. Both are
recorded by :mod:`tests.gate_item11.outcome_recorder`, and the assertions below
compare collected ids, per-phase outcomes and the digests of the files each run
read. Anything that had to change for the bound run to pass shows up as a
difference in one of the three.

**Why subprocesses.** A provider bound inside this process would be bound after
the control-plane suite had already been collected and run by the same session,
so "the suite ran while a backend was live" would be a claim about ordering
rather than a fact about the run. Two processes make it a fact, and make the
bound one the literal command CI runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from . import registry
from .outcome_recorder import REPORT_ENV
from .provider_plugin import PROVIDER_ENV

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The control-plane suite, named exactly once so that both runs cannot drift
#: apart by argument. Issue ``#20``'s scope: the SQLite source of truth, the
#: lease, and outbox delivery / ack / dedup.
CONTROL_PLANE_SUITE = ("tests/control_plane",)

#: Flags shared by both runs. ``no:cacheprovider`` keeps a run from writing a
#: ``.pytest_cache`` that the next one would read -- two runs that share state
#: are not two independent measurements.
COMMON_ARGS = ("-p", "no:cacheprovider", "--tb=short")

RUN_TIMEOUT_SECONDS = 900


def _run(tmp_path: Path, *, provider: str | None) -> dict:
    label = provider or "unbound"
    report = tmp_path / f"{label}.json"
    argv = [
        sys.executable,
        "-m",
        "pytest",
        *CONTROL_PLANE_SUITE,
        *COMMON_ARGS,
        "-p",
        "tests.gate_item11.outcome_recorder",
    ]
    if provider is not None:
        argv += ["-p", "tests.gate_item11.provider_plugin"]

    environment = dict(os.environ)
    environment[REPORT_ENV] = str(report)
    environment.pop(PROVIDER_ENV, None)
    if provider is not None:
        environment[PROVIDER_ENV] = provider
    # ``src`` because the package is a src-layout one and this must not depend
    # on an install; the repository root because ``-p tests.gate_item11...``
    # names an importable module.
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    completed = subprocess.run(
        argv,
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
    )
    assert report.exists(), (
        f"the {label} run wrote no report; pytest exited {completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    recorded = json.loads(report.read_text(encoding="utf-8"))
    recorded["provider"] = provider
    recorded["stdout"] = completed.stdout
    recorded["stderr"] = completed.stderr
    recorded["returncode"] = completed.returncode
    return recorded


@pytest.fixture(scope="module")
def unbound_run(tmp_path_factory) -> dict:
    """The suite as it stands, with no session backend anywhere near it."""

    return _run(tmp_path_factory.mktemp("item11-unbound"), provider=None)


@pytest.fixture(scope="module", params=sorted(registry.PROVIDERS), ids=lambda key: key)
def bound_run(request, tmp_path_factory) -> dict:
    """The same suite, with one registered provider live for its whole duration.

    Parameterised over the whole registry rather than over
    :data:`registry.DEFAULT_PROVIDER`, so that adding S2 (issue ``#17``) buys the
    unchanged-suite run as well as the substitution scenarios. A registry entry
    that only bought the scenarios would leave the measurement item 11 is
    actually about -- *this* suite, unmodified, against *that* provider -- still
    covering S3 alone.
    """

    provider = request.param
    return _run(tmp_path_factory.mktemp(f"item11-{provider}"), provider=provider)


def _failed(run: dict) -> dict[str, dict[str, str]]:
    return {
        nodeid: phases
        for nodeid, phases in run["outcomes"].items()
        if any(outcome == "failed" for outcome in phases.values())
    }


def test_the_suite_passes_with_a_provider_bound(bound_run):
    """The exit condition, literally: zero failures, nothing skipped away.

    A skip would satisfy "did not fail" while measuring nothing, and issue
    ``#20`` rules out annotating, skipping or expected-failing a test that does
    not survive the substitution -- so a skipped test is checked for here rather
    than allowed to hide inside a green run.
    """

    assert _failed(bound_run) == {}
    assert bound_run["returncode"] == 0, bound_run["stdout"]
    skipped = sorted(
        nodeid
        for nodeid, phases in bound_run["outcomes"].items()
        if "skipped" in phases.values()
    )
    assert skipped == [], (
        f"{skipped} was skipped under the bound provider; a test that cannot run "
        "against a provider is a leak to fix, not one to skip"
    )


def test_the_bound_run_collects_exactly_the_same_tests(unbound_run, bound_run):
    """Same ids, not merely the same count.

    A run that lost one test and gained another keeps its total, and a
    substitution that quietly deselected the cases a provider would have
    disturbed is the failure this comparison exists to catch.
    """

    assert set(bound_run["outcomes"]) == set(unbound_run["outcomes"])
    assert unbound_run["outcomes"], "the unbound run collected nothing"


def test_every_test_reaches_the_same_verdict_either_way(unbound_run, bound_run):
    """Per phase, not per test: setup and teardown are where a fixture would show.

    A test whose call still passes because its setup started being skipped is a
    modification the totals would not show.
    """

    assert bound_run["outcomes"] == unbound_run["outcomes"]


def test_both_runs_read_the_same_suite_artifact(unbound_run, bound_run):
    """Issue ``#20``'s fourth criterion: the same artifact, differing only in fixture.

    Evidenced by digest rather than by the two runs having been given the same
    path -- a path says which file was asked for, a digest says which bytes ran.
    """

    assert bound_run["artifact"] == unbound_run["artifact"]
    assert bound_run["artifact"], "no suite file was recorded, so nothing was compared"


def test_the_bound_run_really_had_a_provider_live(bound_run):
    """Otherwise both runs are the unbound one and every comparison above is vacuous.

    The header the plugin prints carries the provider's own version string, so
    this also fails if the binding degraded into something that reported nothing.
    """

    entry = registry.PROVIDERS[bound_run["provider"]]
    assert "gate item 11: control-plane suite bound to" in bound_run["stdout"]
    assert entry.scaffold in bound_run["stdout"]
    assert "live session" in bound_run["stdout"]


def test_the_unbound_run_had_no_provider(unbound_run):
    """The control half of the comparison, checked rather than assumed."""

    assert "gate item 11: control-plane suite bound to" not in unbound_run["stdout"]
