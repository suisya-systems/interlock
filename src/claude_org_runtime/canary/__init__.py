"""Item 10 rehearsal: run-start routing, the run-owner ledger, the writer audit.

.. warning::

   **A REHEARSAL AGAINST A SYNTHETIC COUNTERPARTY (D-0022). NOT A DISCHARGE:
   GATE ITEM 10 IS DISCHARGED AT THE CANARY ITSELF, WITH LIVE V1 AS THE
   COUNTERPARTY. Q-0005 REMAINS OPEN: NO NUMERIC GO/NO-GO CRITERION IS
   STATED OR USED HERE.**

   Everything in this package is throwaway by default (D-0026); the durable
   halves are ``tests/canary/`` and the written contract
   ``docs/canary-routing-rehearsal.md``.

The property under rehearsal (Issue ``#23``): **rollback is a routing change,
not a data migration**. Concretely -- a routing point consulted once per run
at run start (:mod:`.routing`), a separate run-owner ledger whose rows never
change owner (:mod:`.ledger`), a writer audit that reads both stores and
shows no record written by both systems (:mod:`.audit`), and a stand-in
counterparty that is loud about being one (:mod:`.synthetic_v1`).
"""

from __future__ import annotations

from claude_org_runtime.canary.audit import (
    RollbackComparison,
    StoreSnapshot,
    WriterAuditReport,
    canonical_sqlite_bytes,
    canonical_synthetic_bytes,
    compare_across_rollback,
    snapshot_stores,
    writer_audit,
)
from claude_org_runtime.canary.ledger import (
    INTERLOCK,
    OWNING_SYSTEMS,
    SYNTHETIC_V1,
    CorruptLedgerRefused,
    MissingLedgerRefused,
    RoutingLedgerRefusal,
    create_routing_ledger,
    open_routing_ledger,
)
from claude_org_runtime.canary.marking import REHEARSAL_MARKING
from claude_org_runtime.canary.routing import (
    NoRoutingDecision,
    OwnerChangeRefused,
    RoutedRun,
    RoutingDecision,
    RoutingRefused,
    RunStartRoutingPoint,
    UnknownOwningSystem,
    UnroutedRun,
)
from claude_org_runtime.canary.synthetic_v1 import SyntheticStoreRefusal, SyntheticV1RunStore

__all__ = [
    "INTERLOCK",
    "OWNING_SYSTEMS",
    "REHEARSAL_MARKING",
    "SYNTHETIC_V1",
    "CorruptLedgerRefused",
    "MissingLedgerRefused",
    "NoRoutingDecision",
    "OwnerChangeRefused",
    "RollbackComparison",
    "RoutedRun",
    "RoutingDecision",
    "RoutingLedgerRefusal",
    "RoutingRefused",
    "RunStartRoutingPoint",
    "StoreSnapshot",
    "SyntheticStoreRefusal",
    "SyntheticV1RunStore",
    "UnknownOwningSystem",
    "UnroutedRun",
    "WriterAuditReport",
    "canonical_sqlite_bytes",
    "canonical_synthetic_bytes",
    "compare_across_rollback",
    "create_routing_ledger",
    "open_routing_ledger",
    "snapshot_stores",
    "writer_audit",
]
