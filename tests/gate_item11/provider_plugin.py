"""Bind a live ``SessionProvider`` under a pytest run, and change nothing else.

This is the "provider fixture" issue ``#20``'s fourth acceptance criterion
names. Loaded with ``-p tests.gate_item11.provider_plugin``, it starts a real
session on a real provider before collection and stops it after the last test,
so that the control-plane suite runs *while a session backend is live* rather
than merely in a process where one could have been.

It is deliberately inert towards the suite: it registers no autouse fixture,
patches nothing, and sets no environment variable the suite reads. That is what
makes the comparison in ``test_suite_runs_unchanged.py`` mean something -- if
the plugin had to reach into the suite to make it run, the reaching would be the
modification the gate forbids.

Failures here are **fail-closed** (D-0010): a provider that cannot be probed or
cannot start a session aborts the run. Continuing would produce a green suite
that measured nothing, which is the one outcome worse than a red one.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pytest

from claude_org_runtime.session.provider import (
    CapabilityReport,
    Observation,
    SessionProvider,
    SessionReadout,
    StartRequest,
)

from . import registry
from .substitution import drive_once, unwrap

#: Names the registry entry to bind. Absent means :data:`registry.DEFAULT_PROVIDER`.
PROVIDER_ENV = "INTERLOCK_ITEM11_PROVIDER"

#: The role and session the binding is started for. Both are arbitrary and
#: neither reaches the suite; they exist so the readout is of a session that was
#: really asked for something, not of a placeholder.
BOUND_ROLE = "worker"
BOUND_SESSION_ID = "item11-bound-session"

#: How long :func:`bind` waits for the started session to become observable.
#: Bounded and never fatal: a session that is alive and has not reported yet is
#: a legal readout (R4), so the wait improves the evidence in the header without
#: turning a tolerated state into a failed run.
OBSERVE_TIMEOUT_SECONDS = 10.0


@dataclass
class BoundProvider:
    """The provider bound for this run, and what it reported."""

    entry: "registry.ProviderEntry"
    provider: SessionProvider
    capabilities: CapabilityReport
    readouts: list[SessionReadout] = field(default_factory=list)
    root: Path | None = None
    #: What the qualifying round trip did, for the run header. Empty only while
    #: the binding is still being built.
    drove: str = ""


#: The binding for the current run, or ``None`` when the plugin is not loaded.
#: A module global rather than a fixture: the point is that it is reachable
#: *without* the suite asking for it, exactly as a real session backend would be.
BOUND: BoundProvider | None = None


def _selected() -> "registry.ProviderEntry":
    name = os.environ.get(PROVIDER_ENV) or registry.DEFAULT_PROVIDER
    try:
        return registry.PROVIDERS[name]
    except KeyError:
        raise pytest.UsageError(
            f"{PROVIDER_ENV}={name!r} names no provider; known providers are "
            f"{sorted(registry.PROVIDERS)}"
        ) from None


def bind(entry: "registry.ProviderEntry", root: Path) -> BoundProvider:
    """Probe, start one session, and return what the provider said.

    Split out of :func:`pytest_configure` so a test can bind a provider without
    a subprocess and get the same object the plugin would have built.
    """

    provider = entry.factory(root / "state")
    capabilities = provider.require_spawnable()
    readout = unwrap(
        provider.start(
            StartRequest(
                session_id=BOUND_SESSION_ID,
                workspace=str(root / "workspace"),
                role=BOUND_ROLE,
            )
        ),
        f"{entry.id}.start",
    )
    observed = _wait_for_report(provider, readout, entry)
    # Qualify the provider against the control plane *before* the suite runs.
    # Without this the binding would only prove that a child can be started
    # alongside the suite, and a provider the control plane could not use would
    # produce exactly the same green run. Raising here aborts collection, which
    # is the fail-closed answer (D-0010): a run that measured nothing is worse
    # than a red one.
    drove = drive_once(provider, observed, provider_id=entry.id, root=root)
    return BoundProvider(
        entry=entry,
        provider=provider,
        capabilities=capabilities,
        readouts=[observed],
        root=root,
        drove=drove,
    )


def _wait_for_report(
    provider: SessionProvider, readout: SessionReadout, entry: "registry.ProviderEntry"
) -> SessionReadout:
    """Poll until the session reports a state, or return the last readout.

    The header this feeds is the run's evidence that a backend was live; a
    readout taken in the instant after the spawn says only that a start was
    asked for. Giving up quietly is correct: could-not-observe is a state the
    system is required to tolerate (D-0006), not a reason to abort.
    """

    deadline = time.monotonic() + OBSERVE_TIMEOUT_SECONDS
    while readout.observation is Observation.COULD_NOT_OBSERVE:
        if time.monotonic() >= deadline:
            return readout
        time.sleep(0.02)
        readout = unwrap(
            provider.read_state(readout.session_id), f"{entry.id}.read_state"
        )
    return readout


def pytest_configure(config: pytest.Config) -> None:
    global BOUND
    entry = _selected()
    root = Path(tempfile.mkdtemp(prefix="interlock-item11-"))
    try:
        BOUND = bind(entry, root)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def pytest_report_header(config: pytest.Config) -> Sequence[str]:
    """Say which provider the run was bound to, in the log CI keeps.

    Evidence, not decoration: a run whose header does not name a provider did
    not measure item 11, and the header is where that is visible without
    re-reading the workflow file.
    """

    if BOUND is None:  # pragma: no cover -- configure failed; pytest reports that
        return []
    state = ", ".join(
        f"{r.session_id}={r.provider_state or r.could_not_observe_reason}"
        for r in BOUND.readouts
    )
    return [
        f"gate item 11: control-plane suite bound to {BOUND.entry.scaffold} "
        f"({BOUND.entry.issue}), version {BOUND.capabilities.provider_version}, "
        f"live session {state}",
        f"gate item 11: the provider drove the control plane -- {BOUND.drove}",
    ]


def pytest_unconfigure(config: pytest.Config) -> None:
    global BOUND
    bound, BOUND = BOUND, None
    if bound is None:
        return
    try:
        for readout in bound.readouts:
            bound.provider.stop(readout.session_id)
    finally:
        if bound.root is not None:
            shutil.rmtree(bound.root, ignore_errors=True)


@pytest.fixture
def bound_provider() -> BoundProvider:
    """The live binding, for the few tests that assert *about* it."""

    assert BOUND is not None, "the provider plugin is not loaded"
    return BOUND
