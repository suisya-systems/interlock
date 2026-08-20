"""The writer audit over both stores, and the rollback comparison.

.. warning::

   **Item 10 rehearsal artifact (Issue #23, D-0022), throwaway by default
   (D-0026).** Every report this module produces carries
   :data:`~claude_org_runtime.canary.marking.REHEARSAL_MARKING` in its
   ``label`` field: the *output* is labelled, not just the code around it.

**The audit boundary.** The existing fencing write history cannot answer item
10's question: ``writer_epoch`` is a lease generation -- who held write
authority over a *resource*, in which epoch -- not a which-*system* attribution,
and it exists in only one of the two stores anyway. So the audit is defined
over three facts of the rehearsal's construction:

* **The logical record key both stores share is the run.** A "record written
  by both systems" means: one ``run_id`` with state in both stores.
* **Attribution is physical presence.** Each store is written by exactly one
  system -- the S5 control-plane database by Interlock, the JSON-lines store
  by the synthetic counterparty -- so *which store a record sits in* is
  *which system wrote it*, with no writer column to trust or forge.
* **Enumeration is capture.** Every write path of either system lands in its
  own store (both stores are their system's only durable state), so listing a
  store's runs lists that system's writes -- all of them, not a sample.

The audit therefore reads the stores themselves, never the ledger's opinion
of them, and then holds the ledger to account against what it found: a run
present in a store whose ledger row names the *other* system is misrouted
evidence, and a run present in a store with no ledger row at all is a write
that bypassed the routing point.

**The rollback comparison.** "Rollback is a routing change, not a data
migration" is asserted as: across the rollback, both run stores are
**byte-identical** and so is the run ledger; only ``routing_decision`` rows
were appended. The bytes compared are a **canonical serialisation** (stable
row order, stable encoding), not the raw database file: SQLite's file bytes
move with page headers, freelists and journal state that carry no facts, so
raw-file identity would fail on a store nothing was written to -- and a
comparison that must be forgiven its false alarms is not evidence. A bare
row-set equality would be weaker in the other direction (it has no stated
encoding to be identical *in*), so the canonical stream is hashed and the
hashes compared.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from claude_org_runtime.canary.ledger import INTERLOCK, SYNTHETIC_V1
from claude_org_runtime.canary.marking import REHEARSAL_MARKING
from claude_org_runtime.canary.synthetic_v1 import SyntheticV1RunStore

__all__ = [
    "RollbackComparison",
    "StoreSnapshot",
    "WriterAuditReport",
    "canonical_sqlite_bytes",
    "canonical_synthetic_bytes",
    "compare_across_rollback",
    "snapshot_stores",
    "writer_audit",
]


# --------------------------------------------------------------------------
# canonical serialisation
# --------------------------------------------------------------------------


def canonical_sqlite_bytes(connection: sqlite3.Connection, *, exclude_tables: tuple[str, ...] = ()) -> bytes:
    """A canonical byte stream of every user table's rows.

    Tables in name order, rows in the order of their own canonical encoding,
    values as sorted-keys JSON: two databases holding the same facts
    serialise to the same bytes regardless of insertion order, page layout or
    vacuum history. *exclude_tables* is how the rollback comparison excludes
    exactly the routing relation and nothing else.
    """

    tables = [
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        if name not in exclude_tables
    ]
    lines = []
    for table in tables:
        cursor = connection.execute(f'SELECT * FROM "{table}"')
        columns = [column[0] for column in cursor.description]
        rows = [
            json.dumps(
                {"table": table, "row": dict(zip(columns, row))},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            for row in cursor.fetchall()
        ]
        lines.extend(sorted(rows))
    return ("\n".join(lines) + "\n").encode("utf-8")


def canonical_synthetic_bytes(store: SyntheticV1RunStore) -> bytes:
    """The synthetic store's canonical bytes.

    Records are re-serialised through the same sorted-keys JSON as the SQLite
    side rather than hashed as raw file bytes, so both stores are compared in
    one stated encoding. File order is kept: the store is append-only and its
    order is part of its history.
    """

    lines = [
        json.dumps(dict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        for record in store.records()
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------
# the writer audit
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WriterAuditReport:
    """What the audit found, labelled as the rehearsal output it is.

    ``clean`` is a reading aid; the fields are the evidence. A caller that
    wants the acceptance criterion asserts ``dual_written == ()`` itself --
    the report never collapses "no dual write" into a bare boolean it would
    then have to be trusted about.
    """

    label: str
    interlock_written: tuple[str, ...]
    synthetic_v1_written: tuple[str, ...]
    #: run_ids with state in BOTH stores -- item 10's "record written by both
    #: systems". The rehearsal requires this empty.
    dual_written: tuple[str, ...]
    #: (system, run_id) pairs present in a store but absent from the ledger:
    #: writes that bypassed the routing point.
    unledgered: tuple[tuple[str, str], ...]
    #: (system, run_id) pairs present in a store whose ledger row names the
    #: other system.
    misrouted: tuple[tuple[str, str], ...]

    @property
    def clean(self) -> bool:
        return not (self.dual_written or self.unledgered or self.misrouted)


def writer_audit(
    ledger_connection: sqlite3.Connection,
    interlock_connection: sqlite3.Connection,
    synthetic_store: SyntheticV1RunStore,
) -> WriterAuditReport:
    """Audit both stores against each other, then against the ledger."""

    interlock_written = tuple(
        run_id
        for (run_id,) in interlock_connection.execute("SELECT run_id FROM run ORDER BY run_id")
    )
    synthetic_written = synthetic_store.run_ids()
    dual = tuple(sorted(set(interlock_written) & set(synthetic_written)))

    owner_of = dict(
        ledger_connection.execute("SELECT run_id, owning_system FROM run_owner")
    )
    unledgered = []
    misrouted = []
    for system, written in ((INTERLOCK, interlock_written), (SYNTHETIC_V1, synthetic_written)):
        for run_id in written:
            recorded = owner_of.get(run_id)
            if recorded is None:
                unledgered.append((system, run_id))
            elif recorded != system:
                misrouted.append((system, run_id))

    return WriterAuditReport(
        label=REHEARSAL_MARKING,
        interlock_written=interlock_written,
        synthetic_v1_written=synthetic_written,
        dual_written=dual,
        unledgered=tuple(unledgered),
        misrouted=tuple(misrouted),
    )


# --------------------------------------------------------------------------
# the rollback comparison
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreSnapshot:
    """Both stores and the ledger, canonically serialised at one instant."""

    interlock_digest: str
    synthetic_v1_digest: str
    #: The ledger *minus* the routing relation: ``run_owner`` rows, digested.
    run_ledger_digest: str
    #: The routing relation itself, kept as rows rather than a digest so the
    #: comparison can say WHAT was appended, not merely that something was.
    routing_decision_rows: tuple[tuple, ...]


def snapshot_stores(
    ledger_connection: sqlite3.Connection,
    interlock_connection: sqlite3.Connection,
    synthetic_store: SyntheticV1RunStore,
) -> StoreSnapshot:
    return StoreSnapshot(
        interlock_digest=_digest(canonical_sqlite_bytes(interlock_connection)),
        synthetic_v1_digest=_digest(canonical_synthetic_bytes(synthetic_store)),
        run_ledger_digest=_digest(
            canonical_sqlite_bytes(ledger_connection, exclude_tables=("routing_decision",))
        ),
        routing_decision_rows=tuple(
            tuple(row)
            for row in ledger_connection.execute(
                "SELECT decision_seq, owning_system, decided_at_ms, reason "
                "  FROM routing_decision ORDER BY decision_seq"
            )
        ),
    )


@dataclass(frozen=True)
class RollbackComparison:
    """Two snapshots compared across a rehearsed rollback, labelled.

    The claim under test is D-0022's: a rollback changes **only the routing
    decision**. ``only_the_routing_decision_changed`` is that sentence as a
    predicate -- both run stores byte-identical, the run ledger
    byte-identical, and the routing history extended (appended to, never
    rewritten) -- and the fields are the evidence for each clause.
    """

    label: str
    interlock_identical: bool
    synthetic_v1_identical: bool
    run_ledger_identical: bool
    #: True iff the earlier routing history is an untouched prefix of the
    #: later one -- appended to, never edited or truncated.
    decisions_appended_only: bool
    #: The routing_decision rows the rollback appended.
    appended_decisions: tuple[tuple, ...]

    @property
    def only_the_routing_decision_changed(self) -> bool:
        return (
            self.interlock_identical
            and self.synthetic_v1_identical
            and self.run_ledger_identical
            and self.decisions_appended_only
        )


def compare_across_rollback(before: StoreSnapshot, after: StoreSnapshot) -> RollbackComparison:
    prefix = after.routing_decision_rows[: len(before.routing_decision_rows)]
    appended_only = prefix == before.routing_decision_rows
    return RollbackComparison(
        label=REHEARSAL_MARKING,
        interlock_identical=before.interlock_digest == after.interlock_digest,
        synthetic_v1_identical=before.synthetic_v1_digest == after.synthetic_v1_digest,
        run_ledger_identical=before.run_ledger_digest == after.run_ledger_digest,
        decisions_appended_only=appended_only,
        appended_decisions=(
            after.routing_decision_rows[len(before.routing_decision_rows):]
            if appended_only
            else ()
        ),
    )
