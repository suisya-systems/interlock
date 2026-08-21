"""The one place this suite knows the control plane's vocabulary.

Item 11's structural tests (``tests/gate_item11/test_no_provider_detail_leaks``)
pin that no test file outside ``tests/gate_item11`` imports both a session
backend and the control plane. This suite must exercise both worlds -- the bus
is driven while a *session* readout goes stale -- so the knowledge is split by
file instead: this module (and the conftest built on it) knows the control
plane and the bus but no session backend, and
``test_stale_readout.py`` knows the session backend and the bus but reaches
the control plane only through the fixtures defined here. No single file knows
both vocabularies, which is the same confinement the item 11 tests enforce,
applied one directory over.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from claude_org_runtime.control_plane.destination import KeyedDropbox
from claude_org_runtime.control_plane.handlers import NOTIFY_RECIPIENT, spike_registry
from claude_org_runtime.control_plane.schema import create_control_plane
from claude_org_runtime.messagebus import MessageBus

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
RESOURCE = "messagebus-of-run-1"
HOLDER = "bus-writer"
EPOCH = 1
TTL_MS = 300_000
RUN_ID = "run-1"

#: The recipient the spike registry serves, re-exported so files that must not
#: import the control plane directly can still address it.
RECIPIENT = NOTIFY_RECIPIENT


@dataclass
class BusEnv:
    """One isolated delivery world: a fresh database, destination, and bus."""

    bus: MessageBus
    dropbox: KeyedDropbox
    connection: sqlite3.Connection
    db_path: Path

    def effect_count(self, dedup_key: str) -> int:
        """The destination's own count for the spike handler's effect key."""

        return self.dropbox.effect_count(f"{RECIPIENT}:notify:{dedup_key}")

    def acked_row_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'acked'"
        ).fetchone()
        return int(row[0])

    def close(self) -> None:
        self.connection.close()


def make_bus_env(
    root: Path, tag: str, *, now_ms: int = T0, ttl_ms: int = TTL_MS, checkpoint=None
) -> BusEnv:
    """One fresh world. *now_ms* anchors the run and lease rows: the suite's
    fixed instant by default, real wall-clock time for the endpoint tests,
    whose server reads the clock itself."""

    db_path = root / f"control-plane-{tag}.sqlite3"
    connection = create_control_plane(db_path)
    connection.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
        " VALUES (?, 'running', ?, ?)",
        (RUN_ID, now_ms, now_ms),
    )
    connection.execute(
        "INSERT INTO lease (resource, holder, epoch, acquired_at_ms, expires_at_ms)"
        " VALUES (?, ?, ?, ?, ?)",
        (RESOURCE, HOLDER, EPOCH, now_ms, now_ms + ttl_ms),
    )
    connection.commit()
    dropbox = KeyedDropbox(root / f"destination-{tag}", name=f"worker-inbox-{tag}")
    bus = MessageBus(
        connection,
        resource=RESOURCE,
        holder=HOLDER,
        registry=spike_registry(dropbox),
        **({"checkpoint": checkpoint} if checkpoint is not None else {}),
    )
    return BusEnv(bus=bus, dropbox=dropbox, connection=connection, db_path=db_path)


def drop_then_resend_transcript(
    env: BusEnv,
    *,
    message_id: str = "task-1",
    dedup_key: str = "dk-task-1",
    payload: str = '{"task":"say hello"}',
) -> dict:
    """The item 6 acceptance sequence, recorded as comparable facts.

    Send one task; run a first poll whose response is treated as lost on the
    wire (nothing on the worker side survives it, so nothing is acked); poll
    again; ack once; ack again to show idempotency. Everything the sequence
    proves is returned as a plain dict so two runs of it -- one against a
    healthy session backend, one against a deliberately stale readout -- can be
    compared for equality: *delivery outcomes are unchanged* is then literal
    ``==``, not an interpretation.
    """

    bus = env.bus
    bus.send(
        message_id=message_id,
        recipient=RECIPIENT,
        payload=payload,
        dedup_key=dedup_key,
        now_ms=T0,
        epoch=EPOCH,
        run_id=RUN_ID,
    )
    first = bus.poll(RECIPIENT, now_ms=T0 + 1_000, epoch=EPOCH)
    # The first response is dropped: the worker never sees these envelopes,
    # acks nothing, and keeps no state. The rows stay delivered-but-unacked.
    second = bus.poll(RECIPIENT, now_ms=T0 + 2_000, epoch=EPOCH)
    first_ack = bus.ack(message_id, now_ms=T0 + 3_000, recipient=RECIPIENT)
    duplicate_ack = bus.ack(message_id, now_ms=T0 + 4_000, recipient=RECIPIENT)
    return {
        "first_poll": [
            (e.message_id, e.payload, e.retry_count, e.deduplicated) for e in first
        ],
        "second_poll": [
            (e.message_id, e.payload, e.retry_count, e.deduplicated) for e in second
        ],
        "acks_recorded": (first_ack.recorded, duplicate_ack.recorded),
        "effect_count": env.effect_count(dedup_key),
        "acked_rows": env.acked_row_count(),
        "due_after_settlement": [
            m.message_id for m in env.bus.outbox.due(T0 + 10_000)
        ],
    }


#: What the acceptance sequence must record, independent of which session
#: backend exists around it. Spelled out once: the first delivery is attempted
#: (retry_count 1, a fresh effect), the resend re-presents the same payload
#: (retry_count 2, deduplicated by the destination), exactly one ack is
#: recorded, the destination holds exactly one effect, and nothing stays due.
def expected_transcript(
    *,
    message_id: str = "task-1",
    payload: str = '{"task":"say hello"}',
) -> dict:
    return {
        "first_poll": [(message_id, payload, 1, False)],
        "second_poll": [(message_id, payload, 2, True)],
        "acks_recorded": (True, False),
        "effect_count": 1,
        "acked_rows": 1,
        "due_after_settlement": [],
    }
