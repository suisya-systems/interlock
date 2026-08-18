"""The Curator stub, and the human approval step.

The stub stands in for the real Curator (D-0018: on demand, output is a
proposal). What matters for gate item 9 is not how good its curation is but
*where it can write*: into a candidate store, never into skill material. It has
no skill root, cannot obtain one, and the path audit fails the build if it ever
acquires one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .digest import digest_tree
from .ledger import ApprovalLedger
from .records import ApprovalRecord, Candidate


def _confined(component: str, what: str) -> Path:
    """A relative path that cannot leave the directory it is joined to."""

    if not component or component != component.strip():
        raise ValueError(f"empty or untrimmed {what}: {component!r}")
    path = Path(component)
    if path.is_absolute() or path.drive or path.root:
        raise ValueError(f"{what} must be relative: {component!r}")
    if any(part in ("..", "") for part in path.parts):
        raise ValueError(f"{what} must not traverse upwards: {component!r}")
    return path


def _assert_no_symlink_in_chain(store_root: Path, destination: Path) -> None:
    """Refuse a destination reached through a symlink, before writing anything.

    ``..`` is not the only way out of the store. A symlink already sitting in
    it -- ``candidates/demo -> project/.claude/skills/demo`` -- turns an
    innocent-looking relative write into a write into live skill material, and
    by U8 that is a promotion. The chain is checked with ``lstat`` so the check
    itself does not follow the link, and it is checked *before* any mutation:
    noticing afterwards would mean the unapproved bytes are already live.
    """

    current = store_root
    for part in destination.relative_to(store_root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                f"candidate destination passes through a symlink: {current}"
            )


class CuratorStub:
    """Writes candidate knowledge into an immutable-by-convention store."""

    def __init__(self, store_root: Path) -> None:
        self.store_root = Path(store_root)

    def propose(self, candidate_id: str, files: Mapping[str, str | bytes]) -> Candidate:
        """Write a candidate into the store.

        Both the candidate id and every file name are confined to the store.
        Without that, ``propose("../../.claude/skills/evil", ...)`` would be a
        write into live skill material -- which, per U8, is a promotion -- and it
        would never pass anywhere near the gate. The Curator's inability to
        reach skill material is not a matter of it choosing not to.
        """

        root = self.store_root / _confined(candidate_id, "candidate id")
        destinations = {
            root / _confined(relative, "candidate file"): payload
            for relative, payload in files.items()
        }

        # Validate every destination first: a store that is half-written when
        # the tenth file turns out to escape is a store that already leaked.
        self.store_root.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_in_chain(self.store_root, root)
        for path in destinations:
            _assert_no_symlink_in_chain(self.store_root, path)

        for path, payload in destinations.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            _assert_no_symlink_in_chain(self.store_root, path)
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            path.write_bytes(data)
        return Candidate(candidate_id=candidate_id, root=root)


class ApprovalAuthority:
    """The human in the loop (D-0018), reduced to its recordable part.

    Issuing an approval is *only* a ledger append. Nothing is written into skill
    material here -- approval and promotion are separate acts, and the gate is
    what joins them.
    """

    def __init__(self, ledger: ApprovalLedger, *, approver: str = "operator") -> None:
        self._ledger = ledger
        self._approver = approver

    def approve(
        self,
        candidate: Candidate,
        target: str,
        *,
        note: str = "",
        approval_id: str | None = None,
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            approval_id=approval_id or uuid.uuid4().hex,
            candidate_id=candidate.candidate_id,
            content_digest=digest_tree(candidate.root),
            target=target,
            approver=self._approver,
            approved_at=datetime.now(timezone.utc).isoformat(),
            note=note,
        )
        return self._ledger.grant(record)

    def revoke(self, record: ApprovalRecord, *, reason: str) -> None:
        self._ledger.revoke(
            record.approval_id, reason=reason, revoked_by=self._approver
        )
