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
| Q-0001 | SQLite schema/DDL and migration policy for the SoT tables | proposed |
| Q-0002 | Incident dedup key composition and re-notification rate in absolute time | proposed |
| Q-0003 | Reconcile interval and the tolerable detection latency that justifies it | proposed |
| Q-0004 | Which concrete alternative `SessionProvider` if the Agent View gate fails | proposed |
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
| Q-0019 | Who owns each of the retired loop's non-detection duties | proposed |
| Q-0020 | What an incompatible CLI capability probe implies for already-running sessions | proposed |

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
  (unresolved — see Q-0001), and retention policy for evidence references (unresolved — see Q-0006).

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

**Status.** accepted

**Source.** https://github.com/suisya-systems/claude-org-ja/issues/740#issuecomment-5311674345

---

## Open questions

These are gaps where implementation needs an answer and Issue #740 provides no basis. They are
**not decisions**. A `Q-` entry is resolved by adding a new `D-` decision (with the next free `D-`
number) and marking the question resolved by that ID; `Q-` IDs are stable and are never reused.

### Q-0001 — What is the concrete SQLite schema/DDL and migration policy for the SoT tables?

**Status.** proposed

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

**Status.** proposed

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

**Status.** proposed

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

**What would settle it.** The P0 contract: for each state, its meaning, the predicate over observed
evidence that yields it, its precedence when several predicates match, and fixtures pinning each.
This pairs with Q-0009 (detector version), since a state is only deterministically replayable if
"which detector, at which version, produced it" is unambiguous.

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

**Status.** proposed

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
