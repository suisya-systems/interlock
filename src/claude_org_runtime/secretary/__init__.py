"""The stub Secretary intake and its explicit queue boundary (gate item 8 rehearsal).

**Spike scaffold, throwaway by default (D-0026).** This package exists so the
item-8 rehearsal (Issue #21, D-0022) has a concrete intake whose non-blocking
property can be **asserted in code** and **measured under load**, rather than
argued. The durable half is ``tests/secretary/``; nothing here is the real
Secretary, and promotion takes a new ``D-`` entry.

**This is a rehearsal, not a discharge.** D-0022 defers item 8 to its own
discharge point: the same absence of blocking shown against the **real**
Secretary under **genuine** worker load, due **before the canary starts**
(D-0013), against a threshold settled by ``Q-0011``. Every gate-record entry
fed from here is labelled *proven on the spike slice*.

The boundary contract itself is documented in
``docs/secretary-intake-boundary.md`` so later work (e.g. the Secretary Web
interface, Issue #29) inherits the boundary instead of re-deciding it.
"""

from .intake import (
    IntakeQueue,
    IntakeReceipt,
    IntakeRefused,
    SecretaryIntake,
)

__all__ = [
    "IntakeQueue",
    "IntakeReceipt",
    "IntakeRefused",
    "SecretaryIntake",
]
