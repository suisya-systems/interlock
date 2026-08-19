"""State schema for claude-org-runtime (Phase 4 Step B).

Public surface:

- :mod:`.enums` -- string-mixin Enums for worker status, journal event type,
  and anomaly kind, derived from the 2026-05-02 measurement of the
  claude-org-ja journals.
- :mod:`.journal_event` -- frozen dataclass mirror of a single
  ``journal.jsonl`` line, with forward-compatible ``extra`` bucket.

``.org_state`` (the ``org-state.md`` Worker Directory Registry parser) and
``.json_schema`` (the bundled JSONL wire schemas) were removed by the
Discard-bucket purge -- the SQLite state tables replace that surface outright
(PORTING_LEDGER.md D-0001 / D-0014).
"""

from .enums import AnomalyKind, JournalEventType, WorkerStatus
from .journal_event import JournalEvent

__all__ = [
    "AnomalyKind",
    "JournalEvent",
    "JournalEventType",
    "WorkerStatus",
]
