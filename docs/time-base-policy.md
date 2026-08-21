# The time base — detection latency budgets, tolerances, the reconcile interval, and who owns what

**Scope.** Resolves `Q-0003` (reconcile interval and the tolerable detection latency that justifies
it) and `Q-0019` (who owns each of the retired loop's non-detection duties). Every number that
[`production-schema.md`](./production-schema.md) refers to as a tolerance or an interval is decided
here, and every one of them is **policy data**, not a constant in code.

**Status: design, not implementation.** Decisions filed from this document: `D-0031` (the time base)
and `D-0032` (duty owners).

**Why these two together.** `Q-0003` asks what latency the organisation accepts; `Q-0019` asks who
is obliged to act inside it. A budget with no owner is not enforceable and an owner with no budget
has nothing to be late against, which is why the design review put them in the same step.

---

## 1. Why the numbers are data

`D-0002` retires a `/loop 3m` whose period was itself the answer to every timing question, and
`Q-0003` records the 2026-07-20 review's demand: the *requirement basis* for any period — the
tolerable detection latency — must be settled before the period is. The failure mode being avoided
is a constant that acquires authority by being in the code, which is `D-0026`'s shape applied to
numbers instead of schemas.

So every value below is a row in `policy_detection_latency` / `policy_gate_stage_tolerance` /
`policy_gate_stage_owner` (DDL in [`production-schema.md`](./production-schema.md) §10), inserted by
the migration step that accompanies the implementation Issue, carrying the `D-` entry that decided
it in `policy_revision.decided_by`. Changing a tolerance is a new `policy_revision`, never an
`UPDATE`, so a past report can always be recomputed under the tolerances it was actually judged by.

**These values are decisions, not measurements.** Following `DECISIONS.md`'s note on numbers: the
only measured figures available are the 2026-07-18…2026-07-25 dogfood baseline (median dispatcher
iteration interval 180.0 s; 195 completed runs, mean 1.09 h, median 0.66 h, p90 2.55 h). The budgets
below are derived from that baseline plus the non-regression constraint AC-10 imposes; they are
initial values chosen to be defensible and revisable, and the mechanism for revising them is a new
policy revision rather than an argument.

---

## 2. The clock rules

Three clocks appear in the system and conflating any two of them makes a tolerance unevaluable.

| Clock | Column | Whose | Used for |
|---|---|---|---|
| Source | `occurred_at_ms` | The provider's, or the human-facing actor's | Reporting *end-to-end* latency; ordering CI observations within one provider's own stream |
| Control plane | `ingested_at_ms`, `recorded_at_ms` | Ours | **Every tolerance and every aging predicate** |
| Wall clock at evaluation | `:now_ms`, supplied by the caller | Ours | The right-hand side of every aging comparison |

**Rule 1 — tolerances are evaluated against our clock only.** An aging predicate that used
`occurred_at_ms` would make a provider's clock skew look like a relay gap, and skew is not something
we can bound. `ACCEPTANCE.md` §2 already injects clock skew across the lease expiry boundary; the
same injection applies to every predicate here.

**Rule 2 — the evaluation clock is the caller's, never the database's.** Carried from the spike
schema's convention: no timestamp column has a `DEFAULT`, and `:now_ms` is a parameter. A predicate
that read the database's clock could not be driven backwards or forwards by a test.

**Rule 3 — end-to-end latency is reported with both clocks and the difference is kept.** For AC-10
the interesting quantity is onset-to-detection, and onset is a source-clock event. The measurement
harness therefore reports `ingested_at_ms - occurred_at_ms` as its own series (the *ingestion lag*)
alongside the latency, so that a latency regression caused by a slow provider is distinguishable
from one caused by us. See [`measurement-harness.md`](./measurement-harness.md) §4.

**Rule 4 — intervals are half-open.** Every window is `[start, end)`. A row exactly on a boundary
belongs to the later window, and belongs to exactly one. This matters most at report boundaries,
where a closed interval double-counts.

---

## 3. Detection latency budgets and the tolerances derived from them

### 3.1 The derivation rule

For each incident class, three quantities:

- **`T` — tolerance.** How long the condition may legitimately persist before it is abnormal. This
  is a statement about the work, not about our machinery: a relay that has not happened in ten
  seconds is not late.
- **`P` — reconcile period.** How often the deterministic pass evaluates the predicate.
- **`L` — budget.** The ceiling on **onset-to-alarm**.

> **Onset is the moment the condition begins — the state entry — not the moment it crosses `T`.**

There is exactly one definition of onset and this is it. It is used by the budget arithmetic below,
by the fixture harness's `onset_offset_ms`
([`measurement-harness.md`](./measurement-harness.md) §3.2), and by every latency figure in a report.
Defining it as the `T` crossing instead would double-count the tolerance: a `received` gate with
`T = 3 min` inside an `L = 5 min` budget could then be alarmed at minute 8 and still be judged
inside its ceiling. `T` is the part of `L` the work is *entitled* to; it is not a head start on it.

The pass can only notice a crossing at its next run, so the worst case is that the condition crosses
`T` immediately after a pass and waits a full period:

> **`T + P ≤ L`.**

That inequality is the whole derivation, and it is why the reconcile interval is a *consequence*
rather than a choice. Note what it is not: it is not a claim that detection is usually that slow —
most conditions are detected by the event path, not by the reconcile pass, and the pass is a
backstop. `L` is the guarantee that holds when the event path is the thing that failed.

### 3.2 The classes

`T`'s **kind** is part of the policy row, because three of these are not absolute durations:
`watcher_silence` scales with the scope's own poll interval, `lease_orphan` with the lease's own
TTL, and `watcher_error_streak` is a count with no duration in it at all. The
`policy_detection_latency.threshold_kind` column carries which — see
[`production-schema.md`](./production-schema.md) §10. Precomputing a relative threshold into
milliseconds would bake one scope's interval into a row every other scope also reads.

| Incident class | What crossed | `T` (kind) | `L` | Basis |
|---|---|---|---|---|
| `relay_gap` (gate stage) | A gate sat in a relay stage past its stage tolerance | per stage, §4 (`absolute_ms`) | **5 min** | v1's relay-gap detector ran on a 3-minute loop; 5 minutes is a non-regression with headroom for one missed pass |
| `relay_delivery_stall` | A gate relay was enqueued and not acked | 2 min (`absolute_ms`) | **5 min** | Same budget; the stall is a delivery problem, detected on the same pass |
| `ci_outcome_undrained` | A CI outcome is on the spine and its consumer has not drained it | 3 min (`absolute_ms`) | **5 min** | The canary's run completes via a PR (`#64`); a completion the organisation learns about later than v1 did is the AC-10 regression |
| `consumer_backlog` | Any consumer's head-of-line age exceeded tolerance | 5 min (`absolute_ms`) | **10 min** | Generalisation of the above for non-CI consumers; a backlog is a slower-moving fact than a single missed relay |
| `watcher_silence` | A watcher stopped attempting for a scope | 3 (`scope_interval_multiple`) | **10 min** | `tools/relay_scan.py` found its equivalent failure after **20 days**; any bounded number is the improvement, and three missed polls distinguishes a stopped watcher from a slow one |
| `watcher_error_streak` | A watcher is attempting and only failing | 5 (`consecutive_count`) | **10 min** | A separate class from silence because the remedy differs (a broken credential, not a dead process) |
| `watcher_scope_uncovered` | An enabled scope has no liveness row at all | 0 (`absolute_ms`) | **10 min** | There is nothing to wait for: an unwatched scope is wrong the moment it exists |
| `session_no_evidence` | A session produced no activity evidence past tolerance | 10 min (`absolute_ms`) | **15 min** | `NO_ACTIVITY_EVIDENCE` is **not an anomaly** (`D-0005`, `D-0006`); this class raises an incident for *assessment*, never a verdict, and the tolerance is deliberately generous because the p90 run is 2.55 h and quiet stretches are ordinary |
| `observation_unavailable` | The observation path itself failed | 5 min (`absolute_ms`) | **10 min** | `D-0006`: an observation outage must not be able to masquerade as a fleet-wide worker failure, so it is its own class with its own alarm |
| `lease_orphan` | A lease expired with work still attributed to its holder | 1 (`lease_ttl_multiple`) | **2 × lease TTL** | Expressed in TTLs because the TTL is the thing that defines staleness here |

### 3.3 The reconcile interval

The binding constraint is the smallest `L - T` across the classes the pass serves:

| Class | `L - T` |
|---|---|
| `relay_gap` (`received` stage, `T` = 3 min) | 2 min |
| `relay_delivery_stall` | 3 min |
| `ci_outcome_undrained` | 2 min |
| `watcher_scope_uncovered` | 10 min |
| everything else | ≥ 5 min |

For the three relative classes, `L - T` is **not a constant** — it depends on the subject's own
interval or TTL — so `T + P ≤ L` is asserted per subject at reconcile time rather than read off this
table. A subject that violates it is a misconfiguration and raises `policy_budget_violation`
([`production-schema.md`](./production-schema.md) §10). With the 10-minute `watcher_silence` budget
and `P = 120 s`, the constraint bounds a scope's `expected_interval_ms` at
`(600 000 − 120 000) / 3 ≈ 160 s`; a scope registered slower than that is reported rather than
silently under-served.

> **The reconcile period `P` is 120 seconds.**

Two consequences to be explicit about.

**The Issue's "10 minutes" is not this number, and was never a decision.** `Q-0003` records that it
appears inside a projection of program tick counts — an assumption used to produce an estimate. A
10-minute pass would blow the `relay_gap` and `ci_outcome_undrained` budgets outright. The estimate
it fed (roughly 470–1,576 program ticks per 100 runs) is affected: at 120 s the reconcile pass alone
contributes about 5× what a 10-minute pass would. That is accepted, and the reason it is acceptable
is `D-0002`'s own scope note, restated in `ACCEPTANCE.md` §5: **program-tick reduction is explicitly
not the objective.** The objectives are AI workload, determinism, reproducibility and
recoverability, and a program pass takes no model turn (AC-1). Trading program ticks for detection
latency is trading the thing that is not the goal for the thing AC-10 measures.

**A class may run on a coarser period.** The pass is one loop, but a predicate whose `L - T` is
large does not need evaluating every 120 s. `policy_detection_latency` therefore carries
`reconcile_period_ms` **explicitly, per class** — a multiple of the base period, defaulting to it —
and the `T + P ≤ L` constraint is checked against that column rather than against a global constant.
Making it a column rather than an implicit `floor((L - T) / P)` is what lets the invariant be a
`CHECK` instead of a convention; the implementation may still set every row to 120 000 and add
coarser values only when the pass cost justifies it.

### 3.4 Boundary conditions

- **Onset is the state entry** (§3.1), so a gate entering `received` is an onset immediately and its
  clock is running from that moment. The distinction between "detected late" and "was legitimately
  slow" is still available, and it is `T`: a report reads the alarm's age against both `T` and `L`,
  so a detection at `T + ε` is prompt and a detection past `L` is a regression, without either
  number having to move.
- **A condition that resolves before the next pass is never detected, and that is correct.** It is
  not a miss. AC-10's miss counter (see the measurement document) counts only conditions that
  persisted past their budget, because a self-resolving condition has no incident to raise.
- **There is no "human present / absent" modifier.** `Q-0003` allows for one and this document
  declines it: the presence of a human is not observable to the deterministic layer, and a tolerance
  that varied on an unobservable input could not be evaluated deterministically. The place where
  human availability legitimately enters is the gate's `deadline_at_ms`, which a human or a policy
  sets explicitly per gate — an input, not an inference.
- **Suspension.** A run that is deliberately paused suspends its session-class predicates by moving
  to a status that the predicates exclude, not by adjusting their tolerances. Excluding by status is
  auditable; a suppressed tolerance is not.

---

## 4. Gate stage tolerances

`#65`'s core claim is that a single open-past-deadline predicate would both false-alarm on slow
humans and miss a dropped forward, and that a staged form does neither. The values that make that
true, for `gate_type='worker_escalation'`:

| Stage | Ball holder | `T` | Is a gap? | Reasoning |
|---|---|---|---|---|
| `received` | `secretary` | **3 min** | **Yes** | The Secretary has the request durably and has not put it in front of the human. Nothing here waits on anyone outside the machine, so minutes is generous |
| `presented` | `human` | **`NULL`** | **No** | A slow human is not a gap. `#65` says this directly. The governing limit is the gate's own `deadline_at_ms`, whose breach is `outcome='expired'`, not a `relay_gap` incident |
| `answered` | `secretary` | **2 min** | **Yes** | The answer is durable and the worker does not have it. This is the leg v1 actually dropped, and it is the tightest tolerance because the work is blocked on it |
| `forwarded` | — | — | — | Terminal; the gate is closed |

For `gate_type='merge_approval'` the same shape with a different standing owner; the `presented`
stage is again untimed, and the `received` and `answered` legs are the same 3 min / 2 min.

**`NULL` is how "never a gap" is expressed**, so the detector query (`production-schema.md` §9.6)
has no special case for the human stage — it joins the policy table and the row simply does not
match. A special case in the query would be a place where a future gate type could be given a human
tolerance by accident.

**The delivery stall is a separate predicate at 2 minutes**, over `gate_relay` joined to `outbox`,
because a relay that was enqueued and never acked is stalled in the delivery layer while the gate's
stage is legitimately unchanged. Ageing the stage would report the wrong thing about the wrong
component; `D-0037`'s ack-gated advance is what makes the two states distinguishable at all.

---

## 5. What AC-10's non-regression compares

AC-10 forbids regressing detection latency, false termination and misses. Against these budgets it
becomes a checkable statement, and the comparison is stated here so the measurement harness is not
inventing it:

- **Latency** — the distribution of onset-to-incident per class, compared against the v1 shadow
  and against the budget `L`. The budget is the acceptance bound; the shadow is the non-regression
  bound. Both are reported; neither is silently substituted for the other.
- **Misses** — a labelled condition that persisted past `L` with no incident. Counted against the
  fixture labels and the shadow, never against Interlock's own rows alone, for the reason
  [`measurement-harness.md`](./measurement-harness.md) §3 gives.
- **False termination** — counted at the applied `action`, per `D-0039`.

---

## 6. `Q-0019` — the retired loop's non-detection duties

`Q-0019` asks for a migration table naming, per duty, the owning component and the decision that
places it there. The 2026-07-20 review enumerates five duties.

| Retired duty (v1) | Owner in Interlock | Mechanism | Basis |
|---|---|---|---|
| **Pull-fallback drain** — the DELEGATE receive path when the sidecar is unhealthy | **Dispatcher Core**, reconcile pass | There is no separate pull path: delivery is the outbox, fed transactionally from the event append, and the pass re-attempts `pending`/`failed` consumption rows | `D-0002` (program-side reconcile loop is kept), `D-0030` (§5.4 of the schema document removes the second delivery path by construction) |
| **Curate-inflight management** | **Curator** for the work; **Dispatcher Core** for the aging | The Curator runs on demand and produces proposals; a proposal awaiting human approval is a `Gate`, and Core ages it like any other | `D-0018` (Curator is on-demand and not resident), `D-0008` (deadline evaluation is the deterministic layer's) |
| **CI relay** | **Dispatcher Core** | The CI watcher appends to the spine; fan-out to consumers happens in the append transaction; the pass backstops undrained rows. No relay scanner exists as a separate process | `D-0030`, `#64` (single event spine) |
| **`pending_decisions` aging** | **Dispatcher Core** detects; **Secretary** acts | Core evaluates the stage tolerances of §4 and raises `relay_gap`; the Secretary owns the `received → presented` and `answered → forwarded` legs and is the ball holder the incident names | `D-0008` (dedup, deadline evaluation, incident creation are Core's), `D-0016` (Secretary is the single human window), `D-0032` |
| **Auto-stop** | **Nobody automatically.** Core raises the incident; the Secretary, a human, or a privileged handler executes | Core may name a stall *candidate* and may not conclude one; the Dispatcher AI may recommend and may not execute; the applied `action` row is written by the third layer | `D-0008` (Core does not pronounce a verdict on an ambiguous stall), `D-0004` / AC-6 (no direct terminate/restart from the AI), `D-0016` |

The last row is the one that changes behaviour rather than relocating it, so it is worth saying
plainly: **v1's loop could stop a worker; Interlock's reconcile pass cannot.** That is not an
oversight to be fixed later — it is `D-0004` and the `CHARTER.md` §4 boundary table working as
designed, and it is why AC-10's false-termination counter must be defined at the applied action
rather than at the recommendation (`D-0039`). A design that quietly gave the pass an auto-stop would
be reintroducing the layer the fork exists to remove.

### 6.1 Gate ownership, resolved

The review's remaining ownership question — whether a gate's `owner` is the standing responsible
party or the current ball holder — is answered by having neither on the gate row and both in policy
data (`policy_gate_stage_owner`):

- **`ball_holder`** is a function of `(gate_type, stage)`. It is who a `relay_gap` incident names,
  and it changes as the gate advances.
- **`standing_owner`** is a function of `gate_type`. It is who answers for the class of decision
  overall, and it does not change as the gate advances.

Neither is stored on `gate`, so neither can drift from the stage. Reporting joins the effective
policy revision, which means a report can also say what the ownership *was* at the time, not only
what it is now.
