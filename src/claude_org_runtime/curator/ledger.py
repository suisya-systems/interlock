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

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

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

    # -- writing ---------------------------------------------------------

    def append(self, event: str, **payload: Any) -> LedgerEvent:
        entry = LedgerEvent(event=event, at=self._clock(), payload=payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_json(), sort_keys=True) + "\n"
        # Open in append mode and flush to the OS on every event: a refusal that
        # is lost on crash is a refusal that was not recorded.
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def grant(self, record: ApprovalRecord) -> ApprovalRecord:
        self.append(
            EVENT_APPROVED,
            record=record.to_json(),
            record_digest=record.record_digest(),
        )
        return record

    def revoke(self, approval_id: str, *, reason: str, revoked_by: str) -> None:
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
