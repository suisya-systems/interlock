"""The append-only record of approvals, revocations and gate decisions.

Two properties of this file carry the gate's weight:

1. **An approval counts only if it is here.** The gate never trusts an approval
   record it is handed; it looks the ``approval_id`` up in the ledger and
   compares digests. That is what makes "approval forged but unrecorded" a
   refusal rather than a bypass.
2. **Refusals are recorded, not just returned.** Gate item 9 asks for promotion
   to be *refused and the refusal recorded* in all five negative cases, so the
   ledger append happens inside the gate, before the caller ever sees the
   decision.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from .records import ApprovalRecord

EVENT_APPROVED = "approval-granted"
EVENT_REVOKED = "approval-revoked"
EVENT_APPLIED = "promotion-applied"
EVENT_REFUSED = "promotion-refused"


@dataclass(frozen=True)
class LedgerEvent:
    event: str
    at: float
    payload: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"event": self.event, "at": self.at, **self.payload}


class ApprovalLedger:
    """Append-only JSONL ledger. The file is created on first append."""

    def __init__(self, path: Path, *, clock=time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._thread_lock = threading.RLock()

    # -- serialization ---------------------------------------------------

    @contextlib.contextmanager
    def transaction(self):
        """Serialize a read-check-write sequence against other revocations.

        The gate reads the ledger, decides, and then publishes into a directory
        a session is watching. Without a common boundary a revocation landing
        between the check and the publish would be ignored, and the promotion it
        was meant to stop would go live and be recorded as applied.

        Cross-process exclusion uses an ``flock`` on a sibling lock file. On a
        platform without ``fcntl`` the thread lock still holds, and concurrent
        *processes* are not serialized -- a documented limit of this spike, not
        a property the gate's tests depend on.
        """

        with self._thread_lock:
            if fcntl is None:  # pragma: no cover - Windows
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + ".lock")
            with open(lock_path, "a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- writing ---------------------------------------------------------

    def append(self, event: str, **payload: Any) -> LedgerEvent:
        entry = LedgerEvent(event=event, at=self._clock(), payload=payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_json(), sort_keys=True) + "\n"
        # Open in append mode and flush to the OS on every event: a refusal that
        # is lost on crash is a refusal that was not recorded. ``newline=""``
        # pins the record separator to the ``\n`` written above: text mode would
        # otherwise emit CRLF on Windows, making the same ledger a different file
        # byte-for-byte depending on where it was appended to.
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def grant(self, record: ApprovalRecord) -> ApprovalRecord:
        """Record an approval. Approval ids are unique.

        A reused id would make ``recorded_approval`` answer with the *first*
        record, so the second approval could never be spent and a revocation
        would be ambiguous between the two. Refusing the reuse keeps one id
        meaning one approval.
        """

        with self.transaction():
            if self.recorded_approval(record.approval_id) is not None:
                raise ValueError(
                    f"approval id already recorded: {record.approval_id}"
                )
            self.append(
                EVENT_APPROVED,
                record=record.to_json(),
                record_digest=record.record_digest(),
            )
        return record

    def revoke(self, approval_id: str, *, reason: str, revoked_by: str) -> None:
        with self.transaction():
            self.append(
                EVENT_REVOKED,
                approval_id=approval_id,
                reason=reason,
                revoked_by=revoked_by,
            )

    # -- reading ---------------------------------------------------------

    def events(self) -> Iterator[LedgerEvent]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                event = payload.pop("event")
                at = payload.pop("at")
                yield LedgerEvent(event=event, at=at, payload=payload)

    def recorded_approval(self, approval_id: str) -> tuple[ApprovalRecord, str] | None:
        """The recorded record and its recorded digest, or ``None``."""

        for entry in self.events():
            if entry.event != EVENT_APPROVED:
                continue
            record = ApprovalRecord.from_json(entry.payload["record"])
            if record.approval_id == approval_id:
                return record, entry.payload["record_digest"]
        return None

    def is_revoked(self, approval_id: str) -> bool:
        return any(
            entry.event == EVENT_REVOKED
            and entry.payload.get("approval_id") == approval_id
            for entry in self.events()
        )
