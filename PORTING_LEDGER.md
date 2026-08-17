# Interlock — PORTING LEDGER

Per-path record of what Interlock **carries**, **rewrites**, or **discards** from its fork source.

- **Fork source:** `suisya-systems/claude-org-runtime` at commit
  [`befd3096110d18c928793d4862dba02e4da7ea22`](https://github.com/suisya-systems/claude-org-runtime/commit/befd3096110d18c928793d4862dba02e4da7ea22)
  (base release `v0.1.42`).
- **Authority for the classification rubric:** claude-org-ja Issue #740,
  [2026-08-17 — Interlock 分岐決定](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345),
  recorded in `DECISIONS.md` as **D-0014**.
- **Cross-referencing:** every row cites stable `D-` IDs from `DECISIONS.md`. Never cite this file
  by row order, table position, or line number.

---

## The headline constraint

> The initial seed selective port is roughly **12–15k LOC**, and a **parity rewrite of the existing
> codebase is not a goal** (D-0014).

That figure is an **estimate stated in the Issue**, not a measurement and not a budget derived from
one. LOC reduction is explicitly *not* a success metric (D-0014), and cutting an existing safety
fence purely to reduce LOC is a declared non-goal (D-0015).

This ledger is a **classification, not a commitment to port every `carry` and `rewrite` row in the
first seed.** See [Tally against the 12–15k target](#tally-against-the-1215k-target) — the
classified surface is roughly twice the seed estimate, and prioritisation within it is a separate,
still-open act.

---

## The three buckets (D-0014)

Reproduced in English from
[the 2026-08-17 Interlock 分岐決定 comment](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345).
These are the rubric the tables below apply.

### Carry — contracts, implementation and tests to be rescued first

- SQLite state semantics, single-writer, lease, outbox, resume conditions
- Secretary / Dispatcher / Worker / Curator responsibility boundaries and the human gate
- Permission / sandbox / hooks generation and validation, and breach probes
- Message identity, ack, and dedup invariants
- Accident-derived fixtures, fault injection, and recovery tests
- The Curator candidate → human approval → skill reflection contract

### Rewrite — re-derived from invariants, not from the old mechanism

- The Dispatcher Core state machine and reconcile loop
- The `SessionProvider` for Agent View
- Incident packet / assessment / action handlers
- Session↔run binding, capability probe, restart recovery
- The non-blocking Secretary intake / queue boundary

### Discard — never enters v2's permanent surface

- Backend contracts that assume tmux / pane layout, pane IDs, and send-keys
- Screen hash, spinner, screen regex, and other old-platform-specific observation mechanisms
- renga / herdr compatibility layers and permanent compatibility shims for the old backend
- The resident Dispatcher AI loop, and the bulk of the handover / resume prompt prose
- The A / B / C worker layout and bespoke worktree orchestration

And, governing all three:

> Old monitoring documents are treated as a **quarry, not a porting source**: take out only
> invariants, detection semantics, and accident-derived fixtures.

---

## Legend

| Class | Meaning |
|---|---|
| `carry` | The code or test moves forward substantially intact; its contract is preserved. |
| `rewrite` | The **need** survives and is re-derived from invariants; the old mechanism does not move. A `rewrite` row is not a promise that any line of the old file is reused. |
| `discard` | Nothing enters Interlock's permanent surface. The file may still be read as a quarry (D-0014) — that is not a port. |
| `carry (invariant) / rewrite (mechanism)` | Used mostly for tests: the **invariant the test pins** is carried, while the **mechanism it drives** is rewritten or discarded. The Issue explicitly wants accident-derived fixtures, fault injection, and recovery tests rescued even when the module under test is discarded, so forcing one label onto these rows would misrepresent them. |

**Quarry** is not a class. When a row's rationale says a lesson is "quarry material", the file is
still classified `discard` — the lesson may be extracted into a new design, the code may not.

---

## Scope of this scan

Walked, in the working tree at
`/home/happy_ryo/work/org/workers/interlock/.worktrees/interlock-founding-docs-20260818`
(the fork source tree at the base commit):

| Tree | Measured size |
|---|---|
| `src/` | 21,821 LOC of Python across 46 files |
| `tests/` | 27,860 LOC across 53 `.py` files, plus 4 fixture data files under `tests/fixtures/synthetic/` |
| `docs/` | 6 markdown files |

Enumeration was by `find` / `ls` / `wc -l`, with each file read or symbol-scanned
(`grep` over `class` / `def`, module docstrings, and pane / tmux / send-keys mention counts).
**Every path in the tables below was verified to exist**; no path is inferred.

Explicitly **not** covered, and therefore **unclassified**:

- Anything outside `src/`, `tests/`, and `docs/` — repository root files, packaging, CI
  configuration, and tooling. In particular `README.md` and `pyproject.toml` are untouched and
  unclassified; the rename question is recorded as `Q-0008` in `DECISIONS.md`, not settled here.
- Non-`.py` assets referenced from scanned code but not separately enumerated (for example the
  bundled JSON Schema files are covered only through their loader package row).
- The claude-org-ja repository, which is a **different repository** and therefore has no path rows
  here at all — see [Documents outside this tree](#documents-outside-this-tree).

Some rows below group several trivially small files (package `__init__.py` plumbing, bundled
resource directories) into one line, so the row count does not equal the file count above.
A grouped row is written exactly as the scan reported it.

---

## Ledger — `src/claude_org_runtime/broker/`

The broker cluster is where the ledger splits hardest: its **delivery core** is the single most
directly reusable thing in the tree, and everything wrapped around that core is pane machinery.

| Path | Class | Rationale | Decisions |
|---|---|---|---|
| `src/claude_org_runtime/broker/store.py` | carry (invariant) / rewrite (mechanism) | Queue store + append-only journal implementing `UNDELIVERED → CLAIMED → DELIVERED` with lease-based claim/reap, mode-epoch fencing, idempotent `DELIVERED`, and lock-outside-I/O journal discipline. No `PaneId` / send-keys / screen coupling found; the **delivery invariants** are the Carry bucket's "message identity, ack, dedup invariants" and "lease, outbox, resume conditions" almost line for line. But the **storage mechanism cannot be carried**: the authoritative `_rows` is in memory and the file states outright that the journal is not replayed to reconstruct state ("`_rows` は in-memory・journal replay で再構築しない (crash recovery なし)"). A daemon kill therefore loses queue state, which contradicts D-0001 and would fail the outbox/resume fault-injection gate. Override of the mechanical survey's flat `carry` — the invariants port, the store is re-derived on SQLite. | D-0001, D-0009, D-0014 |
| `src/claude_org_runtime/broker/sidecar.py` | carry (invariant) / rewrite (mechanism) | Carries two invariants: non-secret discovery metadata (`daemon.json`) is separated from the `0600` secret (`admin.token`), and journal verification is **run-sliced by offset** so a prior run's lines cannot produce a false positive. The concrete daemon-file shape is superseded once SQLite is the source of truth. | D-0001, D-0014 |
| `src/claude_org_runtime/broker/residents.py` | carry (invariant) / rewrite (mechanism) | Carries process-identity invariants worth keeping: PID reuse defeated by kernel start time, ownership established by repo fingerprint, **fail closed on an unknown platform**, and observation injected as a seam rather than by patching globals — recovery/fault-injection material. The file-registry mechanism predates SQLite-of-record and is re-derived. Override of the mechanical survey's flat `carry`: the registry file cannot survive D-0001 unchanged. | D-0001, D-0014, D-0017 |
| `src/claude_org_runtime/broker/channel_sidecar.py` | rewrite | Push delivery into a live session is a real Interlock need — `MessageBus` must deliver and re-send independently of the session UI — but the mechanism here is a bespoke hand-rolled protocol for keeping one interactive Claude child alive and idle-woken. Re-derived against `MessageBus` + `SessionProvider`; the at-least-once/lease semantics it consumes are already carried at `store.py`. | D-0009, D-0014 |
| `src/claude_org_runtime/broker/notify.py` | rewrite | A small external-process bridge into the queue with a frozen, best-effort, non-throwing exit-code contract. No pane coupling. The *shape* — an outside caller causes an effect only through an explicit, auditable enqueue — is the handler boundary Interlock needs; the daemon/token-mint plumbing beneath it is not. | D-0004, D-0009, D-0014 |
| `src/claude_org_runtime/broker/rpc.py` | rewrite | Backend-neutral localhost HTTP client. Its useful idea is that health is proven by a **round trip through the real protocol**, not by PID liveness — directly relevant to the capability/version probe. Call shapes are new. | D-0010, D-0014 |
| `src/claude_org_runtime/broker/server.py` | discard | The orchestrator is wired to terminal-adapter pane nudge delivery (`NUDGE_TEXT`, `PANE_LIVE_*`, send-keys-style injection) and pane/session lifecycle — the Discard bucket's backend contract assuming pane layout. Its documented single-`Lock` concurrency contract and "issue `DELETE` outside the lock to avoid re-entrant deadlock" discipline are quarry material for the rewritten store, not a port. Override of the survey's `rewrite`: the MessageBus-relevant parts live in `store.py`, which is carried separately, leaving this file as pane machinery. | D-0009, D-0014 |
| `src/claude_org_runtime/broker/surface.py` | discard | The pane-spawn/pane-control MCP surface (`spawn_claude_pane`, `spawn_codex_pane`, `set_pane_identity`, `list_panes`) plus renga-compatible argv builders. Quarry only: the separation of a fixed `auth_role` from a mutable display role, which prevents privilege escalation by renaming, deserves restating as a decision rather than porting as code — and the Dispatcher AI's own tier is still open (`Q-0007`). | D-0009, D-0014, D-0015 |
| `src/claude_org_runtime/broker/tokens.py` | discard | `AgentBind` is defined against `PaneId` and mixed into the pane-bound `Broker` state; the identity model itself is what Interlock replaces. Same `auth_role`-vs-display-role quarry note as `surface.py`. | D-0009, D-0014 |
| `src/claude_org_runtime/broker/launcher.py` | discard | `org up` / `org down` built around spawning an interactive TUI pane, backend detection, argv construction for that pane, and closing panes on teardown — pane/session bootstrap and A/B/C-style role layout. The health-probe-by-real-round-trip pattern is quarry, already noted at `rpc.py`. | D-0009, D-0010, D-0014 |
| `src/claude_org_runtime/broker/cli.py` | discard | The daemon CLI's substance is terminal-backend selection (`VALID_BACKENDS`, `make_adapter`, `default_backend`) for pane-based nudge delivery — the discarded backend-selection contract. Override of the survey's `rewrite`: what is reusable is the queue-store bootstrap idea, and that travels with `store.py`. | D-0009, D-0014 |
| `src/claude_org_runtime/broker/placement.py` | discard | Rect-based balanced pane-split geometry; the module's own docstring already marks it superseded and renga-only. | D-0014 |
| `src/claude_org_runtime/broker/__init__.py` | discard | Re-export shim whose own docstring frames the package as the pane-control + messaging MCP surface. | D-0014 |
| `src/claude_org_runtime/broker/__main__.py` | discard | `python -m …broker` entry shim for the discarded pane daemon. Also appears inside the grouped package-plumbing row below. | D-0014 |

## Ledger — `src/claude_org_runtime/dispatcher/`

| Path | Class | Rationale | Decisions |
|---|---|---|---|
| `src/claude_org_runtime/dispatcher/runner.py` | rewrite | Direct predecessor of the Dispatcher Core state machine, which the Rewrite bucket names explicitly — so `rewrite`, overriding the survey's `discard`. **The override is about the role, not the code:** essentially none of this file survives. Its computation is renga rect/pane/tab geometry (balanced split, `TabPlacement`, sidebar-width arithmetic, pane-id parsing) and it embodies the pattern where a human-driven Dispatcher actor executes the plan through `spawn_claude_pane` / `send_keys`, which the on-demand assessment-only Dispatcher AI replaces. What carries forward is the *separation itself* — a deterministic component computes, a separate privileged component executes. | D-0002, D-0003, D-0004, D-0009, D-0010, D-0014 |
| `src/claude_org_runtime/dispatcher/__init__.py` | discard | Lazy-import shim fronting `runner.py`; no independent content. | D-0014 |

## Ledger — `src/claude_org_runtime/terminal/`

The entire cluster is the Discard bucket's named subject: pane-ID-addressed backends, send-keys,
screen scraping, and renga/herdr compatibility. Nothing here is classified anything but `discard`.
Note the distinction drawn in D-0008: screen hash and spinner remain legitimate *watcher signals*;
what is discarded is binding them to a permanent backend contract.

| Path | Class | Rationale | Decisions |
|---|---|---|---|
| `src/claude_org_runtime/terminal/base.py` | discard | Defines the `TerminalAdapter` Protocol itself around `pane_id` / spawn / send-keys, plus `classify_pane_state`, which infers busy/idle by parsing raw TUI screen text — the old-platform-specific observation mechanism named for discard. Its venv/login-shell wrappers exist only to build spawn argv. | D-0009, D-0010, D-0014 |
| `src/claude_org_runtime/terminal/tmux.py` | discard | tmux driven by `new-session` / `send-keys` / `capture-pane` against pane IDs — the canonical example of the discarded backend contract. | D-0009, D-0014 |
| `src/claude_org_runtime/terminal/wezterm.py` | discard | Pane-ID-addressed WezTerm backend (`spawn`, `send-text`, `get-text`, `kill-pane`) with bracketed-paste workarounds. | D-0009, D-0014 |
| `src/claude_org_runtime/terminal/herdr.py` | discard | The largest single file in the cluster: a compatibility layer for the renga/herdr daemon with protocol-version shims and workspace/space/pane lifecycle. Precisely "renga/herdr compatibility layers and permanent shims". | D-0009, D-0014 |
| `src/claude_org_runtime/terminal/keys.py` | discard | Canonical raw-key vocabulary existing only to feed `send_named_keys` into a pane. | D-0014 |
| `src/claude_org_runtime/terminal/__init__.py` | discard | Re-export surface for the pane-adapter subpackage; its docstring frames the package as backend CLI invocation. | D-0014 |

## Ledger — `src/claude_org_runtime/attention/`

The old attention watcher is the closest thing in the tree to Interlock's deterministic watcher, so
this cluster is mostly `rewrite`: the need survives, the vocabulary does not. Its classification
table is keyed on an ad-hoc anomaly vocabulary (`pane_silent`, `pane_crashed`, `worker_stalled`,
`relay_gap_suspected`, …) that the closed fact-state set replaces.

| Path | Class | Rationale | Decisions |
|---|---|---|---|
| `src/claude_org_runtime/attention/dedup.py` | carry | Two dedup namespaces with different semantics — record-once-forever for events, cooldown-gated for pending decisions — plus corruption-tolerant load ("a broken state file recovers as empty"). This is the Carry bucket's dedup invariant, and it is what the incident dedup key must preserve, even though the JSON sidecar is replaced by SQLite. The key's actual composition remains open (`Q-0002`). | D-0001, D-0007, D-0009, D-0014 |
| `src/claude_org_runtime/attention/classifier.py` | carry (invariant) / rewrite (mechanism) | Carries the invariant that classification is a **pure, I/O-free function over persisted rows**, which is what makes detection fixture-testable and deterministically replayable. The mechanism — the severity table keyed on the old anomaly vocabulary — is rewritten onto the closed fact-state set and the incident packet. Rescuing it is the "detection semantics" the quarry rule permits. | D-0005, D-0006, D-0007, D-0014 |
| `src/claude_org_runtime/attention/readers.py` | rewrite | The graceful-degradation invariant is worth preserving — a missing file, absent table, or corrupt DB yields an empty result and never crashes the watcher, which is exactly how `OBSERVATION_UNAVAILABLE` must behave rather than escalating. The schemas read here (`events` table, `pending_decisions.json`, the broker `queue.jsonl` tail) all predate the SQLite SoT tables. | D-0001, D-0006, D-0014 |
| `src/claude_org_runtime/attention/cli.py` | rewrite | The operator-facing scan/watch entry point. In Interlock this role splits across the Dispatcher Core reconcile loop and Secretary intake, so the classify→dedup→notify poll loop is re-derived rather than rewired. | D-0002, D-0008, D-0016, D-0014 |
| `src/claude_org_runtime/attention/config.py` | rewrite | Config loading and validation is reusable as a pattern, but its knobs (`DEFAULT_NOTIFY` severity per old anomaly kind, cooldowns shaped for the polling watcher) are keyed to the replaced vocabulary. Note that the intervals themselves are not settled — see `Q-0003`. | D-0005, D-0007, D-0014 |
| `src/claude_org_runtime/attention/notify.py` | rewrite | Human-facing delivery survives as a need but moves behind the Secretary / human gate and the action handler; template rendering with unknown-placeholder fallback and truncation are the reusable parts. | D-0004, D-0007, D-0016, D-0014 |
| `src/claude_org_runtime/attention/__init__.py` | rewrite | Re-export surface whose public vocabulary mirrors the old event kinds; rebuilt around the incident/assessment contract. | D-0005, D-0007, D-0014 |
| `src/claude_org_runtime/attention/platform.py` | discard | A self-contained OS notification-backend probe (osascript / notify-send / wsl-notify-send / PowerShell). Overrides the survey's `carry`: the code is clean, but the Issue gives **no basis** for a desktop-notification backend in Interlock's three-layer design, and carrying it would be inventing a delivery channel the source does not decide. Reinstating one is a new decision, not a port. | D-0004, D-0008, D-0016, D-0014 |

## Ledger — platform, settings, schema, transport, prompts

| Path | Class | Rationale | Decisions |
|---|---|---|---|
| `src/claude_org_runtime/settings/generator.py` | carry | Schema-driven generation of per-role `settings.local.json` (permissions, sandbox, hooks). Named verbatim in the Carry bucket and the machinery behind per-role fencing. | D-0014, D-0017 |
| `src/claude_org_runtime/settings/sandbox_doctor.py` | carry | Detects symlink-crossing deny paths that silently defeat bubblewrap sandboxing — a breach probe, which the Carry bucket names alongside generation and validation. | D-0014, D-0017 |
| `src/claude_org_runtime/schema/enums.py` | carry | String-mixin enums with canonical-name normalisation (`task_id` vs `worker`, `pane_id` vs `pane_name`) — SQLite state semantics and canonical identifiers, which the Carry bucket lists first. The `pane_*` names are legacy-compat identifiers to fold, not pane control logic. | D-0001, D-0014 |
| `src/claude_org_runtime/schema/journal_event.py` | rewrite | Frozen-dataclass record with lossless round-trip of unknown fields — good typing discipline for a durable record, but the JSONL journal format is superseded by SQLite tables. Concrete DDL remains open (`Q-0001`). | D-0001, D-0014 |
| `src/claude_org_runtime/migrate/v1_to_v2.py` | rewrite | Field-normalisation logic is the nearest existing reference for the migration/comparison bridge, which is permitted only for migration and comparison and must never become a permanent API. Its target shape is still file/JSONL-based, so it is re-derived. | D-0013, D-0014 |
| `src/claude_org_runtime/cli.py` | rewrite | Top-level CLI scaffolding; reusable in shape, but almost every subcommand it dispatches to is itself `rewrite` or `discard`. | D-0014 |
| `src/claude_org_runtime/{schema,settings,migrate,transport}/__init__.py` (subpackage-contract docstrings) | rewrite | Grouped row, narrowed to the subpackages that have **no** individual verdict elsewhere in this ledger. Each states a subpackage boundary and dependency-direction contract worth re-authoring for Interlock's package layout; the subpackages themselves are being restructured. `broker/__init__.py` and `dispatcher/__init__.py` are classified `discard` in their own cluster tables, and `attention/__init__.py` `rewrite` in its; those rows are the single authority for those three paths and they are **not** counted here. | D-0014 |
| `src/claude_org_runtime/schema/org_state.py` | discard | Parses the `org-state.md` Worker Directory Registry markdown table — a file-based registry that the SQLite source of truth replaces outright rather than reads. | D-0001, D-0014 |
| `src/claude_org_runtime/schema/json_schema/` (dir: `__init__.py` + 3 `.schema.json` files) | discard | Bundled schemas describing the legacy JSONL / queue wire formats, superseded by the SQLite tables and the incident contract. Field vocabulary is quarry for new schema design. | D-0001, D-0007, D-0014 |
| `src/claude_org_runtime/transport/descriptor.py` | discard | Per-transport MCP tool-prefix and spawn-injection descriptors built around renga's pane-control surface and the broker's mirror of it — a backend compatibility shim. Per-role allowlist derivation is conceptually reusable and is quarry. | D-0009, D-0014 |
| `src/claude_org_runtime/prompts/` (dir: `__init__.py` + `templates/{dispatcher,secretary,curator}.md`) | discard | Reference role-prompt prose for the always-on role loop. The Discard bucket names the bulk of handover/resume prompt prose specifically; what Interlock needs instead is the incident packet and assessment schema. | D-0002, D-0003, D-0007, D-0014 |
| `src/claude_org_runtime/{__init__.py, __about__.py, broker/__main__.py}` (package-level plumbing) | discard | Grouped row as reported by the scan: top-level package `__init__`, version string, and the broker `-m` entry shim. Trivial plumbing with no design content. (`broker/__main__.py` also appears in the broker table above.) | D-0014 |

## Ledger — tests

The Issue asks specifically for accident-derived fixtures, fault injection, and recovery tests to be
rescued (D-0014). Several tests therefore carry an invariant even though the module they drive is
discarded — those rows use the hybrid class, and the "(mechanism)" half means the test must be
re-targeted at whatever contract Interlock defines.

### `tests/broker/`

| Path | Class | Rationale | Decisions |
|---|---|---|---|
| `tests/broker/test_delivery.py` | carry (invariant) / rewrite (mechanism) | The largest and most directly relevant test file in the tree: claim-then-confirm state machine, claim-respecting drain, **lease-reap recovery**, **mode-epoch fencing against stale confirms**, claim-issuance gating, and delivery-scoped credential isolation. No pane coupling found. It is simultaneously the message-identity/ack/dedup invariant set and the fault-injection matrix the acceptance gate names. Mechanism half: re-target from `store.py`'s API to the `MessageBus` contract. | D-0001, D-0009, D-0014 |
| `tests/broker/test_store.py` | carry (invariant) / rewrite (mechanism) | Delivery-lifecycle assertions (registered-binds-only targets, at-most-once drain, single-flight guard) are carried; the nudge-worker assertions that use a fake terminal adapter are dropped with pane nudging. | D-0009, D-0014 |
| `tests/broker/test_control_plane.py` | carry (invariant) / rewrite (mechanism) | Exercises discovery-metadata/secret separation and offset-scoped journal verification with no pane coupling — resume-condition and verification-probe material. Mechanism half follows `sidecar.py`. | D-0001, D-0014 |
| `tests/broker/test_residents.py` | carry (invariant) / rewrite (mechanism) | Process-identity fault injection through injected platform seams, with no real process kills and no patched globals. Mirrors the `residents.py` classification. | D-0001, D-0014, D-0017 |
| `tests/broker/test_notify.py` | rewrite | Validates the best-effort, always-non-throwing external-bridge contract (and ASCII-only help). Useful as a reference for the rewritten handler boundary; targets discarded daemon plumbing. | D-0004, D-0014 |
| `tests/broker/test_launcher.py` | discard | Centres on spawning or reusing a real interactive TUI pane and closing broker panes on teardown. The up/down health-probe-by-real-round-trip pattern is quarry, already noted at `rpc.py`. | D-0009, D-0010, D-0014 |
| `tests/broker/test_server.py` | discard | Exercises the full pane-oriented MCP surface. Sub-scenarios such as auth rejection, unknown-method handling, and session revocation are generically good test shapes and are quarry, but they are written against the discarded tool catalogue. | D-0009, D-0014 |
| `tests/broker/test_surface.py` | discard | Tests the pane-spawn argv builders and renga-compatible tool catalogue. Its tier-gating-by-`auth_role` pattern is quarry for role-boundary tests in the rewritten layer. | D-0008, D-0009, D-0014 |
| `tests/broker/test_cli.py` | discard | Exercises terminal-backend selection and pane-daemon serve wiring. | D-0009, D-0014 |
| `tests/broker/test_bootstrap_folder_trust.py` | discard | Pins that a folder-trust prompt on a spawned pane can only be dismissed via `send_keys(enter=true)` — a pane-specific bootstrap ritual. | D-0009, D-0014 |
| `tests/broker/test_nudge_misroute.py` | discard | Regression for nudges racing an ambient renga MCP server of the same tool name — a renga backend-compatibility concern with no target in Interlock. | D-0009, D-0014 |
| `tests/broker/test_channel_sidecar.py` | discard | Tests the bespoke push-into-a-live-child-session sidecar. The *need* is re-derived under `channel_sidecar.py`'s `rewrite`, but no assertion here is separable from the hand-rolled protocol. | D-0009, D-0014 |
| `tests/broker/test_channel_sentat_drop.py` | discard | Regression for a silent drop caused by a numeric `sent_at` breaking the host channel's schema validation. The general lesson — a serialisation type mismatch can drop messages **silently**, so schema conformance must be asserted at the boundary — is quarry for the new outbox tests; the fixture is bound to the discarded channel payload shape. | D-0009, D-0014 |
| `tests/broker/test_schema.py` | discard | Validates the bundled queue-event schema against real journal lines, including an intentional timestamp-type divergence. Overrides the survey's split verdict toward `discard`: the schema under test is a legacy JSONL wire format. Schema-drift detection as a *technique* is quarry for the SQLite-era contracts. | D-0001, D-0007, D-0014 |
| `tests/broker/test_placement.py` | discard | Pins the import contract for the deprecated rect/pane-split wrapper. | D-0014 |
| `tests/broker/test_space_layout.py` | discard | Workspace/space layout mapping and per-backend capability branching on spawn. | D-0009, D-0014 |
| `tests/broker/conftest.py` | discard | Fixture scaffolding purpose-built for the pane-based Broker and its MCP-over-HTTP surface. | D-0009, D-0014 |
| `tests/broker/__init__.py` | discard | Empty package marker. | D-0014 |

### `tests/attention/`

| Path | Class | Rationale | Decisions |
|---|---|---|---|
| `tests/attention/test_dedup.py` | carry | Directly pins the once-ever vs cooldown-gated dedup invariants and corruption recovery — a Carry-bucket invariant that outlives the move from a JSON sidecar to SQLite. | D-0001, D-0007, D-0009, D-0014 |
| `tests/attention/test_broker_journal_contract.py` | carry (invariant) / rewrite (mechanism) | Drives a real broker into a double-claimer condition and asserts the downstream consumer actually surfaces the resulting journal line. Overrides the survey's `discard`: this is an **accident-derived fixture** in the exact sense the Carry bucket means — it encodes the real regression that detection existed with no consumer, and it is the one test here that pins a producer↔consumer contract end to end rather than each side in isolation. Field names change; the discipline is carried to the new outbox/incident path. | D-0007, D-0009, D-0014 |
| `tests/attention/test_classifier.py` | carry (invariant) / rewrite (mechanism) | Exhaustive table-driven coverage of the classification mapping. The invariant carried is that **every** fact-vocabulary row has a pinned expectation; the mechanism is rewritten onto the closed fact-state set, including the fact/anomaly separation. | D-0005, D-0006, D-0014 |
| `tests/attention/test_readers.py` | rewrite | Missing-file / corrupt-DB graceful-degradation assertions and bounded tail-scan windowing are worth preserving as behavioural specs, against the new tables. | D-0001, D-0006, D-0014 |
| `tests/attention/test_cli.py` | rewrite | End-to-end coverage of the classify→dedup→notify loop, including dedup-state recovery; the pipeline under test is being re-derived. | D-0002, D-0003, D-0008, D-0014 |
| `tests/attention/test_config.py` | rewrite | Defaults, severity table, template loading, and placeholder-allowlist enforcement; follows `config.py`. | D-0005, D-0007, D-0014 |
| `tests/attention/test_notify.py` | rewrite | Dry-run (no subprocess) behaviour, backend fallback, template override and truncation; follows `notify.py` to the human-gate boundary. | D-0004, D-0016, D-0014 |
| `tests/attention/conftest.py` | rewrite | The fake-SQLite-DB fixture factory is a good pattern to keep, rebuilt against the incident/assessment schema. | D-0001, D-0007, D-0014 |
| `tests/attention/test_platform.py` | discard | Follows `platform.py`. The injected-stub technique is quarry; the module it covers has no home in the new design. | D-0014, D-0016 |
| `tests/attention/__init__.py` | discard | Empty package marker. | D-0014 |

### `tests/` (root) and other test packages

| Path | Class | Rationale | Decisions |
|---|---|---|---|
| `tests/test_settings_generator.py` | carry | Verification tests for a Carry module: permission / sandbox / hooks rendering per role. | D-0014, D-0017 |
| `tests/test_sandbox_symlink_deny.py` | carry | Breach-probe tests for `sandbox_doctor.py`, named by the Carry bucket. | D-0014, D-0017 |
| `tests/scrub/scrub_fixture.py` | carry | Deterministic PII/secret scrubber used to promote real `.state/` snapshots into fixtures — the pipeline that makes accident-derived fixtures publishable at all, so it carries with them. | D-0007, D-0014 |
| `tests/scrub/test_scrub.py` | carry | Verification tests for the redaction rules of a Carry-classed tool. | D-0007, D-0014 |
| `tests/test_migrate.py` | rewrite | Fixture-driven checks of v1→v2 key normalisation; re-authored against whatever migration/comparison bridge the run-boundary cutover needs. | D-0013, D-0014 |
| `tests/test_smoke.py` | rewrite | A five-line import check: re-created for the new package rather than literally carried. | D-0014 |
| `tests/fixtures/synthetic/` (`journal_v1_sample.jsonl`, `org_state_v1_sample.md`, `scrub_input_sample.jsonl`, `expected_output.jsonl`) | rewrite | Useful as reference shapes for v1 data during bridge/comparison work, but the formats are v1-specific; SQLite-shaped fixtures will be needed alongside them. | D-0013, D-0014 |
| `tests/test_dispatcher_runner.py` | discard | Imports `runner.py`'s pane-geometry internals directly. Its non-geometry assertions (name/cwd validation, outbox write semantics) are quarry for the rewritten Dispatcher Core, but none were found cleanly separable from the geometry assertions. Overrides the survey's split verdict toward `discard` for that reason. | D-0009, D-0010, D-0014 |
| `tests/test_dispatcher_multitab_geometry.py` | discard | Pins renga sidebar/rect arithmetic (for example that pane rects already exclude sidebar width and must not be subtracted twice) against a discarded module. | D-0014 |
| `tests/test_dispatcher_multitab_placement.py` | discard | Tab placement, overflow, plan shape and CLI regressions for renga multi-tab spawn targeting. Its fail-closed-when-a-capability-is-not-asserted pattern is philosophically the same instinct as the CLI capability probe and is quarry — but it is written in renga selectors, not the public CLI. | D-0010, D-0014 |
| `tests/test_dispatcher_multitab_population.py` | discard | Peer/pane population counting and `list_peers`-vs-`list_panes` id-space reconciliation, tied one-to-one to discarded types. | D-0009, D-0014 |
| `tests/test_dispatcher_multitab_portability.py` | discard | AST-scans the sibling dispatcher test sources for hardcoded POSIX-only paths. The technique is reusable and can simply be re-derived, but every file it polices is discarded, and the Issue gives no basis for calling it carry-worthy on its own. | D-0014 |
| `tests/test_schema.py` | discard | Primarily exercises the `org-state.md` markdown-table parser, itself discarded. | D-0001, D-0014 |
| `tests/test_prompts.py` | discard | Tests loading of the discarded bundled role-prompt prose. | D-0002, D-0014 |
| `tests/transport/test_descriptor.py` | discard | Backend-specific coverage of per-role allowlist derivation across renga/broker transports. | D-0009, D-0014 |
| `tests/terminal/test_herdr.py` | discard | The largest test file in the terminal cluster, covering the renga/herdr compatibility backend across protocol versions. Its size is itself a useful datum about how much of the tree is backend shim. | D-0009, D-0014 |
| `tests/terminal/test_wezterm.py` | discard | Backend-specific coverage of the WezTerm pane adapter. | D-0009, D-0014 |
| `tests/terminal/test_tmux.py` | discard | Backend-specific coverage of the tmux pane adapter. | D-0009, D-0014 |
| `tests/terminal/test_base.py` | discard | Covers the screen-scrape state classifier, the polling wait loop, and the adapter Protocol shape — the discarded observation mechanism. | D-0009, D-0014 |
| `tests/terminal/test_keys.py` | discard | Covers key normalisation that exists only to serve send-keys. | D-0014 |
| `tests/terminal/test_venv.py` | discard | Covers argv/env wrapper machinery whose only consumer is pane spawn. | D-0014 |
| `tests/terminal/conftest.py` | discard | Fixture scaffolding exclusive to the discarded terminal adapters. | D-0014 |
| `tests/terminal/__init__.py` | discard | Empty package marker. | D-0014 |
| `tests/__init__.py`, `tests/scrub/__init__.py`, `tests/transport/__init__.py` | discard | Empty package markers (0 lines each), listed for completeness so that every one of the 53 `.py` files under `tests/` has a verdict. Re-created as needed by the new package layout rather than ported. | D-0014 |

## Ledger — `docs/`

Documents are classified by whether their **content** is an invariant Interlock still needs, not by
whether the prose is good. Line counts here are markdown lines and are excluded from the LOC tally.

| Path | Class | Rationale | Decisions |
|---|---|---|---|
| `docs/channel-delivery-model-decision.md` | carry (invariant) / rewrite (mechanism) | A living decision record for owner-scoped exclusive claim versus broadcast-plus-dedupe, including the history of two reversals. The invariant and — more valuable — the record of *why* the alternatives failed are direct input to the `MessageBus` design. Reversal history is exactly what the quarry rule is for. Its concrete channel mechanism does not move. | D-0009, D-0014 |
| `docs/scrub-policy.md` | carry | The policy backing the Carry-classed scrubber; keeping the tool without its policy would strand the accident-derived fixture pipeline. | D-0007, D-0014 |
| `docs/broker-residents-registry-contract.md` | rewrite | The crash-recovery registry contract (registrant writes, runtime scans and reaps) remains relevant to resume semantics, but is re-derived for the SQLite-of-record model rather than a file registry. | D-0001, D-0014 |
| `docs/cli.md` | discard | Reference documentation for a CLI surface whose subcommands are almost entirely `rewrite` or `discard`; documentation is a quarry, not a porting source. | D-0014 |
| `docs/broker-bootstrap-folder-trust-approval.md` | discard | Investigation of machine-approving a folder-trust prompt on a freshly spawned pane — pane/terminal bootstrap UX for the old backend. | D-0009, D-0014 |
| `docs/broker-bootstrap-stage1-folder-trust-design.md` | discard | Same pane-spawn bootstrap scope as its sibling. | D-0009, D-0014 |

---

## Documents outside this tree

The 803-line worker-monitoring document and the dispatcher prose both live in the **claude-org-ja
repository**, not in this tree. They therefore have **no path row in this ledger** — a ledger row
asserts a path that exists in the fork source, and inventing one for another repository would be a
fabrication.

Their status is nonetheless settled: they are a **quarry, not a porting source** (D-0014, and
[the 2026-07-20 guidance](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5017340960)).
Only invariants, detection semantics, and accident-derived fixtures may be taken out of them.
Faithful transcription would reproduce the workarounds of the old platform, since the prose is an
accurate blueprint of a building Interlock is not constructing. The same reasoning underlies
D-0012: v1 remains the maintenance line and is not converged with.

---

## Tally against the 12–15k target

Approximate LOC in rows classified `carry`, `rewrite`, or hybrid. Sums use the scan's per-file
counts; grouped rows contribute their grouped totals. Markdown documents are excluded. Every path is
counted **once**: where a path appears in both a cluster table and a grouped row, the cluster row is
the authority and the grouped row excludes it.

| Cluster | carry + rewrite LOC (approx.) |
|---|---|
| `src/` broker | ~3,360 |
| `src/` dispatcher | ~3,471 |
| `src/` attention | ~2,404 |
| `src/` platform / settings / schema / migrate / CLI | ~3,332 |
| **`src/` subtotal** | **~12,567** |
| `tests/broker` | ~4,576 |
| `tests/attention` | ~3,759 |
| `tests/` root, `tests/scrub`, `tests/fixtures` | ~4,648 (4,610 LOC of Python + 38 lines of fixture data) |
| **`tests/` subtotal** | **~12,983** |
| **Total classified carry + rewrite** | **~25,550** |

**This overshoots the 12–15k seed estimate by roughly a factor of two, and that should be said
plainly rather than smoothed over.** Three things follow.

1. **The ledger is a classification, not a commitment.** A `carry` or `rewrite` row states that the
   path's contract or need survives into Interlock. It does not state that the path is in the first
   seed. Selecting the seed from these rows is a separate act that has not happened.
2. **A `rewrite` row costs far less than its source LOC.** By definition a rewrite re-derives from
   invariants and does not reuse the mechanism — `dispatcher/runner.py` alone contributes ~3,471 LOC
   to the tally while contributing approximately zero lines of ported code. Reading the tally as a
   port budget would badly overstate it.
3. **The `src` subtotal landing inside 12–15k is a coincidence, not a plan.** It is recorded here to
   forestall the misreading that the coincidence constitutes a decision. The Issue's figure is an
   estimate about the seed, and no split between `src` and `tests` is decided anywhere.

Since parity rewrite is explicitly not a goal (D-0014) and LOC reduction is explicitly not a success
metric, the correct response to the overshoot is prioritisation within the `carry` rows, not
reclassification to make an arithmetic target come out.

---

## How to use this ledger

Every implementation Issue must state three fields, per
[the 2026-08-17 Interlock 分岐決定 comment](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345):

| Field | Meaning | Where the ledger feeds it |
|---|---|---|
| **Preserves** | The decisions and invariants the change keeps intact | The `carry` rows and the `(invariant)` half of hybrid rows it touches, cited by path and by `D-` ID |
| **Changes** | The contracts the change modifies or retires | The `rewrite` and `discard` rows it acts on, and the `(mechanism)` half of hybrid rows |
| **Proof** | Tests, fixtures, and dogfood evidence | The carried tests and accident-derived fixtures listed above; the gate items in `ACCEPTANCE.md` |

Working rules:

- **Cite paths and `D-` IDs, never row positions.** Rows may be reordered or split; IDs and paths
  are stable.
- **Amend, do not silently reclassify.** If implementation shows a `rewrite` row should have been
  `carry` (or the reverse), change the row *and* say why in the Issue that changed it. A
  reclassification that contradicts a `D-` entry needs a new decision, not a table edit.
- **A `discard` row is not a licence to delete carelessly.** Discard means the path does not enter
  Interlock's permanent surface. Reading it as a quarry is expected and encouraged; reintroducing
  its mechanism is not, and cutting a safety fence it implements purely to reduce LOC is a declared
  non-goal (D-0015).
- **New upstream material needs a new row.** Under D-0011 there are no periodic upstream merges;
  individual security fixes are taken in with recorded rationale, and this ledger is the natural
  home for that record.

---

## Open questions arising from this ledger

There is exactly **one** registry of open questions, and it is the `Open questions` section of
[`DECISIONS.md`](./DECISIONS.md), as `Q-00NN` entries with `Status: proposed`. This ledger does not
define a second namespace. The questions raised by the scan are recorded there and are listed below
only as pointers; the full text, rationale, and resolution condition of each live in `DECISIONS.md`.

| Question | What it asks |
|---|---|
| `Q-0014` | Which subset of the classified rows is the initial seed, and in what order. The `carry` + `rewrite` rows total roughly twice the 12–15k estimate. |
| `Q-0015` | Whether carried tests port before, with, or after the module they cover — most acute for the hybrid rows. |
| `Q-0016` | Which quarry lessons from `discard` rows become `D-` decisions: `auth_role`-vs-display-role separation (`broker/surface.py`, `broker/tokens.py`), re-entrant-lock deadlock discipline (`broker/server.py`), health by real protocol round trip (`broker/rpc.py`), silent message loss from a serialisation type mismatch (`tests/broker/test_channel_sentat_drop.py`). |
| `Q-0017` | What replaces the discarded desktop human-notification path (`attention/platform.py` and its test). |
| `Q-0018` | Whether repository-root files, packaging, and CI configuration — everything outside `src/`, `tests/`, `docs/` — need their own classification pass. |

Also bearing on this ledger and already recorded in `DECISIONS.md`: `Q-0001` (schema/DDL and
single-writer assignment), `Q-0002` (dedup key composition), `Q-0007` (Dispatcher AI auth identity
and tier), `Q-0008` (package rename), `Q-0010` (unclassified-anomaly counter and its route to a
human).

A `Q-` entry is **not** a decision and none of them authorises action. Resolving one means adding a
new `D-` entry in `DECISIONS.md` that answers it.

---

## Related documents

- `CHARTER.md` — purpose, non-goals, and role boundaries
- `DECISIONS.md` — the `D-` decisions cited throughout this ledger, and the `Q-` open questions
- `ACCEPTANCE.md` — the Agent View gate, fault injection targets, canary, and rollback conditions
