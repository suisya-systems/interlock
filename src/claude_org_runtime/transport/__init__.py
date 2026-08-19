# -*- coding: utf-8 -*-
"""Transport package -- currently empty by design.

The transport *surface descriptor* that used to live here mapped a transport
flag (``renga`` | ``broker``) onto an MCP server name, a spawn-injection flag,
and a role-tier tool set, deriving the broker half structurally from
``broker.surface``. Both that descriptor and the surface it read are Discard
rows (PORTING_LEDGER.md D-0009 / D-0014), so the mechanism is gone.

The transport contract Interlock replaces it with has not been authored yet.
This package init is retained (stripped, not deleted) because the ledger's
grouped subpackage-docstring row classes it ``rewrite``.
"""

from __future__ import annotations

__all__: list[str] = []
