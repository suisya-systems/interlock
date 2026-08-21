"""Item 6's static assertion: no dependency edge from MessageBus to a session backend.

``ACCEPTANCE.md`` item 6, last clause: *statically assert the MessageBus
implementation has no dependency edge to the SessionProvider*, enforced in CI
so a later edge fails the build rather than being found at the gate. This file
is that assertion, and ``.github/workflows/test.yml`` runs it both in the full
suite and as its own named step so its absence would itself be visible.

It follows the precedent of ``tests/control_plane/test_lease.py``'s
no-dependency-edge test as widened by
``tests/gate_item11/test_no_provider_detail_leaks.py``: imports are read from
the AST, never executed, so testing for the forbidden edge cannot create it.
The pairing with item 11 is deliberate -- item 11 pins that the control plane
knows no provider; this pins that the delivery layer built on it doesn't
either, which is what makes D-0009's two-contract split structural on both
sides.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MESSAGEBUS_PACKAGE = REPO_ROOT / "src" / "claude_org_runtime" / "messagebus"
SUITE = REPO_ROOT / "tests" / "messagebus"

SESSION_PACKAGE = "claude_org_runtime.session"
CONTROL_PLANE = "claude_org_runtime.control_plane"

#: The one suite file allowed to know the session vocabulary -- the stale
#: readout case must produce a genuinely stale readout to be about anything.
SESSION_AWARE_SUITE_FILES = frozenset({"test_stale_readout.py"})


def _imported_modules(path: Path) -> set[str]:
    """Every module name *path* imports, absolute and relative alike.

    Same shape as the item 11 helper: relative imports are resolved against
    the file's own package name so that ``from ..session import x`` cannot
    slip past as a top-level name, and every alias in a ``from`` list is
    recorded as a candidate module too.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = path.parent.name
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = f"{package}.{node.module}" if node.module else package
            elif node.module:
                base = node.module
            else:  # pragma: no cover -- an absolute import always names a module
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _names_a_session_backend(imported: str) -> bool:
    parts = imported.split(".")
    return (
        "session" in parts
        or "provider" in parts
        or "stub_provider" in parts
        or "claude_cli_provider" in parts
    )


@pytest.mark.parametrize(
    "path", _python_files(MESSAGEBUS_PACKAGE), ids=lambda path: path.name
)
def test_no_messagebus_module_reaches_a_session_backend(path: Path):
    """The edge item 6 forbids, checked file by file.

    An implementation with no edge to the ``SessionProvider`` cannot be
    invalidated by replacing it -- the reason Issue ``#19`` survives C2
    unchanged, asserted rather than argued.
    """

    leaks = sorted(
        name for name in _imported_modules(path) if _names_a_session_backend(name)
    )
    assert leaks == [], (
        f"{path.name} imports {leaks}; the MessageBus must take no dependency "
        "edge to a session backend (ACCEPTANCE.md item 6, D-0009)"
    )


def test_the_assertion_is_not_vacuous():
    """A guard that guards nothing would pass forever.

    The package must exist, contain the bus and its endpoint, and demonstrably
    import the control plane -- so an empty directory, a renamed package, or a
    scan rooted at the wrong path fails here instead of passing everything
    above.
    """

    files = {path.name for path in _python_files(MESSAGEBUS_PACKAGE)}
    assert {"__init__.py", "bus.py", "endpoint.py"} <= files
    # The package imports the outbox relatively, so the recorded name carries
    # the ``control_plane`` component rather than the absolute prefix.
    imports_control_plane = any(
        "control_plane" in name.split(".")
        for path in _python_files(MESSAGEBUS_PACKAGE)
        for name in _imported_modules(path)
    )
    assert imports_control_plane, (
        "the MessageBus package no longer imports the control plane; this "
        "import-graph test is probably scanning the wrong tree"
    )


@pytest.mark.parametrize("path", _python_files(SUITE), ids=lambda path: path.name)
def test_session_knowledge_in_this_suite_stays_in_the_stale_readout_case(path: Path):
    """The suite-side confinement, mirroring item 11's.

    One file must know the session vocabulary to make a readout go stale;
    every other file here must not, so that the suite as a whole stays
    runnable -- and meaningful -- against a control plane with no session
    backend installed at all.
    """

    if path.name in SESSION_AWARE_SUITE_FILES:
        return
    leaks = sorted(
        name for name in _imported_modules(path) if _names_a_session_backend(name)
    )
    assert leaks == [], (
        f"{path.name} imports {leaks}; only {sorted(SESSION_AWARE_SUITE_FILES)} "
        "may know the session vocabulary in this suite"
    )


@pytest.mark.parametrize(
    "path", _python_files(MESSAGEBUS_PACKAGE), ids=lambda path: path.name
)
def test_no_messagebus_module_imports_dynamically(path: Path):
    """The evasion route the AST scan cannot follow, closed separately.

    ``importlib.import_module("...")`` and ``__import__("...")`` create edges
    no import statement records, so the statement scan above would miss them.
    Rather than pretend to resolve dynamic strings, this bans the primitives
    from the package outright -- a spike delivery layer has no business
    importing anything it cannot name statically.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = sorted(
        {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "__import__"
        }
        | {
            name
            for name in _imported_modules(path)
            if name.split(".")[0] == "importlib"
        }
    )
    assert offenders == [], (
        f"{path.name} uses {offenders}; dynamic imports would evade the "
        "no-edge assertion above and are banned from this package"
    )


def test_the_stale_readout_case_does_not_import_the_control_plane():
    """The other half of the split ``_env.py`` documents.

    The session-aware file reaches the control plane only through fixtures, so
    no single file in this suite knows both vocabularies -- the same property
    ``tests/gate_item11`` pins for the rest of the tree.
    """

    for name in SESSION_AWARE_SUITE_FILES:
        imported = _imported_modules(SUITE / name)
        leaks = sorted(n for n in imported if n.startswith(CONTROL_PLANE))
        assert leaks == [], f"{name} imports {leaks}"
