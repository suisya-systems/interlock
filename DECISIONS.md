# Interlock — DECISIONS

This file is the canonical, append-only record of Interlock's design decisions.

Interlock is a **lineage fork** of `suisya-systems/claude-org-runtime` at commit
[`befd3096110d18c928793d4862dba02e4da7ea22`](https://github.com/suisya-systems/claude-org-runtime/commit/befd3096110d18c928793d4862dba02e4da7ea22)
(base release `v0.1.42`).

## How to use this file

- **IDs are permanent.** `D-0001` … are stable identifiers. Once assigned, an ID is never
  reused, renumbered, merged into another entry, or deleted.
- **Supersession keeps the ID.** A decision that stops being true keeps its ID and gains
  `Status: superseded by D-XXXX`; the replacement gets a new ID at the end of the list.
- **Cross-reference by ID only.** `CHARTER.md`, `PORTING_LEDGER.md` and `ACCEPTANCE.md` cite
  these IDs. Never cite this file by line number, heading order, or table position.
- **Only what the Issue decided is `accepted`.** Everything that implementation needs but the
  source Issue does not settle lives in [Open questions](#open-questions) with a `Q-` prefix,
  phrased as a question. A `Q-` entry is never an authority to act.
- **Source of truth for these decisions** is claude-org-ja Issue #740, specifically the two
  2026-08-17 comments:
  - [COMMENT — Interlock 分岐決定](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345)
    (top-level source of truth: implementation target and migration method)
  - [COMMENT — 発動決定と終端設計の修正](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070)
    (decided and explicitly inherited, not retracted: watcher fact states, incident contract,
    responsibility boundary)

  Earlier comments (2026-07-19 / 2026-07-20) are historical context. They explain *why* several
  decisions exist and are cited as context, but they are superseded wherever they conflict with
  the two comments above.

### A note on numbers

Where a figure appears below, its epistemic status is labelled. Figures such as "12–15k LOC",
"5–20 incidents per 100 worker runs" and "about 470–1,576 program ticks per 100 runs" are
**estimates/targets stated in the Issue**, not measurements. The 2026-07-18…2026-07-25 dogfood
figures are **measured baseline**.

---

## Index

| ID | Title | Status |
|---|---|---|
| D-0001 | SQLite is the single source of truth | accepted |
| D-0002 | Retire the resident/periodic LLM monitoring loop, keep program loops | accepted |
| D-0003 | Dispatcher AI is on-demand and incident-triggered only | accepted |
| D-0004 | Dispatcher AI returns an assessment; it never executes side effects | accepted |
| D-0005 | The watcher's fact state is a closed set | accepted |
| D-0006 | Separate "observation unavailable / insufficient evidence" from "anomaly" | accepted |
| D-0007 | Incident contract: persisted packet, restricted tools, fixed return schema | accepted |
| D-0008 | Three-layer responsibility boundary with explicit non-responsibilities | accepted |
| D-0009 | `SessionProvider` and `MessageBus` are separate contracts | accepted |
| D-0010 | Agent View is consumed through the public CLI only, fail closed on incompatibility | accepted |
| D-0011 | Interlock is a lineage fork, not an upstream-tracking fork | accepted |
| D-0012 | Do not back-port Interlock's design into v1 | accepted |
| D-0013 | Cut over at the run boundary; do not dual-write | accepted |
| D-0014 | Selective seed port (~12–15k LOC); parity rewrite is not a goal | accepted |
| D-0015 | Declared non-goals | accepted |
| D-0016 | Secretary is the single non-blocking human window | accepted |
| D-0017 | Workers are few, capped, and fenced per role | accepted |
| D-0018 | Curator is on-demand; skill reflection requires human approval | accepted |
| D-0019 | The Agent View gate is a precondition; failing it replaces only the `SessionProvider` | accepted |
| D-0020 | The Agent View gate is discharged by a minimum vertical slice, built contract-first (Strategy B+) | accepted |
| D-0021 | The `SessionProvider` interface is a provisional spike artifact, promoted only by decision | accepted |
| D-0022 | Scoped exception to D-0019: gate items 8 and 10 are rehearsed on the spike and proven later | accepted |
| D-0023 | Gate item 3 is observed by a breach-probe battery, and fail-closed is Interlock's own obligation | accepted |
| D-0024 | Session identity is settled by experiment first; a negative result fails gate item 2 | accepted |
| D-0025 | If the gate fails: local execution is mandatory, C2 is the designated second spike, and D-0014 does not reach the `SessionProvider` role | accepted |
| D-0026 | The spike's durable output is the interface and the tests; implementations are throwaway by default | accepted |
| D-0027 | Gate item 2 fails on Agent View; C2 becomes the spike's `SessionProvider` | accepted |
| D-0028 | Carried end-to-end tests land as failing specifications; pane-liveness lease release is discarded with the pane | accepted |
| D-0029 | The production schema is authored and migrated on its own terms; the spike schema is not promoted | accepted |
| D-0030 | The event spine fans out per consumer at append time, in one transaction with the enqueue | accepted |
| D-0031 | The time base: detection latency budgets per incident class, and a reconcile period derived from them | accepted |
| D-0032 | Owners for the retired loop's non-detection duties, and gate ownership as stage-derived policy data | accepted |
| D-0033 | CI observations carry a full identity and project monotonically; arrival order never decides a verdict | accepted |
| D-0034 | run↔PR linkage is many-to-many with exactly one live primary, and a repository is never resolved by working directory | accepted |
| D-0035 | Watcher liveness is a fenced, unconditional trace against an expected scope roster | accepted |
| D-0036 | A `Gate` is an entity whose stage is a projection of an immutable transition history, with a closed terminal taxonomy | accepted |
| D-0037 | A gate's relay stages advance on the delivery ack, never on the send | accepted |
| D-0038 | AC-9's cohort is terminal-in-period Interlock-owned runs, its unit is the AI invocation, and coverage is required output | accepted |
| D-0039 | AC-10's ground truth is external: a labelled fixture suite and shadow reconciliation, with false termination counted at the applied action | accepted |
| D-0040 | A measurement report records its own provenance, and the harness is read-only by capability | accepted |
| D-0041 | The run status vocabulary is closed and forward-only, and a detection budget carries its own kind | accepted |
| D-0042 | A message nobody wants is cancelled, not left pending, and cancellation is terminal rather than an erasure | accepted |
| Q-0001 | SQLite schema/DDL and migration policy for the SoT tables | resolved by D-0029 |
| Q-0002 | Incident dedup key composition and re-notification rate in absolute time | proposed |
| Q-0003 | Reconcile interval and the tolerable detection latency that justifies it | resolved by D-0031 |
| Q-0004 | Which concrete alternative `SessionProvider` if the Agent View gate fails | resolved by D-0025 |
| Q-0005 | Canary duration, sample size, and numeric exit criteria | proposed |
| Q-0006 | Retention and scrubbing policy for evidence references and incident history | proposed |
| Q-0007 | Dispatcher AI auth identity and permission tier in Interlock | proposed |
| Q-0008 | Repository/package rename timing and compatibility policy | proposed |
| Q-0009 | Detector version semantics and the compatibility rule across versions | proposed |
| Q-0010 | Where the "unclassified anomaly" counter and its threshold live in Interlock | proposed |
| Q-0011 | Tolerable Secretary window response latency, and the load it is measured under | proposed |
| Q-0012 | Per-state semantics and detection predicates of the closed fact-state set | proposed |
| Q-0013 | Whether the "control plane outside the worker" principle survives into Interlock | proposed |
| Q-0014 | Which subset of the porting ledger constitutes the initial seed, and in what order | proposed |
| Q-0015 | Whether carried tests port before, with, or after the modules they cover | proposed |
| Q-0016 | Which quarry lessons from `discard` rows become decisions | proposed |
| Q-0017 | What replaces the discarded desktop human-notification path | proposed |
| Q-0018 | Whether repository-root, packaging, and CI files need a classification pass | proposed |
| Q-0019 | Who owns each of the retired loop's non-detection duties | resolved by D-0032 |
| Q-0020 | What an incompatible CLI capability probe implies for already-running sessions | proposed |
| Q-0021 | What scaffold makes each Agent View gate item checkable before implementation | resolved by D-0020 |
| Q-0022 | Whether a carried artifact may keep enumerating v1 pane vocabulary as a normative list | proposed |
| Q-0023 | Whether a carried test may drive a `discard`-classed module to reach the contract it pins | resolved by D-0028 |

---

## D-0001 — SQLite is the single source of truth

**Context.** In v1 the authoritative picture of "what is happening" was spread across an LLM
dispatcher's context, pane/screen state, and a state DB used as a backstop. That made mid-flight
kill unrecoverable in the general case: whatever only existed in the AI's context or on screen
was lost, and the surviving records were not sufficient to reconstruct in-flight work. Both
2026-08-17 comments settle this by naming a single durable store.

**Decision.** SQLite is the single source of truth for `run`, `task`, `session`, `lease`,
`incident`, `assessment`, `action`, and `outbox`. AI context and the Agent View UI are explicitly
**not** the source of truth. Assessments, approvals, and execution results are appended to SQLite.
After a mid-flight kill of supervisor, Dispatcher Core, Dispatcher AI, or Secretary, the system
resumes from SQLite — from unresolved incidents — without double execution.

**Consequences.**
- Every component that makes a durable claim must write it to SQLite before that claim is
  observable elsewhere; UI and AI context are projections and may be rebuilt at any time.
- Resume-after-kill is a first-class requirement, not an error path, and must be testable
  (see `ACCEPTANCE.md`).
- Auditability follows: incident → assessment → approval → action → result must be reconstructible
  by query alone.
- Costs: single-writer discipline per state item, explicit schema/migration handling
  (unresolved at the time — see Q-0001, resolved by D-0029), and retention policy for evidence
  references (unresolved — see Q-0006).

**Status.** accepted

**Source.**
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345 ,
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070

---

## D-0002 — Retire the resident/periodic LLM monitoring loop, keep program loops

**Context.** v1 ran a Claude `/loop 3m` dispatcher. Measured dogfood baseline
(2026-07-18…2026-07-25): 1,041 three-minute scheduled iterations, 959 with model activity,
3,531 unique assistant/model responses, 4,960 AI tool calls, 567,839 output tokens,
1,399,565,488 cache-read tokens, median iteration interval 180.0 s. The overwhelming majority of
those turns observed nothing. A 2026-07-20 review also found the loop's *headline* value was not
accident prevention, so the case for a resident LLM tick collapsed. But the same review and the
2026-08-17 comment are equally clear that deterministic program-side polling still has to exist.

**Decision.** Retire the resident/periodic LLM monitoring loop (`/loop 3m`). The program-side
**event loop** and a **low-frequency reconcile loop** remain: push is the primary path, reconcile
is the miss-catcher. "Delete every loop" is explicitly a non-goal (see D-0015).

**Consequences.**
- Idle time costs zero AI turns; the reduction target the Issue states is ≥95% AI prompts and
  ≥90% output tokens per 100 worker runs (target, to be measured — see `ACCEPTANCE.md`).
- Program monitoring ticks are *not* the savings target: the Issue estimates roughly 470–1,576
  program ticks per 100 runs after migration (estimate), i.e. 0–70% change.
- The loop's non-detection duties (relay, drain, aging, auto-stop, curate-inflight) do not
  disappear with it. The 2026-07-20 review requires that each duty be given an explicit new owner;
  it does not say which component that owner is, and neither 2026-08-17 comment assigns them.
  The assignment is therefore unresolved — see Q-0019.
- The reconcile period itself is not fixed by the Issue (unresolved — see Q-0003).

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070

---

## D-0003 — Dispatcher AI is on-demand and incident-triggered only

**Context.** The 2026-07-20 plan's terminal state was full retirement of the Dispatcher LLM. The
2026-08-17 comment corrects exactly that one point, citing #873: adding observation signals does
**not** make "long normal processing" and "semantic stall" deterministically separable. Some
residual semantic judgement is irreducible. The 2026-07-20 adversarial review had already shown
the same shape of problem from the other side: the assumption "approval-pending is a recorded
fact" was empirically false (an unanswered approval prompt is not written to the transcript until
after it is answered; a 124-second on-screen prompt produced zero appended records), so a
confidence branch does not vanish — it only moves.

**Decision.** A deterministic watcher handles ordinary monitoring, dedup, deadline evaluation,
retry, and persistence. The Dispatcher AI is started **on demand, only for semantically ambiguous
incidents**. If there is no incident, there are zero AI turns.

**Consequences.**
- Ambiguity must be an explicit, detectable condition of the watcher, not a fallback for
  "anything the watcher did not understand" being silently dropped.
- The AI must be startable statelessly from a persisted incident packet (D-0007); there is no warm
  resident context to rely on.
- Expected volume, per the Issue's **estimate**, is roughly 5–20 semantic-triage incidents per 100
  worker runs, derived from measured baseline events (`worker_spawned` 288, `anomaly_observed` 95,
  of which `spinner_active_suppress` 29, `stall_acked` 14, `error` 13, `stall_suspected` 11,
  `stall_suppressed_by_secretary` 4).
- Full retirement of the Dispatcher AI is re-evaluated only if ambiguous incidents approach zero
  in practice; it is not planned.

**Status.** accepted

**Source.**
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070 ,
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0004 — Dispatcher AI returns an assessment; it never executes side effects

**Context.** A monitoring AI that can also act is a monitoring AI that can act on a
misclassification. The 2026-07-20 trust-boundary analysis required a minimal tool surface and no
secretary-tier capability for the automated dispatcher; the 2026-08-17 comments turn this into a
hard contract by separating judgement from execution.

**Decision.** The Dispatcher AI returns a structured assessment and nothing else. It never directly
executes `approve`, `restart`, `close`, `reassign`, or any other side effect. All side effects go
through the Secretary, a human gate, or a privileged runtime handler.

**Consequences.**
- The AI's tool/permission set must **exclude** direct approve/restart/close/reassign; this is an
  acceptance item, not a convention.
- Every recommendation is an enum value routed to an executor, so the executing component — not the
  AI — owns policy, permission checks, and the audit record of the action.
- A wrong assessment degrades to a wrong *recommendation*, which a gate can still refuse.
- Cost: an extra hop and latency on every AI-triaged incident, plus a handler layer that must exist
  even for actions the AI could technically perform itself.
- The AI's own auth identity and permission tier are not fixed by the 2026-08-17 comments
  (unresolved — see Q-0007).

**Status.** accepted

**Source.**
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070 ,
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0005 — The watcher's fact state is a closed set

**Context.** v1's detection vocabulary grew ad hoc, one paragraph per incident, mixing observed
facts with interpretations ("looks like it is waiting for approval"). Without a fixed vocabulary,
detection cannot be fixture-tested and every new detector silently widens the state space.

**Decision.** The watcher's fact state is a **closed set**, minimally:

- `ACTIVE_EVIDENCE`
- `KNOWN_WAIT`
- `EXPLICIT_BLOCK`
- `NO_ACTIVITY_EVIDENCE`
- `OBSERVATION_UNAVAILABLE`
- `TERMINAL`

`NO_ACTIVITY_EVIDENCE` is **not** an anomaly (see D-0006). These are **facts**, not verdicts: any
interpretation on top of them belongs to the Dispatcher AI (D-0003) or to a human/handler (D-0004).

The Issue enumerates these six names and settles only that `NO_ACTIVITY_EVIDENCE` is not an
anomaly. It defines **no semantics or detection predicate for any state** — it lists fact-state
definition as a P0 contract item still to be done. This document therefore does not gloss them;
the semantics are open (see Q-0012).

**On "minimally".** The Issue's wording is 「watcher の fact state は最低限、次の閉じた集合にする」
— *at minimum*, make it this closed set. That phrasing is doing two things at once and they pull in
opposite directions: "closed set" forbids ad-hoc growth, while "at minimum" implies the six are a
floor rather than the exact set. This document does not resolve that by choosing one reading. The
operative rule below is therefore procedural, not numeric: **six states are decided, and any
seventh requires a new `D-` entry.** Whether such an entry would be a permitted extension or a
supersession of this decision is not settled by the Issue — see Q-0012, which must fix the exact
enumeration before any persisted schema or replay-compatibility rule depends on it.

**Consequences.**
- Adding a seventh state is a decision that requires a new `D-` entry, not a code change.
- Detectors emit one of these values plus a detector version (D-0007); this makes lifecycle, known
  wait/error, relay, and terminal determination fixture-testable and deterministically replayable.
- Prose detectors that emitted free-form status strings cannot be carried over unchanged.
- The per-state semantics and detection predicates are not fixed by the Issue (unresolved — see
  Q-0012), and neither is the versioning rule for detector version (unresolved — see Q-0009).

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070

---

## D-0006 — Separate "observation unavailable / insufficient evidence" from "anomaly"

**Context.** In v1 a silent worker and an unobservable worker collapsed into the same alarm.
"No appended records" cannot by itself distinguish idle from dead; #869/#894 fixed the principle
that observation failure is its own category, and #873 established that even with more signals a
long normal computation and a semantic stall are not always separable. Treating absence of
evidence as evidence of failure produces exactly the high-cost false positives v1 kept fighting.

**Decision.** Keep "observation unavailable / insufficient evidence" strictly separate from
"anomaly". `NO_ACTIVITY_EVIDENCE` is **not** an anomaly. Do not assume that normal vs. abnormal can
always be decided for an ambiguous stall; the system must be able to *express* insufficient
evidence rather than being forced to pick a verdict.

**Consequences.**
- The assessment schema must carry `missing_evidence` and `confidence` (D-0007), and "cannot
  determine" must be a legitimate, non-escalating outcome.
- Alarming, notification, and retry policy key off the distinction, so a monitoring outage must not
  be able to masquerade as a fleet-wide worker failure.
- Conversely, unknowns must not be silently swallowed — where an unclassified anomaly surfaces, it
  needs a route to a human (mechanism unresolved — see Q-0010).
- Detection tests must include observation-failure fixtures, not only stall fixtures.

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070

---

## D-0007 — Incident contract: persisted packet, restricted tools, fixed return schema

**Context.** An on-demand AI (D-0003) has no warm context, so everything it needs must be in the
record. And because it must not act (D-0004), its interface has to be narrow enough that acting is
not expressible. The 2026-07-20 trust-boundary analysis additionally required that only structured
fields be copied into notifications — free-text transcript quotation was forbidden — which pushes
the same discipline into the incident packet.

**Decision.** Persist every incident in SQLite with at least:

- incident id; worker/run id; created and updated timestamps
- fact state (D-0005) and **detector version**
- evidence references (transcript / state DB / pane / process / CI / …)
- known pattern, elapsed time, recent state transitions
- dedup key, retry count, previous assessment/action

The Dispatcher AI receives **only this incident packet**. Its tools are limited in principle to
`read_incident`, `request_evidence`, `submit_assessment`. Its return value is a fixed schema, not
free-text commands:

- `classification`
- `confidence`
- `evidence_refs`
- `missing_evidence`
- `recommendation` (enumerated)
- `human_gate`

**Consequences.**
- An incident is replayable: the same packet must yield a reproducible triage path, and stored
  packets double as regression fixtures.
- The AI cannot widen its own observation surface except through `request_evidence`, which is
  itself an auditable, persisted request.
- Any new recommendation requires extending the enum — a reviewable schema change rather than a
  new sentence in a prompt.
- The composition of the dedup key is not fixed by the Issue (unresolved — see Q-0002), and neither
  is the retention policy for evidence references (Q-0006).

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070

---

## D-0008 — Three-layer responsibility boundary with explicit non-responsibilities

**Context.** v1's failure mode was overlapping ownership: the LLM dispatcher did some detection,
some judgement, and some execution, so no layer could be tested in isolation and dual monitoring
during migration was a structural risk. The 2026-08-17 comment fixes a boundary table in which
each layer is defined by what it must **not** do as much as by what it does.

**Decision.** Three layers, each with explicit non-responsibilities:

| Layer | Responsible for | **Not** responsible for |
|---|---|---|
| Deterministic watcher | pane/process lifecycle, lease, queue/outbox, screen hash & spinner, known error/approval patterns, DB/CI state, timers, dedup/retry, incident creation | semantic evaluation of the work itself; asserting a verdict on an ambiguous stall |
| Dispatcher AI (on-demand) | semantic evaluation of the incident packet, requesting further evidence, evidence-backed classification/recommendation | raw shell/tmux operation; approval; directly terminating, restarting, or reassigning a worker |
| Secretary / human / handler | approving and executing operations that cross a permission boundary; applying policy | LLM polling for continuous monitoring |

**Consequences.**
- "Which layer owns this?" has a written answer before implementation; a capability that fits no
  layer is a design gap, not an implementation detail.
- The watcher may name a stall *candidate* but may never conclude it; the AI may conclude but never
  act; the executor may act but never poll.
- The non-responsibility column is testable: the AI's tool surface must not contain shell/tmux
  (see D-0004), and the Secretary must not host a monitoring loop (see D-0002, D-0016).
- Note that the watcher's listed inputs include screen hash and spinner as *watcher* signals, while
  the port ledger discards old screen-scraping mechanisms as permanent backend contracts — this is
  a deliberate distinction between a signal and a backend contract (see `PORTING_LEDGER.md`).

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070

---

## D-0009 — `SessionProvider` and `MessageBus` are separate contracts

**Context.** v1 bound delivery to the pane: messages travelled by keystrokes into a tmux pane, so
session lifecycle and message delivery were the same mechanism. Consequences were structural — a
destructive single-consumer read meant a shadow observer could not watch delivery without stealing
it, and at least six senders hard-coded the destination `dispatcher`. Interlock's session backend
(Agent View) is a research preview and expected to change, so binding delivery to it would import
the same coupling into v2.

**Decision.** Session management and messaging are two separate contracts.

- `SessionProvider` — start, list, obtain structured state of, stop, and resume **top-level worker
  sessions**. Agent View is treated as one interchangeable implementation.
- `MessageBus` — task delivery, ack, dedup, outbox, and message identity.

Do not push both into one backend contract. The Agent View UI / session state is not the source of
truth for delivery (consistent with D-0001).

**Consequences.**
- The `MessageBus` must be able to deliver and re-send independently of the Agent View UI — an
  acceptance item, not an aspiration.
- Replacing the session backend (D-0019) must not touch delivery semantics, and vice versa.
- Message identity, ack, and dedup invariants are carried from v1 as contracts even though the
  transport is discarded (see `PORTING_LEDGER.md`, D-0014).
- Cost: two contracts, two test surfaces, and an explicit join between session and run
  (see D-0010, and the crash-window re-identification item in `ACCEPTANCE.md`).

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0010 — Agent View is consumed through the public CLI only, fail closed on incompatibility

**Context.** v1 learned the cost of depending on unofficial harness internals: transcript JSONL is
an internal format, and measured CC version churn ran at roughly one release every four days, so a
parser bound to internals breaks on a cadence faster than the team can react. Agent View is a
research preview, i.e. explicitly unstable.

**Decision.** Depend on the **public CLI only**. Do not depend on `~/.claude/jobs`, internal
sockets, or unpublished JSON/transcript formats. Provide a CLI capability/version probe, and on
incompatibility **fail closed on new spawn** rather than proceeding with degraded assumptions.

**Consequences.**
- Any capability not exposed by the public CLI is either out of scope or a gate failure (D-0019) —
  it is not a reason to reach into internals.
- The probe is a startup dependency: spawn is refused, not attempted, when the CLI is incompatible.
  The Issue scopes "fail closed" to *new* spawns; it says nothing about what an incompatible probe
  implies for already-running sessions, which is unresolved — see Q-0020.
- The system must therefore tolerate degraded observation (D-0006) rather than restoring fidelity
  by internal access.
- Cost: less signal than v1's transcript tail had, which is part of why irreducible ambiguity
  remains (D-0003).

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0011 — Interlock is a lineage fork, not an upstream-tracking fork

**Context.** The 2026-08-17 comment supersedes the plan to graft the new design onto
`claude-org-ja` and the current runtime, because that plan required the old pane contract and the
new session contract to coexist for a long period. Interlock is built in parallel from a fixed
commit instead. What is worth keeping from upstream is history and accident-derived knowledge, not
a continuously merging code line.

**Decision.** Interlock is a **lineage fork** (系譜分岐) of
`suisya-systems/claude-org-runtime@befd3096110d18c928793d4862dba02e4da7ea22` (`v0.1.42`), not a
fork maintained for continuous upstream tracking. No periodic upstream merges. Individual security
fixes may be taken in, each with recorded rationale.

**Consequences.**
- No merge tooling, no compatibility branch, no expectation that upstream refactors arrive.
- Every incoming upstream change is a deliberate, justified, individually recorded act — the
  natural place for that record is a `PORTING_LEDGER.md` row.
- Divergence from upstream is expected and is not a defect to be reconciled later.
- Upstream fixes that are *not* security fixes do not arrive automatically; if one is wanted, it is
  a normal port decision under D-0014.

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0012 — Do not back-port Interlock's design into v1

**Context.** The alternative that was superseded — extending the existing repositories — would have
kept two contracts alive in one codebase indefinitely. Back-porting v2 design into v1 recreates
that state through the back door, and would make both lines harder to reason about during the
run-boundary cutover (D-0013).

**Decision.** `claude-org-ja` and the runtime 0.1 line become the **v1 / maintenance line**.
Interlock's new design is **not** back-ported into them.

**Consequences.**
- v1 receives maintenance only; it does not gain the SQLite SoT, the fact-state model, or the
  incident-driven Dispatcher AI.
- The migration path is cutover (D-0013), not convergence — v1 stays exactly as good at running v1
  runs as it is today, which is what makes rollback viable.
- Improvements discovered while building Interlock generally stay in Interlock; deliberately
  applying one to v1 would need its own decision entry.

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0013 — Cut over at the run boundary; do not dual-write

**Context.** Migrating a live delegation path is the risky part. Dual-write and in-flight state
conversion would require the two systems to agree continuously on ownership and on the source of
truth — precisely the ambiguity D-0001 exists to eliminate. A run is a natural atomic unit with a
clean start and end, so switching at that boundary avoids converting anything mid-flight.

**Decision.** Cut over at the **run boundary**. Do **not** dual-write.

- Runs already started on v1 finish on v1.
- Only new runs from the canary onward go to Interlock.
- Shadow observation, if used, is **read-only**: it rewrites neither ownership nor the source of
  truth.
- No state conversion of in-flight runs.
- Rollback = route subsequent **new** runs back to v1.
- Any bridge is limited to migration/comparison and never becomes a permanent Interlock API.

**Consequences.**
- Rollback is cheap and needs no data migration, which is what makes a one-worker canary safe.
- Two systems run side by side for the length of the longest in-flight run. Measured baseline for
  completed runs was mean 1.09 h, median 0.66 h, p90 2.55 h — i.e. 90% of measured completed runs
  finished within 2.55 h, so the coexistence window is *expected* to be hours rather than weeks. No
  maximum run length is recorded in the baseline, so this is an expectation, not a bound.
- Any comparison bridge must be built with a removal date in mind and must not accrue callers.
- Rollback *conditions* and canary exit criteria are not fixed by the Issue (unresolved — see
  Q-0005); `ACCEPTANCE.md` records the qualitative conditions the Issue does state.

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0014 — Selective seed port (~12–15k LOC); parity rewrite is not a goal

**Context.** The 2026-07-20 guidance established the "quarry, not a porting source" principle for
the 803-line worker-monitoring document: it is excellent prose but a blueprint of the *old
building*, so copying it faithfully reproduces the old platform's workarounds. The 2026-08-17
comment applies the same logic to the codebase and puts a size figure on the initial seed.

**Decision.** The initial seed selective port is roughly **12–15k LOC** (an Issue estimate, not a
budget derived from measurement). A **parity rewrite of the existing codebase is not a goal**. Old
monitoring documents are a **quarry, not a porting source**: extract only invariants, detection
semantics, and incident-derived fixtures.

Carry / Rewrite / Discard classification per the Issue:

- **Carry** (contracts, implementation, tests worth rescuing): SQLite state semantics,
  single-writer, lease, outbox, resume conditions; Secretary / Dispatcher / Worker / Curator
  responsibility boundaries and the human gate; permission / sandbox / hook generation, validation
  and breach probes; message identity, ack and dedup invariants; accident-derived fixtures, fault
  injection and recovery tests; the Curator candidate → human approval → skill reflection contract.
- **Rewrite** (re-derived from invariants, not from the old mechanism): Dispatcher Core state
  machine and reconcile loop; the Agent View `SessionProvider`; incident packet / assessment /
  action handlers; session↔run binding, capability probe, restart recovery; the non-blocking
  Secretary intake/queue boundary.
- **Discard** (never enters v2's permanent surface): tmux/pane layout, pane IDs and send-keys as a
  backend contract; screen hash, spinner and screen regex as old-platform-specific observation
  mechanisms; renga/herdr compatibility layers and permanent shims for the old backend; the
  resident Dispatcher AI loop and the bulk prompt prose for handover/resume; the A/B/C worker
  layout and bespoke worktree orchestration.

**Consequences.**
- LOC reduction is explicitly **not** a success metric; the Issue's own projection for the
  monitoring cluster is roughly 5,000–7,300 LOC after migration versus about 5,862 today
  (difference −900…+1,400 — an estimate), while monitoring prompt prose is expected to fall to
  roughly 200–400 LOC. The goals are AI workload, determinism, reproducibility, and recoverability.
- A missing v1 feature is not automatically a regression; it must be argued back in.
- Every carried file needs a reason and a decision reference — the per-path record lives in
  `PORTING_LEDGER.md`.
- Cutting an existing safety fence purely to reduce LOC is forbidden (D-0015).

**Status.** accepted

**Source.**
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345 ,
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5017340960

---

## D-0015 — Declared non-goals

**Context.** Both 2026-08-17 comments carry an explicit non-goals list. They exist because each
item is a plausible-sounding direction that would either restore a failure mode (a resident AI
loop, evidence-free health claims) or expand scope past what a small, few-worker organisation
needs (Kubernetes, agent farms). Naming them prevents rediscovery by drift.

**Decision.** The following are non-goals for Interlock:

- Kubernetes or general-purpose orchestrators.
- Large-scale agent farms.
- A resident AI monitoring loop.
- Deleting every event/reconcile loop (the program-side loops stay — D-0002).
- Asserting that a worker is healthy without evidence (D-0006).
- Granting the AI unrestricted tmux/shell permission (D-0004, D-0008).
- Cutting existing safety fences purely to reduce LOC (D-0014).

**Consequences.**
- A proposal matching any bullet is rejected by reference to this ID; reopening one requires a new
  decision entry that supersedes this one in part.
- Scale assumptions follow from "no agent farm": few, capped workers (D-0017), and designs may
  assume that regime.
- "No resident AI loop" and "keep program loops" are one pair — quoting either alone misstates the
  decision.

**Status.** accepted

**Source.**
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345 ,
https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311008070

---

## D-0016 — Secretary is the single non-blocking human window

**Context.** The human-facing role is the one place where latency is felt directly. In v1 the
Secretary sat on the same paths as monitoring and long-running work, so a slow worker or a slow
judgement could stall the conversation with the human. Interlock's fixed architecture separates the
window from the work.

**Decision.** The Secretary is the **single human-facing window** and must not block its response
on worker monitoring, on long-running work, or on waiting for AI judgement.

**Consequences.**
- Intake and queue boundaries must be asynchronous by construction — this is one of the Rewrite
  items in D-0014.
- Secretary responsiveness under worker load is an acceptance item (`ACCEPTANCE.md`).
- The Secretary remains an *executor* of approved side effects (D-0004, D-0008) but never a poller
  (D-0002).
- Cost: results reach the human through completion/notification paths rather than by the Secretary
  holding a call open.

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0017 — Workers are few, capped, and fenced per role

**Context.** Interlock targets a small coding-agent organisation, not a farm (D-0015). Workers run
with real write access to real repositories, so containment is a per-role property rather than a
global setting. v1 already fenced workers with permissions, sandboxes, and hooks, and that
machinery is Carry material (D-0014).

**Decision.** Workers are **few and capped in number**, and fenced per role by permission, sandbox,
hooks, and tool surface.

**Consequences.**
- A concurrency cap is part of the control plane, not an operational convention; measured baseline
  average parallelism while active was 1.38, so the regime is genuinely small.
- Role fencing must survive restart and must **fail closed** rather than fall back to default
  permissions when configuration is missing (an acceptance item).
- Permission/sandbox/hook generation, validation, and breach probes are carried from v1 (D-0014).
- Scaling out is not a design axis; a proposal requiring many concurrent workers meets D-0015.

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0018 — Curator is on-demand; skill reflection requires human approval

**Context.** Knowledge organisation is bursty, so a resident curator would be another idle AI loop
(D-0002, D-0015). More importantly, skills change how every future worker behaves, which makes
automatic reflection a self-modifying path — v1 already required a human gate there, and that
contract is Carry material.

**Decision.** The Curator runs **on demand** and organises candidate knowledge. Reflection into
skills happens **only after human approval**.

**Consequences.**
- The candidate → human approval → skill reflection contract is carried from v1 (D-0014).
- "Curator output never reaches a skill without human approval" is an acceptance item.
- Curation output is a proposal artifact; nothing downstream may treat it as active guidance until
  approved.
- Cost: a human is in the loop for every skill change, deliberately.

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0019 — The Agent View gate is a precondition; failing it replaces only the `SessionProvider`

**Context.** The whole session layer rests on Agent View, which is a research preview. Building the
control plane first and discovering afterwards that the session backend cannot support unique
session↔run re-identification, restart-persistent permissions, or independent delivery would be an
expensive way to learn it. D-0009 already separates the two contracts precisely so that this risk
is contained.

**Decision.** The Agent View gate (enumerated in `ACCEPTANCE.md`) is a **precondition for starting
implementation**. If the gate fails, the Interlock control-plane design is **not** discarded — only
the `SessionProvider` is replaced.

**Consequences.**
- The first work after these founding documents is the Agent View spike, not feature code.
- The gate's eleven items are pass/fail entry criteria, not a wish list; the last of them —
  "`SessionProvider` alone can be swapped" — is what makes this decision enforceable.
- The SQLite SoT, fact-state model, incident contract, responsibility boundary, and migration method
  are independent of the outcome of the gate.
- Which concrete alternative provider would be used is not decided (unresolved — see Q-0004).
- The items differ in what they presuppose: some are provable against the CLI with a thin spike
  harness, while others (SQLite recovery, `MessageBus`, Curator promotion, the canary, the
  second-provider suite) presuppose the pieces they test. The Issue states the gate's position
  without describing the scaffold that discharges it, so the minimum scaffold per item is
  unresolved — see Q-0021. This is a question about *how* the gate is discharged, not a licence to
  start implementation without it.

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## D-0020 — The Agent View gate is discharged by a minimum vertical slice, built contract-first (Strategy B+)

**Context.** Q-0021 recorded that the eleven gate items are not uniform in what they presuppose, and
that read literally the gate cannot be discharged before the things it tests exist. The proposal
`docs/proposals/agent-view-gate-scaffold.md` answers that by classifying the items into three tiers —
T1 (provider probe: items 1, 3, 7), T2 (minimal durable core: items 2, 4, 5, 6, 11) and T3
(organisational context: items 8, 9, 10) — naming a scaffold inventory S1–S10, giving a minimum
scaffold and a pass/fail predicate per item (§3.3), and comparing three whole-slice strategies:
A (probe only), B (minimum vertical slice) and C (contract-first dual provider throughout).

**Decision.** The gate is discharged by **Strategy B+**: Strategy B's minimum vertical slice
(S1–S9, with S10 carried from `settings/generator.py`) built with one rule taken from Strategy C —
**S1 is written first, and S3 (the stub provider) is implemented before S2 (the Agent View
provider)**. The per-item minimum scaffold of §3.3 and the phase order of §3.5 are adopted as the
gate's discharge plan. Estimated cost is 12–16 engineer-days.

**Consequences.**
- The discharge order is fixed and its early-exit points are real: phases 0, 1a, 1b and 2a can each
  end the sequence by producing a Q-0004 situation. A phase that fails its exit condition is a report
  to a human, not a reason to proceed to the next phase.
- Writing S3 before S2 means no Agent-View-shaped assumption enters the control-plane suite, so gate
  item 11 measures a structural property rather than a retrofit. This costs roughly 1–2 days over
  plain B; item 11 mandates S3 regardless.
- Item 2 moves from T1 to T2: its crash-window proof needs a durable binding row and a supervisor to
  kill, so it is not dischargeable by a thin CLI harness.
- Item 9 has zero session-backend dependency and may run in parallel from day 1, independently of the
  Agent View verdict.
- **This discharges nine of the eleven items, not all of them.** Items 8 and 10 are rehearsed only;
  B+ is therefore adopted together with the scoped D-0019 exception in D-0022 and must not be
  described as satisfying the precondition without it.
- Strategy B+ carries the highest over-build risk of the three — a spike schema becoming the schema by
  inertia, answering Q-0001 by accident. That risk is managed, not avoided, by D-0026.
- Strategy A was rejected because it leaves seven items open at implementation start, which reads
  D-0019 as advisory; Strategy C was rejected because S1 designed before any provider exists is a
  contract designed from imagination.

**Traceability.** The operator's 2026-08-18 ruling covered all ten decisions requested in §6 of the
proposal. They are recorded as follows:

| §6 Decision | Ruling | Recorded in |
|---|---|---|
| 1 — Which Q-0021 strategy? | B+ | D-0020 |
| 2 — Is the `SessionProvider` interface scaffold or decision? | 2a | D-0021 |
| 3 — Which items must the spike discharge? | 3a | D-0022 |
| 4 — How is gate item 3's observable defined? | 4a′ | D-0023 |
| 5 — Where does fail-closed live? | 5a′ | D-0023 |
| 6 — Pre-assigned session identity or post-hoc adoption? | 6a | D-0024 |
| 7 — Which pre-filters apply to Q-0004 candidates? | 7a | D-0025 |
| 8 — Which candidate is the designated second spike? | 8a | D-0025 |
| 9 — Does D-0014's discard extend to the `SessionProvider` role? | 9a | D-0025 |
| 10 — What is the durable output of the spike? | 10a | D-0026 |

**Status.** accepted

**Source.** `docs/proposals/agent-view-gate-scaffold.md` §3 and §6 Decision 1; operator ruling,
2026-08-18 (recommendation adopted as written). Resolves Q-0021 together with D-0021, D-0022, D-0023,
D-0024 and D-0026.

---

## D-0021 — The `SessionProvider` interface is a provisional spike artifact, promoted only by decision

**Context.** Gate item 11 says only the `SessionProvider` need be swapped and proposes to demonstrate
it against "the same contract" — but that contract does not exist in writing. D-0009 names five verbs
(start, list, obtain structured state of, stop, resume) with no signatures, no state model and no
error contract. Three capabilities the gate leans on belong to neither contract as written: delivering
a message to a worker (item 6), reading back a session's *effective* permission / sandbox / hook
configuration (item 3), and observing or vetoing a workspace lifecycle transition (item 7). Until the
interface is written down, item 11 has nothing to substitute against.

**Decision.** S1 — the `SessionProvider` interface — is **spike scaffold, not a settled contract**. It
is written during the Agent View spike (first, per D-0020), marked provisional in the file itself, and
promoted to a settled contract only by a later `D-` entry. S1 carries five verbs, a provider-neutral
lifecycle/availability readout including an explicit "could not observe" case, a typed
error/unavailable result that is never an empty one, and a capability/version probe with a fail-closed
spawn precondition (D-0010). **S1 must not map provider lifecycle states onto D-0005's fact-state
set**; conversion from provider lifecycle to fact state belongs to the detector layer, where it is
fixture-testable and versioned.

**Consequences.**
- Item 11 becomes assessable, and each of the three unassigned capabilities above must be given a
  named owner explicitly rather than settled by inertia. **Assigning them is not the same as putting
  them in S1**: message delivery to a worker stays with `MessageBus` per D-0009 and is built as S8, so
  what S1 records for it is the *absence* of a delivery verb — the property gate items 6 and 11 exist
  to check. Only capabilities that are genuinely the provider's, such as observing a workspace
  lifecycle transition, may land in S1, and where a capability belongs to neither contract that must be
  written down as such.
- The provisional label is what stops S1 answering Q-0012 (fact-state predicates) or Q-0001-adjacent
  questions by implementation rather than by decision.
- Leaving the contract implicit and letting S2 define it de facto (option 2c) was rejected: that is
  exactly how session-backend detail leaks into the control plane, which is the failure item 11 exists
  to catch. Settling the contract as a `D-` entry before the spike (2b) was rejected because it
  designs the contract before any provider has taught anyone what it must express.

**Status.** accepted

**Source.** `docs/proposals/agent-view-gate-scaffold.md` §2, §3.2 (S1) and §6 Decision 2; operator
ruling, 2026-08-18 (2a).

---

## D-0022 — Scoped exception to D-0019: gate items 8 and 10 are rehearsed on the spike and proven later

**Context.** D-0019 makes all eleven Agent View gate items pass/fail entry criteria for *starting*
implementation. Two of them cannot meet that reading. Item 8 (Secretary never blocks) needs a real
Secretary intake under genuine worker load. Item 10 (canary routing and rollback) needs v1 as a live
counterparty, which by construction needs the implementation to be running. Item 9, by contrast, has
zero session-backend dependency and is absent from `ACCEPTANCE.md` §4's re-run list. D-0020 adopts a
strategy that discharges nine items; this entry records what happens to the other two, because
describing them as "in force" would be a euphemism.

**Decision.** The Agent View spike discharges tiers T1 and T2 — **items 1–7 and 11** — in full. **Item
9** is discharged in full, but in parallel and independently of the spike, since it tests nothing about
the session backend. **Items 8 and 10 are rehearsed on substitutes during the spike and are explicitly
not discharged before implementation starts. This is a scoped exception to D-0019, limited to those two
items.** Every gate record entry is labelled either "proven on the spike slice" or "re-proven on the
real implementation".

The exception is bounded per item:

| Item | Rehearsal during the spike | Real proof | Discharged at |
|---|---|---|---|
| **8** — Secretary never blocks | Stub Secretary intake with an explicit queue boundary, driven by a load generator of S4 `--exec` jobs at the worker cap; assert structurally that intake and queue boundary are asynchronous, and record baseline-vs-load latency | The same absence of blocking shown against the real Secretary under genuine worker load, against a threshold settled by Q-0011 | **Before the canary starts** (D-0013) — a Secretary that blocks under load would invalidate the canary's own measurements |
| **10** — canary routing and rollback | A run-start routing point, a run→owning-system ledger and a writer audit over both stores, against a synthetic counterparty; a rehearsed rollback changes only the routing decision | The same audit with v1 as the live counterparty, under the numeric criteria settled by Q-0005 | **At the canary itself** — the item passes when canary runs complete with exactly one owner per run, no record written by both systems, and a real rollback that changes only routing |

**Consequences.**
- D-0019 keeps its ID and its `accepted` status; it is not superseded. What changes is that two of its
  eleven items now have a named, dated discharge point instead of being satisfied up front.
- Implementation may begin with items 8 and 10 outstanding — and only those two. Any further item
  slipping past the gate is a new decision, not an extension of this one.
- If either discharge point is reached without its predicate being met, that is a **gate failure**
  recorded as such. This exception defers the two items; it does not waive them.
- Two provider-side unknowns feed item 8's rehearsal and are probed as part of S4: whether the daemon's
  control interface serialises status queries behind busy workers, and readout latency under N jobs.
- Option 3b (spike discharges all eleven, deferring implementation until item 10 has a real
  counterparty) was rejected because it blocks implementation on a canary that needs the
  implementation, inverting the ordering the Issue gives. Option 3c (formally reclassify 8, 9 and 10
  out of the gate) was rejected because Q-0021 explicitly warns that reclassification is how a
  deliberately-placed gate gets weakened.

**Status.** accepted

**Source.** `docs/proposals/agent-view-gate-scaffold.md` §3.1, §3.3 and §6 Decision 3; operator ruling,
2026-08-18 (3a, adopted with the scoped exception stated explicitly as the proposal requires).

---

## D-0023 — Gate item 3 is observed by a breach-probe battery, and fail-closed is Interlock's own obligation

**Context.** `ACCEPTANCE.md` proposes proving item 3 by diffing a session's effective configuration
before and after restart. No public surface returns that configuration, so the method is not runnable
as written. Separately, nothing documented promises fail-closed behaviour on missing or corrupt
configuration, and there is evidence of fail-*open*. A third hole sits under both: the provider's own
supervisor can restart a worker with no Interlock spawn call at all, and in the missing/corrupt cases
the `PreToolUse` backstop may itself be part of what is missing.

**Decision.** Three parts, taken together.

1. **Observable.** First probe for a public effective-configuration readback. If one exists, run item
   3's equality check as written. If none exists, substitute a **behavioural breach-probe battery** —
   one forbidden operation per *rule* in the role's fence, not one per role — plus a diff of
   Interlock's own rendered inputs. **This substitution is recorded as a deliberate weakening of item
   3, accepted by a human, not as an equivalent method.**
2. **Fail-closed is Interlock's.** Interlock validates the rendered per-role configuration and refuses
   to spawn on a broken one, and installs a `PreToolUse` deny hook in session. The obligation is
   Interlock's under D-0017 regardless of provider, so this work is not wasted under any Q-0004
   outcome.
3. **Supervisor-initiated restarts are in scope.** Item 3 additionally requires a fence that mediates
   restarts initiated by the provider's supervisor rather than by Interlock. Phase 2a's restart-fence
   probe is a **terminal** exit condition: if no such handle exists, **item 3 fails on Agent View and
   the sequence routes to Q-0004** rather than continuing to phase 2b.

**Consequences.**
- The residual is stated rather than hidden: diffing Interlock's rendered inputs proves what we wrote,
  not what the provider loaded, and that gap is exactly what item 3 exists to close. Probing every
  rule narrows it; it does not close it.
- Reading internal state to obtain the effective configuration (option 4b) was rejected outright as a
  violation of D-0010. Treating item 3 as unprovable and failing the gate on it (4c) remains the
  honest fallback had the operator declined the weakening; the operator did not decline it.
- Treating supervisor restarts as covered by the harness's documented persistence (option 5a) was
  rejected: that guarantee covers the happy path — configuration present and valid — and says nothing
  about the degraded one the item names.
- Failing the backend before probing for a handle (5b) and recording D-0017's fail-closed clause as
  simply unmet (5c) were both rejected; 5c contradicts D-0017 and `CHARTER.md` §3.4.
- This is the hole that most strongly favours the C2 fallback, since under C2 no other party can
  restart a worker (see D-0025).

**Status.** accepted

**Source.** `docs/proposals/agent-view-gate-scaffold.md` §3.3 (item 3), §3.5 (phases 2a, 2b) and §6
Decisions 4 and 5; operator ruling, 2026-08-18 (4a′ and 5a′).

---

## D-0024 — Session identity is settled by experiment first; a negative result fails gate item 2

**Context.** Whether a background session's identity can be chosen *before* spawn is the single
riskiest unknown in the gate. It decides obligation O6 — a stable session identity re-matchable to a
run across the crash window, admitting exactly one active writer — for the incumbent provider, and it
is a one-command experiment.

**Decision.** Run the experiment first, as phase 0, against a **real background session** rather than
an `--exec` job, and include the case where the requested UUID is already in use. If it succeeds, bind
session identity to the run **before** spawn. If it fails, search for any other **pre-spawn**
idempotent identity or fence; if none exists, **gate item 2 fails and the Q-0004 path opens**.
Post-hoc adoption is explicitly **not** an acceptable substitute for a pre-spawn fence.

**Consequences.**
- The experiment costs two short model-backed sessions and decides a design, so nothing downstream is
  built on an untested assumption (option 6b) and nothing pays for the harder path unconditionally
  (6c).
- Attribute matching on `cwd` + `startedAt` + `name` is named as *not* a fence and does not rescue
  item 2: `startedAt` is only knowable after the spawn, `name` is a display name rather than an
  identity, and a crash-then-retry can leave two matching workers alive before any reconciliation runs.
- The tail of this decision is the part that matters: a negative result must be allowed to fail the
  gate. An adoption rule that picks a winner without proving the loser never wrote is a
  reclassification of item 2 wearing the clothes of a mitigation.
- Being able to name the identity closes the *binding* half of O6 only. The single-writer half still
  comes from Interlock's own fencing token, validated atomically as part of each protected write
  (`ACCEPTANCE.md` §2), and must be tested rather than assumed — under any provider, including the
  D-0025 fallback.

**Status.** accepted

**Source.** `docs/proposals/agent-view-gate-scaffold.md` §3.3 (item 2), §3.5 (phase 0) and §6 Decision
6; operator ruling, 2026-08-18 (6a).

---

## D-0025 — If the gate fails: local execution is mandatory, C2 is the designated second spike, and D-0014 does not reach the `SessionProvider` role

**Context.** D-0019 promises that a gate failure replaces only the `SessionProvider`, but Q-0004
recorded that no candidate backend had been named, and that D-0014's discard of tmux/pane/send-keys
made "fall back to v1's transport" unavailable as an automatic answer. The proposal derives twelve
obligations (O1–O12) from D-0009's verbs plus the gate items, and scores eight candidates against them.

**Decision.** Three parts.

1. **Pre-filter.** **Local execution is mandatory** — worker sessions run on the operator's own machine
   against real local repositories. This removes C4 (cloud sessions / self-hosted environments) and C5
   (Managed Agents) before scoring. No separate "no server-side retention" pre-filter is added; it is a
   policy judgement only the operator can make, and the local-execution filter already renders it moot.
2. **Designated second spike.** If the Agent View gate fails, the designated replacement to spike is
   **C2 — Interlock-supervised `claude -p` subprocesses**, where Interlock spawns the worker as a child
   process it owns outright and Interlock's own process supervision *is* the session lifecycle. **C3
   (the Claude Agent SDK) is recorded as a genuine second choice**, not a weak one.
3. **Scope of D-0014.** D-0014's discard of tmux/pane/send-keys is a discard of a *message transport*
   contract and **does not extend automatically** to the `SessionProvider` role. C8 (panes as session
   lifecycle) stays ranked last on its own merits, and adopting it would require a new `D-` entry.

**Consequences.**
- Q-0004's "only the `SessionProvider` is replaced" promise now has a concrete referent, which is what
  the question was opened to fix. Naming none and re-evaluating on the failure's specifics (option 8d)
  was rejected for that reason; the §5.5 selection criteria still apply if the failure's specifics are
  unlike the ones anticipated.
- C2 was chosen on the obligations that cannot be compensated for from the control-plane side: O8
  (nobody else owns the working tree) and O11 (a real capability probe of the kind D-0010 asks for). It
  is also the most D-0010-consistent candidate, since every fact about it comes from documented flags.
- C2 removes the supervisor-restart hole in D-0023 part 3 entirely, because under C2 no other party can
  restart a worker, so the fail-closed spawn precondition covers every start.
- C2's O6 advantage is the *binding* window, not proven single-writer: no source states that a second
  process using the same identity is refused. The exclusion still has to come from Interlock's own
  fencing token and still has to be tested (see D-0024).
- C2's cost is that Interlock writes the process supervision the incumbent supplies for free; C3's cost
  is a dependency whose own reference calls the transport seam internal and subject to change — the
  same class of risk that produced this gate.
- Excluding C8 by silent extension of D-0014 would have decided by omission exactly the kind of
  question Q-0022 was opened to avoid deciding by omission; `CHARTER.md` §4 already draws the same
  signal-versus-backend-contract distinction for watcher signals, and separating these two roles is
  D-0009's whole purpose.
- The gaps listed in §5.4 of the proposal remain open — in particular, no candidate's *interface*
  stability has a measured churn figure, and no candidate's documentation was found to state whether
  state-changing endpoints honour a client-supplied idempotency key. This designation is made in
  knowledge of those gaps.

**Status.** accepted

**Source.** `docs/proposals/agent-view-gate-scaffold.md` §4, §5 and §6 Decisions 7, 8 and 9; operator
ruling, 2026-08-18 (7a, 8a, 9a). Resolves Q-0004.

---

## D-0026 — The spike's durable output is the interface and the tests; implementations are throwaway by default

**Context.** Strategy B+ (D-0020) builds a slice that is indistinguishable in kind from the real
implementation, only smaller. That is its one serious weakness: a spike schema becomes *the* schema by
inertia, and Q-0001 gets answered by accident instead of by decision.

**Decision.** The `SessionProvider` interface (S1) and the tests are the **durable** output of the
Agent View spike. Every implementation produced by the spike, **including S5's schema**, is
**throwaway by default** and may be promoted into the real implementation only by a new `D-` entry
that says so. S5 carries, in the file itself, an explicit note that it is a spike schema and that no
migration path is promised from it.

**Consequences.**
- Q-0001 (SQLite schema/DDL and migration policy) stays open until it is decided on its own terms;
  nothing in the spike closes it by inertia.
- The tests that matter are named by D-0014's rescue list — accident-derived fixtures, fault injection
  and recovery tests — which is why treating the whole slice as seed code (option 10b) and treating
  everything as throwaway (10c) were both rejected: 10b is how a spike schema silently answers Q-0001,
  and 10c discards the very tests the Carry bucket wants.
- Promotion becomes an explicit, auditable act rather than a default, which is the mitigation that
  makes B+'s over-build risk manageable.

**Status.** accepted

**Source.** `docs/proposals/agent-view-gate-scaffold.md` §3.4 (Strategy B mitigation) and §6 Decision
10; operator ruling, 2026-08-18 (10a).

---

## D-0027 — Gate item 2 fails on Agent View; C2 becomes the spike's `SessionProvider`

**Context.** D-0024 made session identity an experiment-first question and gave its negative result a
tail: if the pre-spawn experiment fails, search for any other **pre-spawn** idempotent identity or
fence, and "if none exists, gate item 2 fails and the Q-0004 path opens". The experiment failed (U1),
the search ran as `interlock-fence-search-20260818`, and it came up empty. That tail is now due, and
Q-0004's answer is already on the record: D-0025 designated **C2 — Interlock-supervised `claude -p`
subprocesses** as the second spike.

**Decision.** Four parts.

1. **Item 2 fails on Agent View (C1).** The pre-spawn fence search D-0024 triggered came up empty on
   the documented CLI surface of 2.1.234. Gate item 2 therefore **fails on Agent View**, and the
   Q-0004 path opens.
2. **C2 is adopted as the spike's `SessionProvider`,** per D-0025's designation. C3 (the Claude Agent
   SDK) remains the recorded second choice and is not re-opened by this entry.
3. **The exclusion is Interlock's own, under either provider.** No provider surface examined supplies
   an exclusion primitive item 2 could rest on; the single-writer half of O6 comes from Interlock's own
   fencing token, validated atomically as part of each protected write (`ACCEPTANCE.md` §2).
4. **Nothing else in the gate moves.** D-0019's and D-0022's gate structure and D-0025's candidate
   evaluation stand unchanged; only what S2 implements changes.

**Basis for the fail, and what would overturn it.** Twelve candidate handles were examined; every one
is discarded at spawn, documented as explicitly *not* deduplicated, a post-spawn artifact, or out of
scope under D-0025's local-execution pre-filter (`investigation/pre-spawn-fence-search.md` §3.1 for
the surfaces searched, §3.2 for the candidate table). Rows 1–3, 5, 11 and 12 were refuted by
experiment rather than by reading. Exhaustiveness is not claimed: §3.1 enumerates what was read, and
anything outside it is *unsearched*, not *absent*.

The falsification conditions are carried over verbatim in substance from §5.1, so this decision can be
overturned on evidence rather than on argument. Any one of the following overturns it:

- a documented handle on a surface not in §3.1's list;
- a future CLI release that honours `--session-id` under `--bg`;
- a future release that adopts the id named by `--resume` as the background session's own identity
  rather than forking;
- a future release that refuses a duplicate `--name` for background sessions;
- a `WorktreeCreate` hook arrangement that turns worktree creation into a genuine pre-spawn exclusive
  claim (§5.4).

None is available today, and D-0024 asks for the verdict on what exists, not on what might. This is a
failure of the *provider*, not of the gate's design: item 2's predicate is unchanged, and softening it
would be a reclassification rather than a mitigation.

**What U27, U28 and U32 settle.**

| Result | Finding | Consequence |
|---|---|---|
| **U27 — negative** | The `-p` `--session-id` refusal is **not atomic**: an admission window of roughly 2–3 s on one machine, in which 5 of 5 simultaneous trials admitted **both** processes, both exiting 0, both reporting the same `session_id`, and **both writing to the same transcript** | The create path C2 would use has no usable exclusion at its front edge. The width is a one-machine, one-load measurement (U34) and must not be designed on as a constant |
| **U28 — positive, both halves** | After a SIGKILL of the holder the claim held through every probe taken (out to ~25 minutes), and `--resume` returned the same `session_id` with exit 0 | Identity is **durable across a crash**. This is the binding half of O6 and was part of why C2 was designated |
| **U32 — registered** | `--resume` carries **no exclusion at all**: two concurrent `claude -p --resume <same uuid>` processes were both admitted, simultaneously and at a 5 s stagger | The path C2 actually uses after a crash excludes nothing. U28's positive is about *durable identity* only, and carries no single-writer content |

Taken together these say the same thing from both directions, and it is the same thing part 3 states:
**exclusion is not obtainable from the provider, and this is as true under C2 as it was under C1.** On
C1 there is no identity input at all, so there is nothing to fence with; on C2 there is a durable
identity that admits two writers inside the creation window and two concurrent writers via `--resume`.
The exposure item 2 names is the narrow one: the original claimant crashing while still inside the
admission window, followed by a retry that also lands inside it — F3's crash window exactly.

The `--bg --resume` result carries its own lesson and is recorded as such: the flag is honoured for
*content* and ignored for *identity*, exit 0 and no warning of any kind, with a copy of the transcript
written under a new CLI-assigned id. A caller that committed the requested UUID before the spawn would
hold a binding to **no live session** while the work ran under an id it never saw. The general rule
this fixes: **do not treat exit 0, or a binding committed before the spawn, as evidence that the
identity was accepted.** Readback of what the provider actually assigned is required, and that
requirement is not specific to Agent View.

**Scope of the C2 adoption.**

- **Unchanged.** D-0019 (gate is a precondition; a failure replaces only the `SessionProvider`),
  D-0022 (scoped exception for items 8 and 10), D-0023 (item 3's breach-probe battery and Interlock's
  own fail-closed obligation), D-0024 (the experiment-first rule, whose tail this entry discharges),
  D-0025 (the candidate evaluation and its designation of C2, **including C2's O6 grade, which stays
  `~`**) and D-0026 (durable output) all keep their IDs and their `accepted` status. None is
  superseded and none is amended by this entry.
- **Changed.** The implementation target of **S2** is replaced: the spike builds a **C2**
  `SessionProvider` — Interlock spawning the worker as a child process it owns outright, with
  Interlock's own process supervision *as* the session lifecycle — instead of an Agent View provider.
- **O6's grade does not move.** It stays `~` on a materially worse footing than U1 left it: durable
  identity plus crash survival are established, with no usable exclusion primitive. A move to `Y`
  would be wrong on this evidence; so would a move to `N`.
- D-0023 part 3's supervisor-restart hole does not exist under C2, since no other party can restart a
  worker. The fail-closed spawn precondition therefore covers every start — which is a property of the
  adoption, not a licence to skip it.

**Consequences for the 19-issue plan.** `docs/plans/spike-issue-decomposition.md` §4's survival matrix
is the named reference for what this verdict does to the issue set: 12 issues survive untouched, I-04
and I-16 stand with their provider-facing half re-pointed, I-01/I-02/I-12/I-13 are rewritten onto the
new surface, and I-03 is moot. Two cautions from §4 apply as written: "survives" means the issue's
*deliverable* survives, not the gate evidence it produced — `ACCEPTANCE.md` §4 still requires items 1,
2, 3, 7, 8 and 10 to be re-run in full against a new provider — and the `sequence_precondition` that
held the set is now discharged. **Rewriting the issue texts onto C2 is explicit follow-up work and is
not performed by this entry.**

**Consequences.**
- D-0024's tail is discharged rather than left hanging: the negative result was allowed to fail the
  gate, which is the part of D-0024 that mattered. No adoption rule was invented to rescue item 2.
- Q-0004's path is opened and immediately answered by the referent D-0025 already put there, so the
  gate failure costs a provider and not a design. This is D-0019's promise being paid out in the
  concrete.
- The fencing token and its tests move from "belt and braces" to the **only exclusion in the system**
  on the evidence available today. D-0014's rescue list — accident-derived fixtures, fault injection,
  recovery tests — is where the item-2 risk is now carried, and those tests must exist before C2 is
  trusted with a protected write.
- C2's cost stands as D-0025 stated it: Interlock writes the process supervision the incumbent
  supplied for free.
- U34, U36 and U37 remain open. In particular the admission window's width is unexplained, so a
  supervisor retry delay must not be designed against the measured 2–3 s figure.
- Nothing here promotes any spike artifact: D-0026 still governs, and the C2 provider is throwaway by
  default like every other implementation the spike produces.

**Status.** accepted

**Source.** `investigation/pre-spawn-fence-search.md` (2026-08-18) §3.1, §3.2, §5.1, §5.2, §5.3 and
§7; `docs/plans/spike-issue-decomposition.md` §4; operator ruling, 2026-08-18 (adopting §5.1's
proposed reading). Enacts D-0024's negative-result tail and applies D-0025's designation.

---

## D-0028 — Carried end-to-end tests land as failing specifications; pane-liveness lease release is discarded with the pane

**Context.** Q-0023 asked what "carried" means for a test that can reach its pinned contract only
through a `discard`-classed module: the five quarantined suites
(`tests/broker/test_delivery.py`, `test_store.py`, `test_control_plane.py`, `test_notify.py`,
`tests/attention/test_broker_journal_contract.py`) keep their assertions verbatim but drive the
deleted `broker/server.py`, so none of them run. The question was deliberately deferred to be
taken *with* the `MessageBus` contract; S8 (Issue `#19`) builds that contract, and the operator
directed the sequencing on 2026-08-21 (recorded on Issue `#19`). Q-0023 also demanded, either way,
an explicit statement of where the delivery/session boundary inside `broker/server.py` falls.

**Decision.**

1. **Failing-specification sequencing.** A carried end-to-end assertion lands as a specification
   against the **new** contract — passing where the contract already satisfies it, failing
   (`xfail(strict=True)`) where it does not yet — never by keeping a `discard`-classed module in
   the drive path. Keeping `broker/server.py` alive to run carried tests would prolong exactly the
   dependency shape the S8 static no-edge assertion exists to forbid, and the failing-spec form
   fixes the new contract first, consistent with Q-0015's sequencing question for this case. There
   is **no bulk retarget**: the disposition is per assertion, recorded in
   `docs/messagebus-carry-drop.md` (all 201 test functions across the five files), consistent with
   this ledger's partial-carry prose (e.g. `test_store.py` 3/6, the `test_control_plane.py`
   five-plus-three carve). The quarantined files stay verbatim at their ledgered paths — the table
   supersedes their *disposition*, not their text — and the "self-heals the day a `broker.server`
   replacement lands" note in the purge record is narrowed accordingly: nothing may revive them by
   resurrecting `broker/server.py`.

2. **The delivery/session boundary in `broker/server.py`, stated.** The boundary ran *through* the
   module, not around it, in two places: the `register_delivery_instance` override that released a
   stale lease when a pane-liveness probe said the owner's pane died out of band
   (server.py:817, `_probe_dead_pane_for_stale_lease` at :845-851), and the store-side adoption
   rollback conditioned on pane survival (`_detach_owner_panes_locked` /
   `_reattach_owner_panes_locked`). Both are **discarded with the pane, and deliberately have no
   transport-neutral successor on the delivery path.** Their transport-neutral *duties* are met
   elsewhere and pane-blind: writer exclusivity and release are the lease's own expiry plus fenced
   epochs (S6), and orphaned unfinished work is re-owned by fenced recovery adoption
   (`Outbox.recover`, S7) — time- and epoch-based, never probe-based. The stale-readout path *is*
   the natural C2 counterpart of the pane probe — a session id whose child is gone, a `read_state`
   answering "could not observe" — and the decision is that it informs **session management only**
   (`SessionProvider` verbs, D-0009's other half). Connecting it to delivery decisions is the
   exact edge item 6 forbids: `tests/messagebus/test_import_graph.py` makes the absence of that
   edge a build failure, and `tests/messagebus/test_stale_readout.py` demonstrates the consequence
   — delivery outcomes identical, `==` over the whole transcript, with the readout healthy, stale,
   or absent.

**Consequences.**

- Q-0023 is resolved by this entry. `tests/broker/test_delivery.py::test_stale_lease_is_released_when_the_pane_died_out_of_band`
  — the one assertion that made pane liveness *cause* lease release — is dropped with the pane,
  and its duty is named above rather than ported.
- The carried-but-not-MessageBus rows (`carried-deferred` in the table: the journal
  producer-consumer discipline toward D-0007's incident surface, the `sidecar.py`
  discovery/liveness carve) stay quarantined until their own successor surfaces exist; landing
  them as MessageBus specs would misattribute the invariant.
- One failing specification exists at this writing (recipient aliasing,
  `tests/messagebus/test_carried_specifications.py`); an XPASS there is the signal to remove the
  mark, per the file's own docstring.
- Cost: the quarantined text and the new specifications overlap in intent while the quarantine
  persists. Accepted — deleting quarantined assertions would overwrite ledger history, and D-0014
  keeps accident-derived fixtures readable in place.

**Status.** accepted

**Source.** Operator direction on Q-0023, 2026-08-21, recorded on Issue `#19`
(https://github.com/suisya-systems/interlock/issues/19); enacted by the S8 PR this entry ships
with. Not drawn from Issue #740.

---

## D-0029 — The production schema is authored and migrated on its own terms; the spike schema is not promoted

**Context.** `Q-0001` has been open since the beginning: `D-0001` names the SoT entities but not
their columns, keys, indices, per-item single-writer assignment, or how schema changes reach a live
database. `D-0026` says the spike's `spike_schema.sql` is throwaway by default and that the failure
mode to avoid is a spike schema becoming *the* schema by inertia. G3 (`#64`) and G4 (`#65`) need
tables for events, CI observations, run↔PR linkage and gates; adding them to the spike schema would
promote it by accident, which is the one outcome `D-0026` was written to prevent. `Gate` was
additionally absent from `D-0001`'s entity list while both Issues treat it as first-class.

**Decision.** The production control-plane schema is **authored**, not copied from the spike, and it
lives in `src/claude_org_runtime/control_plane/migrations/` as numbered, forward-only steps with a
`schema_migration` ledger, a distinct `PRAGMA application_id`, checksum verification of
already-applied steps, refusal of a database ahead of the code, and migration as an explicit call
separate from opening. There are no down migrations and there is no spike-to-production converter.
`docs/production-schema.md` is the DDL, and it records for every table whether its semantics are
carried verbatim from the spike, re-derived, or new — which is the record `Q-0001` asks for.

The `D-0001` SoT entity list is extended with `event`, `event_consumption`, `consumer`,
`consumer_subscription`, `repository`, `pull_request`, `run_pr_link`, `ci_observation`,
`watcher_scope`, `watcher_liveness`, `gate`, `gate_transition`, `gate_relay`, and `ai_invocation`.

The per-item single-writer table is `docs/production-schema.md` §4.2, and it turns on one
distinction `Q-0001` does not make: **single-writer discipline governs state items that are updated
in place; append-only tables are governed by identity uniqueness instead.** Without that
distinction the event spine could not have several producers, which is what `#64` requires of it.
`run.status` transitions stay exclusively the Secretary's — v1's 2026-07-20 review requirement,
which `Q-0001` records as never having been restated for Interlock, is restated here.

**Consequences.**
- `Q-0001` is resolved by this entry. Nothing in the spike is promoted; where the production schema
  agrees with the spike, the agreement is recorded per table as a re-derivation, not inherited.
- A CI watcher observing a merge may not complete a run: it appends an event, and the Secretary
  makes the transition. v1 collapsed those roles and the collapse is how a repo-resolution mistake
  wrote a foreign PR's metadata onto a run row.
- The measurement harness can be read-only by construction, because migration is not a side effect
  of opening (`D-0040`).
- `Q-0002` and `Q-0006` stay open and are not narrowed: `incident.dedup_key` stays non-unique, no
  re-notification window appears in any table, and every table forbids `DELETE` — a posture, not a
  retention policy.
- `task` and `assessment` remain without DDL. They are named by `D-0001` and exercised by neither G3
  nor G4; the first Issue that needs them writes their step.
- Costs: a migration ledger and its checksum discipline are machinery the spike did not need, and
  every future schema change is a file rather than an edit.

**Status.** accepted

**Source.** `docs/production-schema.md` §§2–4; Issues `#64`, `#65`; pre-implementation design review
of the G3/G4/G6 drafts, 2026-08-21 (Blocker: "Q-0001 を解決せずに実装できない"). Resolves `Q-0001`.
Not drawn from Issue #740.

---

## D-0030 — The event spine fans out per consumer at append time, in one transaction with the enqueue

**Context.** `#64` asks for a single event spine: CI outcomes written once, every consumer reading
from the same table, which removes v1's push-vs-poll duplication by construction. The design review
found the hole: **"undrained" is undefined until it is defined per consumer.** With one `drained_at`
column on the event, the first consumer to finish hides every other consumer's backlog — which
reproduces `tools/relay_scan.py`'s documented failure, 134 terminal events accumulating undelivered
for twenty days, through a different mechanism. A silent no-op is again indistinguishable from a
clean scan.

**Decision.** Consumption is **fanned out at append time**: appending one event is one transaction
that inserts the event, reads the subscription table inside that same transaction, inserts one
`event_consumption` row per subscribed consumer, and — for consumers whose consumption *is* a
delivery — inserts the `outbox` row in the same transaction and links it. Any typed side table the
event carries commits with it. The whole thing commits or none of it does.

**"Undrained by C"** means a row in `event_consumption` with `consumer_id = C` and
`status IN ('pending','failed')`. There is no global "undrained"; the phrase is only used with a
consumer named. The cursor-shaped view — a consumer's drain frontier — is derived as
`MIN(event_seq)` over its undrained rows, never stored, so it cannot disagree with the rows.

**Consequences.**
- There is no window in which an event exists with no delivery record, so the outbox is the only
  delivery path and the reconcile pass is a backstop over the same rows rather than a second path.
  This is what removes the push-vs-poll duplication; without the shared transaction, "single spine"
  would be a table layout rather than a property.
- Two exactly-once steps, each naming its `ACCEPTANCE.md` §2 mechanism: fact→enqueued is
  `transactional_with_record`; enqueued→delivered stays the outbox's `destination_idempotency_key`.
- Per-consumer rows were chosen over a per-consumer cursor because a cursor cannot express "event 5
  failed, event 6 succeeded" and forces head-of-line blocking on every failure. The cost is N rows
  per event, which is acceptable only because consumers are few — consistent with `D-0017`'s
  premise that this system is not a unit of scale.
- The spine's `seq` is an `AUTOINCREMENT` integer and a cursor over it is sound **only because
  SQLite serialises write transactions**, so a committed gap is permanent and never back-filled.
  This is a dependency, not folklore: the implementation carries a test that interleaves two
  appending transactions and asserts no committed `seq` is observed out of commit order.
- A `delivery` subscription without a recipient is refused **at registration**, by trigger, because
  the alternative is that it fails later inside the append transaction of the next matching event —
  taking the event down with it, since the append is all-or-nothing. The failure belongs to the party
  that can fix it.
- A `skipped` consumption must append a `consumption_skipped` event, so that a skip is
  distinguishable from a consumer quietly dropping work.
- A late-registering consumer's back-fill decision is made once, in its registration transaction,
  and is visible in the rows.

**Status.** accepted

**Source.** `docs/production-schema.md` §5; Issue `#64`; Issue `#60` operator direction (single
event spine); design review 2026-08-21 (Blocker: "「undrained」の意味が consumer ごとに定義されていない").
Not drawn from Issue #740.

---

## D-0031 — The time base: detection latency budgets per incident class, and a reconcile period derived from them

**Context.** `Q-0003` asks for the reconcile interval and the tolerable detection latency that
justifies it, and records the 2026-07-20 review's demand that the requirement basis come first. It
also records that the Issue's "10 minutes" appears only inside a projection of program tick counts —
an assumption feeding an estimate, not a decision. G4's stage tolerances and AC-10's non-regression
judgement are both unimplementable until these numbers exist.

**Decision.** For each incident class three quantities are decided as **policy data**: a tolerance
`T` (how long the condition may legitimately persist), a budget `L` (the ceiling on onset-to-alarm),
and the reconcile period `P`, constrained by

> **`T + P ≤ L`**

because the pass can only notice a crossing at its next run. The classes and their values are
`docs/time-base-policy.md` §3. The binding constraint is `min(L - T) = 2 min`, so:

> **The reconcile period is 120 seconds.**

The values live in `policy_detection_latency` / `policy_gate_stage_tolerance` /
`policy_gate_stage_owner`, with `P` itself carried per class row so that `T + P ≤ L` is a `CHECK`
rather than a convention, versioned by `policy_revision` and never updated in place, each revision
carrying the `D-` entry that set it. Three clock rules hold throughout: tolerances are evaluated
against our clock only (never the provider's `occurred_at_ms`); the evaluation clock is the caller's
parameter, never the database's; and every window is half-open `[start, end)`.

**Consequences.**
- `Q-0003` is resolved by this entry, and `Q-0002` — which depends on it — becomes answerable.
- The Issue's 10-minute figure is superseded as an input: at 120 s the reconcile pass contributes
  roughly 5× the program ticks that figure assumed. That is accepted, because `D-0002` and
  `ACCEPTANCE.md` §5 both say program-tick reduction is explicitly **not** the objective, and a
  program pass takes no model turn (AC-1). Program ticks are traded for the latency AC-10 measures.
- There is deliberately **no "human present / absent" modifier**, which `Q-0003` allows for: human
  presence is not observable to the deterministic layer, and a tolerance varying on an unobservable
  input cannot be evaluated deterministically. Human availability enters as a gate's explicit
  `deadline_at_ms` instead — an input, not an inference.
- A self-resolving condition is never detected and is **not** a miss; AC-10's miss counter counts
  only conditions that persisted past their budget.
- Suspension is expressed by a run status the predicates exclude, not by suppressing a tolerance:
  exclusion by status is auditable, a suppressed tolerance is not.
- Changing any number is a new `policy_revision`, so a past report can be recomputed under the
  tolerances it was actually judged by. The corollary is a rule every reader must follow: **a
  `policy_*` join without a `revision_id` predicate is a defect.** Omitting it matches every
  historical tolerance at once, so the detector emits one incident per revision ever recorded and
  some of them alarm on a tolerance that was retired. Detectors bind the revision effective at
  `:now_ms`; reports bind the revision effective over their period.
- These are **decisions, not measurements**, in the sense of this file's note on numbers. The only
  measured input is the 2026-07-18…2026-07-25 baseline.

**Status.** accepted

**Source.** `docs/time-base-policy.md` §§1–4; Issues `#64`, `#65`, `#67`; `ACCEPTANCE.md` AC-10;
design review 2026-08-21 (Blocker: "stage tolerance と AC-10 latency を決める Q-0003 が未解決").
Resolves `Q-0003`. Not drawn from Issue #740.

---

## D-0032 — Owners for the retired loop's non-detection duties, and gate ownership as stage-derived policy data

**Context.** `Q-0019` asks who owns each duty that retiring `/loop 3m` (`D-0002`) removed beyond
detection: pull-fallback drain, curate-inflight management, CI relay, `pending_decisions` aging, and
auto-stop. It requires only that each duty's new owner be made explicit; neither 2026-08-17 comment
assigns them, and assuming they all fall to Dispatcher Core would be an invention. Separately, G4's
`Gate` carries an `owner` field whose meaning — standing responsible party, or whoever currently
holds the ball — was undefined, which made relay-gap reporting unable to name a responsible role
deterministically.

**Decision.** The duty owner table is `docs/time-base-policy.md` §6. In summary: pull-fallback drain
and CI relay are Dispatcher Core's reconcile pass and have no separate path at all (`D-0030`);
curate-inflight is the Curator's for the work and Core's for the aging; `pending_decisions` aging is
Core's to detect and the Secretary's to act on; and **auto-stop has no automatic owner** — Core
raises the incident, and the Secretary, a human, or a privileged handler executes.

Gate ownership is **not a column**. `ball_holder` is a function of `(gate_type, stage)` and
`standing_owner` is a function of `gate_type`; both live in `policy_gate_stage_owner`. A `relay_gap`
incident names the ball holder; the standing owner answers for the class of decision overall.

**Consequences.**
- `Q-0019` is resolved by this entry, and the ownership half of G4's design is settled without a
  field that means different things on different rows. Neither owner is stored on `gate`, so neither
  can drift from the stage, and a report can say what ownership *was* by joining the effective
  policy revision.
- **v1's loop could stop a worker; Interlock's reconcile pass cannot.** That is a deliberate
  behaviour change, not an omission: `D-0008` forbids Core pronouncing a verdict on an ambiguous
  stall and `D-0004`/AC-6 forbid the AI executing. It is also why AC-10's false-termination counter
  must be defined at the applied `action` (`D-0039`) — a pass with a quiet auto-stop would be
  reintroducing the layer the fork exists to remove.
- The escalation *policy* — what must be escalated — remains operating-layer prose, per `#65`.

**Status.** accepted

**Source.** `docs/time-base-policy.md` §6; `CHARTER.md` §4; Issues `#64`, `#65`; design review
2026-08-21 (Minor: "Gate の `owner` が静的責任者か、現在ボールを持つ主体か不明"). Resolves `Q-0019`.
Not drawn from Issue #740.

---

## D-0033 — CI observations carry a full identity and project monotonically; arrival order never decides a verdict

**Context.** `#64` requires durable CI outcome ingestion. Without an identity, a re-poll, a CI rerun,
a PR head update and a late arrival are indistinguishable, and the event spine's dedup key has
nothing to be made of. Without an ordering rule, arrival-order last-write-wins lets a stale
observation overwrite a newer one — reporting a red PR as green because the red observation was
slower, which is `D-0006`'s verdict honesty violated in the most direct way available.

**Decision.** A CI observation's identity is
`(provider, repo_id, pr_number, head_sha, check_scope, scope_id, attempt, verdict)`, enforced by a unique
index on `ci_observation` and rendered as the event's `dedup_key` so a re-poll is an idempotent
no-op at the first statement of the append transaction. `head_sha` is a full 40-character lowercase
SHA; an abbreviated SHA is not an identity.

Observations are evidence and are never overwritten. The **current verdict** is a projection
(`ci_current_verdict`, a view, not a column): only observations matching the PR's current
`head_sha` are eligible, so **a head update invalidates prior verdicts rather than letting them be
overwritten**; among eligible rows the order is `(attempt DESC, occurred_at_ms DESC, seq DESC)`; a
`rollup` observation is eligible only where no fine-grained scope exists; **a late arrival that
orders lower is stored and does not move the projection**; and disagreeing scopes resolve by
severity `failed > timed_out > cancelled > indeterminate > passed`, with `no_run` meaning "no
eligible evidence" rather than a pass.

**Consequences.**
- The verdict vocabulary is closed and keeps `indeterminate` and `no_run` separate from `failed`.
  `indeterminate` is `OBSERVATION_UNAVAILABLE`'s CI shape (`D-0005`, `D-0006`) and is reserved for a
  *continued* fetch failure, carrying v1's `tools/pr_watch.py` discipline forward.
- `indeterminate` outranking `passed` in the severity order is deliberate: an unobservable check is
  not a green one.
- Storing rather than overwriting late arrivals means the table grows with evidence that never
  affects a projection. That is the intended trade — `Q-0006` governs what is eventually done about
  it.
- **`verdict` is part of the identity**, without which a fetch failure recorded as `indeterminate`
  and a later successful poll of the *same* attempt collide: the recovery becomes an idempotent
  no-op and the PR stays projected `indeterminate` with the real verdict discarded. With it, a
  repeat of the same answer is still refused and a changed answer is a new observation. The cost is
  that a flapping provider appends a row per flap, which is genuine evidence rather than noise.
- **The head projection is itself monotonic in the provider's order.** `ci_current_verdict` selects
  eligible evidence by `pull_request.head_sha`, so a late older head observation overwriting that
  column would make superseded CI evidence current again — the same last-write-wins defect, reached
  through the column the verdict projection depends on rather than through the verdict. A head
  *change* requires the provider's observation time and our append order both to advance;
  re-observing the same head may refresh the timestamp and no more.
- The rollup's subordinate eligibility is enforced **in the `ci_current_verdict` view**, not only in
  prose: a view that returned a coarse rollup beside the fine-grained scopes would let a stale
  `failed` dominate the severity fold while every real check is green.
- Multi-provider support is not designed in: `provider` is `CHECK`ed to `'github'` alone, matching
  `#64`'s statement that `gh` is the interface to GitHub and gate item 11's thin-seam target. A
  second provider widens the `CHECK` in a migration step and brings its substitution test then.

**Status.** accepted

**Source.** `docs/production-schema.md` §6; Issue `#64`; `D-0006`; design review 2026-08-21 (Major:
"poll の再取得、CI rerun、PR head 更新、遅延到着を区別する identity が未定義"). Not drawn from Issue #740.

---

## D-0034 — run↔PR linkage is many-to-many with exactly one live primary, and a repository is never resolved by working directory

**Context.** On 2026-08-06 v1 recorded renga PR #302 with claude-org-ja PR #302's branch, commit and
merge time, because the run→PR tools defaulted an omitted `--repo` to `gh repo view` — the cwd
repository, always the home repo for the Secretary. The tool exited `ok`. Whether it corrupted
silently or failed loudly depended only on whether the home repo happened to own that number.
`#64` cites the incident; the linkage's cardinality and its repository identity were both undefined.

**Decision.** Repository identity is `repository.repo_id`, **never a URL string**. `owner`/`name`
are mutable and are the lookup key, matched case-insensitively while stored case-preserved;
`provider_repo_id` — the provider's immutable id — is the identity where available, so a rename or
transfer is absorbed on the existing row instead of forking the identity. A pull request is keyed
`(repo_id, pr_number)`, which is sound because a provider does not reuse a PR number within a
repository — so a *recreated* PR is a new number and therefore a new row.

`run_pr_link` is many-to-many: a run may have several PRs across repositories, and a PR may be
touched by several runs. **At most one link per run is `primary` at a time**, enforced by a partial
unique index, and only the primary drives the run's completion transition.

`run_pr_link.resolution` is `CHECK`ed to `('project_registry','explicit_operator','provider_event')`.
**There is no value that means "we guessed from the working directory."** A run whose repository
cannot be resolved by one of the three fails to link, and the failure is an incident, not a default.

**Consequences.**
- The three cardinality questions get three different answers, and the "which PR completes the run"
  ambiguity is resolved by the primary rule without forbidding the cross-repo or shared-PR cases.
- v1's `RepoResolutionError` — raise "so the caller can exit non-zero instead of writing a foreign
  repo's PR onto the run" — becomes a `CHECK` constraint rather than a convention.
- A run may be re-pointed by unlinking the primary with a recorded reason and linking another; both
  links stay in the table, so the history of a re-point is auditable.
- Storing `owner`/`name` case-preserved and folding only in the index is required by the use, not
  cosmetic: the value is handed to `gh --repo` and recorded in payloads.

**Status.** accepted

**Source.** `docs/production-schema.md` §7; Issue `#64`; `docs/parity-audit.md` P4; design review
2026-08-21 (Major: "cardinality と repository identity が不足"). Not drawn from Issue #740.

---

## D-0035 — Watcher liveness is a fenced, unconditional trace against an expected scope roster

**Context.** `#64` requires a watcher heartbeat written unconditionally plus a reconcile pass over
undrained events, because a dead `gh` watcher produces no row that proves it stopped — the silent
no-op `tools/relay_scan.py` documents, where a broken cron accumulated undelivered events for twenty
days and was found only when a human queried the ledger by hand. A single `last_heartbeat_at` cannot
express four distinctions the failure needs: poll-succeeded-with-nothing versus poll-failed; a
replaced watcher's late heartbeat; a scope with no watcher at all; and partial coverage.

**Decision.** Two tables. `watcher_scope` is the **expected roster** — which scopes should be
watched, at what interval — derived from work that exists (a scope is created when a run's primary
PR is linked, retired when the PR terminates). `watcher_liveness` is written on **every attempt**,
including the ones that observed nothing, carrying `last_result IN ('observed_change',
'observed_no_change','error')`, separate `last_attempt`/`last_success`/`last_change`/`last_error`
timestamps, and a `consecutive_errors` counter.

The heartbeat write is **fenced inside the write** — the single-statement shape of
`docs/lease-fencing.md`, validating the scope lease's holder and epoch as part of the `UPDATE`,
because `ACCEPTANCE.md` §2 says expiry discovery alone is insufficient. A replaced watcher returning
with its old epoch matches nothing; the refusal is recorded as an `action` row in `status='refused'`,
never silently dropped.

**Consequences.**
- `last_success_at_ms` and `last_error_at_ms` are **history and survive the result that did not
  produce them**, so their constraints are implications rather than biconditionals. Tying either to
  `last_result` both ways would abort the first success-after-error and the first error-after-success
  — every recovery and every failure, which is precisely the alternation the table exists to record.
- **The lease resource is derived from the scope**, `'watcher_scope:' || scope_id`, computed inside
  the statement rather than passed beside it. Accepting it as a separate parameter would let a
  watcher holding a valid lease for one scope heartbeat another — marking an uncovered scope healthy
  and silencing exactly the `watcher_silence` predicate the fence exists to protect.
- The heartbeat is an **upsert**, not an `UPDATE`. A newly registered scope has no row, so a bare
  `UPDATE` affects zero rows on every scope's first heartbeat — and zero rows is also how a stale
  writer is refused, so bootstrap would be permanently indistinguishable from rejection. The insert
  arm carries the same lease fence, and the two remaining causes of zero rows are disambiguated by
  one follow-up read and recorded in the refusal.
- Three distinct incident classes fall out and are kept distinct because their remedies differ:
  `watcher_silence` (a stopped process), `watcher_error_streak` (a broken credential — attempting but
  only failing), and `watcher_scope_uncovered` (a roster entry with no liveness row at all).
- The coverage query is the one a heartbeat table alone cannot express, and it is the direct answer
  to the twenty-day failure: an unwatched scope is wrong the moment it exists, so its tolerance is
  zero.
- The roster is derived rather than hand-maintained, so it cannot fall out of date silently — which
  would recreate the invisible-absence problem one level up.

**Status.** accepted

**Source.** `docs/production-schema.md` §8; Issue `#64`; `D-0001`, `D-0002`; design review 2026-08-21
(Major: "`last_heartbeat_at` だけでは … 区別できない"). Not drawn from Issue #740.

---

## D-0036 — A `Gate` is an entity whose stage is a projection of an immutable transition history, with a closed terminal taxonomy

**Context.** `docs/parity-audit.md` §1.2 records the operator direction that human gates are
first-class `Gate` entities — owner, rationale, options, deadline, outcome — rather than journal
events plus prose plus memory. `#65` gives the escalation form with stages received →
presented-to-human → answered → forwarded-to-worker, and `#64` gives the merge-approval form; the
audit notes they share the schema. The design review found two holes: a single `stage` plus
`updated_at` cannot reconstruct when each stage was entered, by whom, or what was resent, corrected
or answered verbatim; and `forwarded` was the only terminus, so a cancelled run, a withdrawn
question, an expired deadline, an unanswerable question or a superseding question each leaves a
permanently open row that either alarms forever or is quietly ignored.

**Decision.** `gate_transition` is an **append-only, immutable history** — `UPDATE` and `DELETE` both
raise — carrying `transition_kind IN ('open','advance','resend','correction','close')`, the actor
kind and id, the writer epoch, the relay's `message_id` where one applies, a verbatim `body`, a
`supersedes_seq` for corrections, and separate `occurred_at_ms` and `recorded_at_ms`. Only `advance`
moves the stage. `gate.stage`/`stage_seq` are a **projection**, enforced by a trigger that lets them
name only an existing `advance` transition of that gate, and they never walk backwards. There is no
backwards edge in the transition table at all: a question needing re-asking is a **new gate** linked
by `superseded_by`, because a rewind would destroy the aging basis the relay-gap detector reads.

The terminal taxonomy is closed: `answered_and_forwarded`, `withdrawn`, `subject_gone`, `expired`,
`unanswerable`, `superseded`. A gate is closed iff it has an outcome, and only open gates are aged.

`deadline_at_ms` is the **business deadline and is not a relay tolerance**. Missing it produces
`outcome='expired'`; a relay tolerance is a property of a stage and produces a `relay_gap` incident.

**Consequences.**
- Every item the review listed is reconstructible by query: stage entry times, actors, resends,
  corrections, and the verbatim answer — a changed answer is a `correction`, so both texts survive.
- `subject_gone` gets a mechanism, not only a name: the reconcile pass closes gates whose subject run
  reached a terminal status. Without the sweep the outcome would exist in the enumeration and never
  be used, which is the permanent-open-row problem with extra vocabulary.
- `presented → answered` has **no relay tolerance** — a slow human is not a gap (`#65`) — and this
  is expressed as a `NULL` tolerance row in policy data rather than as a special case in the
  detector query, so a future gate type cannot be given a human tolerance by accident.
- Relay-gap detection stays a deterministic query over incomplete transitions with the AI nowhere in
  the path, which `#65` requires and `D-0008` requires more generally.
- A human answering a question is an **actor, not a writer**: the transition is appended through
  Dispatcher Core, because admissibility is a deterministic check and `D-0008` puts deterministic
  evaluation in Core's row.
- **A gate is created without a projection.** `gate_transition` has a foreign key back to `gate`, so
  the gate row must exist before its opening transition can — which means creation is the one moment
  the projection trigger cannot validate. Rather than leave that moment unenforced, creation is
  forbidden from asserting a projection at all: a gate opens at `received` with a null `stage_seq`
  and no outcome, and the opening transition, in the same transaction, sets the projection through
  the `UPDATE` path where the trigger governs. A gate can therefore never be created already
  claiming to be presented, answered, or pointing at another gate's transition.

**Status.** accepted

**Source.** `docs/production-schema.md` §9; `docs/parity-audit.md` §1.2; Issues `#64`, `#65`; design
review 2026-08-21 (Major: "単一 `stage` と `updated_at` だけでは … 復元できない"、"`forwarded-to-worker`
以外の終端がない"). Not drawn from Issue #740.

---

## D-0037 — A gate's relay stages advance on the delivery ack, never on the send

**Context.** `presented` and `forwarded` are claims about something that happened *outside* SQLite.
Advancing the stage before sending loses the relay to a kill in between, and the gate then looks
presented when nobody saw it; advancing after sending, as its own write, re-sends on recovery and
the human sees the question twice. Reordering the two operations does not help, because the gap is
between a durable write and an external effect — the case `ACCEPTANCE.md` §2 says SQLite alone
cannot resolve, since a query cannot distinguish "the effect completed" from "it never started".

**Decision.** A relay is enqueued through the outbox and **the stage follows the ack**:
(1) one transaction inserts a `gate_relay` row and its `outbox` row with
`dedup_key = 'gate/<gate_id>/<to_stage>'`; (2) the delivery worker delivers, the destination
deduplicating on that key; (3) the ack is recorded once, by the existing set-once trigger;
(4) a second transaction appends the `advance` transition referencing that `message_id` and updates
the projection. `gate_relay`'s `(gate_id, to_stage)` primary key makes the enqueue itself idempotent,
so a restarted Secretary re-enqueuing collides and takes the existing `message_id`; retries
accumulate on one outbox row rather than producing a second message.

The reconcile pass completes the crash window: an acked relay with no matching advance transition is
advanced, idempotently, guarded by the same admissibility check as any other advance.

**`presented` means the human window's durable acknowledgement that the gate entered the
human-visible queue** — the Secretary's ack of the relay. It is not a read receipt, because a read
receipt is unobservable here and a tolerance measured against an unobservable event cannot be
evaluated deterministically; and it is not "the message was sent", because that is the crash window
this entry closes. `D-0016` makes the Secretary the single human window, so its queue admission is
the observable boundary that exists.

**Consequences.**
- Both `ACCEPTANCE.md` §2 mechanisms appear, each named for its step: the enqueue is
  `transactional_with_record`, the delivery is `destination_idempotency_key`.
- **`presented` proves the question reached the human's queue, not that a human saw it**, and this
  is the reason the `presented → answered` leg is governed by the gate's `deadline_at_ms` rather than
  by a relay tolerance (`D-0036`).
- A relay enqueued and never acked is a *delivery* stall, detected by its own predicate over
  `gate_relay` joined to `outbox`, not by ageing a stage that is legitimately unchanged. The two
  states are distinguishable only because the advance is ack-gated.
- `outbox.dedup_key` is deliberately **not** made unique: the spike's comment explains why that
  column is non-unique on purpose, and gate relays get their own identity table instead of changing
  a shared table's semantics.

**Status.** accepted

**Source.** `docs/production-schema.md` §9.5; `ACCEPTANCE.md` §2 (fault injection, mid-flight kill);
Issue `#65`; design review 2026-08-21 (Major: "送信後・stage commit 前の kill により重複、commit 後・
送信前なら欠落"). Not drawn from Issue #740.

---

## D-0038 — AC-9's cohort is terminal-in-period Interlock-owned runs, its unit is the AI invocation, and coverage is required output

**Context.** AC-9 states its targets "per 100 worker runs" — a normalisation, not a cohort. The
design review found the denominator, the prompt unit, the period-crossing rule and the
missing-telemetry rule all undefined, and noted that treating a missing usage figure as zero
overstates the reduction in exactly the criterion the reduction is judged by.

**Decision.** The **cohort** is runs whose **entire lifetime** falls inside the report period —
created at or after its start and terminal before its end — and that were Interlock-owned
throughout. Stating it as "terminal in period" alone leaves two defensible readings and therefore
two denominators; the lifetime clause is what makes `started_before_period` an exclusion rather than
a contradiction. A started-run cohort is right-censored by construction and
its bias always flatters the target; ownership is decided once at run start (`D-0013`) so the second
clause is automatic and is asserted rather than assumed; and the v1 baseline normalises *completed*
runs, so a started-run cohort would not be comparable to it. Runs outside the cohort go to an
`excluded` bucket with a reason — including `started_before_period`, which is excluded from the rate
rather than contributing a partial numerator to a full denominator.

**One AI prompt is one model response** — one assistant turn returned by the provider — so AC-9's
numerator is `SUM(model_response_count)` over the cohort's invocations. The unit matches the
baseline's, and it is easy to get wrong in both directions: counting **tool calls** compares against
v1's 4,960 rather than its 3,531 model responses and reports a reduction that does not exist, while
counting whole **agent invocations** compares a coarser unit against a finer one and *overstates*
the reduction by the tool-use factor. Transport retries of one request add no response. The
invocation is still counted and printed as `invocation_count`, because AC-1 is a statement about
invocations, not responses. Cache-read tokens are neither input nor output tokens (`ACCEPTANCE.md`
§5) and never enter the arithmetic.

**Coverage and the excluded-reason breakdown are required output. A reduction rate printed without
them is not a valid report.** `ai_invocation.usage_status IN ('reported','partial','unavailable')`
makes a missing usage record a named fact; the report prints coverage, an observed reduction over
covered invocations only, a **bounded** reduction imputing each missing invocation at its recorded
`max_output_tokens × model_response_count` ceiling, and a **sensitivity** reduction imputing at the
covered p95.

Only the bounded figure supports an acceptance claim, and the distinction is load-bearing: **a
percentile of the observed sample does not bound the unobserved values.** A missing invocation may
exceed the covered p95, and is more likely to if telemetry loss correlates with long responses — so
calling a p95 imputation "conservative" can pass a target the real numbers fail. The request's own
output ceiling *is* a bound, because the provider cannot return more than the caller allowed. An
invocation with no recorded ceiling is not imputed at all but counted as `unbounded_missing`, and a
report with a non-zero `unbounded_missing` count cannot support an AC-9 acceptance claim.

**Consequences.**
- AC-1 is the same measurement from the other side: every invocation row must carry an
  `incident_id`, and a row without one is reported as an AC-1 violation rather than folded into the
  count.
- **The harness emits no pass/fail verdict.** Whether a cohort is large enough to judge on is canary
  exit criteria — `Q-0005`, open — and a threshold invented here would answer it by inertia.
- The provider seam is one adapter filling three usage columns; everything else in the harness is
  provider-neutral, which is `#67`'s split.
- The p95 imputation is a labelled assumption, not a law, and is recorded in the report header so a
  reader can recompute under a different one. It is printed because the bounded figure alone is too
  loose to say anything about the likely truth.

**Status.** accepted

**Source.** `docs/measurement-harness.md` §2; `ACCEPTANCE.md` AC-9, §5; Issue `#67`; design review
2026-08-21 (Major: "denominator と telemetry completeness が未定義"). Not drawn from Issue #740.

---

## D-0039 — AC-10's ground truth is external: a labelled fixture suite and shadow reconciliation, with false termination counted at the applied action

**Context.** Interlock's own tables cannot contain a miss. A missed condition produces no incident
row, so an aggregate over `incident` counts what was detected and is structurally blind to what was
not; the latencies that survive are the fast ones if the slow ones were dropped. Any harness reading
only our rows measures its own recall as 100%. Separately, `D-0004` and AC-6 mean the Dispatcher AI
cannot terminate anything, so counting recommendations would compare Interlock's recommendations
against v1's executions — and counting Interlock's executions of a capability it does not have would
report a structural zero as a triumph.

**Decision.** Ground truth comes from **two sources outside the thing being measured**.

**Source A, the labelled fixture suite** — AC-2's corpus extended with an `expected.json` per case
carrying `incident_class` (or `none`), `onset_offset_ms`, `budget_ms`, `fact_state`,
`must_not_recommend`, and `provenance`. A miss is a labelled condition with no matching incident
within its budget; a false positive is a `none` case that produced one; latency is exact because the
clock is synthetic. **Negative cases are mandatory** — `D-0006` requires observation-failure
fixtures, and a positive-only corpus would let a detector that alarms on everything score a perfect
miss rate.

**Source B, shadow reconciliation** — episode-to-episode, not row-to-row, over declared correlation
keys per subject class (`docs/measurement-harness.md` §3.3). **Unmatched buckets are first-class
output** — `both`, `interlock_only`, `v1_only`, `unmatched_key`, `censored` — carrying forward v1's
reporter policy of an explicit unmatched bucket and never a silent drop. A `v1_only` episode is a
*candidate* miss, adjudicated against the labels or by a human, never silently converted into a miss
count and never silently discarded.

**A false termination is an `action` row with `kind='terminate_session'` and `status='applied'` whose
subject was not in fact stuck**, decided by the fixture label, then the subject's own subsequent
evidence, then human adjudication — and `undetermined` where none of the three settles it, which is
`D-0006` applied to the measurement instead of the detection.

**Every episode's observation window is right-censored**: an episode whose window is not fully inside
the report period is `censored`, excluded from the miss and latency numerators, and counted.

**Consequences.**
- The fixture suite is the ground truth that exists **before** the canary, which matters because
  AC-10 gates the canary and the shadow source only exists during it.
- Without censoring, every report boundary manufactures misses out of episodes detected seconds after
  the period ended, and the manufactured rate rises as the period shortens. The censored count is
  printed because a large one means the period is too short for the budgets being judged.
- Three supporting series are reported beside the false-termination count — `recommended_terminate`,
  `recommended_but_not_applied`, `applied_terminate` — so the AI's precision and the human gate's
  value stay visible rather than being hidden by a headline number.
- The escalation correlation key is **positional within a run** and is the weakest join in the
  reconciliation; its failures surface as unmatched episodes rather than wrong pairings, which is
  the safe direction, and a canary producing many of them is telling us the key needs replacing.

**Status.** accepted

**Source.** `docs/measurement-harness.md` §3; `ACCEPTANCE.md` AC-2, AC-6, AC-10; `CHARTER.md` §4;
Issue `#67`; design review 2026-08-21 (Blocker: "miss / false termination の独立した ground truth が
ない"、Minor: "watcher candidate、AI recommendation、human approval、実行済み termination のどこを
数えるか未定義"). Not drawn from Issue #740.

---

## D-0040 — A measurement report records its own provenance, and the harness is read-only by capability

**Context.** `ACCEPTANCE.md` §3 condition 5 requires shadow observation to be read-only **enforced by
capability, not by convention**, and AC-7 requires a divergence report to exist. The design review
added that a report which does not record its period, versions, query definitions and input database
identity cannot be reproduced later — and v1's own reporter records the concrete trap: the ordinary
connect helper "would happily run forward migrations", so a report tool using it is a writer.

**Decision.** The harness opens the database `mode=ro` with `PRAGMA query_only=ON`, holds no lease
and no writer epoch, and never calls the migrator — which `D-0029` keeps as an explicit call
separate from opening, so this is achievable rather than aspirational.

Every report carries a provenance header in both renderings: half-open period bounds, tool version,
database path with `application_id`/`user_version`, a database fingerprint, the
`schema_migration` head, the **`policy_revision_id`** the latencies were judged under, the **set** of
`detector_version` and `adapter_version` values observed, the query definitions as text with a hash
over the set, the fixture suite reference with its positive/negative split, the imputation rule, and
the coverage, censoring and unmatched-bucket counts.

A period spanning more than one `detector_version` or `policy_revision_id` is **non-homogeneous** and
the report says so at the top rather than averaging across the change.

**Consequences.**
- Reporting the *set* of detector versions rather than a single value is the report's obligation
  under `Q-0009`, which stays open: exposing the set is not the same as deciding cross-version
  compatibility, and collapsing it would hide the thing `Q-0009` exists to settle.
- The database fingerprint is a **content** hash over the ordered rows of each table read. Row counts
  and `MAX(seq)`/`MAX(rowid)` were considered and rejected: most of what a report reads is updated in
  place — a verdict projection, an `outbox` status, a `gate` outcome, a backfilled `usage_status` —
  and every one of those changes the answer while changing no count and no maximum, so an aggregate
  fingerprint would certify two materially different reads as identical. The cost is linear in rows
  read, which the measured baseline puts in the low thousands per period. The aggregate form survives
  as an explicitly weaker interactive mode, stamped `fingerprint_mode: aggregate`.
- Query definitions travel as data, in the same spirit as the spike's `RECONSTRUCTION_QUERIES`, so a
  reader can run them by hand against a recovered database.
- The report emits no go/no-go verdict, for the same reason `D-0038` gives: `ACCEPTANCE.md` §3 says
  AC-9's targets are not canary thresholds and does not convert one into the other, so neither does
  the instrument.

**Status.** accepted

**Source.** `docs/measurement-harness.md` §§1, 5, 6; `ACCEPTANCE.md` AC-7, §3; Issue `#67`; design
review 2026-08-21 (Minor: "report は read-only に加え、期間、schema/detector version、adapter version、
query definition、入力 DB identity/hash を記録しないと後日再現できない"). Not drawn from Issue #740.

---

## D-0041 — The run status vocabulary is closed and forward-only, and a detection budget carries its own kind

**Context.** `D-0029`'s DDL was authored ahead of the implementation, deliberately, so that the
implementation Issues would start against a settled schema. Writing `0001_initial.sql` for Issues
`#64`/`#65`/`#67` found two places where it could not be applied as written, and both were found the
same way — by there being nothing to type.

The first is `run.status`. `docs/production-schema.md` §2 records `run` as **re-derived** and promises
the production table "a `CHECK` on a closed status set and a forward-only trigger", but no section of
the document enumerates the set or says which of its members are terminal. A promise of a closed set
with no members is unconstrained text, which is what the spike had.

The second is `policy_detection_latency`. `threshold_kind` exists because three of
`docs/time-base-policy.md` §3.2's classes have a `T` that is not a duration. The same table's §3.2 row
for `lease_orphan` gives its `L` as **"2 × lease TTL"**, and `budget_ms` is a plain absolute
millisecond column with a `CHECK` — `threshold_value + reconcile_period_ms <= budget_ms` — that
compares it against `T`. The seed migration therefore had no way to write the row: a TTL multiple does
not fit an absolute column, and a lease's TTL is a per-acquire parameter with no default anywhere in
the code, so there is no number to convert it to.

**Decision.** Two changes, adjudicated together because they are the same shape — a design authored in
advance meeting the first thing that has to be typed against it.

1. **`run.status` is the closed set `('created', 'running', 'suspended', 'completed', 'failed',
   'cancelled')`,** with `{completed, failed, cancelled}` terminal. A `CHECK` enforces the set and a
   trigger enforces the walk: leaving a terminal status is refused (including `completed → failed`,
   which no rank ordering catches because the ranks are equal), and `running ↔ suspended` moves in both
   directions because a suspend is a pause rather than a step, while every other reversal is refused.
   Recorded as `docs/production-schema.md` §4.3.
2. **`policy_detection_latency` gains `budget_kind`** (`'absolute_ms' | 'lease_ttl_multiple'`,
   defaulting to `'absolute_ms'`), the symmetric partner of `threshold_kind`, and the `T + P ≤ L`
   `CHECK` is narrowed to apply only when `threshold_kind` **and** `budget_kind` are both
   `'absolute_ms'`. Rows relative on either side are asserted per subject by the
   `policy_budget_violation` reconcile pass, which already exists for the relative-`T` case.

**Consequences.**
- `suspended` is what makes `docs/time-base-policy.md` §3.4's suspension rule implementable: a
  deliberately paused run suspends its session-class predicates by **moving to a status the predicates
  exclude**, never by suppressing a tolerance, because exclusion by status is auditable and suppression
  is not. That is also why `suspended` is not terminal.
- The terminal set is now a thing three readers can select on rather than a phrase: `production-schema.md`
  §9.4's `subject_gone` sweep, `D-0038`'s "terminal before period end" cohort, and the harness's
  `terminal_status_unknown` excluded-reason bucket — which, with the set closed, should stay empty, so a
  non-zero count reads as a schema-integrity signal instead of routine noise.
- **Rejected for the status set: leaving it unconstrained text.** That is what the spike did, and
  `production-schema.md` §2 records *why* — the writer assignment was open, so a `CHECK` enumerating the
  statuses would have answered `Q-0001` in DDL before `Q-0001` was decided. `Q-0001` is resolved by
  `D-0029` and §4.2 assigns `run.status` exclusively to the Secretary, so the reason for the restraint
  is gone and keeping the restraint would be inheriting a workaround for a question that has an answer.
- **Rejected for the budget: seeding no `lease_orphan` row at all.** It is the cheapest way to make the
  migration apply, and it silently drops a class `docs/time-base-policy.md` §3.2 lists as policy data —
  a detector class that exists in the design and nowhere in the database, which is the failure the
  versioned-policy shape exists to prevent.
- **Also rejected: precomputing the TTL multiple into milliseconds.** It is the identical mistake
  `threshold_kind` was introduced to prevent, one column to the right — baking one lease's TTL into a
  row every other lease also reads.
- The narrowed `CHECK` is weaker in DDL than the original: an absolute `T` against a relative `L` is no
  longer refused at insert. That is the trade, and the `policy_budget_violation` pass is where the
  inequality is asserted for those rows instead. The costs are one more column and one more thing the
  pass must cover.
- No `D-` entry is superseded. `D-0029` and `D-0031` stand; this entry fills two gaps their documents
  left rather than reversing anything either decided.

**Status.** accepted

**Source.** `docs/production-schema.md` §§2, 4.2, 4.3, 10, 11; `docs/time-base-policy.md` §§3.2, 3.4;
`docs/measurement-harness.md` §2.1; `D-0029`, `D-0031`, `D-0038`; the implementation of Issues `#64`,
`#65` and `#67`. Not drawn from Issue #740.

---

## D-0042 -- A message nobody wants is cancelled, not left pending, and cancellation is terminal rather than an erasure

**Context.** `D-0029`'s `outbox` was carried verbatim from the spike, status vocabulary included:
`pending`, `delivered`, `acked`, walked by a forward-only trigger whose comment argues that a *total*
order is what protects the delivery evidence. That vocabulary has no state meaning **"this message is
no longer wanted"**, and the implementation of `#65` found the gap the way gaps get found here -- an
external review reproduced it.

Closing a gate (`withdrawn`, `expired`, `subject_gone`, `superseded`) left the relay it had enqueued
sitting at `pending`. Two consequences, both real and both reproduced:

1. A delivery worker reading `outbox` is still instructed to present a withdrawn question, or to
   forward an answer to a gate that is closed.
2. `stalled_relays()` (`docs/production-schema.md` section 9.6) names that relay for as long as the
   database lives, with an age that grows without bound -- which is exactly the **alarms forever**
   failure that section 9.4's `subject_gone` outcome exists to end, reproduced one table over.

The second consequence is what makes this a decision rather than a bug fix. Section 9.4 introduced an
outcome *specifically* so a gate whose subject vanished would stop alarming. Leaving the relay live
reproduces the failure that outcome was written against, so the taxonomy's own argument implies that
closure must be able to retire the relay -- and the schema, as it stood, made that inexpressible.

**Decision.** Two parts, taken together because either alone leaves half the defect open.

1. **`outbox.status` gains a fourth, terminal value `cancelled`,** in migration
   `0003_outbox_cancelled_status.sql`. The forward-only trigger becomes a **lattice** rather than a
   total order: `pending -> {delivered, cancelled}`, `delivered -> {acked, cancelled}`, and no edge out
   of `acked` or `cancelled`. `close_gate` cancels every not-yet-acked relay of the gate **in the same
   transaction as the closure and its spine event**.
2. **A `delivered` relay may be cancelled; an `acked` one may not.** `delivered` means *sent*, not
   *answered*. Section 9.5 makes the stage advance on the **ack**, so an unacked `delivered` relay is
   precisely one waiting for something that will now never come. Refusing to cancel it would leave
   consequence 2 open for every relay that happened to be delivered first, which is most of them.

**Consequences.**
- **The lattice still protects the evidence, which is what the total order was for.** Cancellation is a
  terminal *status change*, never an erasure: `delivered_at_ms` and `retry_count` survive it untouched,
  the set-once triggers still refuse to rewrite them, and the `(status = 'acked') = (acked_at_ms IS NOT
  NULL)` `CHECK` is what makes "a cancelled row can never carry an ack" a schema fact rather than a
  convention. What the total order actually guaranteed -- that evidence is never walked backwards or
  rewritten -- is unchanged; what it also happened to forbid was saying that a message is moot.
- **The send-time re-check is kept, not replaced.** A delivery worker still re-reads
  `gate.closed_at_ms` before sending, because a worker that read the outbox row *before* the
  cancellation committed holds a row that was true when it read it. The schema closes the durable half;
  the re-check closes the in-flight half. Neither is redundant.
- **Every predicate that meant "not yet done" had to move.** `status <> 'acked'` silently began matching
  cancelled rows, so the `outbox_undelivered` partial index, `events.orphaned_outbox` and
  `gates.stalled_relays` are now spelled `status IN ('pending', 'delivered')`. The spike-side readers in
  `outbox.py` and `schema.py` are deliberately **not** changed: `spike_schema.sql` has no `cancelled`,
  and a widened predicate there would be a lie about a schema that does not have the value (`D-0026`).
- **One incidental tightening, stated rather than smuggled:** `pending -> acked` in a single hop is now
  refused, where `0001`'s rank comparison admitted it. An ack on a row never recorded as sent is either
  a lost `delivered` write or an ack for something that was never sent, and neither should be silent.
- **The migration is a full table rebuild, and it changed the migrator.** SQLite has no `ALTER TABLE`
  that touches a `CHECK`, so the vocabulary can only widen through the documented 12-step rebuild.
  Step 1 of that procedure is `PRAGMA foreign_keys = OFF`, which is a **no-op inside a transaction** --
  and `production-schema.md` section 3.2 puts every step inside one. `PRAGMA defer_foreign_keys` was
  tried and measured not to work: `DROP TABLE` increments the deferred violation counter per orphaned
  child row and restoring the parent by rename does not decrement it. So the pragma is issued by
  `_apply_pending` around the whole run and each step now ends with a whole-database
  `PRAGMA foreign_key_check` inside its own transaction. That is a *wider* check than the per-statement
  enforcement it replaces, since it also catches violations a step's own DDL created.
- **Rejected: a `cancelled_at_ms` column.** The instant is already durable twice -- `gate.closed_at_ms`
  and the `gate_closed` / `gate_expired` spine event that commits with it -- and a third copy is a third
  thing that can disagree.
- **Rejected: having `stalled_relays()` exclude closed gates and stopping there.** It silences the alarm
  on a defect that is still live: the delivery worker would go on presenting withdrawn questions, now
  with nothing pointing at it. Removing the signal while leaving the fault is the failure mode this
  system is built against.
- **Rejected: relying on the delivery worker's check alone.** No delivery driver exists in this branch,
  so adopting it as the whole answer would have left the defect fully open in code and present only as
  a promise made to a component nobody has written yet.
- `D-0029` and `D-0030` stand. This extends `outbox`'s vocabulary; it reverses nothing either decided.

**Status.** accepted

**Source.** `docs/production-schema.md` sections 5.6, 5.7, 9.4, 9.5, 9.6, 11; `D-0026`, `D-0029`,
`D-0030`; the implementation of Issues `#64` and `#65` and an external review of its diff. Not drawn
from Issue #740.

---

## Open questions

These are gaps where implementation needs an answer and Issue #740 provides no basis. They are
**not decisions**. A `Q-` entry is resolved by adding a new `D-` decision (with the next free `D-`
number) and marking the question resolved by that ID; `Q-` IDs are stable and are never reused.

### Q-0001 — What is the concrete SQLite schema/DDL and migration policy for the SoT tables?

**Status.** resolved by D-0029

**Question.** D-0001 names the SoT tables (`run`, `task`, `session`, `lease`, `incident`,
`assessment`, `action`, `outbox`) but not their columns, keys, indices, per-item single-writer
assignment, or how schema changes are applied to a live database.

**Why unresolved.** The Issue fixes the store and the entity list but stops above the schema layer.
It also does not restate v1's per-item single-writer table (a 2026-07-20 review requirement, e.g.
"run status transitions stay exclusively the Secretary's"), so the writer assignment for Interlock's
entities is unstated.

**What would settle it.** A schema design accompanying the first implementation Issue: DDL, a
single-writer table per state item, a migration mechanism, and the resume-after-kill queries that
D-0001 requires. Carrying v1's SQLite state semantics is already mandated (D-0014), so the resolution
should record which v1 semantics are carried verbatim and which are re-derived.

### Q-0002 — What exactly composes the incident dedup key, and what is the re-notification rate in absolute time?

**Status.** proposed

**Question.** D-0007 requires each incident to carry a dedup key, but not what the key is made of
(worker/run id? fact state? detector version? known pattern? a time bucket?). The re-notification
rate and dedup window are likewise unspecified in absolute time.

**Why unresolved.** The 2026-07-20 review established the *rule* — dedup windows and re-notification
rates expressed as cycle counts must be re-derived into absolute time **before** any period is
shortened — but neither 2026-08-17 comment fixes the values. With `/loop 3m` retired (D-0002), the
old cycle-count denominators no longer have a meaning at all.

**What would settle it.** Re-deriving the windows in absolute time from the accepted detection
latency (see Q-0003), plus a decision on key composition validated against accident-derived
fixtures — a key too broad suppresses genuine incidents, too narrow re-notifies the same one.

### Q-0003 — What is the reconcile interval, and what tolerable detection latency justifies it?

**Status.** resolved by D-0031 (with D-0032)

**Question.** D-0002 keeps a low-frequency reconcile loop but does not give it a period, nor state
the detection latency the organisation is willing to accept.

**Why unresolved.** "10 minutes" appears in the Issue only inside a projection of program tick counts
("push notification + 10-minute reconcile … roughly 470–1,576 ticks per 100 runs"), i.e. as an
assumption used to produce an estimate, not as a decision. The 2026-07-20 review explicitly demanded
that the requirement basis for any period — the tolerable detection latency — be settled first, and
that has not been recorded.

**What would settle it.** Deciding tolerable detection latency per incident class (with and without
a human present), then deriving the reconcile period from it. This must precede Q-0002, since
re-notification windows depend on it.

### Q-0004 — If the Agent View gate fails, which concrete alternative `SessionProvider` is used?

**Status.** resolved by D-0025

**Question.** D-0019 says the control-plane design survives a gate failure and that "another
`SessionProvider` is considered", but names no candidate.

**Why unresolved.** The Issue deliberately keeps the fallback abstract; no candidate backend is
evaluated anywhere in it. Note that D-0014 discards tmux/pane/send-keys as a *backend contract*, so
"fall back to v1's transport" is not automatically available as an answer.

**What would settle it.** The Agent View spike producing a pass/fail result; on failure, an
evaluation of candidate providers against the same eleven gate items, recorded as a new decision.

### Q-0005 — How long does the canary run, over how many runs, and what numeric criteria advance or roll it back?

**Status.** proposed

**Question.** D-0013 fixes the canary *shape* (one worker, run-boundary rollback) but not its
duration, sample size, or the numeric thresholds that permit advancing beyond one worker.

**Why unresolved.** The Issue states qualitative conditions — a shadow-period divergence report must
exist, rollback conditions must exist, and detection latency / false termination / misses must not be
worse than today — plus reduction *targets* (≥95% AI prompts, ≥90% output tokens per 100 worker
runs). It does not convert these into a pass threshold, a sample size, or a time box.

**What would settle it.** Defining, before the canary starts, the metric set, the sample size that
makes the comparison meaningful (measured baseline: 195 completed runs in under 7 days, union uptime
153.7 h), and the numeric go/no-go thresholds. Recorded in `ACCEPTANCE.md` once decided.

### Q-0006 — What is the retention and scrubbing policy for evidence references and incident history?

**Status.** proposed

**Question.** D-0007 requires evidence references and incident history in SQLite, but nothing states
how long they are kept, whether referenced material is copied or only pointed at, or whether any
content is redacted before storage.

**Why unresolved.** The Issue specifies what to store and that the record must be auditable and
resumable; it is silent on lifetime and on content policy. The nearest prior constraint is the
2026-07-20 rule that only structured fields (task id, state enum, timestamps, counts) may be copied
into events/notifications and that free-text transcript quotation is forbidden — but that rule was
written for the *notification* surface, and neither 2026-08-17 comment restates it for incident
storage.

**What would settle it.** A decision covering: reference vs. copy, retention period per table,
whether the structured-fields-only rule extends to stored evidence, and how expiry interacts with
fixtures derived from real incidents (D-0014 wants those kept).

### Q-0007 — What auth identity and permission tier does the Dispatcher AI hold in Interlock?

**Status.** proposed

**Question.** D-0004 forbids the Dispatcher AI from executing side effects and D-0007 restricts its
tools, but neither says what identity it authenticates as, what tier it holds, or how that identity
is provisioned and rotated.

**Why unresolved.** The 2026-07-20 trust-boundary analysis explicitly required, for the daemon
design, a **dedicated auth identity, a dispatcher-equivalent tier, and a minimal tool set (never
secretary tier)**. Neither 2026-08-17 comment restates this requirement, and the messaging
architecture it was written against (broker peer identity, inherited peer `dispatcher`, old-queue
drain) is superseded by D-0009's `MessageBus`. So it is genuinely unclear whether the requirement
carries forward unchanged, is subsumed by the `MessageBus` message-identity contract, or needs
restating in Interlock's terms.

**What would settle it.** An explicit confirmation-or-restatement recorded as a new decision: the
Dispatcher AI's identity, its tier relative to Secretary, its provisioning, and how `MessageBus`
message identity relates to auth identity.

### Q-0008 — When is the repository/package renamed from `claude_org_runtime` to Interlock naming, and what is the compatibility policy?

**Status.** proposed

**Question.** The tree inherited from the fork base still carries the upstream package name
`claude_org_runtime`. When does it become Interlock naming, and does any import-compatibility alias
exist during the change?

**Why unresolved.** The Issue names the fork and its base commit but says nothing about renaming.
Two accepted decisions bear on it without deciding it: D-0011 (no upstream tracking, so no rename
cost from future merges) and D-0014 (permanent compatibility shims for the old backend are
discarded, which argues against a long-lived alias — though that bullet is about backend contracts,
not package names).

**Explicitly out of scope for this document task.** The founding-documents task must not touch
`README.md` or `pyproject.toml`. This entry records the question only.

**What would settle it.** A rename decision taken with the first implementation Issue, stating
timing, whether any transitional alias exists, and its removal date if so.

### Q-0009 — What are the semantics of `detector version`, and what is the compatibility rule across versions?

**Status.** proposed

**Question.** D-0007 requires every incident to record a detector version, but nothing defines its
granularity (global, per-detector, semantic?), when it must be incremented, or how incidents and
fixtures recorded under an older version are treated after a bump.

**Why unresolved.** The Issue lists detector version among the fields to define in phase P0 and
among the things to persist, but does not define it. This matters directly for replay: D-0007 makes
stored incidents double as regression fixtures, and D-0005's closed fact-state set is only
deterministically testable if "which detector produced this" is unambiguous.

**What would settle it.** A P0 contract definition: versioning scheme, increment rule, and the
policy for replaying or re-classifying incidents recorded under a superseded detector version.

### Q-0010 — Where does the "unclassified anomaly" counter live in Interlock, and what threshold routes it to a human?

**Status.** proposed

**Question.** D-0006 forbids treating insufficient evidence as an anomaly, but something must still
prevent genuinely unknown states from being silently swallowed. Which component counts them, and at
what threshold does a human get told?

**Why unresolved.** The 2026-07-20 review required an unclassified-anomaly counter with a threshold
notification as the replacement for the LLM's "something feels off" layer, stating that unknowns must
reach the Secretary rather than be ignored. Neither 2026-08-17 comment restates it, and the layering
has changed since: with an on-demand Dispatcher AI (D-0003) it is no longer obvious whether the
counter belongs to the watcher, to the AI-triage path, or to the reconcile loop.

**What would settle it.** A decision assigning ownership of the counter within the D-0008 boundary
table, plus a threshold expressed in absolute time (compare Q-0002/Q-0003) and the route by which it
reaches a human.

### Q-0011 — What is the tolerable Secretary window response latency, and under what worker load is it measured?

**Status.** proposed

**Question.** D-0016 requires the Secretary not to block its response on worker monitoring,
long-running work, or AI judgement, and `ACCEPTANCE.md` gate item 8 requires responsiveness "while
workers are loaded". Neither states what latency is acceptable, nor the load profile (concurrency,
open incidents, in-flight long tasks) the measurement is taken under.

**Why unresolved.** The Issue states the property qualitatively and never converts it into a number
or a load definition. Q-0005 covers canary duration and go/no-go criteria — a different subject —
so nothing in the Issue supplies a responsiveness threshold.

**What would settle it.** A decision fixing an acceptable latency (or latency distribution) for
Secretary request→response, the load profile it is measured under, and whether it is a gate item
threshold, a canary criterion, or an ongoing service objective.

### Q-0012 — What are the precise semantics and detection predicates of each fact state?

**Status.** proposed

**Question.** D-0005 fixes the six fact-state names as a closed set, but what evidence puts a
worker into `ACTIVE_EVIDENCE` rather than `NO_ACTIVITY_EVIDENCE`, what makes a wait a `KNOWN_WAIT`,
what counts as an `EXPLICIT_BLOCK` as opposed to an inferred one, and what distinguishes
`OBSERVATION_UNAVAILABLE` from `NO_ACTIVITY_EVIDENCE` are all undefined.

**Why unresolved.** The Issue enumerates the names and settles exactly one semantic point — that
`NO_ACTIVITY_EVIDENCE` is not an anomaly (D-0006). It lists fact-state definition among the P0
contract items still to be produced, so the semantics are deferred by the source, not omitted by
this document.

This question also carries the **exact-enumeration** point. The Issue says 最低限 ("at minimum")
about a set it simultaneously calls closed, which leaves it ambiguous whether the six are the
complete vocabulary or a floor. Since the fact state is persisted, that ambiguity propagates into
schema and replay compatibility: a reader must know whether encountering a seventh value is a
corrupt record, a newer writer, or a legitimate extension.

**What would settle it.** The P0 contract: for each state, its meaning, the predicate over observed
evidence that yields it, its precedence when several predicates match, and fixtures pinning each —
plus an explicit statement of whether the six are exhaustive, and if not, how a reader must behave
on an unrecognised value. This pairs with Q-0009 (detector version), since a state is only
deterministically replayable if "which detector, at which version, produced it" is unambiguous.

### Q-0013 — Does the "control plane outside the worker" principle survive into Interlock?

**Status.** proposed

**Question.** Issue #740's original body stated a design principle that no runtime machinery may
live inside the observed party (the worker). Does that principle hold for Interlock, and if so in
what form?

**Why unresolved.** The 2026-07-20 adversarial review
([COMMENT](https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5017473782))
explicitly revised it: workers already execute their own checkout's `PreToolUse` hooks, so the
principle was already broken at its most important point, and an org-managed, read-only
`Notification` hook placed on the worker side — in a configuration layer the worker cannot write —
was adopted as the primary approval-pending signal. D-0017 then fences workers *with* hooks. So the
original absolute form is superseded, and neither 2026-08-17 comment restates any replacement form.
Recording the original wording as a binding Interlock role boundary would republish a revised
principle as a decision, which is why `CHARTER.md` does not state it.

**What would settle it.** A decision stating the surviving form, if any: which categories of
worker-side configuration are permitted (read-only, org-owned, worker-unwritable), which are
forbidden (anything the worker can modify, anything that exerts control), and how that interacts
with D-0017's hook-based fencing and with D-0010's public-CLI-only observation.

### Q-0014 — Which subset of the porting ledger constitutes the initial seed, and in what order?

**Status.** proposed

**Question.** `PORTING_LEDGER.md`'s `carry` + `rewrite` rows total roughly twice the 12–15k LOC
seed estimate in D-0014. Which subset is the initial seed, and in what sequence?

**Why unresolved.** The Issue gives a size estimate for the seed but classifies material only into
carry / rewrite / discard; it never selects or orders a subset. The ledger is a classification, not
a commitment to port every classified row.

**What would settle it.** A prioritisation recorded with the first implementation Issues, selecting
seed rows by path and stating the order and the criterion used — not a reclassification of rows to
make the arithmetic land on the estimate (D-0014 forbids treating LOC as the metric).

### Q-0015 — Do carried tests port before, with, or after the module they cover?

**Status.** proposed

**Question.** D-0014 mandates rescuing accident-derived fixtures, fault injection, and recovery
tests. Nothing states their sequencing relative to the code they exercise.

**Why unresolved.** The Issue is silent on sequencing. It matters most for the ledger's hybrid
`carry (invariant) / rewrite (mechanism)` rows, whose mechanism half must be re-targeted at a
contract that does not exist yet.

**What would settle it.** A working rule recorded with the first implementation Issues: whether a
carried invariant is landed first as a failing specification against the new contract, alongside
it, or after it — and how the `Proof` field of each implementation Issue evidences the choice.

### Q-0016 — Which quarry lessons from `discard` rows become decisions?

**Status.** proposed

**Question.** Several `discard` rows in `PORTING_LEDGER.md` carry a design lesson worth keeping as
a rule rather than as code: the `auth_role`-versus-display-role separation
(`broker/surface.py`, `broker/tokens.py`), the re-entrant-lock deadlock discipline
(`broker/server.py`), health proven by a real protocol round trip (`broker/rpc.py`), and silent
message loss from a serialisation type mismatch (`tests/broker/test_channel_sentat_drop.py`).
Which of these become `D-` entries?

**Why unresolved.** The Issue's quarry rule permits extracting invariants and detection semantics
from old material but does not enumerate which specific lessons are promoted, and none of these is
named in either 2026-08-17 comment.

**What would settle it.** A decision per lesson, taken when the corresponding contract is designed.
Related open items: Q-0001 (schema/DDL and single-writer assignment), Q-0002 (dedup key
composition), Q-0007 (Dispatcher AI auth identity and tier).

### Q-0017 — What replaces the discarded desktop human-notification path?

**Status.** proposed

**Question.** `attention/platform.py` and its test are classified `discard` in
`PORTING_LEDGER.md` because the Issue gives no basis for a desktop-notification backend in the
three-layer design. Beyond the decision that a human is reached through the Secretary or a human
gate (D-0004, D-0008, D-0016), how a human is actually notified is unstated.

**Why unresolved.** Neither 2026-08-17 comment names a notification channel or delivery mechanism
for reaching a human. Carrying the old OS-notification backend forward would be inventing a
delivery channel the source does not decide.

**What would settle it.** A decision naming the human-notification route, its ownership within the
D-0008 boundary table, and its relationship to Q-0010 (where the unclassified-anomaly counter lives
and at what threshold it reaches a human).

### Q-0018 — Do repository-root, packaging, and CI files need their own classification pass before the seed begins?

**Status.** proposed

**Question.** `PORTING_LEDGER.md` scanned `src/`, `tests/`, and `docs/` only. Everything else —
repository-root files, packaging, CI configuration, tooling — has no row and is unclassified. Does
it need a classification pass before the seed starts?

**Why unresolved.** The Issue's carry / rewrite / discard rubric is written about contracts,
implementation, and tests; it says nothing about build and repository infrastructure.

**What would settle it.** A decision either extending the ledger to those paths with the same
rubric, or explicitly declaring them out of the ledger's scope and handled by the implementation
Issues that touch them. The package rename is tracked separately as Q-0008.

### Q-0019 — Who owns each of the retired loop's non-detection duties?

**Status.** resolved by D-0032 (with D-0031)

**Question.** Retiring `/loop 3m` (D-0002) removes more than detection. The 2026-07-20 review
enumerates the loop's other duties: pull-fallback drain (the DELEGATE receive path when the sidecar
is unhealthy), curate-inflight management, CI relay, `pending_decisions` aging, and auto-stop. Which
component owns each in Interlock?

**Why unresolved.** The review requires only that each duty's new owner be made *explicit*; it does
not name the owners. Neither 2026-08-17 comment assigns them. Assuming they all fall to Dispatcher
Core would be an invention — some are plausibly Secretary or handler duties under D-0004.

**What would settle it.** A migration table naming, per duty, the owning component and the decision
that places it there. The review also requires that de-dup windows and re-notification rates be
re-derived from cycle counts into absolute time before any period is shortened (see Q-0002, Q-0003).

### Q-0020 — What does an incompatible CLI capability probe imply for already-running sessions?

**Status.** proposed

**Question.** D-0010 requires that a failed capability/version probe make *new spawns* fail closed.
What happens to sessions already running when the probe starts failing — are they left to finish,
drained, or terminated?

**Why unresolved.** The Issue scopes the fail-closed rule to `new spawn` and is silent on in-flight
sessions. Both readings are defensible: letting them finish preserves work, while draining bounds
the blast radius of an unverified CLI surface.

**What would settle it.** A decision stating the in-flight policy, consistent with D-0013's rule
that runs already started on one side finish on that side, and with the unsaved-artifact protection
required by the Agent View gate (`ACCEPTANCE.md`).

### Q-0021 — What exactly must exist for each Agent View gate item to be checkable "before implementation"?

**Status.** resolved by D-0020 (with D-0021, D-0022, D-0023, D-0024, D-0026)

**Question.** The Issue heads the eleven-item list 「実装開始前の Agent View gate」 — before
implementation starts — and D-0019 records that as decided. But the items are not uniform in what
they presuppose. Items 1–3 are genuinely provable against the Agent View CLI with a thin spike
harness. Items 4–6 presuppose SQLite recovery and a `MessageBus`; item 9 presupposes a Curator
promotion path; item 10 presupposes an operational canary; item 11 presupposes a control-plane test
suite to re-run against a second provider. Read literally, the gate cannot be discharged before the
things it tests exist.

**Why unresolved.** The Issue states the gate and its position without describing the scaffold that
discharges it, and it does not split the list into spike-checkable and implementation-dependent
halves. Resolving this by silently reclassifying items would weaken a gate the Issue placed
deliberately, so it is recorded rather than decided here. Note the ordering the Issue does give:
these founding documents come first, "その後に Agent View spike を行う" — the spike follows, which
is consistent with the gate being discharged *by* the spike rather than before it.

**What would settle it.** A decision defining, per item, the minimum scaffold that counts as
satisfying it — plausibly a deliberately minimal vertical slice (a schema, a lease, an outbox, one
handler) built as part of the Agent View spike specifically so the gate is checkable, with the
distinction between "proven on the spike slice" and "re-proven on the real implementation" made
explicit. `ACCEPTANCE.md` separates the pre-implementation gate from the inherited criteria, which
is the shape such a decision would formalise.

### Q-0022 — May a carried artifact keep enumerating v1 pane vocabulary as a normative list?

**Status.** proposed

**Question.** `tests/scrub/scrub_fixture.py` and `docs/scrub-policy.md` are both classified `carry`
in `PORTING_LEDGER.md`, and both enumerate `pane_id` and `pane_name` among the structural
identifiers that are **never** modified. The redaction rules themselves are transport-neutral and
genuinely carry. But D-0014's Discard bucket names "tmux/pane layout, pane IDs and send-keys as a
backend contract" outright, and the fixtures the scrubber produces (`tests/fixtures/synthetic/`)
are classified `rewrite` for being v1-specific. Does the scrubber's preserved-field allowlist, and
the policy document that makes it normative, have to be re-derived along with the identifiers it
names?

Note what this question is **not** based on. `schema/enums.py` is classified `rewrite` in this
ledger, but not for defining `pane_id` / `pane_name` — those appear only in its module docstring,
which is the very discrepancy that row records. Its enums define `WorkerStatus`,
`JournalEventType` and `AnomalyKind`, including pane *events* (`PANE_CLOSED`, `PANE_SILENT`,
`PANE_CRASHED`), and it is the free-vocabulary model behind those that the closed fact-state set
(D-0005) replaces. The scrubber's allowlist is a different thing — pane *identifiers* — so the
basis for reconsidering it is D-0014, not D-0005.

**Why unresolved.** The distinction is real in both directions and the Issue settles neither. Read
one way, an allowlist of field names is an implementation detail of a tool whose contract — redact
PII and secrets deterministically, preserve identifiers the tests assert on — survives the move
intact, and rewriting it is churn. Read the other way, `docs/scrub-policy.md` is a **policy**: it
states which identifiers are structural, and that statement is v1's answer, made under a scheme
where a pane was an identity. Carrying it unchanged re-authorises the vocabulary as normative in v2
by omission rather than by decision. This audit found the tension but not the evidence to settle it,
so neither row was reclassified.

**What would settle it.** A decision taken when the SQLite fixture shape is fixed (Q-0001), naming
which identifiers are structural in Interlock and whether the scrub policy is re-authored or
amended. Related: Q-0006 (retention and scrubbing policy for evidence references), Q-0016 (which
quarry lessons become decisions).

### Q-0023 — May a carried test drive a `discard`-classed module to reach the contract it pins?

**Status.** resolved by D-0028

**Question.** Several hybrid test rows reach their subject only through a module this ledger
classifies `discard`. `tests/attention/test_broker_journal_contract.py` — carried specifically
because it is an accident-derived fixture that pins a producer↔consumer contract end to end —
imports and instantiates `Broker` from `broker/server.py`. `tests/broker/test_store.py` and
`tests/broker/test_delivery.py` do the same. The invariant under test is Carry-bucket material; the
only available way to exercise it end to end is the discarded module. What does "carried" mean for
such a test before its subject's replacement exists?

**Why unresolved.** The `discard` verdict on `broker/server.py` rests on the claim that the
delivery-relevant logic lives in `store.py`, leaving server.py as pane machinery. That is not quite
exact: server.py overrides `register_delivery_instance` (server.py:817) to release a stale lease
when a liveness probe says the owner's pane died out of band
(`_probe_dead_pane_for_stale_lease`, server.py:845-851). How live that override is differs by test
and the difference is instructive. In `tests/attention/test_broker_journal_contract.py` the override
itself still runs on every registration before delegating to `StoreMixin`, but its pane-probe branch never
does — every scenario builds the broker with `adapter=None` — so what that row carries is transport-neutral
even though the object it drives is not. In `tests/broker/test_delivery.py` it is driven
deliberately: `test_stale_lease_is_released_when_the_pane_died_out_of_band` (test_delivery.py:1378)
builds the broker with a live fake adapter, calls `kill_pane`, and asserts the stale lease is released and the
delivery credential revoked. It is the only test in the file that kills a pane, and the distinction matters:
the neighbouring adopt tests reach a *different* pane-conditioned path — store-side adoption rollback, where a
detached pane's survival decides whether an expired adoption restores the previous instance (the
`broker/store.py` row records that half). So two separate delivery invariants, one server-side and one
store-side, are each conditioned on pane state. The boundary between the carried invariant and the discarded mechanism
therefore runs *through* `broker/server.py` rather than between modules. And not only there: the
`broker/store.py` row in this ledger now records the same shape one layer down, where adoption
rollback is pane-conditioned inside `store.py` itself via the `_detach_owner_panes_locked` /
`_reattach_owner_panes_locked` hooks. So the cut runs through both modules, and a per-file
`carry` / `discard` verdict cannot express it on either.

**What would settle it.** A decision, taken with the `MessageBus` contract, on whether an
end-to-end carried test is landed as a failing specification against the new contract (see Q-0015)
or is permitted to keep driving the old module until the replacement lands — and, either way, an
explicit statement of where the delivery/session boundary inside `broker/server.py` actually falls,
including whether pane-liveness-driven lease release has any transport-neutral successor at all or
is simply discarded with the pane.
Related: Q-0015 (sequencing of carried tests), D-0009 (`SessionProvider` / `MessageBus` separation).
