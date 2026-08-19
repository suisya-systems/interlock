"""S5 -- the spike SQLite schema slice, and the refusals that guard it.

**Spike scaffold, throwaway by default (D-0026).** ``spike_schema.sql`` carries
the marking in the file itself: it is a spike schema and **no migration path is
promised from it**. Promotion into the real implementation takes a new ``D-``
entry; being imported, being depended on by S6/S7, or having survived a gate run
promotes nothing, and ``Q-0001`` stays open.
"""

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
    "RECONSTRUCTION_QUERIES",
    "SCHEMA_REVISION",
    "SPIKE_MARKING",
    "SPIKE_SCHEMA_PATH",
    "STATE_TABLES",
    "ControlPlaneRefusal",
    "ControlPlaneState",
    "CorruptStateRefused",
    "MissingStateRefused",
    "create_control_plane",
    "load_schema_sql",
    "open_control_plane",
    "reconstruct",
]
