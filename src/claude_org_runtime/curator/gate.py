"""The promotion gate -- the only writer into live skill material.

U8's answer (skills hot-reload; see :mod:`claude_org_runtime.curator.skill_root`)
puts the gate here rather than at a promotion function: the filesystem write
*is* the promotion, so the check and the write must be the same operation, in
the same module, with no other module able to perform the write.

The five negatives gate item 9 requires, and where each is caught:

===========================================  ==========================================
negative                                     refusal reason
===========================================  ==========================================
approval record absent                       ``approval-absent``
approval forged but unrecorded               ``approval-unrecorded`` / ``approval-tampered``
approval revoked                             ``approval-revoked``
candidate mutated after approval             ``digest-mismatch``
valid approval replayed at another candidate ``candidate-mismatch`` / ``target-mismatch``
===========================================  ==========================================

Every one of them is appended to the ledger before the decision is returned, and
in every one of them nothing is written into skill material.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .ledger import EVENT_APPLIED, EVENT_REFUSED, ApprovalLedger
from .records import ApprovalRecord, Candidate
from .skill_root import SkillRoot


class RefusalReason:
    """Stable refusal identifiers; the ledger stores these strings verbatim."""

    APPROVAL_ABSENT = "approval-absent"
    APPROVAL_UNRECORDED = "approval-unrecorded"
    APPROVAL_TAMPERED = "approval-tampered"
    APPROVAL_REVOKED = "approval-revoked"
    DIGEST_MISMATCH = "digest-mismatch"
    CANDIDATE_MISMATCH = "candidate-mismatch"
    TARGET_MISMATCH = "target-mismatch"
    TARGET_INVALID = "target-invalid"
    CANDIDATE_UNREADABLE = "candidate-unreadable"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None
    detail: str = ""
    written: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed


class PromotionGate:
    """Guards one live skill root.

    ``skill_root`` is the *only* place this class writes, and this class is the
    only place in the package that names a skill root at all --
    :mod:`claude_org_runtime.curator.audit` fails the build if that stops being
    true.
    """

    def __init__(self, skill_root: SkillRoot, ledger: ApprovalLedger) -> None:
        self._skill_root = skill_root
        self._ledger = ledger

    def promote(
        self,
        candidate: Candidate,
        target: str,
        approval: ApprovalRecord | None,
    ) -> Decision:
        """Promote ``candidate`` into ``target`` under the skill root.

        Returns a :class:`Decision`; never raises for a refusal, because a
        refusal has to be recorded rather than propagated as an exception a
        caller might swallow.
        """

        # 1. approval record absent.
        if approval is None:
            return self._refuse(
                RefusalReason.APPROVAL_ABSENT,
                "no approval record was presented",
                candidate_id=candidate.candidate_id,
                target=target,
            )

        # 2. forged: either never recorded, or recorded and since edited.
        recorded = self._ledger.recorded_approval(approval.approval_id)
        if recorded is None:
            return self._refuse(
                RefusalReason.APPROVAL_UNRECORDED,
                "approval id is not in the ledger",
                candidate_id=candidate.candidate_id,
                target=target,
                approval_id=approval.approval_id,
                presented_record_digest=approval.record_digest(),
            )
        recorded_record, recorded_digest = recorded
        presented_digest = approval.record_digest()
        if presented_digest != recorded_digest or recorded_record != approval:
            return self._refuse(
                RefusalReason.APPROVAL_TAMPERED,
                "presented approval does not match the recorded one",
                candidate_id=candidate.candidate_id,
                target=target,
                approval_id=approval.approval_id,
                presented_record_digest=presented_digest,
                recorded_record_digest=recorded_digest,
            )

        # 3. revoked.
        if self._ledger.is_revoked(approval.approval_id):
            return self._refuse(
                RefusalReason.APPROVAL_REVOKED,
                "approval has been revoked",
                candidate_id=candidate.candidate_id,
                target=target,
                approval_id=approval.approval_id,
            )

        # 5a. replay against a different candidate, or at a different target.
        # Checked before the digest so that a replay is reported as a replay
        # even in the pathological case of two candidates with equal bytes.
        if approval.candidate_id != candidate.candidate_id:
            return self._refuse(
                RefusalReason.CANDIDATE_MISMATCH,
                "approval names a different candidate",
                candidate_id=candidate.candidate_id,
                approved_candidate_id=approval.candidate_id,
                target=target,
                approval_id=approval.approval_id,
            )
        if approval.target != target:
            return self._refuse(
                RefusalReason.TARGET_MISMATCH,
                "approval names a different target",
                candidate_id=candidate.candidate_id,
                target=target,
                approved_target=approval.target,
                approval_id=approval.approval_id,
            )

        # 4. mutated after approval -- the candidate is re-digested from disk
        # here, at write time, not trusted from whatever produced it.
        try:
            observed_digest = candidate.digest()
        except (OSError, ValueError) as exc:
            return self._refuse(
                RefusalReason.CANDIDATE_UNREADABLE,
                f"candidate could not be digested: {exc}",
                candidate_id=candidate.candidate_id,
                target=target,
                approval_id=approval.approval_id,
            )
        if observed_digest != approval.content_digest:
            return self._refuse(
                RefusalReason.DIGEST_MISMATCH,
                "candidate content differs from the approved version",
                candidate_id=candidate.candidate_id,
                target=target,
                approval_id=approval.approval_id,
                approved_digest=approval.content_digest,
                observed_digest=observed_digest,
            )

        try:
            destination = self._skill_root.resolve_target(target)
        except ValueError as exc:
            return self._refuse(
                RefusalReason.TARGET_INVALID,
                str(exc),
                candidate_id=candidate.candidate_id,
                target=target,
                approval_id=approval.approval_id,
            )

        written = self._write(candidate, destination)
        self._ledger.append(
            EVENT_APPLIED,
            candidate_id=candidate.candidate_id,
            target=target,
            approval_id=approval.approval_id,
            content_digest=observed_digest,
            written=list(written),
        )
        return Decision(allowed=True, written=written)

    # -- the write itself ------------------------------------------------

    def _write(self, candidate: Candidate, destination: Path) -> tuple[str, ...]:
        """Copy the approved bytes in. The only filesystem write in the package
        that targets skill material."""

        from .digest import read_tree  # local import keeps the module surface small

        files = read_tree(candidate.root)
        written: list[str] = []
        for relative in sorted(files):
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".promote-")
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(files[relative])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_name, path)
            except BaseException:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                raise
            written.append(str(path))
        return tuple(written)

    def _refuse(self, reason: str, detail: str, **payload) -> Decision:
        self._ledger.append(EVENT_REFUSED, reason=reason, detail=detail, **payload)
        return Decision(allowed=False, reason=reason, detail=detail)
