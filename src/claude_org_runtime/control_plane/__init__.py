"""S5/S6/S7 -- the spike control plane: the schema slice, the lease, the outbox.

**Spike scaffold, throwaway by default (D-0026).** ``spike_schema.sql`` carries
the marking in the file itself: it is a spike schema and **no migration path is
promised from it**. Promotion into the real implementation takes a new ``D-``
entry; being imported, being depended on, or having survived a gate run promotes
nothing, and ``Q-0001`` stays open. That covers S6 -- :mod:`.lease` -- and S7 --
:mod:`.outbox`, :mod:`.handlers` and :mod:`.destination` -- exactly as it covers
the schema they sit on. The durable half of all three issues is the test suite.

:mod:`.lease` is S6: the lease, and the fencing token every protected write
validates **atomically as part of the write**. After the fence search it is the
only exclusion in the system -- the provider supplies none (U27, U32) -- so
nothing here may be softened on the strength of a provider refusing a duplicate.
See ``docs/lease-fencing.md``.

**One name is deliberately not re-exported here, because two modules define it
and shadowing one with the other would be silent:**

``Destination``
    :class:`.destination.Destination` is a delivery *target* with a receipt.
    S6's register entry -- whether a target can refuse a stale epoch, and what
    residual is left when it cannot -- is :class:`.lease.DestinationFencing`,
    renamed for the property rather than the place so the two can coexist.

``StaleWriterRefused`` used to be a second such name: S7 landed first and grew
its own copy while S6 was in flight. The two classes were consolidated into
:class:`.lease.StaleWriterRefused` (#45) -- :mod:`.outbox` re-exports it and
every raiser carries the ``action_id`` of the durable refusal row and the
lease actually ``observed`` -- so ``except control_plane.StaleWriterRefused``
now catches every refusal and the name is exported here.

``EXACTLY_ONCE_MECHANISMS`` is defined by both and is the *same* tuple in each:
it is ``ACCEPTANCE.md`` section 2's clause and the DDL's enumeration, not either
module's policy. It is exported once, and the suite asserts the copies are equal
so they cannot drift apart.
"""

from .destination import (
    DeliveryReceipt,
    Destination,
    DestinationRefusal,
    KeyedDropbox,
)
from .handlers import (
    HumanGatedHandler,
    NotifyDestinationHandler,
    spike_registry,
)
from .lease import (
    DESTINATIONS,
    FENCE_SQL,
    PROTECTED_TABLES,
    WRITE_HISTORY_QUERY,
    Authority,
    Claim,
    ClockSkewRefused,
    DestinationFencing,
    DestinationRejectedStaleToken,
    EpochGuardedDestination,
    FencedStatement,
    Lease,
    LeaseHeld,
    LeaseNotHeld,
    LeaseRefusal,
    LeaseUsageError,
    ProtectedWrite,
    ProtectedWriteMissed,
    StaleWriterRefused,
    UnfencedStatement,
    acquire,
    and_,
    applied_epoch_regressions,
    authority_timeline,
    claimed_timeline,
    effect_kind,
    epoch_regressions,
    eq,
    fence_epoch,
    fenced_insert,
    fenced_update,
    increment,
    is_null,
    ne,
    overlapping_claims,
    param,
    protected_write,
    read_lease,
    release,
    renew,
    resource_of_kind,
    value,
    write_history,
)
from .outbox import (
    CHECKPOINTS,
    EXACTLY_ONCE_MECHANISMS,
    UNOWNED_OUTBOX_QUERY,
    AckOutcome,
    ActionHandler,
    AttemptOutcome,
    HandlerRegistry,
    HandlerRejected,
    HumanGateRequired,
    Outbox,
    OutboxMessage,
    RecoveryReport,
)
from .schema import (
    APPLICATION_ID,
    RECONSTRUCTION_QUERIES,
    SCHEMA_REVISION,
    SPIKE_MARKING,
    SPIKE_SCHEMA_PATH,
    STATE_TABLES,
    ControlPlaneRefusal,
    ControlPlaneState,
    CorruptStateRefused,
    MissingStateRefused,
    create_control_plane,
    load_schema_sql,
    open_control_plane,
    reconstruct,
)

__all__ = [
    "APPLICATION_ID",
    "CHECKPOINTS",
    "DESTINATIONS",
    "EXACTLY_ONCE_MECHANISMS",
    "FENCE_SQL",
    "PROTECTED_TABLES",
    "RECONSTRUCTION_QUERIES",
    "SCHEMA_REVISION",
    "SPIKE_MARKING",
    "SPIKE_SCHEMA_PATH",
    "STATE_TABLES",
    "UNOWNED_OUTBOX_QUERY",
    "WRITE_HISTORY_QUERY",
    "AckOutcome",
    "ActionHandler",
    "AttemptOutcome",
    "Authority",
    "Claim",
    "ClockSkewRefused",
    "ControlPlaneRefusal",
    "ControlPlaneState",
    "CorruptStateRefused",
    "DeliveryReceipt",
    "Destination",
    "DestinationFencing",
    "DestinationRefusal",
    "DestinationRejectedStaleToken",
    "EpochGuardedDestination",
    "FencedStatement",
    "HandlerRegistry",
    "HandlerRejected",
    "HumanGateRequired",
    "HumanGatedHandler",
    "KeyedDropbox",
    "Lease",
    "LeaseHeld",
    "LeaseNotHeld",
    "LeaseRefusal",
    "LeaseUsageError",
    "MissingStateRefused",
    "NotifyDestinationHandler",
    "Outbox",
    "OutboxMessage",
    "ProtectedWrite",
    "ProtectedWriteMissed",
    "RecoveryReport",
    "StaleWriterRefused",
    "UnfencedStatement",
    "acquire",
    "and_",
    "applied_epoch_regressions",
    "authority_timeline",
    "claimed_timeline",
    "create_control_plane",
    "effect_kind",
    "epoch_regressions",
    "eq",
    "fence_epoch",
    "fenced_insert",
    "fenced_update",
    "increment",
    "is_null",
    "ne",
    "param",
    "value",
    "load_schema_sql",
    "open_control_plane",
    "overlapping_claims",
    "protected_write",
    "read_lease",
    "reconstruct",
    "release",
    "renew",
    "resource_of_kind",
    "spike_registry",
    "write_history",
]
