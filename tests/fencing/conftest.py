"""Shared fixtures for the fencing suite.

Per D-0026 this directory is the **durable** output of issue #9; everything
under ``src/claude_org_runtime/fencing/`` is throwaway spike quality. So these
tests are written against the *contract* -- render, refuse, probe, deny, respawn
-- and avoid pinning incidental structure of the implementation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_org_runtime.fencing import FenceContext, default_hook_script, load_document


@pytest.fixture
def document() -> dict:
    return load_document()


@pytest.fixture
def hook_script(tmp_path: Path) -> Path:
    """A real, existing hook file.

    The renderer requires hook commands to name files that exist, so a fixture
    that hands back a path to nothing would make every test a refusal test.
    """

    return default_hook_script()


@pytest.fixture
def ctx(tmp_path: Path, hook_script: Path) -> FenceContext:
    interlock_root = tmp_path / "interlock"
    worker_dir = tmp_path / "worker"
    org_path = tmp_path / "claude-org"
    for path in (interlock_root, worker_dir, org_path):
        path.mkdir(parents=True, exist_ok=True)
    return FenceContext(
        interlock_root=interlock_root,
        worker_dir=worker_dir,
        claude_org_path=org_path,
        hook_script=hook_script,
        fence_path=interlock_root / "state" / "fence-worker.json",
    )


def mutate(document: dict, role: str, **changes) -> dict:
    """A deep copy of ``document`` with ``role`` altered.

    ``None`` as a value deletes the key, which is how the "config deleted" and
    "sandbox profile absent" cases are built without editing the shipped
    document.
    """

    clone = json.loads(json.dumps(document))
    body = clone["roles"][role]
    for key, value in changes.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    return clone
