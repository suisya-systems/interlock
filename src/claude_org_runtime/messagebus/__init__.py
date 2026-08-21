"""S8 -- the ``MessageBus``: worker-outbound delivery, with no session edge.

.. warning::

   **Spike scaffold, throwaway by default (D-0026).** The durable half of Issue
   ``#19`` is the suite under ``tests/messagebus/`` and the static no-edge
   assertion; nothing in this package is promoted by being imported.

This package is the delivery half of D-0009's two-contract split. The other
half, :mod:`claude_org_runtime.session`, manages sessions and deliberately
carries **no delivery verb** (see ``DELIVERY_ABSENCE_IS_DELIBERATE`` there).
The mirror-image property holds here and is enforced structurally: **no module
in this package imports** ``claude_org_runtime.session`` **or any session
backend** -- ``tests/messagebus/test_import_graph.py`` fails the build the day
such an edge appears (gate item 6's static assertion, paired with item 11's).

Per F1 there is no non-interactive way to push a message *into* a running
worker session, so the transport is **worker-outbound**: the worker connects to
:mod:`claude_org_runtime.messagebus.endpoint` as an MCP client and pulls.
Delivery decisions -- what is due, what resends, what is settled -- derive from
SQLite alone (:meth:`~claude_org_runtime.control_plane.outbox.Outbox.due`),
never from a session readout, which is what item 6 asks and what the missing
import edge makes structural rather than disciplinary.
"""

from __future__ import annotations

from .bus import DeliveredEnvelope, MessageBus

__all__ = [
    "DeliveredEnvelope",
    "MessageBus",
]
