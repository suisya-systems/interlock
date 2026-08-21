"""Carried v1 delivery invariants, landed against the new contract (Q-0023 -> D-0028).

The quarantined ``tests/broker/`` suites pin invariants the ledger classified
``carry (invariant) / rewrite (mechanism)``, but the module they drive --
``broker/server.py`` -- is deleted, so none of them run. Per the operator's
2026-08-21 direction on Q-0023, a carried end-to-end assertion is landed as a
**specification against the new MessageBus contract**, not kept driving the
old module: this file is where those specifications live. Each test names the
quarantined assertion it carries. The full assertion-level disposition of the
quarantined files -- what carries here, what S7 already pins, what was
superseded, what drops with the pane or the HTTP transport -- is
``docs/messagebus-carry-drop.md``.

A carried specification the new contract does not satisfy yet is landed
**failing**, as ``xfail(strict=True)``: the suite stays green, the contract
gap stays visible, and the day an implementation satisfies it the XPASS turns
the mark into a loud reminder to remove it.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.control_plane.handlers import HUMAN_GATED_RECIPIENT
from claude_org_runtime.control_plane.outbox import HandlerRejected

from ._env import EPOCH, RECIPIENT, RUN_ID, T0


def send(env, *, message_id="task-1", recipient=RECIPIENT):
    return env.bus.send(
        message_id=message_id,
        recipient=recipient,
        payload='{"task":"t"}',
        dedup_key=f"dk-{message_id}",
        now_ms=T0,
        epoch=EPOCH,
        run_id=RUN_ID,
    )


def outbox_status(env, message_id):
    row = env.connection.execute(
        "SELECT status FROM outbox WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row[0] if row else None


def test_a_message_is_sent_only_to_a_registered_recipient(bus_env):
    """Carries tests/broker/test_store.py::test_enqueue_only_to_registered.

    The roster is the handler registry now, not the pane bind table, and the
    refusal happens before the durable write so no undeliverable row exists.
    """

    with pytest.raises(HandlerRejected):
        send(bus_env, recipient="never-registered")
    assert outbox_status(bus_env, "task-1") is None


def test_a_settled_message_is_never_presented_again(bus_env):
    """Carries tests/broker/test_store.py::test_drain_is_at_most_once and
    tests/broker/test_delivery.py::test_check_messages_drains_unclaimed.

    v1's drain removed the row from the queue on read; the new contract keeps
    the row and settles it with the ack instead -- at-most-once *presentation
    after settlement* is the transport-neutral invariant underneath both.
    """

    send(bus_env)
    bus_env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
    bus_env.bus.ack("task-1", now_ms=T0 + 2_000, recipient=RECIPIENT)
    for later in (3_000, 4_000):
        assert bus_env.bus.poll(RECIPIENT, now_ms=T0 + later, epoch=EPOCH) == ()


def test_pull_then_ack_walks_the_claim_then_confirm_states(bus_env):
    """Carries tests/broker/test_delivery.py::test_claim_then_confirm_lifecycle.

    The v1 state machine was pending -> CLAIMED -> confirmed, driven by a
    sidecar; the successor is pending -> delivered -> acked, driven by the
    recipient's own poll. Same shape, one honest difference: the middle state
    no longer expires back -- a delivered-but-unacked row simply stays due,
    which is the resend.
    """

    send(bus_env)
    assert outbox_status(bus_env, "task-1") == "pending"
    bus_env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
    assert outbox_status(bus_env, "task-1") == "delivered"
    outcome = bus_env.bus.ack("task-1", now_ms=T0 + 2_000, recipient=RECIPIENT)
    assert outcome.recorded
    assert outbox_status(bus_env, "task-1") == "acked"
    # And the double confirm stays idempotent, as v1's lifecycle test pinned.
    assert not bus_env.bus.ack(
        "task-1", now_ms=T0 + 3_000, recipient=RECIPIENT
    ).recorded


def test_a_poll_returns_only_the_polling_recipients_rows(bus_env):
    """Carries tests/broker/test_delivery.py::test_poll_claims_only_returns_owner_rows."""

    send(bus_env, message_id="task-mine")
    send(bus_env, message_id="task-gated", recipient=HUMAN_GATED_RECIPIENT)
    envelopes = bus_env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
    assert [e.message_id for e in envelopes] == ["task-mine"]
    assert outbox_status(bus_env, "task-gated") == "pending"


def test_an_ack_from_the_wrong_recipient_is_refused(bus_env):
    """Carries tests/broker/test_delivery.py::test_confirm_not_owner_rejected.

    Without the credential machinery, the boundary is the stated recipient:
    an ack across it is a caller bug, refused before the settlement write.
    """

    send(bus_env)
    bus_env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
    with pytest.raises(ValueError):
        bus_env.bus.ack(
            "task-1", now_ms=T0 + 2_000, recipient=HUMAN_GATED_RECIPIENT
        )
    assert outbox_status(bus_env, "task-1") == "delivered"


def test_a_non_ascii_payload_survives_delivery_byte_for_byte(bus_env):
    """Carries tests/broker/test_notify.py::test_send_delivers_unicode_body.

    Payload fidelity is transport-neutral: whatever framing the endpoint uses
    (its JSON is emitted ASCII-safe), the payload a poll presents is the one
    the sender enqueued, escapes and all.
    """

    payload = '{"task":"こんにちは — café \U0001f680"}'
    bus_env.bus.send(
        message_id="task-u",
        recipient=RECIPIENT,
        payload=payload,
        dedup_key="dk-task-u",
        now_ms=T0,
        epoch=EPOCH,
        run_id=RUN_ID,
    )
    envelopes = bus_env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
    assert [e.payload for e in envelopes] == [payload]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "failing specification (Q-0023 -> D-0028): recipient aliasing is not "
        "part of the MessageBus contract yet; carried from "
        "tests/broker/test_store.py::test_enqueue_matches_by_name"
    ),
)
def test_a_send_to_a_registered_alias_reaches_the_canonical_recipient(bus_env):
    """v1 resolved a human-readable name to the bound agent id at enqueue.

    The carried invariant is that a sender may address a recipient by a
    registered alias and the message reaches the canonical queue. The new
    contract has exactly one name per recipient (the registry key), so this
    specification fails until an aliasing surface exists -- landed failing
    rather than driving the deleted ``broker/server.py`` to reach it.
    """

    send(bus_env, message_id="task-aliased", recipient="notify")
    envelopes = bus_env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
    assert [e.message_id for e in envelopes] == ["task-aliased"]
