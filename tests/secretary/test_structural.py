"""Item 8's structural half: the intake cannot block, shown on the syntax tree.

``ACCEPTANCE.md`` §1 item 8 asks for the absence of blocking dependencies to be
shown *structurally* — "no Secretary response path can be blocked behind worker
monitoring, long-running work, or an AI judgement". Convention cannot show
that; the syntax tree can, following the precedent of
``tests/control_plane/test_lease.py``'s no-dependency-edge test and
``tests/fault_injection/test_import_graph.py``.

Three assertions, matching the three design rules in
``claude_org_runtime.secretary.intake``:

1. **Import allowlist.** The intake package imports nothing but the stdlib
   modules it names — in particular no ``claude_org_runtime`` sibling at all,
   so no dependency edge to ``session`` (worker supervision), ``dispatcher``
   (AI judgement), or ``control_plane`` exists to block behind.

2. **No blocking primitive.** The names by which a Python thread waits on
   another — ``join``, ``wait``, ``get``, ``sleep``, blocking reads, selects,
   polls — do not occur as calls anywhere in the intake **package**.

3. **No lock exists at all.** A ``with lock:`` is a blocking ``acquire()``
   whenever the holder is descheduled — a wait the ban in (2) cannot see
   because context managers acquire implicitly. So the package is held to
   having no ``with`` block, no lock constructor, and no ``threading``
   import anywhere: the boundary is lock-free by construction, not by
   discipline.

These are durable tests (D-0026): the stub implementation is throwaway, the
property being pinned is not. This is a **rehearsal** of gate item 8 (Issue
#21, D-0022), not its discharge; the discharge re-proves the property against
the real Secretary under genuine worker load, before the canary starts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import claude_org_runtime.secretary as secretary_pkg

PKG_ROOT = Path(secretary_pkg.__file__).resolve().parent

#: Everything the intake package may import, exhaustively. ``threading`` is
#: deliberately absent: the boundary is lock-free (rule 3 above).
ALLOWED_IMPORT_ROOTS = frozenset({
    "__future__",
    "collections",
    "dataclasses",
    "itertools",
    "time",
})

#: Names by which one thread waits on another (or on I/O). None of these may
#: be *called* anywhere in the intake module — neither as ``obj.name(...)``
#: nor as a bare ``name(...)``.
BLOCKING_CALL_NAMES = frozenset({
    "join",
    "wait",
    "wait_for",
    "get",
    "get_nowait",  # spelled out: the boundary is deque-based, not queue.Queue
    "sleep",
    "read",
    "readline",
    "readlines",
    "recv",
    "select",
    "poll",
    "communicate",
    "acquire",
    "result",
    "run",
    "check_output",
    "check_call",
})

#: Lock constructors and synchronisation classes whose *existence* in the
#: package would reintroduce an implicit wait.
LOCK_CONSTRUCTOR_NAMES = frozenset({
    "Lock",
    "RLock",
    "Condition",
    "Semaphore",
    "BoundedSemaphore",
    "Event",
    "Barrier",
})


def _sources() -> dict[str, ast.Module]:
    """Every module in the package, subpackages included.

    Recursive on purpose: a later ``secretary/web/`` subpackage is reachable
    from the intake via an exempt level-1 relative import, so the package-wide
    guarantees hold only if its files are scanned too.
    """
    return {
        str(path.relative_to(PKG_ROOT)): ast.parse(
            path.read_text(encoding="utf-8"))
        for path in sorted(PKG_ROOT.rglob("*.py"))
    }


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                roots.add((node.module or "").split(".")[0])
            elif node.level >= 2:
                # ``from ..session import ...`` escapes the package: a level>=2
                # relative import resolves to a claude_org_runtime sibling —
                # exactly the edge this suite forbids. Only level 1 (the
                # package importing its own modules) is exempt.
                roots.add(f"<relative level {node.level}: {node.module or ''}>")
    return roots


def _called_names(tree: ast.AST) -> set[str]:
    """Names called in ``tree``, with import aliases resolved.

    ``from time import sleep as pause`` followed by ``pause(...)`` must
    register as a call to ``sleep``, or the blocking ban is a spelling check
    rather than a property (codex round 4). Both the local spelling and the
    resolved original are recorded.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name.split(".")[-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                names.add(fn.attr)
            elif isinstance(fn, ast.Name):
                names.add(fn.id)
                names.add(aliases.get(fn.id, fn.id))
    return names


def test_the_intake_package_imports_only_its_stdlib_allowlist() -> None:
    """No dependency edge exists to session, dispatcher, or anything else.

    Worker monitoring and AI judgement cannot block a code path that cannot
    reach them. Relative imports (the package importing its own modules) are
    exempt; everything absolute must be on the allowlist.
    """

    offenders = {
        name: sorted(_imported_roots(tree) - ALLOWED_IMPORT_ROOTS)
        for name, tree in _sources().items()
        if _imported_roots(tree) - ALLOWED_IMPORT_ROOTS
    }
    assert not offenders, (
        f"secretary intake imports outside its allowlist: {offenders}; "
        "the non-blocking claim rests on there being no edge to block behind"
    )


def test_the_init_reexports_only_from_its_own_package() -> None:
    """``__init__`` may re-export from ``.intake``; nothing absolute beyond it."""

    tree = _sources()["__init__.py"]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] in ALLOWED_IMPORT_ROOTS, (
                f"__init__.py imports {node.module!r} absolutely; the package "
                "re-exports its own modules and nothing else"
            )


def test_no_blocking_primitive_is_called_anywhere_in_the_package() -> None:
    """No module of the package calls a name by which a thread waits.

    Package-wide on purpose: a sibling module added later is part of the same
    import-reachable surface, so a blocking call there is a blocking call the
    intake path can reach.
    """

    offenders = {
        name: sorted(_called_names(tree) & BLOCKING_CALL_NAMES)
        for name, tree in _sources().items()
        if _called_names(tree) & BLOCKING_CALL_NAMES
    }
    assert not offenders, (
        f"blocking primitive(s) called in {offenders}; the response path must "
        "stamp, offer, and answer without waiting on anything"
    )


def test_the_package_takes_no_lock_at_all() -> None:
    """The boundary is lock-free by construction, not by discipline.

    ``with lock:`` acquires implicitly, which the blocking-call ban cannot
    see, and a lock held by a descheduled thread is an unbounded wait on the
    response path. So the package may contain no ``with`` / ``async with``
    block and no lock or synchronisation-object constructor anywhere.
    """

    for name, tree in _sources().items():
        withs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.With, ast.AsyncWith))]
        assert not withs, (
            f"{name}:{withs[0].lineno} uses a with-block; a context manager "
            "acquires implicitly, and the boundary must stay lock-free"
        )
        ctors = _called_names(tree) & LOCK_CONSTRUCTOR_NAMES
        assert not ctors, (
            f"{name} constructs synchronisation object(s) {sorted(ctors)}; "
            "the boundary must stay lock-free"
        )


def test_the_public_surface_is_the_documented_boundary() -> None:
    """The boundary contract's names exist as exported: intake, queue, receipt,
    refusal. A later real Secretary replaces the implementation, not the
    vocabulary (see ``docs/secretary-intake-boundary.md``)."""

    assert set(secretary_pkg.__all__) == {
        "IntakeQueue",
        "IntakeReceipt",
        "IntakeRefused",
        "SecretaryIntake",
    }
