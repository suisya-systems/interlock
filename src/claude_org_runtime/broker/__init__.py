# -*- coding: utf-8 -*-
"""Broker subpackage: queue store, sidecar discovery, resident registry.

The pane-control MCP surface this package was originally built around is gone
(PORTING_LEDGER.md D-0009 / D-0014). What survives is transport-neutral:
:mod:`store`, :mod:`sidecar`, :mod:`residents`, :mod:`rpc`, :mod:`notify`,
:mod:`channel_sidecar`.

This module deliberately re-exports nothing -- import the submodules directly,
so the package init carries no dependency of its own.
"""

from __future__ import annotations

__all__: list[str] = []
