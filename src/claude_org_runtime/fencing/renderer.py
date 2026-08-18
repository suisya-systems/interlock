"""The per-role fencing renderer, carried onto Interlock.

Carried from ``claude_org_runtime/settings/generator.py`` per PORTING_LEDGER's
row for that file: the per-role permission / sandbox / hooks generation and
validation is the invariant that carries; the two axes that do **not** come
with it are named there explicitly and are refused rather than silently
ignored here:

- the ``transport.descriptor`` allowlist derivation (classified ``discard``),
- the A / B / C ``sandbox_by_pattern`` machinery (discarded with the old worker
  layout by D-0014).

Refusing them matters more than dropping them. A role document that still
carries a discarded axis was authored against the old contract, and rendering
it while ignoring the axis produces a fence that is *narrower than its author
believed* -- exactly the silent downgrade D-0023 part 2 forbids.

Everything in this module is fail-closed by construction: every validation
failure raises :class:`FenceRefusal`, and there is no code path that returns a
partially rendered fence. F2/V15/V16 record how easily this codebase reaches
for ignore-and-continue, so the tests assert the refusal, not just the absence
of the bad value.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from .rules import (
    KIND_SANDBOX_DENY_READ,
    KIND_SANDBOX_DENY_WRITE,
    Fence,
    FenceRule,
    RuleSyntaxError,
    parse_permission_rule,
    parse_sandbox_entry,
)

# Axes the ledger discards. Their presence is an authoring error, not a no-op.
DISCARDED_ROLE_KEYS = ("sandbox_by_pattern", "transport", "transport_descriptor")

# Keys on a role that describe it rather than fence it.
_META_KEYS = {"description", "$comment", "role_kind", "permission_mode"}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class RefusalReason:
    """Stable refusal identifiers; the ledger stores these strings verbatim."""

    DOCUMENT_UNREADABLE = "document-unreadable"
    ROLE_ABSENT = "role-absent"
    DISCARDED_AXIS = "discarded-axis"
    FORBIDDEN_ALLOW = "forbidden-allow"
    UNSUBSTITUTED_PLACEHOLDER = "unsubstituted-placeholder"
    HOOK_UNRESOLVABLE = "hook-unresolvable"
    HOOK_ABSENT = "hook-absent"
    HOOK_MATCHER_TOO_NARROW = "hook-matcher-too-narrow"
    HOOK_NOT_A_COMMAND = "hook-not-a-command"
    HOOK_INVOCATION_WRONG = "hook-invocation-wrong"
    GLOBAL_CONFIG_INVALID = "global-config-invalid"
    SANDBOX_PROFILE_ABSENT = "sandbox-profile-absent"
    RULE_SYNTAX = "rule-syntax"
    EMPTY_FENCE = "empty-fence"
    PERMISSION_MODE_INVALID = "permission-mode-invalid"
    PERMISSION_MODE_BYPASS = "permission-mode-bypass"


class FenceRefusal(Exception):
    """A fence that could not be rendered soundly. Never downgraded.

    Carries every reason found rather than the first, so a refusal recorded in
    the ledger explains the whole breakage instead of one symptom of it.
    """

    def __init__(self, role: str, reasons: list[tuple[str, str]]) -> None:
        self.role = role
        self.reasons = list(reasons)
        detail = "; ".join(f"{code}: {detail}" for code, detail in self.reasons)
        super().__init__(f"fence refused for role {role!r}: {detail}")

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(code for code, _ in self.reasons)

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "reasons": [{"code": code, "detail": detail} for code, detail in self.reasons],
        }


@dataclass(frozen=True)
class FenceContext:
    """Everything a rendered fence needs substituted into it.

    ``fence_path`` is where Interlock persists the rendered fence for the deny
    hook to read back. It is an input rather than an output because the hook
    command line embeds it, and a hook whose fence path is decided after the
    settings are written would name a file that does not exist yet.
    """

    interlock_root: Path
    worker_dir: Path
    claude_org_path: Path
    hook_script: Path
    fence_path: Path
    python: str = "python3"
    extra: Mapping[str, str] = field(default_factory=dict)

    def mapping(self) -> dict[str, str]:
        base = {
            "interlock_root": str(self.interlock_root),
            "worker_dir": str(self.worker_dir),
            "claude_org_path": str(self.claude_org_path),
            "hook_script": str(self.hook_script),
            "fence_path": str(self.fence_path),
            "python": self.python,
        }
        base.update({str(k): str(v) for k, v in self.extra.items()})
        return base


def bundled_document_path() -> Path:
    return Path(str(files("claude_org_runtime.fencing").joinpath("roles.json")))


def load_document(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else bundled_document_path()
    try:
        with target.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise FenceRefusal(
            "<document>", [(RefusalReason.DOCUMENT_UNREADABLE, f"{target}: {exc}")]
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("roles"), dict):
        raise FenceRefusal(
            "<document>",
            [(RefusalReason.DOCUMENT_UNREADABLE, f"{target}: no 'roles' object")],
        )
    return document


def role_names(document: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    doc = document if document is not None else load_document()
    return tuple(
        name for name in doc["roles"] if isinstance(name, str) and not name.startswith("$")
    )


def render_fence(
    role: str,
    ctx: FenceContext,
    *,
    document: Mapping[str, Any] | None = None,
) -> Fence:
    """Render one role's fence, or refuse.

    There is no ``strict=False``. A renderer with a lenient mode grows a caller
    that uses it, and that caller is the downgraded spawn D-0023 forbids.
    """

    doc = document if document is not None else load_document()
    reasons: list[tuple[str, str]] = []
    roles = doc.get("roles", {})
    body = roles.get(role)
    if not isinstance(body, dict):
        raise FenceRefusal(role, [(RefusalReason.ROLE_ABSENT, f"no role {role!r} in document")])

    for key in DISCARDED_ROLE_KEYS:
        if key in body:
            reasons.append(
                (
                    RefusalReason.DISCARDED_AXIS,
                    f"{key!r} was discarded by the porting ledger (R5) and may not be authored",
                )
            )

    global_cfg = doc.get("global", {}) if isinstance(doc.get("global"), dict) else {}
    permission_mode = body.get("permission_mode", "default")
    reasons.extend(_check_permission_mode(permission_mode, global_cfg))

    mapping = ctx.mapping()
    rendered = _substitute(_strip_meta(body), mapping)
    # Hook commands are *shell strings*, not argv, so a substituted path
    # containing a space arrives as two arguments and one containing a shell
    # metacharacter arrives as something else entirely. They are re-rendered
    # from the unsubstituted source with a shell-quoted mapping.
    if "hooks" in rendered:
        quoted = {key: shlex.quote(value) for key, value in mapping.items()}
        rendered["hooks"] = _substitute(_strip_meta(body).get("hooks"), quoted)
    reasons.extend(_check_placeholders(rendered))

    permissions = rendered.get("permissions", {})
    if not isinstance(permissions, dict):
        reasons.append((RefusalReason.RULE_SYNTAX, "permissions must be an object"))
        permissions = {}
    reasons.extend(_check_forbidden_allow(permissions.get("allow", []), global_cfg))

    rules: list[FenceRule] = []
    deny = permissions.get("deny", [])
    # A string here iterates character by character and renders one rule per
    # letter -- each of which the self-battery then happily "denies", while the
    # rule that was meant is absent. Refuse the shape rather than the symptom.
    if deny is None:
        deny = []
    elif not isinstance(deny, list):
        reasons.append(
            (
                RefusalReason.RULE_SYNTAX,
                f"permissions.deny must be a list, got {type(deny).__name__}",
            )
        )
        deny = []
    for raw in deny:
        try:
            rules.append(parse_permission_rule(raw))
        except RuleSyntaxError as exc:
            reasons.append((RefusalReason.RULE_SYNTAX, str(exc)))

    sandbox = rendered.get("sandbox")
    if sandbox is None:
        reasons.append(
            (
                RefusalReason.SANDBOX_PROFILE_ABSENT,
                f"role {role!r} declares no sandbox profile",
            )
        )
    elif not isinstance(sandbox, dict) or not isinstance(sandbox.get("filesystem"), dict):
        reasons.append(
            (RefusalReason.SANDBOX_PROFILE_ABSENT, "sandbox.filesystem is missing or not an object")
        )
    else:
        filesystem = sandbox["filesystem"]
        for key, kind in (
            ("denyRead", KIND_SANDBOX_DENY_READ),
            ("denyWrite", KIND_SANDBOX_DENY_WRITE),
        ):
            entries = filesystem.get(key, [])
            if entries is None:
                continue
            if not isinstance(entries, list):
                reasons.append(
                    (
                        RefusalReason.RULE_SYNTAX,
                        f"sandbox.filesystem.{key} must be a list, "
                        f"got {type(entries).__name__}",
                    )
                )
                continue
            for entry in entries:
                try:
                    rules.append(parse_sandbox_entry(entry, kind))
                except RuleSyntaxError as exc:
                    reasons.append((RefusalReason.RULE_SYNTAX, str(exc)))

    reasons.extend(_check_hooks(rendered.get("hooks"), ctx, role))

    deduped = _dedupe(rules)
    if not deduped:
        reasons.append((RefusalReason.EMPTY_FENCE, "a fence with no deny rule is not a fence"))

    if reasons:
        raise FenceRefusal(role, reasons)

    settings = _settings_payload(rendered, permission_mode)
    return Fence(
        role=role,
        role_kind=str(body.get("role_kind", "worker")),
        permission_mode=str(permission_mode),
        rules=tuple(deduped),
        settings=settings,
    )


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def _check_permission_mode(
    mode: Any, global_cfg: Mapping[str, Any]
) -> list[tuple[str, str]]:
    """U15's answer, encoded.

    ``investigation/i04-pretooluse-fence-probe.md`` measured ``PreToolUse``
    ordering under ``bypassPermissions``: the hook still runs and its ``deny``
    decision still stops the tool, but ``bypassPermissions`` removes the
    *permission* layer underneath it, leaving a single point of failure whose
    sibling failure mode (A6/U35: a non-zero exit absorbed) is already on the
    record. So the renderer refuses ``bypassPermissions`` outright rather than
    rendering a one-layer fence.
    """

    allowed = global_cfg.get("permission_modes") or ["default", "plan", "acceptEdits"]
    if mode == "bypassPermissions":
        return [
            (
                RefusalReason.PERMISSION_MODE_BYPASS,
                "bypassPermissions drops the permission layer and leaves the PreToolUse "
                "hook as the only fence (U15); refused",
            )
        ]
    if mode not in allowed:
        return [
            (
                RefusalReason.PERMISSION_MODE_INVALID,
                f"permission_mode {mode!r} is not one of {sorted(allowed)}",
            )
        ]
    return []


def _check_forbidden_allow(
    allow: Any, global_cfg: Mapping[str, Any]
) -> list[tuple[str, str]]:
    if not isinstance(allow, list):
        return [(RefusalReason.RULE_SYNTAX, "permissions.allow must be a list")]
    exact = set(global_cfg.get("forbidden_allow_exact") or ())
    found: list[tuple[str, str]] = []
    patterns = []
    for raw in global_cfg.get("forbidden_allow_regex") or ():
        try:
            patterns.append(re.compile(raw))
        except (re.error, TypeError) as exc:
            # Escaping as re.error would bypass FencedSpawner's refusal
            # handling, so a broken forbidden-allow list would produce no
            # durable spawn-refused event at all.
            found.append(
                (
                    RefusalReason.GLOBAL_CONFIG_INVALID,
                    f"forbidden_allow_regex entry {raw!r} is not a valid regex: {exc}",
                )
            )
    for entry in allow:
        if not isinstance(entry, str):
            found.append((RefusalReason.RULE_SYNTAX, f"allow entry not a string: {entry!r}"))
            continue
        if entry in exact:
            found.append(
                (RefusalReason.FORBIDDEN_ALLOW, f"{entry!r} is on the global forbidden-allow list")
            )
            continue
        for pattern in patterns:
            if pattern.search(entry):
                found.append(
                    (
                        RefusalReason.FORBIDDEN_ALLOW,
                        f"{entry!r} matches forbidden-allow pattern {pattern.pattern!r}",
                    )
                )
                break
    return found


def _check_hooks(hooks: Any, ctx: FenceContext, role: str) -> list[tuple[str, str]]:
    """Every hook command must name a file that exists *now*.

    "Hook path unresolvable" is one of the three broken configurations issue #9
    names. It is checked at render time because that is the last moment before
    the child inherits the settings, and because the hook process itself cannot
    report its own absence -- a missing hook does not fail, it simply never
    runs.
    """

    if not isinstance(hooks, dict):
        return [(RefusalReason.HOOK_ABSENT, "no PreToolUse hooks declared")]
    entries = hooks.get("PreToolUse")
    if not isinstance(entries, list) or not entries:
        return [(RefusalReason.HOOK_ABSENT, "no PreToolUse hooks declared")]
    problems: list[tuple[str, str]] = []
    commands = 0
    interlock_matchers: list[Any] = []
    for group in entries:
        if not isinstance(group, dict):
            problems.append((RefusalReason.RULE_SYNTAX, f"hook group not an object: {group!r}"))
            continue
        for hook in group.get("hooks", []) or []:
            if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
                problems.append((RefusalReason.RULE_SYNTAX, f"hook not a command: {hook!r}"))
                continue
            # Only ``type: "command"`` entries are executed as commands. An
            # entry of another type carrying a ``command`` key looks correct
            # to a reader and is never run, which is the silent direction.
            if hook.get("type") != "command":
                problems.append(
                    (
                        RefusalReason.HOOK_NOT_A_COMMAND,
                        f"PreToolUse hook has type {hook.get('type')!r}, not 'command': "
                        f"{hook['command']!r}",
                    )
                )
                continue
            commands += 1
            problems.extend(_check_command_resolves(hook["command"]))
            if str(ctx.hook_script) in hook["command"]:
                interlock_matchers.append(group.get("matcher"))
                problems.extend(_check_invocation(hook["command"], ctx, role))
    if not commands:
        problems.append((RefusalReason.HOOK_ABSENT, "no PreToolUse command hooks declared"))
    if not interlock_matchers:
        problems.append(
            (
                RefusalReason.HOOK_ABSENT,
                f"no PreToolUse hook invokes Interlock's deny hook ({ctx.hook_script})",
            )
        )
    elif not any(_matcher_is_universal(m) for m in interlock_matchers):
        # A narrow matcher is the quietest hole of all: the fence still holds
        # every rule, the self-battery still denies every probe -- because it
        # calls the decision function directly -- and the CLI simply never
        # consults the hook for the tools the matcher leaves out.
        problems.append(
            (
                RefusalReason.HOOK_MATCHER_TOO_NARROW,
                f"Interlock's deny hook is scoped to matcher {interlock_matchers!r}; it "
                "must match all tools ('*'), because the fence spans Bash, Read, Write, "
                "Edit and WebFetch rules and a narrow matcher silently exempts the rest",
            )
        )
    return problems


# Matchers the CLI treats as "every tool". Anything else is refused rather
# than parsed: guessing at a regex's coverage is how a narrow matcher would
# get admitted, and the fence has nothing to gain from the flexibility.
_UNIVERSAL_MATCHERS = frozenset({"*", ".*", ""})


def _matcher_is_universal(matcher: Any) -> bool:
    return matcher is None or (isinstance(matcher, str) and matcher.strip() in _UNIVERSAL_MATCHERS)


def _check_invocation(command: str, ctx: FenceContext, role: str) -> list[tuple[str, str]]:
    """Interlock's hook has to be invoked *at Interlock's fence*.

    Containing the hook script's path is not enough. ``hook.py --fence
    /tmp/stale.json`` names our hook and reads somebody else's rules, and the
    published fence is simply never consulted -- an admitted spawn enforcing a
    fence nobody rendered. So the flags are parsed and compared.
    """

    problems: list[tuple[str, str]] = []
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return [(RefusalReason.RULE_SYNTAX, f"unparseable hook command: {exc}")]

    expected = {"--fence": str(ctx.fence_path), "--role": role}
    for flag, want in expected.items():
        if flag not in tokens:
            problems.append(
                (
                    RefusalReason.HOOK_INVOCATION_WRONG,
                    f"Interlock's deny hook is invoked without {flag}: {command!r}",
                )
            )
            continue
        index = tokens.index(flag)
        got = tokens[index + 1] if index + 1 < len(tokens) else None
        if got != want:
            problems.append(
                (
                    RefusalReason.HOOK_INVOCATION_WRONG,
                    f"Interlock's deny hook is invoked with {flag}={got!r}, expected {want!r}",
                )
            )
    return problems


def _check_command_resolves(command: str) -> list[tuple[str, str]]:
    """Both halves of a hook command must resolve: the launcher and the script.

    i04 §5 measured an unresolvable hook failing **open** at exit 127 when it
    was launched through ``bash``. A launcher that does not exist produces the
    same 127, so checking only the script would leave the identical hole one
    token to the left.
    """

    problems: list[tuple[str, str]] = []
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return [(RefusalReason.RULE_SYNTAX, f"unparseable hook command: {exc}")]
    if not tokens:
        return [(RefusalReason.RULE_SYNTAX, "empty hook command")]

    launcher = tokens[0]
    if os.sep in launcher or (os.altsep and os.altsep in launcher):
        resolved = Path(launcher).is_file() and os.access(launcher, os.X_OK)
    else:
        resolved = shutil.which(launcher) is not None
    if not resolved:
        problems.append(
            (
                RefusalReason.HOOK_UNRESOLVABLE,
                f"hook launcher not executable: {launcher}",
            )
        )

    for token in tokens[1:]:
        if token.endswith((".sh", ".py")) and not Path(token).is_file():
            problems.append(
                (RefusalReason.HOOK_UNRESOLVABLE, f"hook script not found: {token}")
            )
    return problems


def _check_placeholders(value: Any, path: str = "") -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    if isinstance(value, str):
        for match in _PLACEHOLDER.finditer(value):
            problems.append(
                (
                    RefusalReason.UNSUBSTITUTED_PLACEHOLDER,
                    f"{path or '<root>'}: {{{match.group(1)}}} was never substituted",
                )
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            problems.extend(_check_placeholders(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problems.extend(_check_placeholders(item, f"{path}[{index}]"))
    return problems


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------


def _strip_meta(body: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in _META_KEYS and k not in DISCARDED_ROLE_KEYS}


def _substitute(value: Any, mapping: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            return mapping.get(match.group(1), match.group(0))

        return _PLACEHOLDER.sub(replace, value)
    if isinstance(value, dict):
        return {k: _substitute(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, mapping) for v in value]
    return value


def _dedupe(rules: list[FenceRule]) -> list[FenceRule]:
    seen: set[str] = set()
    unique: list[FenceRule] = []
    for rule in rules:
        if rule.rule_id in seen:
            continue
        seen.add(rule.rule_id)
        unique.append(rule)
    return unique


def _settings_payload(rendered: Mapping[str, Any], permission_mode: str) -> dict[str, Any]:
    """The ``settings.local.json`` body handed to the child.

    Key ordering is fixed and the payload is plain data, because this dict is
    what gets diffed across an Interlock-initiated restart.
    """

    payload: dict[str, Any] = {"permissionMode": permission_mode}
    for key in ("permissions", "sandbox", "hooks", "env"):
        if key in rendered:
            payload[key] = rendered[key]
    return json.loads(json.dumps(payload, sort_keys=True))
