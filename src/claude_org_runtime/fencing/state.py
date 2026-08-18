"""Persisting a rendered fence, and diffing it across an Interlock restart.

Under C2 the only restart there is is Interlock respawning a ``claude -p``
child from persisted state (D-0027; #8 closed as moot). So "the fence survives
restart" reduces to a property of this file plus the renderer: the fence
written before the crash and the fence re-rendered after it must be the same
object, rule for rule and byte for byte in the settings payload.

That is also the *whole* of what a rendered-input diff can prove, and the gate
record says so in D-0023's terms: it shows what we wrote, not what the provider
loaded. The breach battery is what narrows the remainder.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .rules import (
    KIND_PERMISSION_DENY,
    KIND_SANDBOX_DENY_READ,
    KIND_SANDBOX_DENY_WRITE,
    LAYER_PERMISSIONS,
    LAYER_SANDBOX,
    Fence,
    FenceRule,
)

_LAYERS = frozenset({LAYER_PERMISSIONS, LAYER_SANDBOX})
_KINDS = frozenset({KIND_PERMISSION_DENY, KIND_SANDBOX_DENY_READ, KIND_SANDBOX_DENY_WRITE})


def _rule_from_json(entry: Any) -> FenceRule:
    """Reconstruct one rule, refusing anything that is not exactly a rule.

    Coercing these fields with ``str()`` would let a corrupted-but-still-valid
    JSON fence through in the one direction that is silent: a mistyped
    ``layer`` is skipped by :func:`rules.decide`, and a ``null`` spec becomes
    the string ``"None"`` and matches nothing. Either removes a denial while
    the hook goes on treating the fence as sound -- so the vocabularies are
    closed and every field is type-checked.
    """

    if not isinstance(entry, Mapping):
        raise FenceStateError(f"persisted rule is not an object: {entry!r}")
    fields = {}
    for key in ("layer", "kind", "tool", "spec"):
        value = entry.get(key)
        if not isinstance(value, str) or not value:
            raise FenceStateError(
                f"persisted rule field {key!r} must be a non-empty string, got {value!r}"
            )
        fields[key] = value
    if fields["layer"] not in _LAYERS:
        raise FenceStateError(f"persisted rule has unknown layer: {fields['layer']!r}")
    if fields["kind"] not in _KINDS:
        raise FenceStateError(f"persisted rule has unknown kind: {fields['kind']!r}")
    return FenceRule(**fields)

FENCE_FORMAT_VERSION = 1


class FenceStateError(RuntimeError):
    """A persisted fence that cannot be read back. Never recovered from.

    The hook treats this as *deny everything* and the spawn path treats it as
    *refuse to spawn*; neither is allowed to continue with a partially read
    fence.
    """


def fence_to_json(fence: Fence) -> dict[str, Any]:
    return {
        "format": FENCE_FORMAT_VERSION,
        "role": fence.role,
        "role_kind": fence.role_kind,
        "permission_mode": fence.permission_mode,
        "rules": [
            {"layer": r.layer, "kind": r.kind, "tool": r.tool, "spec": r.spec}
            for r in fence.rules
        ],
        "settings": dict(fence.settings),
    }


def fence_from_json(payload: Mapping[str, Any]) -> Fence:
    try:
        if payload.get("format") != FENCE_FORMAT_VERSION:
            raise FenceStateError(f"unsupported fence format: {payload.get('format')!r}")
        rules = tuple(_rule_from_json(entry) for entry in payload["rules"])
        if not rules:
            raise FenceStateError("persisted fence carries no rules")
        return Fence(
            role=str(payload["role"]),
            role_kind=str(payload["role_kind"]),
            permission_mode=str(payload["permission_mode"]),
            rules=rules,
            settings=payload["settings"],
        )
    except FenceStateError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise FenceStateError(f"malformed persisted fence: {exc}") from exc


def write_fence(fence: Fence, path: Path) -> Path:
    """Atomically publish the fence the deny hook will read.

    Written to a temporary sibling and renamed: a hook that read a half-written
    fence would either deny everything (a stalled worker) or, worse, parse a
    truncated rule list and enforce a *subset* of the fence. The rename makes
    the second impossible.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = json.dumps(fence_to_json(fence), sort_keys=True, indent=2) + "\n"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def read_fence(path: Path) -> Fence:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise FenceStateError(f"cannot read fence at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FenceStateError(f"cannot read fence at {path}: not an object")
    return fence_from_json(payload)


@dataclass(frozen=True)
class FenceDiff:
    """The rendered-input diff across an Interlock-initiated restart."""

    added_rules: tuple[str, ...]
    removed_rules: tuple[str, ...]
    settings_changed: bool
    permission_mode_changed: bool

    @property
    def identical(self) -> bool:
        return not (
            self.added_rules
            or self.removed_rules
            or self.settings_changed
            or self.permission_mode_changed
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "identical": self.identical,
            "added_rules": list(self.added_rules),
            "removed_rules": list(self.removed_rules),
            "settings_changed": self.settings_changed,
            "permission_mode_changed": self.permission_mode_changed,
        }


def diff_fences(before: Fence, after: Fence) -> FenceDiff:
    before_ids = set(before.rule_ids())
    after_ids = set(after.rule_ids())
    return FenceDiff(
        added_rules=tuple(sorted(after_ids - before_ids)),
        removed_rules=tuple(sorted(before_ids - after_ids)),
        settings_changed=_canonical(before.settings) != _canonical(after.settings),
        permission_mode_changed=before.permission_mode != after.permission_mode,
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
