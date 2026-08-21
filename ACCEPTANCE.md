# Interlock — ACCEPTANCE

This file records what Interlock has to demonstrate, and how. It has two distinct surfaces, and
they must not be confused:

- **Section 1 — the Agent View gate.** *Pre-implementation* entry criteria. Per D-0019 these are a
  precondition for starting implementation; the first work after these founding documents is the
  Agent View spike, not feature code.
- **Section 5 — inherited acceptance criteria.** The ten Acceptance Criteria decided in the
  [2026-08-17 「発動決定と終端設計の修正」 comment](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070).
  The [Interlock 分岐決定 comment](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345)
  explicitly does not retract them; they are inherited and remain the acceptance surface for the
  watcher / incident design once implementation is under way.

The **Fault injection targets**, **Canary and rollback**, and **If the Agent View gate fails** sections support both.

**Cross-references.** References into `DECISIONS.md` are by stable decision ID (`D-00NN`) and
open-question ID (`Q-00NN`). References within this file are by section name or by the stable
`AC-N` / gate-item numbers. Never cite by line number or by heading ordinal.

**A note on numbers.** Where a threshold is needed and Issue #740 does not state one, this file
says so and points at the relevant `Q-` entry in `DECISIONS.md`. It does not invent thresholds.
Figures the Issue gives as estimates or targets (e.g. "12–15k LOC", "5–20 incidents per 100 worker
runs", "about 470–1,576 program ticks") are labelled as such; the 2026-07-18…2026-07-25 dogfood
figures are **measured baseline**.

---

## 1. Agent View gate (pre-implementation)

Source: [実装開始前の Agent View gate](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345).
All eleven items, verbatim in intent, none omitted. Every item is pass/fail (D-0019). The
verification methods below are proposals for how to demonstrate each item during the Agent View
spike; where a numeric threshold would be required and the Issue fixes none, the row says so.

**Where the verdicts live.** This section states the items and how each is to be verified. The
per-item **verdicts** — verdict, evidence, provider, and the D-0022 label every entry must carry —
live in `docs/gate-record.md`, which is written as the spike runs and is due whether the gate is
discharged or terminated.

**A caveat on "pre-implementation".** The items are not uniform in what they presuppose. Items 1–3
are provable against the Agent View CLI with a thin spike harness. Items 4–6, 9, 10 and 11
presuppose the very things they test — SQLite recovery, a `MessageBus`, a Curator promotion path, an
operational canary, a control-plane suite to re-run against a second provider. The Issue places the
gate before implementation (D-0019) and that position is not weakened here; what it does not say is
what minimum scaffold discharges each item, which is recorded as Q-0021. In practice these rows are
expected to be proven on a deliberately minimal vertical slice built as part of the spike, and
re-proven on the real implementation via the inherited criteria.

| # | Gate item | Verification method | Decisions |
|---|---|---|---|
| 1 | The **public CLI alone** can start, obtain structured state of, stop, and resume a top-level worker. | Drive one worker through start → structured-state read → stop → resume using only documented public CLI commands, with a spike harness that has no code path touching `~/.claude/jobs`, internal sockets, or unpublished JSON/transcript formats. Prove the negative by running the harness with those paths made unreadable/absent and observing no behaviour change. Assert the structured state is machine-parseable from published output, not scraped from rendered screen text. Record the exact CLI version used and the output of the capability/version probe. | D-0010, D-0009 |
| 2 | Session ID and run can be **uniquely re-matched, including across the crash window**, preventing a duplicate active writer. | Persist the session↔run binding in SQLite **before** the spawn (D-0024: the identity is chosen pre-spawn and committed durably before the process exists). Then kill the supervisor at each of: before the binding is committed, between the commit and the spawn, between the spawn and the identity read-back, and after the read-back — where "after the read-back" means after the successful read-back has itself been committed to SQLite; restart, and assert that re-identification yields exactly one session for the run and that a second writer is refused rather than admitted. Enumerate sessions via the provider's public surface and assert no orphan session is adopted twice. A single-writer violation at any injection point is a gate failure. *(This row originally read "between spawn and commit"; D-0024 made commit-before-spawn mandatory and post-hoc adoption unacceptable, so the injection points were re-synchronised to that ordering — the change is disclosed here rather than silently rewritten.)* | D-0001, D-0009, D-0010, D-0024 |
| 3 | Per-role **permission / sandbox / hooks survive restart** and **fail closed** rather than falling back to default permissions when missing. | For each role, capture the effective permission / sandbox / hook configuration after spawn, restart the session via the provider, and diff the effective configuration against the pre-restart capture — it must be identical. Then inject the missing/corrupt cases (config file deleted, hook path unresolvable, sandbox profile absent) and assert the spawn is **refused** and recorded, never downgraded to defaults. Complement with a breach probe: attempt a role-forbidden operation post-restart and assert it is denied. | D-0017, D-0010 |
| 4 | Supervisor / Dispatcher Core / Secretary **resume from SQLite after a mid-flight kill with no double execution**. | `SIGKILL` each of the three, separately and in combination, at points chosen to straddle a durable write (before write, after write and before side effect, after side effect and before acknowledgement). Restart and assert: state is reconstructed by query from SQLite alone; work resumes from unresolved incidents; every side effect is applied exactly once, evidenced by an idempotency/dedup record rather than by absence of a visible duplicate. See **Fault injection targets** for the injection matrix. | D-0001, D-0007 |
| 5 | **Lease, outbox resend, ack, dedup, single-writer** confirmed by **fault injection**. | Run the matrix under **Fault injection targets** as an automated suite, each case asserting its named invariant against a durable observable: a SQLite query for control-plane state, and — for any case involving an **external** effect — additionally the destination's own idempotency/effect record, since SQLite cannot distinguish an effect that completed from one that never started. A case that certifies exactly-once for an external effect from our rows alone does not count. Gate passes only if every case is automated and reproducible (no manual one-shot demonstrations). | D-0001, D-0007 |
| 6 | `MessageBus` **delivers and resends independently of the Agent View UI**. | With no Agent View UI attached (headless, UI process not running, or UI killed mid-delivery), send a task, drop the first delivery attempt, and assert the outbox resends and the recipient acks exactly once. Repeat with the UI attached but its session state deliberately stale, and assert delivery outcomes are unchanged — delivery decisions must be derived from SQLite, not from UI/session state. Statically assert the `MessageBus` implementation has no dependency edge to the `SessionProvider`. | D-0009, D-0001 |
| 7 | **Unsaved artifacts are protected from the Agent-View-managed worktree lifecycle.** | Create uncommitted and untracked changes in a worker worktree, then trigger every provider-driven lifecycle transition the public CLI exposes (stop, resume, session end, cleanup/reclaim) and assert the working tree content is byte-identical afterwards, or that the transition is refused while unsaved work exists. If the provider can reclaim a worktree without an interlock the control plane can observe or veto, that is a gate failure, not a workaround. | D-0009, D-0010 |
| 8 | **Secretary window responsiveness is maintained while workers are loaded.** | Measure Secretary request→response latency at idle to establish a local baseline, then repeat under load: workers running at the concurrency cap, an incident open and awaiting Dispatcher AI judgement, and a long-running task in flight. Assert no Secretary response is blocked *behind* worker monitoring, long-running work, or an AI judgement — structurally, by showing intake and the queue boundary are asynchronous, and empirically, by the latency comparison. **Numeric latency threshold unresolved — see Q-0011 in `DECISIONS.md`**; the gate check is the absence of blocking dependencies plus a recorded baseline-vs-load comparison. | D-0016, D-0002 |
| 9 | **Curator output cannot reach a skill without human approval.** | Run the Curator on demand and attempt to promote its output to a skill with the approval record absent, forged-but-unrecorded, and revoked. Each attempt must be refused and the refusal recorded. An approval record existing is **not** sufficient: the approval must name an immutable candidate version (content digest), so add two further negative cases — mutate the candidate after approval and attempt promotion, and replay a valid approval against a different candidate. Both must be refused, otherwise content no human reviewed reaches a skill while every check nominally passes. Assert there is no code path from Curator output to skill material that does not pass the approval gate (path audit plus a negative test that fails the build if such a path is added). | D-0018 |
| 10 | **One-worker canary and run-boundary rollback hold.** | Execute **Canary and rollback** end to end: route exactly one new run to Interlock, let all v1-started runs finish on v1, run shadow observation read-only, then exercise rollback by routing subsequent new runs back to v1 and assert no data migration or in-flight state conversion was required. Assert no dual-write occurred (writer audit over both systems' stores). **Canary duration, sample size, and numeric go/no-go criteria unresolved — see Q-0005 in `DECISIONS.md`.** | D-0013 |
| 11 | **Even if Agent View does not hold, only the `SessionProvider` need be swapped.** | Demonstrate substitutability rather than argue it: implement a second, deliberately trivial `SessionProvider` (e.g. a local process-based stub) behind the same contract and run the control-plane test suite — SQLite SoT, fact states, incident lifecycle, `MessageBus` delivery/ack/dedup, role boundaries — unchanged against it. Any test that has to be modified marks a leak of session-backend detail into the control plane and must be fixed before the gate passes. See **If the Agent View gate fails**. | D-0019, D-0009 |

---

## 2. Fault injection targets

Scope per gate item 5. Every case is an automated, reproducible test, and every "observable that
proves it" is a durable record — never a screenshot, a log line read by a human, or the absence of a
visible symptom.

For control-plane state, that record is a query against SQLite (D-0001) or a persisted incident
field (D-0007). For an **external side effect**, SQLite is not sufficient on its own: when a process
dies after the effect succeeded but before its result was recorded, no query of ours can tell that
apart from an effect that never started. Those cases must additionally be proven against the
destination's own idempotency or effect record — see the mid-flight kill discussion below. A case
that asserts exactly-once for an external effect using only our own rows does not pass.

| Target | What is injected | Expected invariant | Observable that proves it |
|---|---|---|---|
| **Lease** | Kill the lease holder without release; expire a lease while its holder is paused (SIGSTOP) and let a second claimant take it; return the paused holder; skew the clock forward and backward across the expiry boundary. | At most one live holder per leased resource at any instant. Expiry discovery alone is insufficient — check-then-write leaves a race in which the lease expires between the check and the write. Every protected write must carry a fencing token (lease epoch) validated **atomically as part of the write**, and external destinations must reject a stale token where they can enforce it. | Lease rows in SQLite show a single active holder per resource across the whole timeline; the returning holder's write attempt is refused and that refusal is recorded, not silently dropped. |
| **Outbox resend** | Drop the delivery, kill the sender after the outbox row is written but before delivery, kill after delivery but before the ack is recorded, and hold the recipient unavailable across several retry attempts. | Every enqueued message is eventually delivered at least once; nothing is lost by a kill at any of those points; retry count is durable across restarts. | Outbox row transitions to delivered/acked with a monotonically increasing, restart-surviving retry count; no outbox row remains in a state with no owner after recovery. |
| **Ack** | Lose the ack in flight; duplicate the ack; deliver the ack after the sender has restarted; ack an already-acked message. | Ack is idempotent. A lost ack causes a resend (safe), never a lost message. A duplicate or late ack changes nothing. | Message identity in SQLite shows exactly one acked state regardless of ack multiplicity; the recipient's effect count is one. |
| **Dedup** | Deliver the same message twice; raise the same incident condition repeatedly within a window; replay a persisted incident packet; restart between the duplicate arrivals. | Duplicate delivery causes exactly one effect. A repeated incident condition is collapsed under its dedup key rather than producing an unbounded stream of incidents; `dedup key` and `retry count` are required incident fields (D-0007). | One effect record per delivery dedup key. For incidents, the observable follows from whatever collapse rule Q-0002 settles on — **the Issue fixes the fields, not the semantics: whether a repeat increments `retry count` on the existing incident or opens a linked one is unresolved, as is the re-notification window in absolute time — both are Q-0002** (Q-0003 covers the reconcile interval and tolerable detection latency that Q-0002 depends on). Tests must parameterise both rather than hard-code either. |
| **Single-writer** | Two writers race for the same state item; a partitioned/stale writer returns after its lease expired; a write is attempted concurrently from a resumed process and its replacement. | Exactly one writer may write a given state item at a time; a stale writer is rejected, not merged. | The state item's history in SQLite is a linear sequence with no interleaving from the rejected writer; the rejection is itself recorded. **The per-item writer assignment table is unresolved — see Q-0001 in `DECISIONS.md`**; until it exists, tests assert the property per item exercised, not against a global table. |
| **Observation outage** *(supporting D-0006)* | Make the observation path fail or return nothing while the worker is genuinely healthy. | Observation failure is classified `OBSERVATION_UNAVAILABLE`, never as an anomaly, and `NO_ACTIVITY_EVIDENCE` is not treated as an anomaly either. | Incident/fact-state rows show the outage classified as observation-unavailable; no termination or restart recommendation is produced from it. (See D-0005, D-0006 and inherited criteria AC-3/AC-4.) |

**Mid-flight kill (cross-cutting).** Killing the daemon, the Dispatcher AI, or the Secretary
mid-flight must result in resumption **from unresolved incidents, without double execution**
(D-0001, D-0007, and inherited criterion AC-8). The kill matrix must include, for each
component: before the durable write, after the durable write but before the side effect, and after
the side effect but before its result is recorded.

The third case is the one that proves idempotency rather than luck, and it is also the one with a
real limit: **SQLite alone cannot distinguish "the side effect completed" from "the side effect
never started"**, because by construction the result was not recorded. Recovery must therefore not
be specified as "infer the outcome from SQLite". Exactly-once has to come from one of two places,
and each action handler must declare which one it uses:

1. **A destination-supported idempotency key** — the effect is replayed with a key the destination
   deduplicates, so a repeat is harmless whether or not the first attempt landed; or
2. **Making the effect transactional with its durable record** — the effect and the record commit
   together, which collapses the ambiguous window rather than resolving it after the fact.

Where neither is achievable for a given action, the gap is explicit and the action requires a human
gate (D-0004) rather than automatic recovery. The gate check is that every action handler names its
mechanism and demonstrates it under injection — not that SQLite is queried for an answer it cannot
hold (D-0001, D-0007, and inherited criterion AC-8).

---

## 3. Canary and rollback

The numbered conditions below are from D-0013, as is the rollback rule. Anything this section adds
beyond them is marked explicitly as an assumption or an open question rather than stated as decided.
**Rollback is a routing change, not a data migration** — that is the property that makes the canary
cheap, and it is the property to verify.

**Canary shape.**

1. **One worker.** Exactly one new run at a time is routed to Interlock (consistent with D-0017:
   workers are few and capped).
2. **No dual-write.** Interlock and v1 never both write the same authoritative record. There is one
   source of truth per run (D-0001).
3. **v1-started runs finish on v1.** No run is moved between systems while it is in flight.
4. **Only new runs from the canary onward go to Interlock.** Routing is decided once, at run start.
5. **Shadow observation is read-only.** If shadow observation is used, it rewrites neither ownership
   nor the source of truth. It may read and diff; it may not adopt, correct, or complete anything.
6. **No state conversion of in-flight runs.** There is no converter, and building one is not a
   fallback plan.
7. **Any bridge is migration/comparison only.** A bridge built for the cutover is temporary by
   construction and never becomes a permanent Interlock API (see also D-0014's discard of permanent
   compatibility shims).

**Rollback.**

- **Rollback = route subsequent new runs back to v1.** Nothing else is required and nothing else is
  permitted: no export, no import, no schema translation, no back-fill of Interlock records into v1.
- *Not decided by the Issue:* what becomes of runs already in flight **on Interlock** at the moment
  of rollback. D-0013 forbids converting in-flight state and says v1-started runs finish on v1, but
  it does not state the mirror case. Letting them finish on Interlock is the reading consistent with
  "no state conversion of in-flight runs", and is the assumption this document proceeds on — it is
  recorded here as an assumption, not as a decision, and needs settling before the canary
  (see Q-0005). Rollback reversibility is likewise not addressed by the Issue.

**Verification.**

- Writer audit across both systems' stores over the canary window, asserting no record was written
  by both (condition 2).
- Ledger of run → owning system at run start, asserting no run changed owner mid-flight
  (conditions 3, 4, 6).
- Permission/interface audit of the shadow path, asserting it holds no write capability at all
  (condition 5) — read-only enforced by capability, not by convention.
- A rehearsed rollback executed as part of the canary, timed and recorded, asserting that the only
  change applied was the routing decision (rollback condition).
- Bridge inventory, if a bridge exists, with its callers and its removal condition recorded
  (condition 7).

**Unfixed numbers.** The Issue fixes the canary's *shape* but not its **duration, sample size, or
numeric exit / rollback criteria** — **unresolved, see Q-0005 in `DECISIONS.md`**. It does state
qualitative conditions (a shadow-period divergence report must exist; rollback conditions must
exist; detection latency, false termination, and misses must not regress) and reduction *targets*
(≥95% AI prompts, ≥90% output tokens per 100 worker runs — see inherited criteria AC-9/AC-10). Those
targets are not the same thing as canary go/no-go thresholds, and this document does not convert
one into the other. Related unfixed values: tolerable detection latency and the reconcile period
(Q-0003), and the dedup/re-notification window in absolute time (Q-0002).

For sizing the comparison, the **measured baseline** available is: 195 completed runs within under
7 days, mean 1.09 h, median 0.66 h, p90 2.55 h, 212.2 worker-hours total, 153.7 h union uptime,
average concurrency while active 1.38. 90% of measured completed runs finished within 2.55 h, so
the coexistence window is expected to be hours rather than weeks; no maximum run length is recorded
in the baseline, so this is an expectation, not a bound.

---

## 4. If the Agent View gate fails

Per D-0019: **the Interlock control-plane design is not discarded. Only the `SessionProvider` is
replaced.** A gate failure is a verdict about one backend, not about the architecture.

**What stays intact** (unchanged by the identity of the session backend):

- SQLite as the single source of truth, and resume-after-kill from it — **D-0001**.
- Retirement of the resident/periodic LLM loop; the program-side event loop and low-frequency
  reconcile loop remain — **D-0002**.
- On-demand, incident-triggered Dispatcher AI; zero AI turns absent incidents — **D-0003**.
- Assessment-only AI with side effects routed through Secretary / human gate / privileged handler —
  **D-0004**.
- The closed fact-state set — **D-0005** — and the separation of observation-unavailable /
  insufficient evidence from anomaly — **D-0006**.
- The incident contract: persisted packet, restricted tool surface, fixed return schema —
  **D-0007**.
- The three-layer responsibility boundary with explicit non-responsibilities — **D-0008**.
- `MessageBus` as a separate contract from `SessionProvider`: delivery, ack, dedup, outbox, message
  identity — **D-0009**. This is the decision that makes the whole branch possible.
- Role boundaries: Secretary as the single non-blocking window (**D-0016**), few capped fenced
  workers (**D-0017**), on-demand Curator with a human approval gate (**D-0018**).
- Lineage-fork posture and no back-port to v1 — **D-0011**, **D-0012**.
- Run-boundary cutover with no dual-write — **D-0013**.

**What has to be re-established against a new provider** — i.e. the gate items whose evidence is
provider-specific and must be re-run in full:

- Start / structured state / stop / resume through whatever public interface the new provider
  offers, plus a capability/version probe and fail-closed-on-new-spawn behaviour if that provider is
  also unstable (gate items 1, per D-0010's spirit; note D-0010 as written names the public CLI of
  Agent View specifically).
- Unique session↔run re-identification across the crash window, and prevention of a duplicate active
  writer (gate item 2, D-0001).
- Restart-surviving, fail-closed per-role permission / sandbox / hooks (gate item 3, D-0017).
- Protection of unsaved artifacts from whatever workspace lifecycle the new provider manages
  (gate item 7).
- Secretary responsiveness under load on the new provider (gate item 8, D-0016).
- The one-worker canary and run-boundary rollback re-run against it (gate item 10, D-0013).

The fault-injection targets and the inherited criteria are control-plane properties and
are re-run for regression, but they do not have to be redesigned.

**Which concrete alternative provider is chosen is an open question** — **Q-0004 in
`DECISIONS.md`**. No candidate is named or evaluated in Issue #740, and D-0014's discard of
tmux/pane/send-keys as a *backend contract* means "fall back to v1's transport" is not automatically
available as an answer. This document deliberately does not pick one.

---

## 5. Inherited acceptance criteria

These ten criteria come from the
[2026-08-17 「発動決定と終端設計の修正」 comment](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070).
The [Interlock 分岐決定 comment](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345)
inherits them without retraction. They are the acceptance surface for the **watcher / incident
design** and apply *during and after implementation* — unlike the **Agent View gate**, which is the
**pre-implementation** gate. Both surfaces are live; neither replaces the other.

- [ ] **AC-1 — Zero AI turns absent incidents.** In normal operation, the Dispatcher AI takes no
      turn unless an incident exists. *(D-0003, D-0002)*
- [ ] **AC-2 — Fixtured deterministic tests.** Known lifecycle / wait / error / relay / terminal
      determinations are captured as fixtures and reproduced by deterministic tests. *(D-0005,
      D-0007; detector-version semantics for replay unresolved — Q-0009)*
- [ ] **AC-3 — `OBSERVATION_UNAVAILABLE` is not conflated with anomaly.** *(D-0006, D-0005)*
- [ ] **AC-4 — Insufficient evidence is expressible.** The system does not assume that normal vs.
      abnormal can always be decided for an ambiguous stall; it can say "not enough evidence".
      *(D-0006, D-0007 — `confidence` and `missing_evidence`)*
- [ ] **AC-5 — Auditable and resumable from SQLite.** Incident, assessment, approval, action, and
      result can be audited and resumed by query against SQLite. *(D-0001, D-0007; schema and
      single-writer assignment unresolved — Q-0001)*
- [ ] **AC-6 — Direct approve / restart / close / reassign excluded from the AI's tools and
      permissions.** *(D-0004, D-0007, D-0008; the AI's auth identity and tier unresolved —
      Q-0007)*
- [ ] **AC-7 — Shadow-period divergence report and rollback conditions exist.** *(D-0013; the
      numeric criteria are unresolved — Q-0005. See **Canary and rollback**.)*
- [ ] **AC-8 — Resume from unresolved incidents without double execution after a mid-flight kill**
      of daemon, AI, or Secretary. *(D-0001, D-0007. See **Fault injection targets**.)*
- [ ] **AC-9 — Measured reduction: ≥95% AI prompts and ≥90% output tokens per 100 worker runs.**
      These are the Issue's **targets**, to be confirmed by measurement against the **measured
      baseline** (2026-07-18…2026-07-25 dogfood: 1,041 three-minute scheduled iterations, 959 with
      model activity, 3,531 unique assistant/model responses, 4,960 AI tool calls, 567,839 output
      tokens, 1,399,565,488 cache-read tokens, median iteration interval 180.0 s; 195 completed runs
      normalising to roughly 1,576 three-minute dispatcher ticks per 100 worker runs — an
      Issue-stated derivation). Cache-read is a bandwidth indicator for repeatedly referenced long
      context, not new input tokens and not a billing figure. *(D-0002, D-0003)*
- [ ] **AC-10 — No regression in detection latency, false termination, or misses**, confirmed
      against known incident fixtures and dogfood shadow. *(D-0005, D-0006, D-0013; tolerable
      detection latency itself is unresolved — Q-0003, which AC-10 depends on to be stated
      numerically.)*

**Note on scope.** AC-9's reduction figures and the Issue's per-100-run projections (AI monitoring
prompts ≈1,576 → 5–20; program monitoring ticks ≈470–1,576 after migration) are **estimates and
targets stated in the Issue**, not measurements of Interlock. Program-tick reduction is explicitly
not the objective (D-0002); the objectives are AI workload, determinism, reproducibility, and
recoverability (D-0014).
