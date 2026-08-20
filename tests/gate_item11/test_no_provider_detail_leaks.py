"""Where provider knowledge is allowed to live, asserted structurally.

Item 11's residual in ``docs/gate-record.md``: *any test that has to be modified
to run against S3 marks a leak of session-backend detail into the control plane
and must be fixed before the item passes*. ``test_suite_runs_unchanged.py``
measures the outcome; these tests pin the property that produces it, so a leak
introduced later fails the build on the day it is introduced rather than the day
the next provider arrives.

``tests/control_plane/test_lease.py`` already asserts the narrow form of this --
S6's module and that file import nothing from a provider. It is widened here to
the whole control-plane package and the whole suite, because a leak in
``outbox.py`` or ``test_spike_schema.py`` costs exactly as much and nothing was
watching those.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from claude_org_runtime.session.provider import SessionProvider

from . import registry

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_PACKAGE = REPO_ROOT / "src" / "claude_org_runtime" / "control_plane"
CONTROL_PLANE_SUITE = REPO_ROOT / "tests" / "control_plane"
FIXTURE_PACKAGE = REPO_ROOT / "tests" / "gate_item11"

SESSION_PACKAGE = "claude_org_runtime.session"
CONTROL_PLANE = "claude_org_runtime.control_plane"


def _imported_modules(path: Path) -> set[str]:
    """Every module name *path* imports, absolute and relative alike.

    Relative imports are resolved against the file's own package so that
    ``from .substitution import ...`` inside this fixture is not mistaken for a
    top-level module named ``substitution``.
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
            # ``from claude_org_runtime import session`` names a module in its
            # *alias* list, not in ``node.module``. Recording only the base would
            # miss the plainest way there is to reach a provider, so every name
            # imported is recorded as a candidate module too. A name that turns
            # out to be a class rather than a module is harmless here: nothing
            # below asks whether the dotted path is importable, only whether one
            # of its components names a session backend.
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py"))


def _names_a_session_backend(imported: str) -> bool:
    parts = imported.split(".")
    return "session" in parts or "provider" in parts or "stub_provider" in parts


@pytest.mark.parametrize(
    "path", _python_files(CONTROL_PLANE_PACKAGE), ids=lambda path: path.name
)
def test_no_control_plane_module_reaches_a_session_backend(path: Path):
    """The implementation half. Widened from S6 alone to the whole package."""

    leaks = sorted(name for name in _imported_modules(path) if _names_a_session_backend(name))
    assert leaks == [], f"{path.name} imports {leaks}"


@pytest.mark.parametrize(
    "path", _python_files(CONTROL_PLANE_SUITE), ids=lambda path: path.name
)
def test_no_control_plane_test_reaches_a_session_backend(path: Path):
    """The suite half -- the one item 11 is actually about.

    A suite that imports a provider is a suite that would need editing to run
    against the next one, which is the modification the exit condition counts.
    """

    leaks = sorted(name for name in _imported_modules(path) if _names_a_session_backend(name))
    assert leaks == [], f"{path.name} imports {leaks}"


def test_no_shipped_module_knows_both_a_provider_and_the_control_plane():
    """What a provider swap costs, stated as a set of files.

    Nothing under ``src/`` may import both a session backend and the control
    plane: a module that knew both would be a module the next provider forces
    an edit to, and the whole of item 11's claim is that no such module exists.
    """

    both = []
    for path in _python_files(REPO_ROOT / "src"):
        imported = _imported_modules(path)
        knows_provider = any(name.startswith(SESSION_PACKAGE) for name in imported)
        knows_control_plane = any(name.startswith(CONTROL_PLANE) for name in imported)
        if knows_provider and knows_control_plane:
            both.append(str(path.relative_to(REPO_ROOT)))
    assert both == [], f"{both} would have to be edited by a provider swap"


def test_the_translation_is_confined_to_this_fixture_package():
    """And in the tests, the knowledge lives here and nowhere else.

    Something must turn a readout into a row; the claim is about *where*. Every
    test file that knows both vocabularies is under ``tests/gate_item11``, so
    the cost of the next provider is bounded by this directory plus one entry in
    :mod:`tests.gate_item11.registry`.
    """

    outside = []
    for path in _python_files(REPO_ROOT / "tests"):
        if FIXTURE_PACKAGE in path.parents:
            continue
        imported = _imported_modules(path)
        if any(name.startswith(SESSION_PACKAGE) for name in imported) and any(
            name.startswith(CONTROL_PLANE) for name in imported
        ):
            outside.append(str(path.relative_to(REPO_ROOT)))
    assert outside == [], f"{outside} knows both vocabularies and is outside the fixture"


def test_every_shipped_provider_is_registered():
    """The tripwire for the day S2 lands (issue ``#17``).

    Item 11 is measured against the providers in the registry, so a provider
    that ships without an entry silently narrows the measurement back to the one
    it was already known to pass. Discovering the classes rather than listing
    them is what makes that impossible to do quietly.
    """

    registered = {entry.implementation for entry in registry.PROVIDERS.values()}
    shipped = registry.shipped_providers()
    missing = sorted(name for name, cls in shipped.items() if cls not in registered)
    assert missing == [], (
        f"{missing} implements SessionProvider but is not in "
        "tests/gate_item11/registry.py; add an entry so the control-plane suite "
        "is measured against it too"
    )


def test_the_registry_entries_describe_real_implementations():
    """A registry entry that names nothing is a measurement that ran on nothing."""

    assert registry.PROVIDERS, "no provider is registered"
    assert registry.DEFAULT_PROVIDER in registry.PROVIDERS
    for key, entry in registry.PROVIDERS.items():
        assert key == entry.id
        assert issubclass(entry.implementation, SessionProvider)
        assert entry.scaffold.strip() and entry.issue.startswith("#")
