"""Curator promotion gate -- gate item 9 (D-0018).

Curator output is a *proposal*. Nothing it produces may become skill material
without a human approval that names an immutable candidate version by content
digest.

U8 (answered 2026-08-18, see ``investigation/u8-skill-hot-reload-probe.md``) is
what fixes the shape of this package: an already-running Claude Code session
**re-reads skill material from disk**. Writing a file into a live skill
directory therefore *is* promotion -- there is no later "promotion function" to
guard, because by the time such a function ran the session would already be
using the file. So the gate here is not a policy check that a promotion routine
is expected to call; it is the *only writer* into skill material
(:mod:`claude_org_runtime.curator.gate`), and everything else in this package is
forbidden by :mod:`claude_org_runtime.curator.audit` from even naming the skill
root.

Per D-0026 the tests are the durable output of this slice; the implementation is
throwaway by default and may be promoted into the real implementation only by a
new ``D-`` entry.
"""

from .digest import candidate_digest, content_digest, digest_tree
from .gate import Decision, PromotionGate, RefusalReason
from .ledger import ApprovalLedger, LedgerEvent
from .records import ApprovalRecord, Candidate
from .stub import ApprovalAuthority, CuratorStub

__all__ = [
    "ApprovalAuthority",
    "ApprovalLedger",
    "ApprovalRecord",
    "Candidate",
    "CuratorStub",
    "Decision",
    "LedgerEvent",
    "PromotionGate",
    "RefusalReason",
    "candidate_digest",
    "content_digest",
    "digest_tree",
]
