"""Interlock's ``PreToolUse`` deny hook.

Read A6 of ``investigation/pre-spawn-fence-search.md`` (U35) before changing
anything here. A6 observed a hook that exited **1** being *absorbed*: the CLI
fell back to its own default logic and the session completed normally. A hook
whose failure is swallowed is not a fence. Two consequences are wired into
this file and must stay wired:

1. **The decision is carried in the hook's stdout JSON**, as an explicit
   ``permissionDecision: "deny"``, and the blocking exit status is **2**, not
   1. Exit 1 is the status A6 watched get absorbed.
2. **This hook never reports its own health as a pass.** Nothing downstream
   may read the exit status as evidence that the fence worked -- the evidence
   is that the forbidden operation did not happen. The tests assert the effect
   and are forbidden from asserting the exit code as a proxy for it (see
   ``tests/fencing/test_deny_hook.py``).

Fail-closed, in every direction it can fail:

- fence file missing, unreadable, malformed, or empty  -> deny
- stdin absent, not JSON, or missing ``tool_name``     -> deny
- any unexpected exception at all                      -> deny

F2/V15/V16 record this codebase's habit of ignore-and-continue on bad input,
and ``investigation/u1-session-id-bg-experiment.md`` §5.2 shows the same shape
on the CLI: exit 0 is not evidence of anything. So the ``except`` clause here
denies rather than re-raising into an absorbed non-zero exit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .rules import Decision
from .state import FenceStateError, read_fence

# The CLI treats exit 2 as a blocking error for PreToolUse. Exit 1 is what A6
# watched get absorbed, so it is never used to mean "deny".
EXIT_DENY = 2
EXIT_NO_OPINION = 0

DENY_SELF_CHECK = "fence-unavailable"


def decide_payload(
    fence_path: Path, event: Mapping[str, Any]
) -> tuple[Decision, dict[str, Any]]:
    """Evaluate one ``PreToolUse`` event against the persisted fence."""

    try:
        fence = read_fence(fence_path)
    except FenceStateError as exc:
        decision = Decision(
            denied=True,
            rule_id=DENY_SELF_CHECK,
            layer="hook",
            reason=(
                "Interlock cannot read its own fence, so it cannot tell whether this "
                f"call is permitted: {exc}"
            ),
        )
        return decision, _hook_output(decision)

    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input")
    if not isinstance(tool_name, str) or not tool_name:
        decision = Decision(
            denied=True,
            rule_id=DENY_SELF_CHECK,
            layer="hook",
            reason="PreToolUse event carried no tool_name; denied rather than guessed",
        )
        return decision, _hook_output(decision)
    if not isinstance(tool_input, Mapping):
        tool_input = {}

    decision = fence.decide(tool_name, tool_input)
    return decision, _hook_output(decision)


def _hook_output(decision: Decision) -> dict[str, Any]:
    if not decision.denied:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": decision.reason,
        },
        # The pre-``hookSpecificOutput`` shape, emitted alongside the current
        # one. Which of the two a given CLI build honours is not something a
        # fence should depend on knowing.
        "decision": "block",
        "reason": decision.reason,
        "interlock": {"rule_id": decision.rule_id, "layer": decision.layer},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="interlock-fence-hook",
        description=(
            "Interlock PreToolUse deny hook. Reads the hook event on stdin, "
            "evaluates it against the persisted per-role fence, and denies on "
            "stdout with exit 2. Fails closed on every error."
        ),
    )
    parser.add_argument("--fence", required=True, help="path to the persisted fence JSON")
    parser.add_argument("--role", default=None, help="role name, for diagnostics only")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        try:
            raw = sys.stdin.read()
        except Exception:  # pragma: no cover - stdin closed
            raw = ""
        try:
            event = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            event = {}
        if not isinstance(event, dict):
            event = {}
        decision, payload = decide_payload(Path(args.fence), event)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all
        # Anything at all, including a bug in this file, denies. An unhandled
        # traceback would exit 1, and A6 measured exit 1 being absorbed.
        decision = Decision(
            denied=True,
            rule_id=DENY_SELF_CHECK,
            layer="hook",
            reason=f"Interlock deny hook failed and denied by default: {exc!r}",
        )
        payload = _hook_output(decision)

    if not decision.denied:
        return EXIT_NO_OPINION
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    sys.stderr.write(decision.reason + "\n")
    sys.stderr.flush()
    return EXIT_DENY


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
