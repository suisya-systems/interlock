"""G6 -- the measurement harness (Issue ``#67``, ``docs/measurement-harness.md``).

The instrument that AC-9's rate, AC-10's ground truth and the AC-7 divergence
report are computed with. Its first and most load-bearing property is that it
cannot write: ``ACCEPTANCE.md`` section 3 condition 5 requires the shadow path
to be read-only **enforced by capability, not by convention** (``D-0040``), and
:mod:`.reader` is the only place in this package that opens a database.

Everything else here reads through the connection :func:`.open_for_measurement`
returns, so no module in the harness has to be trusted to refrain from writing:
none of them holds a handle that could.
"""

from __future__ import annotations

from claude_org_runtime.measurement.reader import (
    ControlPlaneRefusal,
    CorruptStateRefused,
    DatabaseAheadOfCodeRefused,
    MigrationChecksumRefused,
    MissingStateRefused,
    ReadOnlyCapabilityRefused,
    open_for_measurement,
)

__all__ = [
    "ControlPlaneRefusal",
    "CorruptStateRefused",
    "DatabaseAheadOfCodeRefused",
    "MigrationChecksumRefused",
    "MissingStateRefused",
    "ReadOnlyCapabilityRefused",
    "open_for_measurement",
]
