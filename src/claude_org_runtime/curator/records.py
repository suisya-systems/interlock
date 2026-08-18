"""The candidate and the approval record.

An approval record is a *human* act (D-0018) that names three things at once:
the candidate, the exact bytes of that candidate (``content_digest``), and the
place in skill material those bytes were approved to land (``target``). All
three are covered by ``record_digest``, so an approval cannot be edited after
the fact into an approval of something else.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .digest import content_digest, digest_tree


@dataclass(frozen=True)
class Candidate:
    """An immutable snapshot of proposed skill material, held outside the gate.

    ``digest()`` reads the snapshot from disk on every call. That is deliberate:
    it is what turns "the candidate was mutated after approval" into a detected
    refusal rather than an undetectable substitution.
    """

    candidate_id: str
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    def digest(self) -> str:
        return digest_tree(self.root)


@dataclass(frozen=True)
class ApprovalRecord:
    """A human approval naming an immutable candidate version."""

    approval_id: str
    candidate_id: str
    content_digest: str
    target: str
    approver: str
    approved_at: str
    note: str = field(default="")

    def canonical_bytes(self) -> bytes:
        """Stable serialization -- the thing ``record_digest`` is taken over."""

        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    def record_digest(self) -> str:
        return content_digest(self.canonical_bytes())

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict) -> "ApprovalRecord":
        fields = {
            "approval_id",
            "candidate_id",
            "content_digest",
            "target",
            "approver",
            "approved_at",
            "note",
        }
        return cls(**{key: payload[key] for key in fields if key in payload})
