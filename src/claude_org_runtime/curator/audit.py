"""Path audit: no route from Curator output to skill material bypasses the gate.

Gate item 9 asks for two different things, and this module is the second one:

* a **path audit** showing that today no code path reaches skill material except
  through the gate, and
* a **negative test that fails the build** if such a path is added later.

An audit that only inspects today's tree satisfies the first and not the second,
so the audit is written as a checkable predicate over the source tree and run
from ``tests/curator/test_path_audit.py``. Adding a bypass turns that test red.

Four rules, each aimed at a way the gate could be routed around:

``skill-root-reference-outside-gate``
    Skill material is only nameable through
    :mod:`claude_org_runtime.curator.skill_root`. Any module other than the gate
    importing it -- or referring to its symbols -- would be a second place that
    can address live skill directories.

``hardcoded-skill-path``
    The obvious way to dodge rule 1 is to write ``".claude/skills"`` by hand
    somewhere else. String literals that look like skill material are findings
    outside the one module allowed to spell them, and outside the read-only
    consumers named in :data:`SKILL_PATH_READERS` -- each with the reason it is
    allowed to name skill material at all.

``skill-path-write``
    The allowlist above is for *readers*. If a module hands a skill-material
    path -- the literal, or a constant bound to one -- to a filesystem-write
    call, that is a write into skill material outside the gate, and no
    allowlist exempts it.

``unallowlisted-writer``
    Any filesystem-write call inside the curator package must live in a module
    that is explicitly allowlisted, with the store it is allowed to write to
    named in the allowlist. A new module that writes anything at all is a
    finding until a human adds it here -- which is the point: the addition is
    where somebody has to argue that the new writer is not a promotion path.

A stale allowlist is itself a finding (``stale-allowlist``), so renaming the
gate module cannot quietly turn the audit into a no-op.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Modules of :mod:`claude_org_runtime.curator`, relative to the package
#: directory, that are allowed to perform filesystem writes -- and the store each
#: one is allowed to write to. Adding an entry is a deliberate, reviewable act.
WRITER_ALLOWLIST: dict[str, str] = {
    "curator/gate.py": "live skill material, behind the approval check",
    "curator/ledger.py": "the append-only approval ledger",
    "curator/stub.py": "the candidate store (never skill material)",
}

#: Modules outside the curator package that may *name* skill material, with the
#: reason. These are read-only consumers: naming a path is allowed here, handing
#: it to a write call is not (rule ``skill-path-write`` has no allowlist).
SKILL_PATH_READERS: dict[str, str] = {
    "dispatcher/runner.py": (
        "reads the org-delegate instruction template out of the consumer repo's "
        "skill directory; never writes to it"
    ),
}

#: The only module allowed to name a skill root, and the only module allowed to
#: import it.
SKILL_ROOT_MODULE = "curator/skill_root.py"
GATE_MODULE = "curator/gate.py"

#: Symbols exported by the skill-root module; referring to any of them is
#: "naming skill material" for the purposes of rule 1.
SKILL_ROOT_SYMBOLS = frozenset(
    {
        "SkillRoot",
        "skill_root_for_project",
        "LIVE_SKILL_MATERIAL",
        "SKILL_DIRNAME",
        "CLAUDE_DIRNAME",
    }
)

#: Literals that address skill material directly.
SKILL_PATH_PATTERN = re.compile(r"\.claude[\\/]skills|(^|[\\/])skills[\\/]?$")

#: Attribute / function names that mutate the filesystem.
WRITE_CALL_NAMES = frozenset(
    {
        "write_text",
        "write_bytes",
        "writelines",
        "mkdir",
        "makedirs",
        "replace",
        "rename",
        "renames",
        "remove",
        "unlink",
        "rmdir",
        "rmtree",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "copyfileobj",
        "move",
        "symlink",
        "symlink_to",
        "hardlink_to",
        "link",
        "touch",
        "fdopen",
        "mkstemp",
        "mkdtemp",
        "truncate",
        "chmod",
    }
)

#: ``str.replace`` is by far the most common false positive for ``replace``.
_STR_METHOD_SAFE_RECEIVERS = frozenset({"str"})


@dataclass(frozen=True)
class Finding:
    rule: str
    module: str
    lineno: int
    detail: str

    def __str__(self) -> str:
        return f"{self.module}:{self.lineno}: [{self.rule}] {self.detail}"


def audit_tree(
    package_root: Path,
    *,
    writer_allowlist: dict[str, str] | None = None,
    reader_allowlist: dict[str, str] | None = None,
    skill_root_module: str = SKILL_ROOT_MODULE,
    gate_module: str = GATE_MODULE,
    write_scope: str = "curator",
) -> list[Finding]:
    """Audit ``package_root`` (a Python package directory) and return findings.

    ``package_root`` is taken as the root the module names are relative to, so
    the same function audits the real tree and the synthetic trees the tests
    build to prove the audit is not vacuous.
    """

    allowlist = WRITER_ALLOWLIST if writer_allowlist is None else writer_allowlist
    readers = SKILL_PATH_READERS if reader_allowlist is None else reader_allowlist
    package_root = Path(package_root)
    findings: list[Finding] = []

    modules = sorted(
        path for path in package_root.rglob("*.py") if path.is_file()
    )
    known = {path.relative_to(package_root).as_posix() for path in modules}

    for expected in (skill_root_module, gate_module, *allowlist, *readers):
        if expected not in known:
            findings.append(
                Finding(
                    rule="stale-allowlist",
                    module=expected,
                    lineno=0,
                    detail=(
                        "allowlisted module does not exist; the audit would not "
                        "cover whatever replaced it"
                    ),
                )
            )

    for path in modules:
        name = path.relative_to(package_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)

        if name != skill_root_module and name != gate_module:
            findings.extend(_skill_root_references(tree, name))

        if name != skill_root_module and name not in readers:
            findings.extend(_hardcoded_skill_paths(tree, name, docstrings))

        if name != gate_module:
            findings.extend(_skill_path_writes(tree, name))

        if name.startswith(f"{write_scope}/") and name not in allowlist:
            findings.extend(_write_calls(tree, name))

    return sorted(findings, key=lambda f: (f.module, f.lineno, f.rule))


# -- rule 1 ---------------------------------------------------------------


def _skill_root_references(tree: ast.AST, module: str) -> list[Finding]:
    found: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[-1] == "skill_root":
                found.append(
                    Finding(
                        "skill-root-reference-outside-gate",
                        module,
                        node.lineno,
                        f"imports skill_root ({_import_text(node)})",
                    )
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == "skill_root":
                    found.append(
                        Finding(
                            "skill-root-reference-outside-gate",
                            module,
                            node.lineno,
                            f"imports skill_root ({alias.name})",
                        )
                    )
        elif isinstance(node, ast.Name) and node.id in SKILL_ROOT_SYMBOLS:
            found.append(
                Finding(
                    "skill-root-reference-outside-gate",
                    module,
                    node.lineno,
                    f"refers to skill-root symbol {node.id}",
                )
            )
        elif isinstance(node, ast.Attribute) and node.attr in SKILL_ROOT_SYMBOLS:
            found.append(
                Finding(
                    "skill-root-reference-outside-gate",
                    module,
                    node.lineno,
                    f"refers to skill-root symbol {node.attr}",
                )
            )
    return found


# -- rule 2 ---------------------------------------------------------------


def _hardcoded_skill_paths(
    tree: ast.AST, module: str, docstrings: set[int]
) -> list[Finding]:
    found: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        if SKILL_PATH_PATTERN.search(node.value):
            found.append(
                Finding(
                    "hardcoded-skill-path",
                    module,
                    node.lineno,
                    f"string literal addresses skill material: {node.value!r}",
                )
            )
    return found


# -- rule 2b --------------------------------------------------------------


def _skill_path_writes(tree: ast.AST, module: str) -> list[Finding]:
    """Write calls whose target involves a skill-material path.

    Covers both the literal spelled inline and the far more likely shape: a
    module constant bound to the literal once and used everywhere else.
    """

    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_skill_path_expr(node.value, bound):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name) and _is_skill_path_expr(
                node.value, bound
            ):
                bound.add(node.target.id)

    found: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _write_call_detail(node) is None:
            continue
        operands = list(node.args)
        operands.extend(keyword.value for keyword in node.keywords)
        if isinstance(node.func, ast.Attribute):
            operands.append(node.func.value)
        if any(_mentions_skill_path(operand, bound) for operand in operands):
            found.append(
                Finding(
                    "skill-path-write",
                    module,
                    node.lineno,
                    "filesystem write whose target is skill material",
                )
            )
    return found


def _is_skill_path_expr(node: ast.AST, bound: set[str]) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(SKILL_PATH_PATTERN.search(node.value))
    if isinstance(node, ast.Name):
        return node.id in bound
    if isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Tuple, ast.List, ast.Call)):
        return any(_is_skill_path_expr(child, bound) for child in ast.iter_child_nodes(node))
    return False


def _mentions_skill_path(node: ast.AST, bound: set[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if SKILL_PATH_PATTERN.search(child.value):
                return True
        elif isinstance(child, ast.Name) and child.id in bound:
            return True
    return False


# -- rule 3 ---------------------------------------------------------------


def _write_calls(tree: ast.AST, module: str) -> list[Finding]:
    found: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        detail = _write_call_detail(node)
        if detail is not None:
            found.append(Finding("unallowlisted-writer", module, node.lineno, detail))
    return found


def _write_call_detail(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        if func.id == "open":
            mode = _open_mode(node, 1)
            if mode is None or any(flag in mode for flag in "wax+"):
                return f"open() in a write mode ({mode or 'mode not statically known'})"
            return None
        if func.id in WRITE_CALL_NAMES:
            return f"calls {func.id}()"
        return None

    if isinstance(func, ast.Attribute):
        if func.attr == "open":
            mode = _open_mode(node, 0)
            # Same fail-closed rule as the builtin: a mode the audit cannot read
            # statically is assumed to write, or `.open(mode)` would be a hole
            # exactly where `open(path, mode)` is not.
            if mode is None or any(flag in mode for flag in "wax+"):
                return f".open({mode or 'mode not statically known'})"
            return None
        if func.attr == "write":
            return "writes to a file handle"
        if func.attr in WRITE_CALL_NAMES:
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in _STR_METHOD_SAFE_RECEIVERS:
                return None
            return f"calls .{func.attr}()"
    return None


def _open_mode(node: ast.Call, mode_index: int) -> str | None:
    """The mode argument, or ``None`` when it is not a literal.

    ``mode_index`` differs between the two call shapes: ``open(path, mode)``
    carries it second, ``Path(...).open(mode)`` first. Reading the wrong slot
    made a dynamic ``.open(mode)`` look like a default-mode read.
    """

    if len(node.args) > mode_index:
        arg = node.args[mode_index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        return None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            if isinstance(keyword.value, ast.Constant) and isinstance(
                keyword.value.value, str
            ):
                return keyword.value.value
            return None
    return "r"


# -- helpers --------------------------------------------------------------


def _docstring_nodes(tree: ast.AST) -> set[int]:
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return docstrings


def _import_text(node: ast.ImportFrom) -> str:
    names = ", ".join(alias.name for alias in node.names)
    return f"from {'.' * node.level}{node.module or ''} import {names}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the runtime package for code paths that reach skill material "
            "without going through the Curator promotion gate."
        )
    )
    parser.add_argument(
        "package_root",
        nargs="?",
        default=str(Path(__file__).resolve().parent.parent),
        help="package directory to audit (default: the installed runtime package)",
    )
    args = parser.parse_args(argv)

    findings = audit_tree(Path(args.package_root))
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("path audit clean: skill material is reachable only through the gate")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
