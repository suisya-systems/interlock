"""The control plane doing its job with a provider actually in the loop.

``test_suite_runs_unchanged.py`` proves the suite does not *need* a provider.
That on its own is also what a control plane that never talks to one would look
like, so this file drives the other direction: real sessions started by a real
provider, their readouts bound into S5's source of truth through the adapter in
:mod:`tests.gate_item11.substitution`, and the lease and outbox run over the
result.

Two properties are worth the round trip, and both are the ones the C1 to C2
switch put in question:

* the binding S5 keeps is the provider's own words, carried uninterpreted --
  including R4's *could not observe*, which is a readout and not an error; and
* the exclusion is **ours**. The provider is happy to start a second child for a
  run that already has one (U27, U32: it offers no exclusion at all), and the
  database is what refuses the second binding.

Parameterised over :data:`tests.gate_item11.registry.PROVIDERS`, so each of
these is re-measured against every provider that ships -- the same shape as the
suite run, and the same one-line cost when S2 lands.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from claude_org_runtime.control_plane.destination import KeyedDropbox
from claude_org_runtime.control_plane.handlers import NOTIFY_RECIPIENT, spike_registry
from claude_org_runtime.control_plane.lease import acquire, effect_kind, write_history
from claude_org_runtime.control_plane.outbox import Outbox
from claude_org_runtime.control_plane.schema import create_control_plane, reconstruct
from claude_org_runtime.session.provider import Observation, StartRequest

from . import registry
from .substitution import BIND_EFFECT, bind_session, release_session, unwrap

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant, as S5's suite uses
TTL_MS = 30_000
RUN_ID = "run-1"
RESOURCE = "sessions-of-run-1"
HOLDER = "item11-writer"

#: Long enough that a read straight after the spawn always lands in the window
#: before the child has reported. Seconds, and never waited for.
NEVER_ANNOUNCES = 3600


@pytest.fixture(params=sorted(registry.PROVIDERS), ids=lambda key: key)
def entry(request) -> registry.ProviderEntry:
    return registry.PROVIDERS[request.param]


@pytest.fixture
def provider(entry: registry.ProviderEntry, tmp_path: Path):
    """A live provider whose children are stopped whatever the test does."""

    instance = entry.factory(tmp_path / "state")
    instance.require_spawnable()
    try:
        yield instance
    finally:
        for readout in unwrap(instance.list_sessions(), "list_sessions"):
            instance.stop(readout.session_id)


@pytest.fixture
def cp(tmp_path: Path):
    connection = create_control_plane(tmp_path / "control-plane.sqlite3")
    try:
        connection.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
            " VALUES (?, 'running', ?, ?)",
            (RUN_ID, T0, T0),
        )
        connection.commit()
        yield connection
    finally:
        connection.close()


@pytest.fixture
def lease(cp):
    return acquire(cp, resource=RESOURCE, holder=HOLDER, now_ms=T0, ttl_ms=TTL_MS)


def _start(provider, tmp_path: Path, session_id: str, **settings):
    workspace = tmp_path / "workspaces" / session_id
    request = StartRequest(
        session_id=session_id,
        workspace=str(workspace),
        role="worker",
        settings=settings,
    )
    return unwrap(provider.start(request), f"start({session_id})")


def _wait_until_observed(provider, session_id: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    readout = unwrap(provider.read_state(session_id), f"read_state({session_id})")
    while readout.observation is Observation.COULD_NOT_OBSERVE:
        assert time.monotonic() < deadline, f"child never reported: {readout}"
        time.sleep(0.02)
        readout = unwrap(provider.read_state(session_id), f"read_state({session_id})")
    return readout


def test_a_provider_readout_becomes_the_binding_item_2_reads(
    provider, entry, cp, lease, tmp_path
):
    """Start a session, bind it, and read it back by query alone (D-0001).

    The state word in the row is the child's own, uninterpreted: nothing between
    the provider and the database ranks it, maps it onto a fact state, or
    invents one when it is missing.
    """

    _start(provider, tmp_path, "sess-1")
    readout = _wait_until_observed(provider, "sess-1")
    bind_session(cp, lease, readout, run_id=RUN_ID, provider=entry.id, now_ms=T0)

    state = reconstruct(cp, now_ms=T0)
    assert [row["session_id"] for row in state.active_sessions] == ["sess-1"]
    bound = state.active_sessions[0]
    assert bound["run_id"] == RUN_ID
    assert bound["provider"] == entry.id
    assert bound["observation"] == "observed"
    assert bound["provider_state"] == readout.provider_state


def test_a_session_that_cannot_be_observed_binds_as_itself_and_not_as_nothing(
    provider, entry, cp, lease, tmp_path
):
    """R4, end to end: could-not-observe reaches the database with its reason.

    This is the case the v1 reader collapsed into an empty result. S5's CHECK
    refuses a row that carries neither a state word nor a reason, so a collapse
    anywhere along this path is a refused INSERT rather than a quiet zero.
    """

    readout = _start(provider, tmp_path, "sess-quiet", announce_after=NEVER_ANNOUNCES)
    assert readout.observation is Observation.COULD_NOT_OBSERVE

    bind_session(cp, lease, readout, run_id=RUN_ID, provider=entry.id, now_ms=T0)

    bound = reconstruct(cp, now_ms=T0).active_sessions[0]
    assert bound["observation"] == "unobserved"
    assert bound["provider_state"] is None
    assert bound["observation_reason"] == readout.could_not_observe_reason
    assert bound["observation_reason"].strip()


def test_the_second_live_session_is_refused_by_us_and_not_by_the_provider(
    provider, entry, cp, lease, tmp_path
):
    """The exclusion is ours (D-0024, U27, U32), shown rather than asserted.

    The provider starts the second child without complaint -- it has no notion
    of a run and no exclusion to offer -- and the partial unique index is what
    refuses the second active binding. This is the case ``ACCEPTANCE.md`` item 2
    turns on, and the reason no test in the control-plane suite may lean on a
    provider refusing a duplicate.
    """

    first = _wait_until_observed(
        provider, _start(provider, tmp_path, "sess-1").session_id
    )
    bind_session(cp, lease, first, run_id=RUN_ID, provider=entry.id, now_ms=T0)

    second = _start(provider, tmp_path, "sess-2")
    assert second.session_id == "sess-2", "the provider offered no exclusion of its own"

    with pytest.raises(sqlite3.IntegrityError):
        bind_session(cp, lease, second, run_id=RUN_ID, provider=entry.id, now_ms=T0 + 1)

    assert [row["session_id"] for row in reconstruct(cp, now_ms=T0).active_sessions] == ["sess-1"]


def test_a_released_binding_frees_the_run_for_the_next_session(
    provider, entry, cp, lease, tmp_path
):
    """Stop and start again: one active binding throughout, two rows of history."""

    first = _wait_until_observed(
        provider, _start(provider, tmp_path, "sess-1").session_id
    )
    bind_session(cp, lease, first, run_id=RUN_ID, provider=entry.id, now_ms=T0)
    unwrap(provider.stop("sess-1"), "stop(sess-1)")
    release_session(cp, "sess-1", released_at_ms=T0 + 10)

    second = _wait_until_observed(
        provider, _start(provider, tmp_path, "sess-2").session_id
    )
    bind_session(cp, lease, second, run_id=RUN_ID, provider=entry.id, now_ms=T0 + 11)

    assert [row["session_id"] for row in reconstruct(cp, now_ms=T0 + 11).active_sessions] == ["sess-2"]
    bound = cp.execute("SELECT session_id FROM session ORDER BY bound_at_ms").fetchall()
    assert [row[0] for row in bound] == ["sess-1", "sess-2"]


def test_a_stale_holder_cannot_bind_a_session_it_started(
    provider, entry, cp, lease, tmp_path
):
    """Losing the lease is not softened by the provider having answered.

    The provider will happily start a session for a holder whose lease has been
    taken over -- it knows nothing about leases -- so the refusal has to come
    from the fence, in the same statement as the write.
    """

    from claude_org_runtime.control_plane.lease import StaleWriterRefused

    readout = _wait_until_observed(
        provider, _start(provider, tmp_path, "sess-1").session_id
    )
    acquire(cp, resource=RESOURCE, holder="another-writer", now_ms=T0 + TTL_MS + 1, ttl_ms=TTL_MS)

    with pytest.raises(StaleWriterRefused):
        bind_session(
            cp, lease, readout, run_id=RUN_ID, provider=entry.id, now_ms=T0 + TTL_MS + 2
        )
    assert reconstruct(cp, now_ms=T0 + TTL_MS + 2).active_sessions == ()

    # The refusal is the durable evidence. ``session`` carries no
    # ``writer_epoch`` of its own -- S5 gives it a partial unique index instead
    # -- so a binding that landed leaves no row in the write history, and the
    # one that was rejected does: stamped with the epoch that was presented.
    history = write_history(cp, kind=effect_kind(RESOURCE, BIND_EFFECT))
    assert [row["status"] for row in history] == ["refused"]
    assert [row["writer_epoch"] for row in history] == [lease.epoch]


def test_an_effect_about_a_provider_session_stays_exactly_once_across_a_resend(
    provider, entry, cp, lease, tmp_path
):
    """The outbox half of the scope, with the provider's session as the subject.

    The evidence is the destination's own count, not our rows -- ``ACCEPTANCE.md``
    §2 refuses a case that proves exactly-once from the sender's side alone.
    """

    readout = _wait_until_observed(
        provider, _start(provider, tmp_path, "sess-1").session_id
    )
    bind_session(cp, lease, readout, run_id=RUN_ID, provider=entry.id, now_ms=T0)

    dropbox = KeyedDropbox(tmp_path / "destination", name="item11-dropbox")
    outbox = Outbox(
        cp, resource=RESOURCE, holder=HOLDER, registry=spike_registry(dropbox)
    )
    outbox.enqueue(
        message_id="msg-1",
        recipient=NOTIFY_RECIPIENT,
        payload=f'{{"session":"{readout.session_id}"}}',
        dedup_key=f"session-bound:{readout.session_id}",
        now_ms=T0,
        epoch=lease.epoch,
        run_id=RUN_ID,
    )

    first = outbox.attempt("msg-1", now_ms=T0 + 1, epoch=lease.epoch)
    again = outbox.attempt("msg-1", now_ms=T0 + 2, epoch=lease.epoch)

    assert first.deduplicated is False
    assert again.deduplicated is True
    assert dropbox.effect_count(first.idempotency_key) == 1
    assert outbox.record_ack("msg-1", now_ms=T0 + 3).recorded is True
