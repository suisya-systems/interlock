# Interlock — CHARTER

Purpose, non-goals, and role boundaries for Interlock.

This document is an **orientation** document. It states *what* Interlock is and *who owns what*.
It does not argue the rationale — every statement here is anchored to a stable decision ID in
[`DECISIONS.md`](./DECISIONS.md), which holds the context, consequences, status, and source of each
decision. Cite decisions by ID (`D-00NN`), never by line number or heading order.

The source of truth for everything below is claude-org-ja Issue #740, specifically its two
2026-08-17 comments:

- [Interlock 分岐決定](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345)
  — top-level source of truth (implementation target and migration method).
- [発動決定と終端設計の修正](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070)
  — decided, and explicitly **inherited, not retracted**, by the comment above. It holds the watcher
  fact-state closed set, the incident contract, and the responsibility boundary table.

---

## 1. Purpose

**Interlock is a durable control plane for a coding-agent organization.**

It exists so that a small organization of coding agents can be delegated to, observed, recovered,
and audited without a language model being permanently on watch. Three properties define it:

- **SQLite is the single source of truth** (D-0001). `run`, `task`, `session`, `lease`, `incident`,
  `assessment`, `action`, and `outbox` live in SQLite. AI context and the Agent View UI are
  projections, never the source of truth. After a mid-flight kill of any component, the system
  resumes from SQLite — from unresolved incidents — without double execution.
- **Monitoring is deterministic** (D-0002). The resident/periodic LLM monitoring loop is retired.
  The program-side event loop (primary path) and a low-frequency reconcile loop (miss-catcher)
  remain — deleting every loop is explicitly *not* the goal.
- **Semantic judgement is on demand** (D-0003). The Dispatcher AI starts only for incidents that are
  semantically ambiguous. With no incident, there are zero AI turns.

The control plane is *durable* in the operational sense: what the organization knows survives a
crash, because knowledge is written down before it is acted upon, not held in a model's context or
inferred from a screen.

---

## 2. Non-goals

The following are non-goals (D-0015). A proposal matching any of them is rejected by reference to
that ID; reopening one requires a new decision entry.

| Non-goal | Note |
|---|---|
| **Kubernetes and general-purpose orchestrators** | Interlock does not grow into, wrap, or target a generic orchestration platform. |
| **Large-scale agent farms** | The target regime is few, capped workers (D-0017). Scale-out is not a design axis. |
| **A resident AI monitoring loop** | The thing being retired in D-0002. No always-on model watching workers. |
| **Deleting every event/reconcile loop** | The paired half of D-0002: the *program-side* loops stay. Quoting "retire the loop" without this misstates the decision. |
| **Asserting a worker is healthy without evidence** | Absence of evidence is not evidence of health, and not evidence of failure either (D-0006). |
| **Granting the AI unrestricted tmux/shell permission** | The Dispatcher AI's surface is deliberately too narrow to act (D-0004, D-0007). |
| **Cutting existing safety fences purely to reduce LOC** | LOC reduction is not a success metric (D-0014). |

---

## 3. Roles

Interlock names five roles. Each is stated as **Responsibilities** and **Non-responsibilities** —
the second list is as binding as the first. Section 4 shows how these five map onto the three-layer
contract.

### 3.1 Secretary

**Responsibilities** (D-0016)

- Be the **single human-facing window** of the organization.
- Accept human intake and return responses without waiting on background work.
- Execute or approve side effects that cross a permission boundary, and apply policy (D-0004,
  D-0008) — including side effects recommended by the Dispatcher AI.

**Non-responsibilities**

- Does **not** block its response on worker monitoring, on long-running work, or on waiting for AI
  judgement (D-0016).
- Does **not** run an LLM polling loop for continuous monitoring (D-0002, D-0008).
- Is not the detector: it does not decide fact states, and does not own incident creation.

### 3.2 Dispatcher Core (deterministic watcher / reconciler)

**Responsibilities** (D-0008, D-0005)

- Ordinary monitoring, deduplication, deadline evaluation, retry, delivery, recovery, and
  persistence — the deterministic layer of the boundary table in section 4.
- Classify observations into the **closed fact-state set** (D-0005): `ACTIVE_EVIDENCE`,
  `KNOWN_WAIT`, `EXPLICIT_BLOCK`, `NO_ACTIVITY_EVIDENCE`, `OBSERVATION_UNAVAILABLE`, `TERMINAL`.
  Adding a seventh state requires a new decision entry, not a code change.
- Create incidents that satisfy the incident contract (D-0007) and persist them to SQLite (D-0001).
- Run the program-side event loop and the low-frequency reconcile loop (D-0002).

**Non-responsibilities**

- Does **not** evaluate the *meaning* of the work a worker is doing, and does **not** pronounce a
  verdict on an ambiguous stall (D-0008). It may name a stall *candidate*; it may not conclude one.
- Does **not** treat `NO_ACTIVITY_EVIDENCE` or `OBSERVATION_UNAVAILABLE` as an anomaly, and does not
  assume that normal vs. abnormal can always be decided (D-0006).
- Does not approve, and does not own the human-facing conversation.

### 3.3 Dispatcher AI (on-demand)

**Responsibilities** (D-0003, D-0004, D-0007)

- Start **on demand**, only for semantically ambiguous incidents. No incident, no AI turn (D-0003).
- Read the persisted incident packet, request further evidence, and return an **evidence-backed
  classification and recommendation** (D-0007). Its return value is the fixed schema
  `classification`, `confidence`, `evidence_refs`, `missing_evidence`, enumerated `recommendation`,
  `human_gate` — not free-text commands.
- Express insufficient evidence rather than forcing a verdict (D-0006).

**Non-responsibilities**

- Does **not** execute side effects. No direct `approve`, `restart`, `close`, or `reassign`; these go
  through the Secretary, a human gate, or a privileged runtime handler (D-0004).
- Does **not** hold raw shell or tmux operation (D-0008, D-0015).
- Sees **only** the incident packet; its tools are limited in principle to `read_incident`,
  `request_evidence`, `submit_assessment` (D-0007).
- Its context is **not** the source of truth; assessments are appended to SQLite (D-0001).

### 3.4 Worker

**Responsibilities** (D-0017)

- Perform delegated task work, as a **few, capped** set of sessions.
- Operate inside a per-role fence: permission, sandbox, hooks, and tool surface.

**Non-responsibilities**

- Does not widen its own fence — role fencing must persist across restart and fail closed rather
  than fall back to default permissions when configuration is missing (D-0017; verified in
  [`ACCEPTANCE.md`](./ACCEPTANCE.md)).
- Is not a unit of scale: designs may not assume many concurrent workers (D-0015, D-0017).

### 3.5 Curator

**Responsibilities** (D-0018)

- Run **on demand** and organize candidate knowledge into proposals.

**Non-responsibilities**

- Does **not** reflect anything into skills without human approval (D-0018).
- Is not a resident process (D-0002, D-0015).
- Its output is a proposal artifact — nothing downstream may treat it as active guidance before
  approval.

---

## 4. Responsibility boundary table

This table is the **contract**. It is reproduced faithfully in English from
<https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070>, and it is
inherited unretracted by
<https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345>. It is recorded
as D-0008.

| Layer | Responsible for (担当) | **Not** responsible for (非担当) |
|---|---|---|
| Deterministic watcher | pane/process lifecycle, lease, queue/outbox, screen hash & spinner, known error/approval patterns, DB/CI state, timers, dedup/retry, incident creation | semantic evaluation of the work content; pronouncing a verdict on an ambiguous stall |
| Dispatcher AI (on-demand) | semantic evaluation of the incident packet, requesting further observation, evidence-backed classification/recommendation | raw shell/tmux operation; approval; directly terminating, restarting, or reassigning a worker |
| Secretary / human / handler | approving and executing operations that cross a permission boundary; applying policy | LLM polling for continuous monitoring |

**How the five roles map onto the three layers.**

- **Dispatcher Core implements the deterministic watcher layer** in full — it is the named component
  for that row (D-0008, D-0005).
- **Dispatcher AI is the second layer**, one-to-one.
- **Secretary sits in the third layer** as the approving/executing party and the human window
  (D-0016). The *human* and the *privileged runtime handler* are the other occupants of that row
  (D-0004).
- **Worker** is not a layer of this table: it is the observed party the three layers act upon, fenced
  per role (D-0017).
- **Curator** sits *around* the third layer: it produces candidates, and its reflection into skills
  is gated by the same human approval that governs that row (D-0018).

The watcher row lists screen hash and spinner as watcher **signals**. That is not in tension with
Interlock discarding old screen-scraping *mechanisms* as permanent backend contracts — a signal and
a backend contract are different things; see D-0014 and
[`PORTING_LEDGER.md`](./PORTING_LEDGER.md).

---

## 5. Lineage

Interlock is a **lineage fork** (系譜分岐) of
[`suisya-systems/claude-org-runtime@befd3096110d18c928793d4862dba02e4da7ea22`](https://github.com/suisya-systems/claude-org-runtime/commit/befd3096110d18c928793d4862dba02e4da7ea22),
base release **`v0.1.42`**.

- It is **not** a fork maintained for continuous upstream tracking. There are no periodic upstream
  merges; individual security fixes may be taken in, each with recorded rationale (D-0011).
- `claude-org-ja` and the runtime 0.1 line are the **v1 / maintenance line**. Interlock's design is
  **not** back-ported into them (D-0012).
- What is kept from upstream is history, invariants, contracts, and accident-derived fixtures — a
  selective seed port, not a parity rewrite (D-0014). The per-path record is
  [`PORTING_LEDGER.md`](./PORTING_LEDGER.md).
- Migration to Interlock happens at the **run boundary**, without dual-write (D-0013).

---

## 6. Related documents

| Document | What it is for |
|---|---|
| [`DECISIONS.md`](./DECISIONS.md) | The canonical, append-only design decisions with stable IDs (`D-00NN`), each with context, decision, consequences, status, and source. Also holds the canonical open-questions list (`Q-00NN`). Every other document cites it by ID. |
| [`PORTING_LEDGER.md`](./PORTING_LEDGER.md) | The per-path carry / rewrite / discard record against the fork base commit, with a reason and a decision ID for each row. |
| [`ACCEPTANCE.md`](./ACCEPTANCE.md) | The Agent View gate checklist and how each item is verified; fault-injection targets; the one-worker canary and run-boundary rollback conditions; and what happens if the gate fails. |
| [`docs/production-schema.md`](./docs/production-schema.md) | The production control-plane DDL, its migration policy, and the per-item single-writer table (D-0029, resolving Q-0001), plus the event-spine, CI-ingestion, run↔PR-linkage, watcher-liveness and `Gate` schemas. |
| [`docs/time-base-policy.md`](./docs/time-base-policy.md) | Detection latency budgets per incident class, the reconcile period derived from them, gate stage tolerances, and the owner of each duty the retired loop gave up (D-0031, D-0032, resolving Q-0003 and Q-0019). |
| [`docs/measurement-harness.md`](./docs/measurement-harness.md) | What AC-9 and AC-10 are measured over, where their ground truth comes from, and what a report records about itself (D-0038, D-0039, D-0040). |

---

## 7. Open questions

Interlock's founding documents deliberately record **only what Issue #740 decided**. Design points
that implementation needs but the Issue does not settle are *not* stated here as decisions.

**The canonical list of open questions lives in [`DECISIONS.md`](./DECISIONS.md)**, in its
`Open questions` section, as `Q-00NN` entries with `Status: proposed`. They are not duplicated here,
so that there is exactly one place where an unresolved question can be found and resolved.

A `Q-` entry is never an authority to act. It is resolved by adding a new `D-` decision that answers
it, at which point this charter may cite that new ID.
