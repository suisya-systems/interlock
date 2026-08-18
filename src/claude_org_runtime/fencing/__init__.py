"""Per-role fencing -- gate item 3 (D-0023, D-0017).

Three things live here, and issue #9 asks for all three together because none
of them is a fence on its own:

1. **The renderer** (:mod:`claude_org_runtime.fencing.renderer`) -- the per-role
   permission / sandbox / hooks generator carried from
   ``settings/generator.py``, minus the transport and ``sandbox_by_pattern``
   axes the porting ledger discards.
2. **The fail-closed spawn precondition** (:mod:`claude_org_runtime.fencing.spawn`)
   -- Interlock refuses to spawn on a broken configuration and records the
   refusal durably. Never a downgraded spawn.
3. **The ``PreToolUse`` deny hook** (:mod:`claude_org_runtime.fencing.hook`) and
   the **breach-probe battery** (:mod:`claude_org_runtime.fencing.battery`),
   which is the *observable* D-0023 substitutes for item 3's equality check --
   one forbidden operation per rule, derived from the rendered fence so it
   cannot drift away from it.

**What this does not prove.** No public surface returns a session's effective
hooks or sandbox configuration (U3, i01 §3.9: ``system/init`` reports
``permissionMode``, ``tools`` and ``mcp_servers`` and nothing else relevant).
So the battery observes behaviour against *the fence Interlock rendered*, and
the rendered-input diff proves *what we wrote, not what the provider loaded*.
D-0023 records that substitution as a **deliberate weakening of item 3,
accepted by a human -- not an equivalent method**, and the gate record states
the residual in those terms.

Per D-0026 the implementations here are throwaway; ``tests/fencing/`` is the
durable output.
"""

from .battery import (
    BatteryReport,
    BreachProbe,
    ProbeResult,
    ProbeSynthesisError,
    probe_for,
    probes_for,
    run_battery,
)
from .renderer import (
    FenceContext,
    FenceRefusal,
    RefusalReason,
    load_document,
    render_fence,
    role_names,
)
from .rules import Decision, Fence, FenceRule, RuleSyntaxError
from .spawn import (
    EVENT_ADMITTED,
    EVENT_REFUSED,
    FenceLedger,
    FencedSpawner,
    SpawnOutcome,
    SpawnPlan,
    default_hook_script,
)
from .state import FenceDiff, FenceStateError, diff_fences, read_fence, write_fence

__all__ = [
    "BatteryReport",
    "BreachProbe",
    "Decision",
    "EVENT_ADMITTED",
    "EVENT_REFUSED",
    "Fence",
    "FenceContext",
    "FenceDiff",
    "FenceLedger",
    "FenceRefusal",
    "FenceRule",
    "FenceStateError",
    "FencedSpawner",
    "ProbeResult",
    "ProbeSynthesisError",
    "RefusalReason",
    "RuleSyntaxError",
    "SpawnOutcome",
    "SpawnPlan",
    "default_hook_script",
    "diff_fences",
    "load_document",
    "probe_for",
    "probes_for",
    "read_fence",
    "render_fence",
    "role_names",
    "run_battery",
    "write_fence",
]
