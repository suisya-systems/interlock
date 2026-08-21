"""S8 -- delivery outcomes are unchanged under a deliberately stale readout.

``ACCEPTANCE.md`` item 6 words this case as "the UI attached but its session
state deliberately stale". Under C2 there is no UI, so the case is translated
rather than skipped (the issue is explicit about the difference): the stale
state is a **provider readout that is stale or wrong** -- a session id whose
child is gone, a ``read_state`` that answers "could not observe" -- and the
assertion is that the delivery sequence records *exactly the same facts* with
that staleness present as it does with no session backend in the process at
all. Not similar facts: equal ones, compared with ``==`` over the transcript
:func:`tests.messagebus._env.drop_then_resend_transcript` returns.

The provider driven stale here is the S3 stub (Issue ``#11``), on purpose:
item 6 is deliberately buildable against the stub alone, which is itself the
no-edge property demonstrated -- a bus that cannot name a provider cannot care
which one is rotting next to it.

Vocabulary confinement (see ``_env.py``): this file knows the session backend
and reaches the control plane only through the suite's fixtures, so no file in
this suite knows both vocabularies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_org_runtime.session.provider import Observation, Ok, StartRequest
from claude_org_runtime.session.stub_provider import LocalProcessSessionProvider

from ._env import drop_then_resend_transcript, expected_transcript


@pytest.fixture
def provider(tmp_path: Path) -> LocalProcessSessionProvider:
    prov = LocalProcessSessionProvider(tmp_path / "sessions")
    yield prov
    listed = prov.list_sessions()
    if isinstance(listed, Ok):
        for readout in listed.value:
            prov.stop(readout.session_id)


def _workspace(tmp_path: Path, name: str) -> str:
    return str(tmp_path / "workspaces" / name)


def test_delivery_is_unchanged_when_the_sessions_child_is_gone(
    bus_env_factory, provider, tmp_path
):
    """First staleness: a session id whose child no longer exists."""

    baseline = drop_then_resend_transcript(bus_env_factory("baseline-gone"))

    result = provider.start(
        StartRequest(
            session_id="worker-1",
            workspace=_workspace(tmp_path, "worker-1"),
            role="worker",
        )
    )
    assert isinstance(result, Ok)
    provider.stop("worker-1")
    readout = provider.read_state("worker-1")
    # The staleness is real, not assumed: the roster still answers for the
    # session id, and what it reports is a child that is gone.
    assert isinstance(readout, Ok)
    assert readout.value.provider_state is not None
    assert readout.value.provider_state.startswith("exited-")

    stale = drop_then_resend_transcript(bus_env_factory("child-gone"))
    assert stale == baseline == expected_transcript()


def test_delivery_is_unchanged_when_the_state_cannot_be_observed(
    bus_env_factory, provider, tmp_path
):
    """Second staleness: a state read that answers "could not observe"."""

    baseline = drop_then_resend_transcript(bus_env_factory("baseline-unobs"))

    result = provider.start(
        StartRequest(
            session_id="worker-2",
            workspace=_workspace(tmp_path, "worker-2"),
            role="worker",
            # The child announces its state only after this many seconds, so
            # every read below lands in the window where the session exists,
            # the child runs, and its state is unobservable.
            settings={"announce_after": 300},
        )
    )
    assert isinstance(result, Ok)
    readout = provider.read_state("worker-2")
    assert isinstance(readout, Ok)
    assert readout.value.observation is Observation.COULD_NOT_OBSERVE

    stale = drop_then_resend_transcript(bus_env_factory("could-not-observe"))
    assert stale == baseline == expected_transcript()

    # The staleness held for the whole delivery sequence, not just before it.
    after = provider.read_state("worker-2")
    assert isinstance(after, Ok)
    assert after.value.observation is Observation.COULD_NOT_OBSERVE
