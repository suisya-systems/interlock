"""S5/S7 -- the spike control plane: the schema slice, and the outbox on it.

**Spike scaffold, throwaway by default (D-0026).** ``spike_schema.sql`` carries
the marking in the file itself: it is a spike schema and **no migration path is
promised from it**. Promotion into the real implementation takes a new ``D-``
entry; being imported, being depended on by S6/S7, or having survived a gate run
promotes nothing, and ``Q-0001`` stays open. That covers S7 -- :mod:`.outbox`,
:mod:`.handlers` and :mod:`.destination` -- exactly as it covers the schema they
sit on. The durable half of both issues is the test suite.
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
    StaleWriterRefused,
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
    "AckOutcome",
    "ActionHandler",
    "AttemptOutcome",
    "CHECKPOINTS",
    "ControlPlaneRefusal",
    "ControlPlaneState",
    "CorruptStateRefused",
    "DeliveryReceipt",
    "Destination",
    "DestinationRefusal",
    "EXACTLY_ONCE_MECHANISMS",
    "HandlerRegistry",
    "HandlerRejected",
    "HumanGateRequired",
    "HumanGatedHandler",
    "KeyedDropbox",
    "MissingStateRefused",
    "NotifyDestinationHandler",
    "Outbox",
    "OutboxMessage",
    "RECONSTRUCTION_QUERIES",
    "RecoveryReport",
    "SCHEMA_REVISION",
    "SPIKE_MARKING",
    "SPIKE_SCHEMA_PATH",
    "STATE_TABLES",
    "StaleWriterRefused",
    "UNOWNED_OUTBOX_QUERY",
    "create_control_plane",
    "load_schema_sql",
    "open_control_plane",
    "reconstruct",
    "spike_registry",
]
