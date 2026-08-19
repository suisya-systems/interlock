"""Public surface of the ``claude-org-runtime`` package.

PORTING_LEDGER.md classes this file ``discard`` as package-level plumbing,
but deleting it would break every carried module under the package (and
``pyproject.toml``'s ``version = { attr = ... }``), so the *discard content* --
the eager re-export of subpackages the ledger deletes -- is stripped and the
file itself is kept (D-0014 / D-0015). Subpackages are imported by path.
"""

from .__about__ import __version__

__all__ = ["__version__"]
