"""The fence, expressed as an explicit list of *rules*.

This module is the reason the breach-probe battery can be mechanically
complete. D-0023 asks for "one forbidden operation per **rule** in the role's
fence, not one per role", and a hand-maintained probe list drifts from the
fence the moment a rule is added. So the fence is not a blob of rendered JSON
here: it is a tuple of :class:`FenceRule`, each with a stable ``rule_id``, and
:mod:`claude_org_runtime.fencing.battery` derives exactly one probe per rule
from that tuple. Coverage is then a set equality, not a promise.

The same tuple is what :func:`decide` evaluates and what the ``PreToolUse``
deny hook (:mod:`claude_org_runtime.fencing.hook`) enforces, so the enforcement
path and the probed path cannot diverge either.

Per D-0026 the implementation is throwaway; the rule model and its tests are
the durable half.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
from dataclasses import dataclass
from typing import Any, Mapping

# Rule layers. ``sandbox`` is checked before ``permissions`` because a sandbox
# deny is a filesystem-level statement and must not be overridable by a
# permission rule that happens to be more specific.
LAYER_SANDBOX = "sandbox"
LAYER_PERMISSIONS = "permissions"

KIND_PERMISSION_DENY = "permission-deny"
KIND_SANDBOX_DENY_READ = "sandbox-deny-read"
KIND_SANDBOX_DENY_WRITE = "sandbox-deny-write"

# Tools a sandbox deny path is enforced against. ``Bash`` is deliberately in
# the write set as well: a denied write path reached through a shell redirect
# is the same breach as one reached through the Write tool.
_READ_TOOLS = ("Read", "Glob", "Grep", "NotebookRead")
_WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")

# The token substituted for a wildcard when a witness operation is synthesized
# from a rule. It has to be inert -- the battery never executes a witness, but
# a value that reads like a real path or flag invites someone to.
WITNESS_TOKEN = "interlock-breach-witness"


class RuleSyntaxError(ValueError):
    """A rule that cannot be parsed. Always fatal -- never skipped.

    F2/V15/V16 record this codebase's habit of ignore-and-continue on bad
    input. A fence rule that fails to parse and is dropped is a hole with no
    probe and no error, which is precisely the failure mode item 3 exists to
    catch, so parsing raises instead of returning ``None``.
    """


@dataclass(frozen=True)
class FenceRule:
    """One denial in a role's fence.

    ``rule_id`` is derived from the rule's own content, never assigned by
    hand, so two renders of the same fence produce the same ids and a diff
    across restart is meaningful.
    """

    layer: str
    kind: str
    tool: str
    spec: str

    @property
    def rule_id(self) -> str:
        return f"{self.layer}:{self.kind}:{self.tool}:{self.spec}"

    def matches(self, tool_name: str, tool_input: Mapping[str, Any]) -> bool:
        if self.kind == KIND_PERMISSION_DENY:
            return _permission_matches(self, tool_name, tool_input)
        if self.kind == KIND_SANDBOX_DENY_READ:
            return _sandbox_matches(self, tool_name, tool_input, _READ_TOOLS)
        if self.kind == KIND_SANDBOX_DENY_WRITE:
            return _sandbox_matches(self, tool_name, tool_input, _WRITE_TOOLS)
        raise RuleSyntaxError(f"unknown rule kind: {self.kind}")


@dataclass(frozen=True)
class Decision:
    """The fence's answer about one tool call."""

    denied: bool
    rule_id: str | None = None
    layer: str | None = None
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.denied


@dataclass(frozen=True)
class Fence:
    """A rendered per-role fence: the rules, plus the settings they came from."""

    role: str
    role_kind: str
    permission_mode: str
    rules: tuple[FenceRule, ...]
    settings: Mapping[str, Any]

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.rule_id for rule in self.rules)

    def rule(self, rule_id: str) -> FenceRule:
        for candidate in self.rules:
            if candidate.rule_id == rule_id:
                return candidate
        raise KeyError(rule_id)

    def decide(self, tool_name: str, tool_input: Mapping[str, Any]) -> Decision:
        return decide(self, tool_name, tool_input)


def decide(fence: Fence, tool_name: str, tool_input: Mapping[str, Any]) -> Decision:
    """Deny-only evaluation. ``denied=False`` means *no opinion*, not approval.

    The fence never says "allow". Saying so would make the hook an authority
    on permitting operations, and a bug in this file would then *widen* the
    worker's reach rather than narrow it.
    """

    for layer in (LAYER_SANDBOX, LAYER_PERMISSIONS):
        for rule in fence.rules:
            if rule.layer != layer:
                continue
            if rule.matches(tool_name, tool_input):
                return Decision(
                    denied=True,
                    rule_id=rule.rule_id,
                    layer=rule.layer,
                    reason=(
                        f"{fence.role}: {tool_name} denied by "
                        f"{rule.kind} rule {rule.spec!r}"
                    ),
                )
    return Decision(denied=False)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parse_permission_rule(raw: Any) -> FenceRule:
    """``"Bash(git push *)"`` -> a :class:`FenceRule`.

    A bare tool name (``"WebFetch"``) denies the whole tool.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise RuleSyntaxError(f"permission rule must be a non-empty string: {raw!r}")
    text = raw.strip()
    if not text.endswith(")"):
        if "(" in text:
            raise RuleSyntaxError(f"unbalanced permission rule: {raw!r}")
        return FenceRule(LAYER_PERMISSIONS, KIND_PERMISSION_DENY, text, "*")
    head, _, tail = text[:-1].partition("(")
    if not head or not _:
        raise RuleSyntaxError(f"unparseable permission rule: {raw!r}")
    return FenceRule(LAYER_PERMISSIONS, KIND_PERMISSION_DENY, head.strip(), tail)


def parse_sandbox_entry(raw: Any, kind: str) -> FenceRule:
    """A sandbox deny entry -> a :class:`FenceRule`.

    Accepts the plain string form and the structured ``{"path": ...}`` form
    the v1 renderer grew. The ``anchor`` key is *not* honoured here: paths
    reach this module already substituted, because a rule whose meaning still
    depends on a later resolution step cannot be probed.
    """

    if isinstance(raw, str):
        path = raw
    elif isinstance(raw, Mapping) and isinstance(raw.get("path"), str):
        path = raw["path"]
    else:
        raise RuleSyntaxError(f"unparseable sandbox entry: {raw!r}")
    path = path.strip()
    if not path:
        raise RuleSyntaxError(f"empty sandbox path: {raw!r}")
    if "{" in path or "}" in path:
        raise RuleSyntaxError(f"unsubstituted placeholder in sandbox path: {path!r}")
    tool = "Read" if kind == KIND_SANDBOX_DENY_READ else "Write"
    return FenceRule(LAYER_SANDBOX, kind, tool, _normalize_path(path))


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def _permission_matches(
    rule: FenceRule, tool_name: str, tool_input: Mapping[str, Any]
) -> bool:
    if tool_name != rule.tool:
        return False
    subject = _permission_subject(tool_name, tool_input)
    if subject is None:
        # A rule scoped to a tool whose subject we cannot read still denies the
        # tool: failing open here would turn an unrecognized payload shape into
        # a silent bypass.
        return True
    return _spec_matches(rule.spec, subject)


def _permission_subject(tool_name: str, tool_input: Mapping[str, Any]) -> str | None:
    if tool_name == "Bash":
        command = tool_input.get("command")
        return command if isinstance(command, str) else None
    for key in ("file_path", "path", "notebook_path", "url", "pattern"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def _spec_matches(spec: str, subject: str) -> bool:
    if spec == "*":
        return True
    if spec.endswith(":*"):
        # ``Bash(git add:*)`` is a prefix rule, not a glob.
        return subject == spec[:-2] or subject.startswith(spec[:-2])
    if any(ch in spec for ch in "*?["):
        return fnmatch.fnmatchcase(subject, spec) or fnmatch.fnmatchcase(
            _normalize_path(subject), _normalize_path(spec)
        )
    return subject == spec or _normalize_path(subject) == _normalize_path(spec)


def _sandbox_matches(
    rule: FenceRule,
    tool_name: str,
    tool_input: Mapping[str, Any],
    tools: tuple[str, ...],
) -> bool:
    if tool_name == "Bash":
        command = tool_input.get("command")
        return isinstance(command, str) and rule.spec in command
    if tool_name not in tools:
        return False
    subject = _permission_subject(tool_name, tool_input)
    if subject is None:
        return True
    return _path_is_within(_normalize_path(subject), rule.spec)


def _normalize_path(path: str) -> str:
    expanded = os.path.expanduser(path)
    return posixpath.normpath(expanded.replace(os.sep, "/"))


def _path_is_within(candidate: str, root: str) -> bool:
    if any(ch in root for ch in "*?["):
        return fnmatch.fnmatchcase(candidate, root)
    return candidate == root or candidate.startswith(root.rstrip("/") + "/")


# ---------------------------------------------------------------------------
# witness synthesis -- the input side of the breach battery
# ---------------------------------------------------------------------------


def witness_subject(rule: FenceRule) -> str:
    """A concrete operand that the rule matches.

    Synthesized from the rule text rather than written by hand: that is what
    keeps the probe list from drifting away from the fence. Callers must check
    the result with :meth:`FenceRule.matches` -- :func:`battery.probes_for`
    does, and refuses to build a battery it cannot prove complete.
    """

    spec = rule.spec
    if spec == "*":
        return WITNESS_TOKEN
    if spec.endswith(":*"):
        return f"{spec[:-2]} {WITNESS_TOKEN}".strip()
    subject = spec.replace("**/", f"{WITNESS_TOKEN}-dir/")
    subject = subject.replace("**", WITNESS_TOKEN)
    subject = subject.replace("*", WITNESS_TOKEN)
    subject = subject.replace("?", "x")
    return os.path.expanduser(subject)
