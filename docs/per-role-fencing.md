# Per-role fencing — the renderer, the deny hook, and the breach-probe battery

**Gate item:** 3 — "per-role permission / sandbox / hook configuration survives restart and fails
closed". **Decisions:** D-0023 (the observable and the fail-closed obligation), D-0017 (fail-closed
is Interlock's), D-0026 (durable tests, throwaway implementation), D-0027 (C2 is the provider).
**Issue:** `#9` (I-04 / S10). **Evidence:** `investigation/i04-pretooluse-fence-probe.md`,
`investigation/i01-supervisor-probe.md` §3.9, `investigation/pre-spawn-fence-search.md` §3.4.2.

---

## 1. What is here, and why all of it is needed at once

| piece | module | what it is |
|---|---|---|
| the per-role fencing renderer | `fencing/renderer.py` | carried from `settings/generator.py`, minus the discarded transport and `sandbox_by_pattern` axes |
| the fail-closed spawn precondition | `fencing/spawn.py` | validates the rendered configuration and **refuses to spawn** on a broken one, recording the refusal |
| the `PreToolUse` deny hook | `fencing/hook.py` | in-session enforcement, fail-closed in every direction it can fail |
| the breach-probe battery | `fencing/battery.py` | **one forbidden operation per rule**, derived from the rendered fence |
| restart persistence and the rendered-input diff | `fencing/state.py` | what an Interlock respawn reads back, and how it is compared |
| the effective-configuration readback | `fencing/readback.py` | `system/init` for permission mode, with i01 §3.9's normalisation rule |

None of the first four is a fence on its own. The renderer produces rules nobody enforces; the hook
enforces rules nobody checked; the battery observes a fence that may never have been passed to a
child; the precondition refuses configurations nobody probed. Item 3's predicate names permission,
sandbox *and* hooks as one object, and this is what treating them as one object costs.

## 2. What this proves, and what it does not — stated first, not last

**It does not prove what the provider loaded.** No public surface returns a session's effective
hooks or sandbox configuration. i01 §3.9 established what *is* public: `system/init` on
`--output-format stream-json` reports `permissionMode`, `tools` and `mcp_servers`, and has no
`hooks` key, no `sandbox` key and no `permissions` key. The rules are not reported — only the tool
list they produce.

So item 3's equality check is not runnable as written, and D-0023 substitutes a **behavioural
breach-probe battery plus a diff of Interlock's own rendered inputs**. In D-0023's own words, and
repeated in the gate record because it must not be softened in transit:

> This substitution is recorded as a **deliberate weakening of item 3, accepted by a human, not as
> an equivalent method.**

Diffing our own rendered inputs proves **what we wrote, not what the provider loaded**, and that gap
is exactly what item 3 exists to close. Probing every rule narrows it. It does not close it.

**What it does prove**, and each of these is asserted mechanically in `tests/fencing/`:

1. Every rule in every role's fence has a probe, and every probe is denied **by the rule it
   targets** — through the fence's own decision function and through the deny hook.
2. A broken configuration refuses the spawn, and the spawner callable is **never invoked**.
3. An Interlock-initiated respawn from persisted state denies every rule as it did before, and the
   rendered-input diff is identical.
4. The deny hook denies on every malformed input it can be handed, rather than shrugging.

## 3. The unit of the battery is the rule, and that is the whole point

D-0023 asks for "one forbidden operation per **rule** in the role's fence, not one per role".
Per-role probing leaves most rules unobserved: today's four roles carry 44 rules between them, so a
per-role battery would observe 4 of 44 and report success.

The battery is therefore **derived, never authored**. `battery.probes_for(fence)` walks
`fence.rules` and synthesizes one probe per rule *from the rule's own text*, and refuses to return a
battery whose synthesized operand the rule does not actually match. Coverage is then a set equality
against the rendered fence, not a promise:

```python
assert {p.rule_id for p in probes_for(fence)} == set(fence.rule_ids())
```

`tests/fencing/test_battery_coverage.py::test_coverage_grows_when_a_rule_is_added` is the assertion
that lasts: it adds a rule to a fence at runtime and requires the battery to grow with it. A
hand-written probe list would still cover today's fence and would fail that test — which is the
point, because a hand-maintained list drifts silently, and it drifts exactly when a new rule has
been added and nobody probed it.

## 4. The deny hook is proven to deny, not merely to run

A6 of `investigation/pre-spawn-fence-search.md` (U35) watched a hook exit **1** and be absorbed: the
CLI fell back to its default logic and the session completed normally.
`investigation/i04-pretooluse-fence-probe.md` reproduced the same absorption **on `PreToolUse`
itself**, and measured what does work:

| hook | outcome |
|---|---|
| JSON `permissionDecision: "deny"` + exit 2 | the operation did not happen |
| exit 2, stderr only, no JSON | the operation did not happen |
| **exit 1, no output** | **the operation happened** — absorbed |
| **unresolvable script via `bash` (exit 127)** | **the operation happened** — absorbed |
| unresolvable script via `python3` (exit 2) | the operation did not happen — *by accident of python3's exit code* |

**Every one of those nine cases exited 0.** Exit 0 is not evidence of anything —
`investigation/u1-session-id-bg-experiment.md` §5.2 records the same shape.

Three consequences, all wired into `fencing/hook.py`:

- The hook exits **2**, never 1, and emits both the `hookSpecificOutput` shape and the older
  `{"decision": "block"}` shape. Which one a given build honours is not something a fence should
  depend on knowing.
- Every failure path — unreadable fence, malformed event, unexpected exception — routes to the
  *same* deny. An unhandled traceback exits 1, which is the status measured being swallowed.
- **No test asserts the hook's exit code as evidence that the fence worked.**
  `tests/fencing/test_deny_hook.py` says so in its module docstring and confines the exit-status
  assertion to one class, whose only claim is that a deny never uses exit 1. Enforcement is
  measured in the investigation file, by whether the forbidden operation happened.

**The hook must survive being invoked the way it is actually invoked.** The rendered settings run
the hook **by path** (`python3 .../hook.py`), which means it executes with no parent package. A
first cut of this file used ordinary relative imports at module scope; they raised `ImportError`
and the process exited **1** — the absorbed status — so every shipped role would have run behind an
inert fence while the suite stayed green, because the tests invoked the hook as
`python -m claude_org_runtime.fencing.hook` instead of as the command the renderer emits. Two
things now hold that shut: the import is bootstrapped for the by-path case, and its failure is
routed to a literal deny rather than to the interpreter's error handling. And
`tests/fencing/test_deny_hook.py::TestTheRenderedCommandIsTheOneThatWorks` takes the command string
**out of the rendered settings** and runs it with the package deliberately not importable.

The general lesson is the one A6 already taught in a different costume: **a fence you test through
a convenient equivalent is not the fence you shipped.**

## 5. U15 — `PreToolUse` ordering under `bypassPermissions`

**Answered:** the hook fires and its `deny` is honoured under `bypassPermissions`. The mode does not
skip `PreToolUse` and does not demote its decision to advisory.

**And the renderer refuses `bypassPermissions` anyway** (`RefusalReason.PERMISSION_MODE_BYPASS`),
which is how the answer is reflected in how the fence is rendered. The reason is the table in §4:
under `bypassPermissions` the hook is the *only* remaining layer, and that layer has a measured
absorption mode which is silent, exits 0, and leaves no trace but the effect. A one-layer fence
whose one layer can vanish quietly is not a fence Interlock will spawn behind.

The claim is narrow and worth stating as such: U15's answer **removes** the reason to avoid
`bypassPermissions` (the hook does still fire) and **leaves** the reason to refuse it (nothing is
behind the hook).

## 6. Fail-closed is Interlock's own obligation

D-0023 part 2, under D-0017, "regardless of provider, so this work is not wasted under any `Q-0004`
outcome". The three broken configurations `#9` names each refuse the spawn:

| broken configuration | refusal reason |
|---|---|
| config deleted / role absent | `document-unreadable` / `role-absent` |
| hook path unresolvable | `hook-unresolvable` |
| sandbox profile absent | `sandbox-profile-absent` |

plus a fourth that is a self-check: a fence that renders cleanly but does not deny its own probes
refuses too (`battery-incomplete`). Shipping a fence Interlock cannot itself prove is the same class
of error as shipping no fence.

The property that carries the criterion is negative, and the tests assert it directly: **on a
refusal the spawner callable is not invoked** — not with a narrowed fence, not with a warning
logged; not invoked. A "best effort" renderer would hand the spawner a fence with the broken part
dropped, and that is the downgraded spawn the criterion forbids. Nothing is published either: a
fence file left behind by a refused spawn would be read by a hook on the next start and enforced as
though it had been approved.

Refusals are appended to a JSONL ledger and `fsync`ed before the caller is told anything. A refusal
lost on crash is a refusal that was not recorded, and the crash is exactly when it is wanted.

**Why hook paths are validated before the spawn rather than trusted to fail closed.** §4's last two
rows are the same missing script, differing only in launcher: `python3` exits 2 and blocks, `bash`
exits 127 and does not. Nothing about the missing script decided that. A missing hook cannot report
its own absence — it does not fail, it simply never runs — so the last moment it can be caught is
before the child is spawned.

**The launcher is checked too, not just the script.** A launcher that does not exist produces the
same exit 127 as a missing shell script, so validating only the `.py`/`.sh` token would leave the
identical hole one token to the left.

## 7. Restart, under C2

D-0027 removed the provider-supervisor restart path: under C2 nobody but Interlock can start a
worker, and `#8` — the issue that would have probed a supervisor-restart handle on Agent View — was
**closed as moot, not passed**. So "survives restart" reduces to Interlock respawning a `-p` child
from persisted state, and the criterion's own wording fixes the method: the battery denies every
rule after restart as it did before.

`tests/fencing/test_restart_preserves_fence.py` runs the battery on both sides and diffs the
rendered inputs, and it carries one test worth reading for its shape:
`test_a_rule_dropped_across_restart_shows_up_as_an_unprobed_gap`. A fence that comes back one rule
short still passes the battery — the battery can only probe the rules it was given. It is the
*diff* that catches the loss. That is why the criterion asks for both, and why neither alone is the
observable.

**This hole exists again the moment a provider with its own supervisor is adopted.** It stopped
existing when the provider changed; it was never proven closed.

## 8. What was carried, and what was left behind

`PORTING_LEDGER.md`'s row for `settings/generator.py` reads *carry (invariant) / rewrite
(mechanism)*, and names two things in that module that do **not** come across: the
`transport.descriptor` import (classified `discard`) and the A / B / C `sandbox_by_pattern`
machinery (discarded with the old worker layout by D-0014).

Both are **refused rather than ignored** (`RefusalReason.DISCARDED_AXIS`). A role document still
carrying a discarded axis was authored against the old contract, and rendering it while dropping the
axis silently produces a fence *narrower than its author believed* — a downgrade wearing the clothes
of a cleanup. `tests/fencing/test_renderer.py::TestDiscardedAxes` also asserts statically that no
module in the package names `transport.descriptor` at all, so the carry cannot re-acquire the
dependency by accident.

## 9. Residual, in D-0023's own terms

- No public surface returns a session's effective hooks or sandbox configuration (U3, U40). The
  battery observes behaviour against the fence Interlock **rendered**; the diff proves **what we
  wrote, not what the provider loaded**. **A deliberate weakening of item 3, accepted by a human —
  not an equivalent method.**
- The absorption modes measured so far are all exit-code-shaped. **U43 is open**: a hook that times
  out, or one writing malformed JSON at exit 0, was not probed, and the battery cannot claim to
  cover it.
- **U42 is new**: an unresolvable hook fails open or closed depending on the launcher's exit code.
  Handled by the spawn precondition; it is a property of the provider, not of Interlock, and could
  change under a new build.
- Every measurement here is **one machine, one CLI build (`2.1.234`), one load, one run per case**
  (U34). No case was repeated, so this is not a flakiness measurement.

Per D-0026 `src/claude_org_runtime/fencing/` is **throwaway spike quality**; `tests/fencing/` is the
durable output. Nothing here is promoted by being documented.
