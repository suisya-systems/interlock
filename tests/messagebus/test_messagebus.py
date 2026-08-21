"""S8 -- item 6's acceptance core: drop, resend, exactly one ack.

**These tests are the durable half of Issue ``#19`` (D-0026)** together with
the import-graph assertion beside them. The bus they drive is throwaway; the
facts they pin -- a dropped first delivery resends to exactly one ack, and
every delivery decision derives from SQLite -- are the contract whatever
replaces :mod:`claude_org_runtime.messagebus` still has to satisfy.

The headline sequence lives in :func:`tests.messagebus._env.drop_then_resend_transcript`
rather than inline, because the stale-readout case
(``test_stale_readout.py``) must run *the same* sequence and compare results
for equality -- see that file for why sameness is the assertion there.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.control_plane.outbox import (
    CHECKPOINT_BEFORE_DURABLE_WRITE,
    HandlerRejected,
    StaleWriterRefused,
)

from ._env import (
    EPOCH,
    RECIPIENT,
    RUN_ID,
    T0,
    drop_then_resend_transcript,
    expected_transcript,
    make_bus_env,
)


def send(env, *, message_id="task-1", dedup_key=None, payload='{"task":"t"}'):
    return env.bus.send(
        message_id=message_id,
        recipient=RECIPIENT,
        payload=payload,
        dedup_key=dedup_key if dedup_key is not None else f"dk-{message_id}",
        now_ms=T0,
        epoch=EPOCH,
        run_id=RUN_ID,
    )


def test_a_dropped_first_delivery_resends_to_exactly_one_ack(bus_env):
    """The item 6 acceptance case, with no UI attached.

    There is no UI in this process, no session backend, no worker process even
    -- only the bus and its database. That the case runs at all in this
    emptiness is the F1 caveat made visible: the "no UI attached" condition is
    trivially satisfied because nothing on the delivery path could attach one.
    """

    assert drop_then_resend_transcript(bus_env) == expected_transcript()


def test_a_message_stays_due_until_the_ack_and_not_after(bus_env):
    """Resend is the default state, not a recovery mode.

    Delivered-but-unacked rows keep being presented -- however many responses
    are lost -- and the destination's own ledger holds one effect throughout.
    The ack, and nothing else, is what stops the presentations.
    """

    send(bus_env, message_id="task-1")
    for lost_response in range(1, 4):
        envelopes = bus_env.bus.poll(
            RECIPIENT, now_ms=T0 + lost_response * 1_000, epoch=EPOCH
        )
        assert [e.message_id for e in envelopes] == ["task-1"]
        assert bus_env.effect_count("dk-task-1") == 1
    bus_env.bus.ack("task-1", now_ms=T0 + 5_000, recipient=RECIPIENT)
    assert bus_env.bus.poll(RECIPIENT, now_ms=T0 + 6_000, epoch=EPOCH) == ()
    assert bus_env.effect_count("dk-task-1") == 1


def test_a_send_to_an_unregistered_recipient_is_refused_before_the_write(bus_env):
    with pytest.raises(HandlerRejected):
        bus_env.bus.send(
            message_id="task-x",
            recipient="nobody-serves-this",
            payload="{}",
            dedup_key="dk-x",
            now_ms=T0,
            epoch=EPOCH,
            run_id=RUN_ID,
        )
    rows = bus_env.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()
    assert rows[0] == 0, "a refused send must not leave an undeliverable row"


def test_an_ack_for_a_never_polled_message_is_refused(bus_env):
    """An ack for an undelivered message is evidence of a lost delivery record."""

    send(bus_env, message_id="task-1")
    with pytest.raises(ValueError):
        bus_env.bus.ack("task-1", now_ms=T0 + 1_000, recipient=RECIPIENT)


def test_poll_presents_only_the_polling_recipients_messages(bus_env):
    """The recipient boundary holds at the poll, not just at the send."""

    send(bus_env, message_id="task-1")
    assert bus_env.bus.poll("someone-else", now_ms=T0 + 1_000, epoch=EPOCH) == ()
    envelopes = bus_env.bus.poll(RECIPIENT, now_ms=T0 + 2_000, epoch=EPOCH)
    assert [e.message_id for e in envelopes] == ["task-1"]


def test_a_stale_writer_cannot_poll_a_delivery_out(bus_env):
    """The fence runs through the bus unchanged: a superseded epoch is refused."""

    send(bus_env, message_id="task-1")
    with pytest.raises(StaleWriterRefused):
        bus_env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH + 1)


def refused_action_count(env) -> int:
    row = env.connection.execute(
        "SELECT COUNT(*) FROM action WHERE status = 'refused'"
    ).fetchone()
    return int(row[0])


def test_a_message_acked_between_polls_is_skipped_without_audit_noise(bus_env):
    """The common late-ack shape: settled between the snapshot and the attempt.

    The poll re-reads the row before attempting it, so an ordinary concurrent
    ack neither errors the poll nor leaves a durable stale-writer refusal
    behind -- the audit trail records only real fence refusals.
    """

    send(bus_env, message_id="task-1")
    send(bus_env, message_id="task-2")
    bus_env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
    bus_env.bus.ack("task-2", now_ms=T0 + 1_500, recipient=RECIPIENT)
    second = bus_env.bus.poll(RECIPIENT, now_ms=T0 + 2_000, epoch=EPOCH)
    assert [e.message_id for e in second] == ["task-1"]
    assert refused_action_count(bus_env) == 0


def test_a_message_settled_mid_poll_is_skipped_not_an_error(tmp_path):
    """A late ack landing between the due() snapshot and the attempt.

    An earlier delivery's ack can settle a row after a poll has read its due
    set but before it attempts that row. A settled message is the poll's
    success case: it is skipped, and the rest of the batch is still presented
    -- the race must not turn a whole poll into an error. Reproduced
    deterministically by acking task-2 from inside task-1's first checkpoint.
    """

    # Fire inside task-2's own attempt (the second BEFORE_DURABLE_WRITE of the
    # armed poll): task-1's attempt runs first, then task-2 is re-read as
    # still unsettled, enters attempt(), and only then is acked -- the
    # residual window the pre-attempt re-read cannot close.
    state = {"armed": False, "seen": 0}

    def settle_task2_mid_poll(name: str) -> None:
        if state["armed"] and name == CHECKPOINT_BEFORE_DURABLE_WRITE:
            state["seen"] += 1
            if state["seen"] == 2:
                env.bus.ack("task-2", now_ms=T0 + 1_500, recipient=RECIPIENT)

    env = make_bus_env(tmp_path, "mid-poll", checkpoint=settle_task2_mid_poll)
    try:
        send(env, message_id="task-1")
        send(env, message_id="task-2")
        first = env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
        assert [e.message_id for e in first] == ["task-1", "task-2"]
        state["armed"] = True
        second = env.bus.poll(RECIPIENT, now_ms=T0 + 2_000, epoch=EPOCH)
        assert [e.message_id for e in second] == ["task-1"]
        assert env.acked_row_count() == 1
        # The known cost of the residual window, pinned so it stays known:
        # the fenced attempt-count update had already recorded one refusal
        # row before the settle was recognised. Audit noise, not a delivery
        # fault -- see MessageBus.poll's own comment.
        row = env.connection.execute(
            "SELECT COUNT(*) FROM action WHERE status = 'refused'"
        ).fetchone()
        assert int(row[0]) == 1
    finally:
        env.close()


def test_two_tasks_settle_independently(bus_env):
    send(bus_env, message_id="task-1")
    send(bus_env, message_id="task-2")
    first = bus_env.bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
    assert [e.message_id for e in first] == ["task-1", "task-2"]
    bus_env.bus.ack("task-1", now_ms=T0 + 2_000, recipient=RECIPIENT)
    second = bus_env.bus.poll(RECIPIENT, now_ms=T0 + 3_000, epoch=EPOCH)
    assert [e.message_id for e in second] == ["task-2"]
    bus_env.bus.ack("task-2", now_ms=T0 + 4_000, recipient=RECIPIENT)
    assert bus_env.bus.poll(RECIPIENT, now_ms=T0 + 5_000, epoch=EPOCH) == ()
    assert bus_env.acked_row_count() == 2
