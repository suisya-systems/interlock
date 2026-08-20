"""The rehearsal marking every artifact of the item 10 rehearsal must carry.

The sentence lives in one module so that the routing ledger DDL, the audit
reports and the written record all carry *the same* sentence, and a test can
hold them to it verbatim. Issue ``#23``'s last acceptance criterion is that the
output be labelled a rehearsal against a synthetic counterparty naming the
canary as its discharge point; a label that each artifact paraphrases for
itself is a label that drifts.
"""

from __future__ import annotations

__all__ = ["REHEARSAL_MARKING"]

#: One sentence, four claims, all load-bearing (D-0022): this is a rehearsal;
#: the counterparty is synthetic, not v1; the discharge point is the canary
#: itself; and Q-0005 remains open, so nothing here is a go/no-go criterion.
REHEARSAL_MARKING = (
    "A REHEARSAL AGAINST A SYNTHETIC COUNTERPARTY (D-0022). NOT A DISCHARGE: "
    "GATE ITEM 10 IS DISCHARGED AT THE CANARY ITSELF, WITH LIVE V1 AS THE "
    "COUNTERPARTY. Q-0005 REMAINS OPEN: NO NUMERIC GO/NO-GO CRITERION IS "
    "STATED OR USED HERE."
)
