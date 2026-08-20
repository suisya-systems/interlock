"""Structural assertions: independence and labelling, held rather than described.

Two claims about the rehearsal are structural and therefore assertable
against the artifact itself. First, the routing point has **no provider
dependency** -- it survives C2, or any later switch, untouched -- which here
is the stronger statement that the ``canary`` package imports no other
Interlock module at all: not ``session``, not ``dispatcher``, and not even
``control_plane`` (the audit takes open connections and never reaches into
either system). Second, the D-0022 labelling is *on the artifacts*: the DDL,
the package, the written record and every report all carry the one marking
sentence, and a labelling that had drifted apart would fail here rather than
be noticed at the canary.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import claude_org_runtime.canary as canary_package
from claude_org_runtime.canary.marking import REHEARSAL_MARKING

PACKAGE_DIR = Path(canary_package.__file__).parent
REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "docs" / "canary-routing-rehearsal.md"


def _collapsed(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^(--|>|#)+ ?", "", text, flags=re.MULTILINE))


def _imported_modules(source: Path) -> set:
    """Every module *source* imports, with relative imports resolved.

    ``ast.walk`` reaches imports inside functions and ``TYPE_CHECKING``
    blocks, and relative imports are resolved against the canary package
    rather than reported by their bare tail -- ``from ..control_plane import
    lease`` names a sibling package and must not slip past a filter keyed on
    the ``claude_org_runtime`` prefix.
    """

    names = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    names.add(node.module)
            elif node.level == 1:
                # from . / from .sibling -- stays inside the package.
                names.add("claude_org_runtime.canary")
            else:
                # from .. and beyond leaves the package.
                names.add("claude_org_runtime." + (node.module or ""))
    return names


def test_the_routing_layer_imports_no_other_interlock_module():
    for source in sorted(PACKAGE_DIR.glob("*.py")):
        foreign = {
            name
            for name in _imported_modules(source)
            if name.startswith("claude_org_runtime")
            and not name.startswith("claude_org_runtime.canary")
        }
        assert not foreign, (
            f"{source.name} imports {sorted(foreign)}; the routing layer sits "
            "above both systems and the provider, and depends on none of them"
        )


def test_the_import_guard_itself_catches_the_ways_around_it(tmp_path):
    # A guard is only as good as what it would have caught, so the escape
    # routes are probed against the guard directly: a relative import of a
    # sibling package, an import inside a function, and one behind
    # TYPE_CHECKING all must be seen.
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from typing import TYPE_CHECKING\n"
        "from ..control_plane import schema\n"
        "def late():\n"
        "    import claude_org_runtime.session\n"
        "if TYPE_CHECKING:\n"
        "    from claude_org_runtime.dispatcher import anything\n",
        encoding="utf-8",
    )
    seen = _imported_modules(probe)
    assert "claude_org_runtime.control_plane" in seen
    assert "claude_org_runtime.session" in seen
    assert "claude_org_runtime.dispatcher" in seen


def test_every_artifact_carries_the_one_marking_sentence():
    artifacts = (
        PACKAGE_DIR / "routing_ledger.sql",
        PACKAGE_DIR / "__init__.py",
        CONTRACT,
    )
    for artifact in artifacts:
        assert REHEARSAL_MARKING in _collapsed(artifact.read_text(encoding="utf-8")), (
            f"{artifact} does not carry the rehearsal marking verbatim"
        )


def test_the_marking_makes_all_four_claims():
    # The four claims the design review requires on the evidence and the gate
    # record alike: synthetic counterparty, not a discharge, discharged at
    # the canary, Q-0005 open.
    for claim in (
        "SYNTHETIC COUNTERPARTY",
        "NOT A DISCHARGE",
        "AT THE CANARY ITSELF",
        "Q-0005 REMAINS OPEN",
    ):
        assert claim in REHEARSAL_MARKING


def test_the_written_record_leaves_q_0005_open():
    text = CONTRACT.read_text(encoding="utf-8")
    assert "Q-0005" in text
    # The one number Q-0005 is about must not appear as a criterion; the
    # record must say the criteria are open rather than quietly supplying
    # any. (A textual assertion cannot prove absence of an invented number,
    # but it can hold the record to stating the openness explicitly.)
    assert "remains open" in _collapsed(text).lower()
