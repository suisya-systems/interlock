# Functional parity audit — operating-organisation capabilities vs Interlock coverage

**Issue:** suisya-systems/interlock#60.
**Audit subject:** the operating organisation's repository, `claude-org-ja`, read at local path
`/home/happy_ryo/work/org/claude-org-ja`, working tree at HEAD
`e17b5c4eca64070d825504eed7b8cd5ef2947049` (2026-08-20). All `file:line` citations for the
operating organisation are against that tree.
**Interlock baseline:** this repository at `afc181e` (origin/main at audit time). Two PRs were
open and unmerged when this audit was finalised — #58 (S8 `MessageBus`) and #59 (gate item 2
crash-window proof on C2); rows that depend on them say so explicitly.
**Citation rules:** Interlock decisions are cited by stable ID (`D-00NN` / `Q-00NN` /
`AC-N` / gate item number), per the rules in `DECISIONS.md`, `ACCEPTANCE.md` and
`PORTING_LEDGER.md`. `PORTING_LEDGER.md` is cited by the *path* a row classifies or by bucket
name, never by row position or line number (its own instruction). Operating-organisation
sources are cited `file:line` against the pinned tree above.

---

## 1. Scope, method, and rubric

### 1.1 What this audit is

Issue #60's operator decision (2026-08-21): the migration must not regress operational
efficiency. The spike gate proves substrate reliability; nothing measures business-workflow
parity with the operating organisation. This document is that ledger, in three steps:

1. **Inventory** — for each stage of the daily flow (triage → delegation → monitoring →
   PR/CI → merge/cleanup → retro → curation, plus cross-cutting), what capability exists in
   the operating organisation and which tool or discipline provides it (§2).
2. **Classification** — each capability is `covered`, `deliberately-reduced`, `undesigned`,
   or `undetermined` against Interlock (§2, per-row; rubric in §1.3).
3. **Gap decomposition** — `undesigned` items that materially affect operating efficiency
   become issue drafts with priority and placement (§4).

Out of scope, per the Issue: implementing any gap; reopening decisions recorded as
non-goals (D-0015) — a `deliberately-reduced` classification with a cited rationale is a
valid terminal state.

### 1.2 Operator directions incorporated

Three operator comments on Issue #60 (all 2026-08-21) extend step 3 and are applied here:

- **Placement.** Each issue draft proposes `core` (Interlock itself — control-plane-natured:
  durability, identity, delivery, recovery) or `operating-layer` (a separate successor repo
  that imports Interlock as its substrate — business-workflow-natured: project registry
  concepts, brief generation, worktree operating conventions, retro/curation). Default is to
  keep Interlock thin, consistent with `CHARTER.md` §2 non-goals (D-0015).
- **Provider-agnosticism.** Business concepts (briefs, delegation, the project registry,
  review/merge gates, retro/curation) are defined provider-neutrally; Claude-Code-specific
  artefacts (`CLAUDE.md` brief files, `.claude/skills`, permission modes, hooks) belong at a
  rendering/adapter edge. Each draft notes whether the capability is **inherently
  provider-shaped** or **provider-neutral with a rendering edge**. Over-abstraction is
  guarded against: the target shape is gate item 11's — a thin seam plus one substitution
  test, not everything abstracted.
- **Substrate discipline extended upward.** The operating layer should not be bound to a
  resident secretary AI any more than to Claude Code. Three facets: (1) **discipline as
  data, machine-enforced** — the delegation lifecycle is modelled as a policy table plus
  state machine whose mechanical obligations execute programmatically on transitions; AI
  is invoked only for judgement-bearing transitions, and prose is a rendered projection of
  policy data, never the source of truth (D-0002's shape applied one layer up); (2)
  **event-driven from the start, single spine** — the events table is the sole drive
  source, with secretary AI, workers, and delivery all consumers of it, assuming the S8
  `MessageBus` as substrate; (3) **human gates as first-class `Gate` entities** (owner,
  rationale, options, deadline, outcome) rather than journal events plus prose plus
  memory — which also defines the web Secretary window (interlock#29) as a gate-queue
  display and approval UI, and yields a candidate answer to Q-0017 (notification = gate
  creation; approval = gate resolution). Where a gap's natural fix looks like "more prose
  discipline for the AI", the drafts below prefer the policy-data + machine-enforcement
  form instead.

### 1.3 Classification rubric

| Class | Meaning | Evidence obligation |
|---|---|---|
| `covered` | An Interlock mechanism provides the capability (implemented, rehearsed as a stub, or recorded design). | Name the mechanism, with a doc/source citation or D-ID. Rows resting on a rehearsal or an unmerged PR say so. |
| `deliberately-reduced` | The capability was discarded or retired with a recorded rationale and a stated substitute. | Cite the `PORTING_LEDGER.md` bucket/path row or the retiring D-entry, *and* name the substitute mechanism. |
| `undesigned` | No Interlock counterpart exists and none is recorded as deliberately dropped. | Say so; if it materially affects efficiency, it gets a §4 draft, otherwise a one-line disposition. |
| `undetermined` | The classification genuinely depends on an open question or an operator preference. | Listed in §3 with the reason. |

A capability may split across classes (mechanism vs content, delivery vs ingestion); such
rows say which part is which.

### 1.4 Method and its limits

The inventory was produced by reading the operating organisation's primary sources —
`CLAUDE.md`, `README.md`, `registry/`, `.claude/skills/` (24 skills), `tools/` (~60
modules), `docs/contracts/` (10 contracts), `knowledge/`, `dashboard/`, `docs/operations/` —
and consolidating ~290 raw capability observations into the ledger rows below (multiple
observations of the same capability from different files are merged; the row cites the
strongest source). Interlock's side was read from `CHARTER.md`, `DECISIONS.md`
(D-0001…D-0027, Q-0001…Q-0023), `ACCEPTANCE.md`, `PORTING_LEDGER.md`, `docs/*.md`, and
`src/claude_org_runtime/`.

Limits: line numbers against a live repo drift; the pinned HEAD above is the reference.
One file needs a special note: `registry/projects.md` is **operator-local and gitignored**
(generated from the tracked `registry/projects.example.md`; the example file says so at
`registry/projects.example.md:3-6`), so citations into it describe the operator's live
registry as read at audit time and are not reproducible from the pinned commit. Schema-level
claims therefore cite the committed `registry/projects.example.md`; the few remaining
`projects.md` citations are for the operator-local-ness itself. `registry/org-config.md`
and `registry/dogfood_pending.md` are tracked and reproducible.
Consolidation is lossy by design — the ledger records operational capabilities, not files.
Where a judgment call was close, the row says so or the item is in §3.

---

## 2. Capability ledger by stage

Legend for the **Class** column: **C** = covered, **DR** = deliberately-reduced,
**U** = undesigned, **?** = undetermined (see §3). `→G<n>` marks the §4 draft that carries
an undesigned item.

### 2.1 Triage

| # | Capability | Provided by (operating org) | Class | Interlock counterpart / rationale |
|---|---|---|---|---|
| T1 | Project name resolution: the Secretary resolves a colloquial project name to slug, clone path, and description via a registry table | `CLAUDE.md:22`; `registry/projects.example.md:61` (schema); parser `tools/registry_parser.py:1-19` | **U** →G1 | No project registry concept exists in Interlock. Not in any discard bucket — `PORTING_LEDGER.md` classifies only the fork source tree, and the registry lives in claude-org-ja. |
| T2 | Cross-repo work discovery: deterministic `gh`-read-only issue scan ranking unblocked candidates, with dependency resolution, priority/effort scoring and a learned effort model | `.claude/skills/work-discovery/SKILL.md:68-90`; `tools/work_discovery_scan.py:1-9,816-902` | **U** →G7 | No counterpart. Interlock deliberately has no opinion on where work comes from. |
| T3 | Triage repo-set resolution from the registry (`triage` opt-out column; `triage_home` opt-in flag) | `registry/projects.example.md:43-48`; `registry/org-config.md:43-53`; `tools/work_discovery_repos.py:1-15` | **U** →G7 | Travels with T2. |
| T4 | Propose-only triage invariant with auditable exclusion/truncation disclosure (excluded candidates always shown; no auto-start) | `.claude/skills/work-discovery/SKILL.md:43-46,128-130,162-167` | **U** →G7 | A policy of the undesigned triage capability; travels with it. |
| T5 | Proactive post-merge next-work proposal (deterministic triage output presented after every merge, human picks) | `CLAUDE.md:39-46` | **U** →G7 | Travels with T2. |
| T6 | Stale worker-directory sweep (archive worker state files whose task is terminal, min-age-guarded orphan handling) | `tools/sweep_stale_workers.py:5-16` | **U** | Worktree/workspace hygiene of the operating layer; no draft of its own — folded into G2's workspace-conventions scope. Interlock's own state needs no md-file sweep (SQLite rows are the record, D-0001). |

### 2.2 Delegation

| # | Capability | Provided by (operating org) | Class | Interlock counterpart / rationale |
|---|---|---|---|---|
| D1 | Deterministic delegation planning: split-target selection, name/cwd validation, instruction-file rendering, capacity ceilings, overflow reservations | `docs/contracts/role-contract.md:130-134`; runtime `dispatcher delegate-plan` | **C** (mechanism) + **U** (C2-era input path, →G2) | The planning mechanism is carried and shipped: `src/claude_org_runtime/dispatcher/runner.py:1-13`, `docs/cli.md:19-26,57-84`. But its renga-shaped pane-JSON input has no producer left in this tree after the purge (`docs/cli.md`), so end-to-end delegation planning under C2 does not exist yet — what feeds the planner once panes are gone is part of G2's delegation-flow scope. |
| D2 | Atomic delegation reservation: `runs.status='queued'` plus a `delegate_sent` event committed in one transaction; idempotent re-apply; identity-drift refusal | `tools/gen_delegate_payload.py:1162-1265,1094-1144`; `.claude/skills/org-delegate/SKILL.md:214-219` | **U** →G2 | Only the *principle* is covered: D-0001 requires durable SQLite state with resume-by-query, and the refusal discipline matches Interlock's recorded-refusal pattern (`docs/lease-fencing.md`). The reservation *design* — the atomic queued-plus-event transaction, replay idempotency, identity-drift refusal — exists nowhere: Q-0001 leaves the run schema and writer assignment open and the spike schema is throwaway (D-0026). A crash or retry during delegation is an uncovered window until G2's state machine (with its core Q-0001 touchpoint) designs it. |
| D3 | Per-worker brief generation: TOML-config → `CLAUDE.md`/`CLAUDE.local.md` rendering with optional blocks, verification-depth variants, knowledge injection, src-layout detection, report-target addressing | `tools/gen_worker_brief.py:315-349,411,271-306,55-63`; `tools/templates/worker_brief_normal.md:1-25`; `.claude/skills/org-delegate/SKILL.md:148-161` | **U** →G2 | No counterpart. The Discard bucket covers handover/resume *prompt prose* for the resident roles, not task briefs (`PORTING_LEDGER.md`, Discard bucket; D-0014) — worker briefs were never classified because they live in claude-org-ja. |
| D4 | Worker-directory Pattern A/B/C resolution and worktree orchestration (active-run collision avoidance, base-clone unification, gitignored-target routing, self-edit pinning) | `tools/resolve_worker_layout.py:1-45,218-230,778-786,189-209,669-702`; `.claude/skills/org-delegate/references/delegate-flow-details.md:41-51` | **DR** (mechanism) + **U** (successor conventions, →G2) | "The A / B / C worker layout and bespoke worktree orchestration" is named in the Discard bucket (`PORTING_LEDGER.md`; D-0014), and the `sandbox_by_pattern` axis is discarded with it (row for `src/claude_org_runtime/settings/generator.py`). Substitute: under C2 Interlock owns the worker's working tree outright (O8, D-0025), and session↔run identity is a SQLite binding (gate item 2). What workspace layout the *successor operating repo* uses is a new operating-layer convention, carried in G2. |
| D5 | base_branch two-track dispatch: worktrees cut from a per-project registered branch, fail-loud when it does not exist on origin | `registry/projects.example.md:50-57`; `tools/gen_delegate_payload.py:1376-1435` | **U** →G1 | Registry content; travels with the registry concept. |
| D6 | Per-role settings generation (permissions/sandbox/hooks) with schema SoT, deny-path symlink canonicalisation, and a sandbox preflight/canary | `tools/gen_delegate_payload.py:1986-2082` (shell-out); runtime `settings generate` / `sandbox doctor` | **C** | Carried: `PORTING_LEDGER.md` rows for `src/claude_org_runtime/settings/generator.py` (carry-invariant) and `settings/sandbox_doctor.py` (carry); shipped at `src/claude_org_runtime/settings/generator.py:1-6`, `settings/sandbox_doctor.py:23-30`, `docs/cli.md:147-150,274-282`. Extended beyond v1 by the fail-closed fence renderer and breach-probe battery (`docs/per-role-fencing.md:70-76`, gate item 3, D-0023). |
| D7 | Role taxonomy content for workers (default / self-edit / doc-audit; per-role write surfaces) | `tools/resolve_worker_layout.py:483-495`; `docs/contracts/role-pattern-sandbox-contract.md:198-1063` | **U** →G2 | The *mechanism* (render a fence from a role document, refuse a broken one) is covered (D6); the successor's worker-role *content* — which roles exist and what each may touch — is operating-layer material nobody has authored. |
| D8 | Spawn-ceremony verification: machine-check that spawn, peer registration and instruction delivery actually completed before `DELEGATE_COMPLETE` | `tools/spawn_gate.py:1-38,577-596` | **DR** | The ceremony verifies pane-transport delivery — the Discard bucket's backend contract (`PORTING_LEDGER.md`; D-0009, D-0014). Substitute: under C2 there is no ceremony to verify — Interlock spawns the child itself, the fail-closed spawn precondition covers every start (`docs/per-role-fencing.md:205-210`; D-0023, D-0027), and identity is read back rather than assumed (D-0027: never treat exit 0 or a pre-committed binding as acceptance). |
| D9 | Backend capability gates (renga `first_drive`, pane-control ladder rungs) with three-way recorded/not-recorded/undetermined lookup | `tools/capability_gate.py:15-22,36-49,78-86` | **DR** | renga compatibility machinery (Discard bucket: "renga / herdr compatibility layers"; D-0014). Substitute: the D-0010 capability/version probe with fail-closed spawn (`src/claude_org_runtime/session/provider.py`, S1), which serves the same "don't act on an unverified backend capability" invariant. |
| D10 | Two-lane task routing (lightweight subagent lane vs full worker lane) with explicit activation conditions and mandatory review gate in both lanes | `CLAUDE.md:71-88`; `.claude/skills/org-delegate/SKILL.md:65-86` | **U** →G10 | Task-sizing policy of the operating layer; no counterpart and no discard record. |
| D11 | Scope discipline: 1 worker = 1 task = 1 scope; scope expansion only via human escalation; Secretary never edits worker files | `CLAUDE.md:92-100` | **U** →G10 | Operating policy. The *human gate* it routes through is covered (D-0004, D-0016); the scope-boundary policy itself is not designed anywhere. |
| D12 | Ultracode (multi-agent workflow) arming for heavy tasks, with dispatcher-side kickoff arming as the activation condition | `CLAUDE.md:90`; `.claude/skills/org-delegate/SKILL.md:92-96` | **U** →G10 | Operating policy with a provider-shaped activation edge; carried as a note in G10. |
| D13 | Concurrency cap on simultaneous workers | `registry/org-config.md:33-40` | **C** | D-0017 (workers are few, capped, fenced; the cap is control-plane property, not convention) and delegate-plan's capacity ceilings (`docs/cli.md:57-84`). |
| D14 | Pre-dispatch context checks: ambiguity/OS-precondition checklist, committed-base file-existence check, contracts grep, parallel same-file collision briefing, release pre-fetch | `.claude/skills/org-delegate/SKILL.md:98-134`; `references/delegate-flow-details.md:82-127`; `references/release-pre-fetch.md:1-26` | **U** →G2 | Delegation-quality disciplines of the operating layer; folded into G2's brief/delegation contract. |
| D15 | Autonomous conveyor: scope contract articulated once as a machine-checkable predicate, then completion-driven triage→delegate→verify→PR→CI belt; merge never pre-approvable; machine exit conditions | `.claude/skills/org-conveyor/SKILL.md:123-140,144-179,71-73,223-232` | **?** | See §3 (U3): whether the successor wants an autonomous belt at all is an operator preference; its merge-stays-human invariant is already Interlock's (D-0004 human gate). |
| D16 | Dogfood paired-follow-up protocol (new tool/runtime PRs get a tracked paired issue with a pending→open→consumed→closed lifecycle) | `registry/dogfood_pending.md:14-19`; `.claude/skills/org-delegate/references/dogfood-protocol.md:9-13` | **U** →G8 | Feedback-loop bookkeeping of the operating layer; folded into G8. |
| D17 | Mid-task worker suspension: secretary-only `SUSPEND:` interrupt with a mandatory four-item checkpoint report (work done, changed files, next step, blockers), no auto-commit, org-suspend collecting every worker's checkpoint before flushing state | `docs/contracts/delegation-lifecycle-contract.md:173-199`; `.claude/skills/org-delegate/references/worker-claude-template.md:218-223` | **U** →G2 | Run-level `suspended` status persistence is D-0001 territory (a run status is a SQLite fact), but the interrupt protocol and its checkpoint-report contract are part of what a worker owes back — folded into G2's brief/completion-report contract. |

### 2.3 Monitoring

| # | Capability | Provided by (operating org) | Class | Interlock counterpart / rationale |
|---|---|---|---|---|
| M1 | Resident LLM monitoring loop: dispatcher `/loop 3m` cycle draining events/messages/panes and forwarding anomalies | `docs/contracts/role-contract.md:84-88` | **DR** | Retired by D-0002 (with the measured baseline that motivated it). Substitute: the program-side event loop plus low-frequency reconcile loop (D-0002), the closed fact-state watcher (D-0005), and the on-demand Dispatcher AI (D-0003) — zero AI turns absent incidents (AC-1). The Discard bucket names "the resident Dispatcher AI loop" (`PORTING_LEDGER.md`; D-0014). |
| M2 | Pane-screen observation: normalized visible-row hashing for stall detection, spinner elapsed parsing with suppression cap, full-scrollback error/anomaly regex scan | `tools/inspect_pane_state.py:12-30`; `tools/inspect_anomaly_scan.py:9-41`; `docs/contracts/role-contract.md:88` | **DR** | Screen hash / spinner / screen regex as *backend contracts* are the Discard bucket's named subject (`PORTING_LEDGER.md`, terminal cluster; D-0014), while D-0008 keeps screen hash and spinner legitimate as watcher *signals*. Substitute: provider-neutral structured readouts (S1's typed lifecycle/availability readout; C2 builds them from the child's stream-json, `src/claude_org_runtime/session/claude_cli_provider.py:1-8`) classified into the closed fact-state set (D-0005). |
| M3 | Silent-worker / stalled-worker heuristics: silence thresholds, deep-loop triggers (30 min same phase, 1 h silent, codex round 4+), secretary-side intervention | `.claude/skills/org-delegate/SKILL.md:368-374`; `docs/contracts/delegation-lifecycle-contract.md:148` | **?** (detector semantics, §3 U6) + **DR** (ad-hoc vocabulary) | The *vocabulary* is retired on record: `NO_ACTIVITY_EVIDENCE` is a fact, not an anomaly (D-0005, D-0006); the watcher may name a stall candidate but never conclude one (D-0008); ambiguous stalls become incidents judged on demand (D-0003, D-0007); the `attention/classifier.py` ledger row carries the pure-classifier invariant while rewriting the vocabulary. But the replacing *detector semantics* do not exist yet — per-state detection predicates and tolerable latency are open (Q-0012, Q-0003), and D-0014 wants v1's accident-derived detection semantics extracted as quarry, not dropped. Until Q-0012/Q-0003 are answered, this capability's parity is undetermined (§3 U6); AC-10 is the acceptance hook that keeps it from being silently lost. |
| M4 | Relay-gap detection: `SECRETARY_RELAY_GAP_SUSPECTED` from a pending-decisions register, plus the register's 4-step lifecycle | `CLAUDE.md:140-142`; `tools/pending_decisions.py:1-20`; `.claude/skills/org-escalation/SKILL.md:53-57,83-87` | **U** →G4 | Worker-initiated judgment requests and their end-to-end relay (worker → Secretary → human → worker) have no designed Interlock counterpart. The incident contract (D-0007) covers watcher-detected conditions; a worker's *own* escalation is a different entry point. Accident-derived: the detector exists because relays were actually dropped. |
| M5 | Awaiting-user signalling at four canonical gates (`worker_completed`, `ci_green_merge_gate`, `escalation_to_user`, `escalation_reply_forward`) feeding an attention watcher | `CLAUDE.md:110-129`; `.claude/skills/org-escalation/SKILL.md:59-76` | **U** →G5 | The *watcher* half survives (see M6); the gate taxonomy — knowing that the organisation is blocked on a human, and saying so at the moment it happens — is operating-layer signalling nobody has designed for the successor. |
| M6 | Attention watcher: classify human-required events (approval blocked, CI failed, pending decision, silent stop) from durable state, dedup, notify | `docs/operations/attention-watch.md:1-17`; `tools/templates/attention.example.json:1-27` | **C** (classifier/dedup invariants) + **U** (control-plane integration, →G5) + **DR** (desktop backend, →G5) | What carries is narrower than the CLI suggests: the pure no-I/O classifier and the two-namespace dedup invariants (`src/claude_org_runtime/attention/__init__.py:3-9`; ledger rows for `attention/classifier.py`, `attention/dedup.py`). The *integration* is not covered — `attention/readers.py` reads the legacy v1 tables (its ledger row is `rewrite`, and the spike control-plane schema has no `events` table for it to read), so against this baseline the watcher cannot consume Interlock's own state; that reader/loop rewrite is G5 work. The desktop notification backend was deleted with `attention/platform.py` (`PORTING_LEDGER.md` purge record: "a capability is lost, deliberately"; notifications resolve to a stdout line) and reinstating one is a recorded open question, Q-0017. |
| M7 | Secretary inbox stall watcher (live-tail of undelivered secretary-addressed messages past a threshold age) | `tools/secretary_queue_watcher.py:1-8` | **?** (aging watch) + **C** (delivery) | The delivery half of the invariant — an enqueued message must not be *lost* — is the outbox's: durable retry count, resend, and the no-unowned-rows recovery query (S7, `docs/gate-record.md` §item 5; ACCEPTANCE §2 outbox row). But those run at delivery/recovery time; *watching aging in steady state* is one of the retired loop's non-detection duties whose owner Q-0019 leaves unassigned, so that half is undetermined (§3 U2) — classifying it covered would answer Q-0019 by audit. |
| M8 | Context handover/resume for resident roles (secretary/dispatcher handover files, `/clear`, resume with reconciliation, loop re-arm, stale-refire guards) | `CLAUDE.md:57-67`; `.claude/skills/dispatcher-resume/SKILL.md:59-129,202-256` | **DR** | The Discard bucket names "the bulk of the handover / resume prompt prose" (`PORTING_LEDGER.md`; D-0014; row for `src/claude_org_runtime/prompts/`). Substitute: there is no resident context to hand over — the Dispatcher AI is startable statelessly from a persisted incident packet (D-0003, D-0007), and every component resumes from SQLite after a kill (D-0001; gate item 4). |
| M9 | Live org dashboard: stdlib HTTP server, SSE push, five panels (workers, work items, activity, projects, knowledge), SQLite-only state with explicit UNINITIALIZED handling | `dashboard/server.py:396-408,9-16,285-296`; `dashboard/index.html:23-81` | **U** →G9 | Interlock fixes that UI is a projection, never the source of truth (D-0001), but ships no projection for an operator. |
| M10 | Read-only human observation of live sessions (dispatcher viewer, org-attach read-only attach command generation) | `README.md:118,141`; `.claude/skills/org-attach/SKILL.md:25-31`; `docs/operations/dispatcher-view.md:1-9` | **DR** | Pane-attach machinery (Discard bucket; D-0009, D-0014). Substitute: structured state via the provider's `read_state`/readouts (S1) and, for the human, whatever G9 builds on the projections. |
| M11 | Graceful teardown honesty: two-pass polite-then-forced shutdown with lifecycle-event confirmation, partial-suspend honesty gate, stale-pid-safe cmdline-verified kills, identity-verified pane close | `.claude/skills/org-suspend/SKILL.md:647-718,765-790`; `.claude/skills/org-down/SKILL.md:104-143`; `tools/secretary_queue_watcher.py:58-68` | **C** (process identity) + **DR** (pane choreography) | The process-identity invariants are carried: PID reuse defeated by kernel start time, ownership by fingerprint, fail closed on unknown platforms (`PORTING_LEDGER.md` row for `src/claude_org_runtime/broker/residents.py`; shipped `src/claude_org_runtime/broker/residents.py:2-8`). Under C2, workers are owned child processes stopped by process group (`src/claude_org_runtime/session/claude_cli_provider.py:32-35`), so the pane-by-pane shutdown choreography has no referent (D-0009). |
| M12 | Startup validation: role-config drift check against schema, herdr protocol preflight, runtime-version drift check | `.claude/skills/org-start/SKILL.md:323-347,175-197,263-291` | **C** (config) + **DR** (herdr) + **U** (package drift) | Config validation is covered stronger than v1: fail-closed spawn on a broken fence, validated before spawn (`docs/per-role-fencing.md:205-214`; D-0023). herdr preflight goes with the herdr compat layer (Discard bucket; D-0014); substitute for the verify-the-backend-before-acting need: the D-0010 fail-closed capability/version probe (S1), as in D9. Python-package drift checking is operating-layer hygiene; no draft (low mechanism content, successor can reuse the pattern). |
| M13 | Org bring-up/teardown lifecycle (org-start parallel boot, org-down gated daemon teardown) | `.claude/skills/org-start/SKILL.md:199-226`; `.claude/skills/org-down/SKILL.md:36-102` | **DR** (pane bootstrap) + **U** (successor bring-up, →G10) | `org up` / `org down` around interactive TUI panes is a Discard row (`PORTING_LEDGER.md` row for `src/claude_org_runtime/broker/launcher.py`; D-0009, D-0010). Substitute: Interlock's supervisor owns worker processes directly (C2, D-0025/D-0027). What "bringing up the successor org" means operationally — which processes exist, in what order, verified how — is undesigned operating-layer material carried by G10. |

### 2.4 PR / CI

| # | Capability | Provided by (operating org) | Class | Interlock counterpart / rationale |
|---|---|---|---|---|
| P1 | CI watch: poll `gh` for check state with terminal/incomplete/indeterminate distinction, startup absolute-state evaluation, mergeability conflict detection with settle-retry, post-CI merge watch with head-change detection | `tools/pr_watch.py:2-119,1050-1098,399-437,1365-1392`; `docs/contracts/delegation-lifecycle-contract.md:145` | **U** →G3 | No Interlock counterpart observes CI at all. The retry-vs-terminal-verdict discipline maps cleanly onto D-0005/D-0006 (`OBSERVATION_UNAVAILABLE` vs a verdict) and should be re-derived, not ported. |
| P2 | Zero-miss CI signal delivery: best-effort low-latency push, relay-scan backstop draining an `event_deliveries` outbox ledger keyed `UNIQUE(source_event_id, recipient)`, self-audit heartbeat | `tools/pr_watch.py:277-334`; `tools/relay_scan.py:1-22,41-55`; `docs/contracts/state-schema-contract.md:254` | **C** (delivery) + **U** (ingestion, →G3) | The delivery half is exactly the carried outbox invariant set: at-least-once, idempotent ack, durable retry, no unowned rows after recovery (S7; ACCEPTANCE §2). What is undesigned is the *ingestion*: something must observe CI and write the durable event (G3). |
| P3 | Watcher survivability: CI monitoring in a dedicated pane outside the Secretary session/sandbox, idempotent per-PR spawn, liveness by side effect not process-list, full-screen hang detection | `CLAUDE.md:36-37`; `.claude/skills/pr-watch-pane/SKILL.md:28-35,160-167,366-410` | **DR** (pane mechanics) + **U** (the need, →G3) | The pane mechanics are the Discard bucket's "backend contracts that assume tmux / pane layout, pane IDs, and send-keys" (`PORTING_LEDGER.md`, Discard bucket; D-0009, D-0014); no existing mechanism substitutes for them — the UI-independence *need* (completion signals must not die with a human-facing session) is carried explicitly as a G3 design constraint. |
| P4 | Run↔PR bookkeeping: back-fill `pr_url`/branch at PR open; auto-complete the run on merge; deterministic owner/repo resolution per run (never cwd) | `tools/set_run_pr_open.py:1-13`; `tools/run_complete_on_merge.py:1-20,27-35`; `tools/resolve_run_repo.py:1-16` | **U** →G3 | Accident-derived (2026-08-06 cross-repo incident named in `tools/resolve_run_repo.py:1-16`). The run rows exist in Interlock (D-0001); the PR linkage and its lifecycle do not. |
| P5 | Independent-model review gate: `codex exec review` self-review with round caps, same-finding plateau detection escalated as a design problem, safety-block treated as unmet gate; pre-dispatch design review on trigger conditions | `.claude/skills/org-delegate/references/worker-claude-template.md:148-156`; `docs/contracts/role-contract.md:249-255`; `.claude/skills/org-delegate/references/codex-design-review.md:7-15` | **U** →G8 | Review-quality policy of the operating layer. Provider-neutral as a concept (an independent reviewer gate); the codex CLI is one adapter. |
| P6 | Completion-report contract: full-depth reports carry a 3-part human-understanding summary used as the human approval surface; minimal-depth is a 1-line fast path; mandatory pre-completion rebase-clean check | `.claude/skills/org-delegate/SKILL.md:352`; `references/worker-claude-template.md:100-108,157-167`; `docs/contracts/role-contract.md:208-212` | **U** →G2 | Part of the brief/delegation contract (what a worker owes back); folded into G2. |
| P7 | Review-feedback loop reuses the same worker session (never respawn; context preserved), run status flipped back to in-use | `.claude/skills/org-pull-request/SKILL.md:296` | **U** →G3 | Operating-layer lifecycle policy over Interlock primitives (session resume is an S1 verb; run status is D-0001 state). Folded into G3's run-lifecycle scope. |
| P8 | Generated-prose drift guard (edit the `.md.in` source, regenerate, `--check` zero drift) | `.claude/skills/org-delegate/references/worker-claude-template.md:90-94` | **U** | claude-org-ja-specific repo hygiene; no draft — the successor repo can adopt the pattern if it generates prose. |

### 2.5 Merge / cleanup

| # | Capability | Provided by (operating org) | Class | Interlock counterpart / rationale |
|---|---|---|---|---|
| X1 | Merge approval is always a human gate: CI green halts the flow, presents the persisted summary, `awaiting_user` emitted; a bare "OK" is insufficient in conveyor mode | `.claude/skills/org-pull-request/SKILL.md:72-77`; `.claude/skills/org-conveyor/SKILL.md:71-73,172-175` | **C** (principle) + **U** (flow, →G3) | The principle — side effects that cross a permission boundary go through the Secretary / a human gate — is D-0004/D-0016 and the boundary table (D-0008). The concrete merge-gate flow is part of G3's PR lifecycle. |
| X2 | Deterministic post-merge close: run → COMPLETED via the single writer API, worker state archived, panes closed, per-pattern directory cleanup (incl. worktree removal and the self-edit CLAUDE.local.md residue) | `.claude/skills/org-pull-request/SKILL.md:300-327` | **U** →G3 (completion transition) + **U** (workspace cleanup, →G2) + **DR** (pane close) | The completion transition is *not* covered by principle alone: D-0001 gives durable state and lease fencing gives single-writer writes, but the run-status vocabulary, the COMPLETED transition, and its authorized writer are open with Q-0001 (same finding as C2/D2) — the run-lifecycle half stays a G3 gap. Pane close is DR (D-0009). Workspace cleanup conventions belong to the successor's worktree conventions (G2). |
| X3 | Merge-race honesty: `PR_MERGED_HEAD_UNCONFIRMED` routes to human confirmation, never auto-cleanup; merge-watch timeout leaves run status untouched | `.claude/skills/org-pull-request/SKILL.md:244-246,230-243` | **U** →G3 | Accident-derived edge-case handling that G3 must re-derive (its shape matches D-0006: don't force a verdict without evidence). |

### 2.6 Retro

| # | Capability | Provided by (operating org) | Class | Interlock counterpart / rationale |
|---|---|---|---|---|
| R1 | Structured delegation retrospective (decomposition granularity, instruction clarity, project selection, parallelism, report sufficiency) with delegation-scoped raw knowledge capture | `.claude/skills/org-retro/SKILL.md:32-39,55-61` | **U** →G8 | No counterpart; retro/curation flows are named operating-layer material by the Issue's placement direction. |
| R2 | Org metrics and activity reporting: read-only aggregation of run/event/PR/CI activity over a period, 3-stage CI-to-run matching with an explicit unmatched bucket | `tools/org_metrics_report.py:1-27` | **U** →G6 | Interlock's acceptance criteria *require* measurement (AC-9 reduction targets, AC-10 no-regression against fixtures and dogfood shadow; canary comparisons in ACCEPTANCE §3) but no measurement/reporting mechanism exists. |
| R3 | Retro-gate ack routing discipline (direct send to dispatcher, never broadcast, because the gate detector only sees direct messages) | `CLAUDE.md:106-108` | **DR** | An artefact of pane-transport message plumbing (Discard bucket; D-0009). Substitute: durable events and the outbox make "did the retro happen" a query, not a message-observation race (D-0001). |

### 2.7 Curation

| # | Capability | Provided by (operating org) | Class | Interlock counterpart / rationale |
|---|---|---|---|---|
| K1 | Raw→curated knowledge pipeline: 4-stage lifecycle (capture → curate → candidate evaluation → promotion), thematic merge, move-then-mark archival, deletion forbidden | `docs/contracts/knowledge-curation-contract.md:80-108,37-49`; `.claude/skills/org-curate/SKILL.md:117-231` | **U** →G8 | No counterpart. The one stage Interlock *does* cover — promotion — it covers more strongly (K3). |
| K2 | On-demand curator activation: threshold script at worker close (raw count, candidate count, skill count), one-shot run, no resident loop, spawn coalescing | `docs/contracts/knowledge-curation-contract.md:90-92`; `tools/check_curate_threshold.py:1-25`; coalescing: `.dispatcher/references/pane-close.md:253-265` | **C** (principle) + **U** (trigger content, →G8) | The on-demand, non-resident principle is D-0018 (and D-0002/D-0015). The concrete trigger thresholds and their evaluation are operating-layer content. |
| K3 | Skill promotion requires human approval; curator never self-applies; secretary never edits skills directly; promotion executed by a delegated worker | `.claude/skills/org-curate/SKILL.md:246-250`; `.claude/skills/org-retro/SKILL.md:117-129`; `docs/contracts/knowledge-curation-contract.md:102-108` | **C** | Covered and strengthened: the Curator promotion gate is the *only* module able to write skill material, approvals pin a sha256 content digest recomputed at write time, all five gate-item-9 negatives refused and recorded, path audit build-failing (`docs/curator-promotion-gate.md:22-24,52-55`; gate item 9 discharged, D-0018). v1's gate was convention; Interlock's is mechanism. |
| K4 | 5-signal skill-candidacy scoring shared by retro and curation; machine-local candidate queue with dedup and deferred protection; batched human sign-off at N=5 | `.claude/skills/skill-eligibility-check/SKILL.md:51-66,93-99,135-146`; `knowledge/skill-candidates.md:3-6` | **U** →G8 | Candidate *generation* policy is undesigned (only the approval gate exists, K3). |
| K5 | Skill inventory governance: state-triggered audit, deprecation candidates from observable signals only, pairwise duplication detection, propose-only | `.claude/skills/skill-audit/SKILL.md:35-59,93-116,161-167` | **U** →G8 | Operating-layer governance; travels with G8. |
| K6 | Privacy discipline: public/local split for operator-private candidates; scrub obligations for tracked knowledge; PII scrub pipeline for state fixtures | `docs/contracts/knowledge-curation-contract.md:62-70,136-146`; `docs/contracts/state-fixture-scrub-policy.md:40-143` | **C** (fixture scrub) + **U** (knowledge privacy split, →G8) | The fixture scrub pipeline is a carry row (`PORTING_LEDGER.md` rows for `tests/scrub/scrub_fixture.py`, `docs/scrub-policy.md`; shipped `docs/scrub-policy.md:10-31`). The knowledge-repo privacy split is successor-repo policy. |

### 2.8 Cross-cutting

| # | Capability | Provided by (operating org) | Class | Interlock counterpart / rationale |
|---|---|---|---|---|
| C1 | SQLite as canonical state: single narrow write API (StateWriter transactions), events append-only via helper with in-band corrections, derived projections regenerated and drift-checked, cwd-independent DB discovery | `docs/contracts/state-schema-contract.md:30-68`; `tools/state_db/writer.py:1-16`; `tools/state_db/discover.py:9-15`; `tools/state_db/snapshotter.py:1-21` | **C** (canonical state + fenced writes) + **U** (events/corrections — →G3/G4 spine; projections — →G9) | The covered half is Interlock's founding decision, made stronger: SQLite SoT with resume-by-query (D-0001), fail-closed open that refuses a corrupt DB rather than recovering it as empty (`src/claude_org_runtime/control_plane/schema.py:21-29` — a deliberate inversion of v1's recover-as-empty, per the `attention/dedup.py` ledger row), lease-fenced single-writer protected writes (`docs/lease-fencing.md:17-21`), and recorded refusals. Not covered here: an `events` table with helper-mediated append and in-band corrections (the spike schema has none — that spine is G3/G4 work), and the regenerated, drift-checked human projections (G9). |
| C2 | Run-status closed vocabulary with transition ownership allow-list and orthogonal predicates (active/reserved/visible/terminal) | `docs/contracts/state-semantics-contract.md:67-197` | **U** →G2 | Not covered by analogy: D-0005 closes only the *watcher's* six-value observation vocabulary, not run lifecycle semantics. The run-status vocabulary, per-transition ownership allow-list, and orthogonal predicates have no Interlock counterpart — Q-0001 deliberately leaves the run schema open and the spike schema accepts arbitrary status text (throwaway, D-0026). Lands in G2's state machine, whose durable schema half is settled with Q-0001 in core. |
| C3 | Message delivery: three-state claim lifecycle with lease reaping, push-first channel sidecar that can wake idle sessions, pull fallback, message identity/ack/dedup | `CLAUDE.md:24-32`; `docs/contracts/backend-interface-contract.md:300-397` | **C** | The invariants are the Carry bucket verbatim ("message identity, ack, and dedup invariants"; `PORTING_LEDGER.md` rows for `broker/store.py`, `tests/broker/test_delivery.py`); shipped as the outbox (S7) and the broker store (`src/claude_org_runtime/broker/store.py:11-12`, `channel_sidecar.py:2-6`). The worker-facing `MessageBus` endpoint (S8) is in flight as PR #58 at audit time — its no-dependency-edge-to-`SessionProvider` assertion is the D-0009 separation. |
| C4 | Backend abstraction: pane control + messaging + events + identity + error vocabulary behind a contract, two swappable transports (renga/broker) with mechanical name substitution | `docs/contracts/backend-interface-contract.md:1-9,32-118,241-280`; `CLAUDE.md:24-32` | **DR** | The pane-shaped backend contract is the Discard bucket's core subject (D-0009, D-0014; `src/claude_org_runtime/transport/descriptor.py` row). Substitute: two *separate* contracts — `SessionProvider` and `MessageBus` (D-0009) — with substitutability proven, not asserted: the 184-case control-plane suite runs unchanged against two providers with CI leak tripwires (gate item 11, `docs/gate-record.md`). The replacement transport contract is deliberately unauthored until needed (`src/claude_org_runtime/transport/__init__.py:2-11`). |
| C5 | Machine-readable error-code vocabulary with tolerant unknown-code handling | `docs/contracts/backend-interface-contract.md:241-280` | **DR** | Discard bucket (backend contracts assuming panes; D-0009, D-0014), as C4. Substitute: typed Ok/Failure results in S1 where "could not observe" is distinct from "observed nothing" (R4; `src/claude_org_runtime/session/provider.py`), and typed refusals in the control plane. |
| C6 | Layered sandbox enforcement (permissions / bwrap / PreToolUse hooks) with per-role×pattern surface tables and per-role fail-open/closed policy | `docs/contracts/role-pattern-sandbox-contract.md:48-88`; `docs/contracts/sandbox-launcher-contract.md:585-613` | **C** (mechanism) + **DR** (pattern axis) | Fencing generation/validation/breach probes are Carry-bucket items (D-0014, D-0017), shipped with stronger semantics: hook deny with unabsorbable exit 2, fail-closed on every malformed input (`src/claude_org_runtime/fencing/hook.py:9-11`), rule-derived breach battery with set-equality coverage (`docs/per-role-fencing.md:70-76`). The `sandbox_by_pattern` A/B/C axis is discarded with the worker layout (`PORTING_LEDGER.md` row for `settings/generator.py`). |
| C7 | Worker git guardrails: per-subcommand boundary classification, stash-stack shared-across-worktrees hazard denied by allowlist, false-positive recovery discipline (never self-override) | `docs/contracts/worker-git-guardrails-design.md:203-296,243,739-785` | **?** | See §3 (U1): the *mechanism* (fence rules + battery) is covered; the git rule *corpus* is unported content whose placement (core default fence vs operating-layer role content) is undecided. |
| C8 | Human escalation discipline: secretary never self-approves worker judgment calls; only receipt-ack allowed before escalating; human decisions relayed verbatim | `CLAUDE.md:131-138`; `.claude/skills/org-escalation/SKILL.md:20-22,42-44` | **C** (boundary) + **U** (ledger, →G4) | The boundary is D-0004/D-0016/D-0008 (assessment never executes; the window executes under policy). The durable escalation *ledger* and its relay-gap detection are G4. |
| C9 | Role architecture: Secretary (sole window), Dispatcher, Curator (on demand), Worker; role boundaries written down | `README.md:78-89`; `docs/contracts/role-contract.md:24-30` | **C** | CHARTER §3's five roles with explicit non-responsibilities (D-0016, D-0017, D-0018; Dispatcher split into deterministic Core + on-demand AI per D-0002/D-0003/D-0008). |
| C10 | Citation-or-abstain discipline for the human window | `CLAUDE.md:5-14` | **U** →G10 | Prose discipline of the operating layer's human window; no mechanism to port — carried into the successor's Secretary material as G10 policy content. |
| C11 | State-file schema versioning and migration framework (per-file versions, tolerant readers, central migration registry) | `docs/contracts/state-schema-contract.md:202-228`; `tools/state_migrate.py:1-9` | **U** (live schema migration) + **C** (v1→v2 cutover bridge) | The v1→v2 cutover bridge is covered as exactly that and no more (`PORTING_LEDGER.md` row for `src/claude_org_runtime/migrate/v1_to_v2.py`; D-0013 — one-shot artifact conversion, never a permanent API; shipped `src/claude_org_runtime/migrate/__init__.py:5-9`). Migration of Interlock's *own* SQLite across schema revisions is undesigned: `control_plane/schema.py` refuses a mismatched revision rather than migrating, and the migration policy is explicitly part of open Q-0001. No separate draft — this is core's Q-0001 resolution work, reachable through G2's core touchpoint. |
| C12 | Release engineering: paired upstream-package + downstream-repo sync, fail-closed multi-gate tag verification, pin-window content-based bumps | `.claude/skills/runtime-release-with-paired-ja-sync/SKILL.md:16-23,129-173,235-249` | **?** | See §3 (U5): claude-org-ja–specific two-repo release machinery; whether the successor org has this shape at all is unknown. |
| C13 | Operator-local vs versioned config split (registry gitignored, generated from tracked template, non-destructive schema migration warnings) | `registry/projects.example.md:3-6,20-22`; `tools/ensure_projects_registry.py:1-22` | **U** →G1 | Travels with the registry concept (an operating-layer privacy/portability property worth keeping in G1's design). |

---

## 3. Undetermined items

| # | Item | Why it cannot be classified here |
|---|---|---|
| U1 | **Worker git guardrail rule corpus** (C7) | The enforcement mechanism is covered (fence rules, breach battery, PreToolUse hook — D-0017, D-0023). But the *content* — the per-subcommand classification and the stash-stack denial, which encode real accidents — exists only as claude-org-ja contract prose. Whether that corpus becomes a default worker fence shipped by Interlock (core) or role content authored by the operating layer is a placement decision nobody has made. It touches Q-0022's shape (v1 vocabulary inside carried artefacts). Disposition: decide placement when G2 authors the successor's worker roles. |
| U2 | **Ownership of the retired loop's non-detection duties** (M7, and the sweep/aging/curate-inflight family) | Q-0019 records exactly this: relay, drain, aging, auto-stop, curate-inflight had the v1 loop as their de facto owner, and neither 2026-08-17 comment assigns new owners. Rows M7/T6/K2 map operating-org capabilities onto that unresolved assignment. Classifying them as covered-by-reconcile-loop would answer Q-0019 by audit, which this document must not do. |
| U3 | **Autonomous conveyor belt** (D15) | Whether the successor org wants completion-driven autonomous operation between human gates is an operator preference, not a parity fact. Its invariants that matter (merge never pre-approved; machine exit conditions; observability) are separable and mostly land in G3/G10 regardless. |
| U4 | **Secretary session-level responsiveness conventions** | The operating org keeps the Secretary responsive by convention (async watchers, background panes). Interlock's counterpart is structural (D-0016; gate item 8 rehearsed with a lock-free bounded intake queue, discharge before the canary at a Q-0011 threshold). Until Q-0011 fixes a threshold and the real Secretary exists, "is the v1 convention matched?" has no measurable answer. |
| U5 | **Two-repo release engineering** (C12) | Specific to claude-org-ja ↔ claude-org-runtime coupling. The successor operating repo's relationship to Interlock (import? pin? vendored?) is not yet designed; until it is, there is nothing to compare against. |
| U6 | **Silent/stalled-worker detector semantics** (M3) | The vocabulary replacement is decided (D-0005/D-0006/D-0008) but the replacing detection predicates and tolerable latency are exactly Q-0012 and Q-0003 — both open. v1's thresholds (30 min same-phase / 1 h silent / round-4) are accident-derived detection semantics that D-0014 says to extract as quarry; whether they survive as predicates, and at what latency, is a Q-0012/Q-0003 resolution, not an audit classification. AC-10 (no regression in detection latency or misses) is the enforcement hook. |

---

## 4. Gap decomposition — issue drafts

Ten `undesigned` gaps materially affect operating efficiency. Each draft below records:
**Placement** (core / operating-layer, one-line rationale), **Priority**
(needed-before-canary / can-follow-canary, one-line rationale), and **Provider shape**
(inherently provider-shaped vs provider-neutral with a rendering edge), per the operator's
two 2026-08-21 directions. Filing goes through the operator's window; these are drafts.

The canary referenced below is ACCEPTANCE §3's: exactly one new run routed to Interlock,
v1 live as counterparty, rollback = routing change (D-0013).

---

### G1 — Project registry: name resolution, clone source, base branch

**Gap rows:** T1, D5, C13.
**Draft title:** `Operating-layer project registry: name→project resolution, clone source, base_branch`

A run needs a project: a resolvable name, a clone source (URL / local path / new), and the
branch work is cut from and merged to (`registry/projects.example.md:32-36,50-57`). v1 keeps this in
an operator-local markdown registry with a tracked template and non-destructive schema
migration (`tools/ensure_projects_registry.py:1-22`). Interlock has run/task rows but no
project concept.

- **Placement:** operating-layer — project identity is business vocabulary, not
  control-plane state; Interlock's runs can carry an opaque project reference without
  owning the registry (keeps Interlock thin per CHARTER §2).
- **Priority:** needed-before-canary — the canary routes a *real* run (ACCEPTANCE §3), and
  a real run cannot be delegated without resolving its project and base branch.
- **Provider shape:** provider-neutral. Pure data + resolution rules; no rendering edge
  needed. The operator-local/tracked-template split (C13) is part of the design.

### G2 — Delegation contract: brief generation, workspace conventions, worker roles

**Gap rows:** D2, D3, D4 (successor half), D7, D14, D17, P6, X2 (workspace half), T6, C2.
**Draft title:** `Operating-layer delegation contract: neutral brief, workspace conventions, worker role content, completion-report schema`

The single largest undesigned surface. v1 renders a per-worker brief from config
(`tools/gen_worker_brief.py:315-349`) with knowledge injection
(`.claude/skills/org-delegate/SKILL.md:148-161`), resolves a workspace
(`tools/resolve_worker_layout.py:1-45`), defines worker roles
(default/self-edit/doc-audit), imposes pre-dispatch quality checks
(`references/delegate-flow-details.md:82-127`), and fixes what the worker owes back (the
completion-report contract with human-understanding summary,
`.claude/skills/org-delegate/SKILL.md:352`). The A/B/C layout itself is discarded (D-0014)
— the successor needs *new, simpler* workspace conventions on top of C2's
Interlock-owned working trees, not a port.

- **Placement:** operating-layer, with one named core touchpoint — briefs, roles,
  workspace conventions, and lifecycle policy are business-workflow material; Interlock
  contributes the fence renderer (D6), the spawn precondition, and session↔run identity,
  and should stay unaware of brief content. The durable half of the lifecycle — the run
  schema, status vocabulary, transition allow-list, and the atomic reservation
  transaction (D2, C2) — is settled with core's Q-0001, not invented by the operating
  layer.
- **Priority:** needed-before-canary — the canary's one real run must be delegated with a
  real brief, a real workspace, and a real completion contract, or it measures nothing
  about operating parity.
- **Provider shape:** provider-neutral with a rendering edge — the brief is a neutral
  document (task, scope, constraints, verification depth, report schema); writing it as
  `CLAUDE.md`/`CLAUDE.local.md` and expressing fences as permission modes/hooks are
  Claude-Code renderers at the edge. One substitution test (render the same neutral brief
  through a second trivial renderer) keeps the seam honest, per the item-11 lesson.
- **Shape (third direction):** the delegation lifecycle this draft owns is modelled as a
  **policy table + state machine** — v1's per-event ack → transition → monitoring flag →
  journal choreography, review-round budgets, and pre-dispatch checklists become
  machine-executed transition obligations, with AI invoked only at judgement-bearing
  transitions (scope edges, escalations). Brief prose and role prose are rendered
  projections of the policy data, eliminating the prose↔practice drift class that v1
  polices by hand (e.g. the hardcoded permission-mode mirrors in
  `registry/org-config.md:1-25`).

### G3 — PR/CI ingestion and run↔PR lifecycle

**Gap rows:** P1, P2 (ingestion half), P3 (need), P4, P7, X1 (flow half), X2, X3.
**Draft title:** `PR/CI outcome ingestion: durable CI events, run↔PR linkage, merge-gate flow`

v1's most accident-hardened area: CI polling with verdict discipline
(`tools/pr_watch.py:2-119`), zero-miss delivery through an exactly-once ledger
(`tools/relay_scan.py:1-22`), run↔PR back-fill and merge auto-completion with
deterministic repo resolution (`tools/resolve_run_repo.py:1-16` — a real 2026-08-06
cross-repo incident), and merge-race honesty (`.claude/skills/org-pull-request/SKILL.md:244-246`).
Interlock's outbox covers delivery (S7); nothing observes CI or links runs to PRs.

- **Placement:** split — the durable event ingestion path and run↔PR linkage rows are
  core-adjacent (durability, delivery, recovery: they extend D-0001 state and reuse the
  outbox); the `gh`-polling watcher and merge-gate presentation flow are operating-layer.
  Proposed: core owns the event/linkage schema; operating layer owns the watcher process
  and the human flow.
- **Priority:** needed-before-canary — AC-10 forbids regressing detection latency and
  misses, and the canary's run completes via a PR; without CI ingestion the canary's
  completion is observed by a human squinting at GitHub, which is the regression.
- **Provider shape:** provider-neutral — `gh` is the interface to GitHub, not to the
  session provider; nothing here touches Claude-Code specifics. The UI-independence
  property (P3's need) is a D-0009-shaped constraint: the watcher must not die with a
  human-facing session.
- **Shape (third direction):** single event spine — CI outcomes are written once to the
  events table and every consumer (secretary AI, delivery, completion transition) reads
  from it, assuming the S8 `MessageBus`; this removes v1's *push-vs-poll duplication*
  (best-effort push + relay scan as separate delivery paths, P2) by construction. It does
  **not** remove the need for liveness: a dead `gh` watcher or a stalled consumer produces
  no row that proves it stopped — the exact silent-no-op failure `tools/relay_scan.py:41-55`
  documents (a broken cron accumulating undelivered events for weeks). G3 therefore keeps
  a deterministic backstop as a design requirement: a watcher/consumer heartbeat written
  unconditionally, plus a reconcile pass over undrained events — D-0002's program-side
  reconcile loop shape, not a repair layer bolted on after. AC-10 (no regression in misses
  or detection latency) is the acceptance hook. The merge-approval halt is a first-class
  `Gate` entity (see §1.2), not a journal event plus prose.

### G4 — Worker escalation ledger and relay-gap detection

**Gap rows:** M4, C8 (ledger half).
**Draft title:** `Durable worker-escalation ledger: judgment-call lifecycle and relay-gap detection`

Worker-initiated judgment requests traverse worker → Secretary → human → worker. v1
persists each hop in a register (`tools/pending_decisions.py:1-20`) precisely so a dropped
relay is detectable (`SECRETARY_RELAY_GAP_SUSPECTED`, `CLAUDE.md:140-142`) and so pending
decisions survive a Secretary restart. Interlock's incident contract (D-0007) covers
watcher-*detected* conditions; a worker's own escalation is a different, undesigned entry
point — though it should land as rows in the same SQLite SoT; which component ages them is
settled together with Q-0019, not by this draft.

- **Placement:** core — this is durable state with delivery and recovery semantics
  (survives restart, resumable by query, aging detectable deterministically); exactly
  D-0001/D-0002 material. The escalation *policy* (what must be escalated) stays
  operating-layer prose.
- **Priority:** needed-before-canary — the canary's worker will ask questions; losing one
  relay during the canary falsifies the migration's headline claim (nothing is lost that
  was written down). The v1 detector exists because relays were actually dropped.
- **Provider shape:** provider-neutral — ledger rows and deadline evaluation; the intake
  edge is the `MessageBus`/Secretary boundary, already provider-neutral by D-0009.
- **Shape (third direction):** the escalation ledger is the `Gate` entity in its
  escalation form — owner, rationale, options, deadline, outcome — driven off the events
  table rather than off message observation. The gate carries explicit **stages**
  (received → presented-to-human → answered → forwarded-to-worker), preserving v1's
  four-step register semantics (`tools/pending_decisions.py:1-20`): relay-gap detection is
  a deterministic query over *incomplete transitions*, each aged with its own tolerance —
  a request received but not presented is a Secretary-side gap within minutes, while
  presented-but-unanswered is a slow human, not a gap, and answered-but-not-forwarded is a
  gap again. A single open-past-deadline predicate would both false-alarm on slow humans
  and miss a dropped forward; the staged form does neither, machine-evaluated, with AI
  nowhere in the detection path.

### G5 — Human attention channel: awaiting-user signalling and notification backend

**Gap rows:** M5, M6 (control-plane integration half and backend half; Q-0017).
**Draft title:** `Human attention channel: blocked-on-human signalling and a notification backend (resolves Q-0017)`

When the organisation is blocked on a human, v1 says so at the moment it happens
(`awaiting_user` at four gates, `CLAUDE.md:110-129`) and a watcher turns it into a desktop
notification/sound (`docs/operations/attention-watch.md:1-17`). Interlock carries the
classifier and dedup but not the integration: the watcher's readers still consume v1's
legacy tables (`attention/readers.py` is a rewrite row, and the spike control-plane schema
has no `events` table for it), so re-targeting the read path onto Interlock's own state is
part of this draft's scope. The desktop backend was deleted deliberately (purge record for
`attention/platform.py`), leaving stdout; Q-0017 records that reinstating a channel is a
new decision. The human-latency reduction this buys is real: v1 added ask-time emits
because 15-minute aging was too slow for interactive gates
(`.claude/skills/org-escalation/SKILL.md:59-76`).

- **Placement:** split — the blocked-on-human *fact* and its gate taxonomy are
  operating-layer signalling written into core state (an event kind, not a new mechanism);
  the notification backend decision is core's Q-0017 to resolve.
- **Priority:** needed-before-canary — canary go/no-go includes human response latency in
  practice; a canary run silently waiting hours on an unnoticed approval measures the
  notification gap, not the migration.
- **Provider shape:** the signalling is provider-neutral; the notification backend is
  inherently *environment*-shaped (OS notification APIs) — an adapter per environment with
  a stdout/bell fallback, which is the shape v1 already had.
- **Shape (third direction):** blocked-on-human is not a fire-and-forget event but the
  creation of a `Gate` entity: notification = gate creation, approval/answer = gate
  resolution, which is the operator's candidate answer to Q-0017. The four v1 gate kinds
  (M5) become gate types, the web Secretary window (interlock#29) is the gate-queue
  display and approval UI, and structured audit (who resolved what, when, why) falls out
  of the entity instead of being reconstructed from journal prose. This draft and G4
  share the `Gate` schema; whichever files first carries it.

### G6 — Measurement: acceptance metrics and activity reporting

**Gap rows:** R2.
**Draft title:** `Measurement harness for AC-9/AC-10 and canary comparison reporting`

AC-9 (≥95% AI prompts / ≥90% output tokens reduction) and AC-10 (no regression in
detection latency, false termination, misses) are measured criteria against a measured
baseline (ACCEPTANCE §5), and the canary requires a divergence report (AC-7). v1 has a
read-only metrics reporter (`tools/org_metrics_report.py:1-27`); Interlock has the
criteria but no instrument.

- **Placement:** core — it measures control-plane behaviour (AI turns, incidents,
  detection latency) from core's own SQLite records; read-only by construction.
- **Priority:** needed-before-canary — a canary without its instrument cannot be judged;
  AC-7's divergence report is due during the shadow period.
- **Provider shape:** provider-neutral for state-derived metrics; the AI-token halves of
  AC-9 are provider-shaped at the edge (token/usage figures come from the provider's own
  reporting) and the draft must name that adapter.

### G7 — Work discovery and triage

**Gap rows:** T2, T3, T4, T5.
**Draft title:** `Operating-layer work discovery: deterministic cross-repo triage with propose-only presentation`

Deterministic, auditable next-work candidate generation (`tools/work_discovery_scan.py:1-9`)
with registry-driven repo sets, exclusion disclosure, and propose-only discipline
(`.claude/skills/work-discovery/SKILL.md:43-46`), fed proactively after merges
(`CLAUDE.md:39-46`).

- **Placement:** operating-layer — where work comes from is business workflow; it consumes
  the registry (G1) and produces delegation candidates (G2), touching no control-plane
  state.
- **Priority:** can-follow-canary — the canary is one routed run; its work item can be
  chosen by a human. Efficiency loss without it is real but bounded and does not
  invalidate canary measurements.
- **Provider shape:** provider-neutral (`gh` reads, deterministic ranking, human choice).

### G8 — Retro, knowledge curation, and review-gate policy

**Gap rows:** R1, K1, K2 (trigger content), K4, K5, K6 (knowledge split), P5, D16.
**Draft title:** `Operating-layer learning loop: retro, raw→curated pipeline, skill candidacy, independent review gate, dogfood register`

The whole learning loop above the (covered) promotion gate: structured retro
(`.claude/skills/org-retro/SKILL.md:32-39`), the raw→curated pipeline with its archival
discipline (`docs/contracts/knowledge-curation-contract.md:80-108`), 5-signal candidacy
scoring and batched human sign-off (`.claude/skills/skill-eligibility-check/SKILL.md:51-66`),
skill-inventory governance (`.claude/skills/skill-audit/SKILL.md:35-59`), the independent
review gate policy (P5), and the dogfood paired-issue register (`registry/dogfood_pending.md:14-19`).

- **Placement:** operating-layer — named as such by the Issue's placement direction
  ("retro/curate 等の業務層"); the only core touchpoint is the already-shipped promotion
  gate (K3), which this layer feeds.
- **Priority:** can-follow-canary — no canary measurement depends on it; the loop's value
  compounds over many runs, and the promotion gate already prevents the dangerous failure
  (unapproved skill reflection). One carve-out so the canary run's completion path stays
  comparable to v1's: the canary run's *own* independent review gate (P5) is carried as
  brief content for that run (G2 renders the review instruction, exactly as this task's
  brief does today) — what can follow the canary is the systematised gate policy, not the
  single run's review.
- **Provider shape:** provider-neutral concepts; two rendering edges to name in the draft —
  skill material rendered as `.claude/skills` (one renderer behind the promotion gate),
  and the review gate's codex CLI adapter.

### G9 — Operator projection: dashboard / org state view

**Gap rows:** M9, and the human-projection residue of C1/M10.
**Draft title:** `Operating-layer dashboard: live read-only projection of runs, incidents, and knowledge`

v1's stdlib dashboard renders workers, work items, activity, projects, and knowledge from
SQLite with SSE push (`dashboard/server.py:396-408`). Interlock declares UI a rebuildable
projection (D-0001) and ships none; with pane-attach viewers discarded (M10), the operator
currently has no way to *see* the organisation.

- **Placement:** operating-layer — a read-only projection over core's SQLite; keeping it
  out of core preserves "AI context and UI are projections, never the source of truth"
  (D-0001) as a one-way dependency.
- **Priority:** can-follow-canary — the canary is one run, observable by query; a
  dashboard becomes valuable at normal operating breadth. (The shadow-period divergence
  report, which *is* needed, lives in G6.)
- **Provider shape:** provider-neutral (reads SQLite; renders HTML).

### G10 — Operating policy pack: task routing, scope discipline, session conventions

**Gap rows:** D10, D11, D12, D15 (invariant residue), M13 (successor half), C10.
**Draft title:** `Operating-layer policy pack: task sizing lanes, scope discipline, heavyweight-task arming, org bring-up conventions`

The residual policy corpus that makes the operating org efficient but is neither state nor
mechanism: two-lane routing conditions (`CLAUDE.md:75-88`), 1-worker-1-task-1-scope with
escalation-only expansion (`CLAUDE.md:92-100`), ultracode arming for heavy tasks
(`CLAUDE.md:90`), and what "bringing the successor org up" means once panes are gone.

- **Placement:** operating-layer — pure business policy over Interlock primitives.
- **Priority:** can-follow-canary — the canary's single run needs none of these lane
  decisions; they matter at operating breadth. (If the canary task itself is heavyweight,
  its brief simply says so — G2 covers brief content.)
- **Provider shape:** provider-neutral policies with named provider-shaped edges
  (permission modes, hook enforcement, multi-agent arming are harness features; the policy
  text must keep them at the edge, per the operator's second direction).
- **Shape (third direction):** these are the gaps whose natural fix looks like "more
  prose discipline for the AI", and the draft must resist exactly that: lane conditions,
  scope boundaries, and arming rules are **policy tables evaluated mechanically** at
  delegation time (G2's state machine consults them); prose renditions are projections
  for humans. AI judgement enters only where the policy says a transition is
  judgement-bearing.

---

## 5. Summary

Ledger totals (69 rows in §2; split rows count once under their first-listed class):

- **covered:** 16 rows — the control-plane core (state, delivery, fencing, identity,
  curation gate, role boundaries) is not where migration loses capability; in several rows
  (C1's covered half, C6, K3) Interlock is strictly stronger than the operating org's
  mechanism.
- **deliberately-reduced:** 12 rows — each traces to a recorded Discard bucket (pane
  transport, screen scraping as contract, resident loops, handover prose, A/B/C layout) or
  a retiring decision entry, with the substitute named; none reopens a D-0015 non-goal.
- **undesigned:** 36 rows, consolidated into 10 drafts (G1–G10) — 6 needed before the
  canary (registry, delegation contract, PR/CI ingestion, escalation ledger, attention
  channel, measurement), 4 can follow it (work discovery, learning loop, dashboard,
  policy pack). One row (P8) is dispositioned without a draft; C11's live-schema-migration
  half is dispositioned to core's Q-0001 resolution; T6 and D17 are folded into G2's
  scope.
- **undetermined:** 5 ledger rows (M3, M7, D15, C7, C12); §3 lists 6 items in total — U1,
  U3, U5, U6 correspond to ledger rows one-to-one, U2 gathers M7/T6/K2 under Q-0019, and
  U4 is a convention with no single ledger row. Each is tied to an open question (Q-0003,
  Q-0011, Q-0012, Q-0019, Q-0022) or an operator preference, deliberately not answered by
  this audit.

The pattern is consistent with the fork's design intent: what Interlock rebuilt, it
rebuilt at least as strong; what it discarded, it discarded on record with substitutes;
what it never designed is almost entirely the **operating layer** — and the operator's
placement direction (a successor operating repo importing Interlock) gives every one of
those gaps a home that keeps Interlock thin.
