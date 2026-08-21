"""The seam holds: only the adapter knows what a spike is.

Design section 6.1. ``D-0026`` makes the tests durable and the S5-S7
implementations throwaway. A harness that imported ``Outbox`` internals would be
destroyed with them -- or worse, would preserve them by making the spike schema
load-bearing for the gate record. So exactly one module in this tree may import
``claude_org_runtime``, and it is asserted structurally rather than agreed
socially.

The assertion is over the parsed syntax tree, following the precedent in
``tests/control_plane/test_lease.py``'s no-dependency-edge test.
"""

from __future__ import annotations

import ast
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent

#: The modules allowed to reach the implementation of the day: one adapter per
#: component generation -- the S6/S7 spike driver, and #18's session driver
#: over the real orchestrator and the C2 provider.
ADAPTER_MODULES = frozenset({"spike_driver.py", "session_driver.py"})

FORBIDDEN_ROOT = "claude_org_runtime"


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add((node.module or "").split(".")[0])
    return roots


def test_only_the_adapter_imports_the_implementation_under_test() -> None:
    offenders = {
        path.name: sorted(_imported_roots(path))
        for path in sorted(HARNESS_ROOT.glob("*.py"))
        if path.name not in ADAPTER_MODULES
        and FORBIDDEN_ROOT in _imported_roots(path)
    }
    assert not offenders, (
        f"{sorted(offenders)} import {FORBIDDEN_ROOT}; the coupling to the "
        "spike internals lives in the adapter alone (design 6.1), so the "
        "durable half survives the S5-S7 discard"
    )


def test_the_adapter_exists_and_does_import_it() -> None:
    """The rule is a seam, not a ban: something has to bind to today's schema."""

    for name in ADAPTER_MODULES:
        assert FORBIDDEN_ROOT in _imported_roots(HARNESS_ROOT / name), name


def test_the_contract_and_controller_name_no_spike_symbol() -> None:
    """Not even in prose-as-code: no ``Outbox``/``Lease`` identifiers leak in.

    A durable module that mentioned ``Outbox`` by name in a type hint or a
    default would still have to be edited when S7 is discarded, which is exactly
    the coupling the seam exists to prevent.
    """

    for name in ("contract.py", "controller.py", "manifest.py"):
        source = (HARNESS_ROOT / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        identifiers = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        for spike in ("Outbox", "KeyedDropbox", "ProtectedWrite", "FencedStatement"):
            assert spike not in identifiers, f"{name} names {spike}"
