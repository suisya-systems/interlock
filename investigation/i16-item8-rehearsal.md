# I-16 — item 8 rehearsal: stub Secretary intake under `claude -p` load

**Status:** rehearsed — **this is a rehearsal, not a discharge** (D-0022)
**Date:** 2026-08-21
**Task:** `interlock-21-item8-rehearsal` (issue #21, phase 9, scaffold S4)
**Scope:** the rehearsal of gate item 8 on the spike slice. Every result below is labelled
**proven on the spike slice**. The discharge point is unchanged: the same absence of blocking
shown against the **real** Secretary under **genuine** worker load, **before the canary starts**
(D-0013), against a latency threshold settled by `Q-0011`. No numeric pass criterion is stated
here and none of the numbers below is one.
**Refs:** issue #21; `ACCEPTANCE.md` §1 item 8; `DECISIONS.md` D-0016, D-0022, `Q-0011`;
`docs/secretary-intake-boundary.md` (the boundary contract); `tests/secretary/` (the durable
structural and behavioural assertions); `investigation/i01-supervisor-probe.md` §3.7/§5.3 (the
provider-side input and the inconclusive blocking control this note re-measures).
**Harness:** `investigation/i16_item8_rehearsal.py`, reusing `investigation/i01_supervisor_probe.py`
(spawn and non-blocking line framing). `src/claude_org_runtime/session/` is neither touched nor
imported — the load generator is this harness's own supervision of `-p` children, per the design
review's boundary with #17.

---

## 1. Verdict — proven on the spike slice

The three things issue #21 asked for are measured, and all three came out the structural way:

1. **Baseline vs load: the intake's request→response latency does not move.** Idle median
   0.00114 ms / p95 0.0172 ms; with 9 live `claude -p` children (8 workers at the spike-slice cap
   plus one long-running task), an open incident parked awaiting a stub AI judgement, and a 100 ms
   non-blocking supervision sweep running, median 0.00127 ms / p95 0.0195 ms. Same order,
   externally bracketed around the whole `submit()` call.

2. **The U6 C2 fold-in is re-measured against live children, and the hazard is real —
   in the supervisor.** #6's blocking-`readline()` control was inconclusive (every child had
   finished). Here, against a child mid-turn, one blocking read cost **13,006.7 ms** (a second
   live child: 1,308.2 ms); against nine live children in their startup-event phase, the first
   blocking read cost 664 ms. The non-blocking whole-sweep over the same nine live children:
   median **0.064–0.071 ms**. A supervisor that blocks per child serialises on the slowest
   child's next event — seconds per child, exactly as #6 §3.7 bounded from measured data.

3. **The stall does not propagate: while the supervisor thread was blocked for 14.3 s, the
   intake kept answering** — 5,720 requests, median 0.00109 ms, max 0.397 ms. That is the
   structural claim (no call, lock, or import edge from intake to monitoring/judgement) showing up
   empirically, which is all a number can do for item 8 while `Q-0011` is unresolved.

**Spend, as the issue requires:** USD **2.82** total across 47 spawns, itemised in §2 — including
the three early runs review corrections superseded (§3 provenance). `--setting-sources project`
cut per-spawn cost ~5.6× (0.45 → 0.08 USD for the identical trivial prompt) by not loading the
invoking user's configuration — the load profile decision #6 §5.4 anticipated.

---

## 2. Environment, load profile, and what was spent

```
$ claude --version
2.1.237 (Claude Code)
```

Note the build moved since #6 (2.1.234); nothing here re-verifies #6's provider facts — this note
measures Interlock's own code under load, not the provider's admission/refusal surfaces.

Working directory for every spawn: a scratch directory outside the repository, not a git
repository. The harness ran with this worker's sandbox disabled (children need network for model
turns); nothing outside the scratch directory and the repo worktree was written.

**Load profile (planned, per the issue's cost note).** C2 has no free surface, so the profile was
priced before it was run:

| Element | Choice | Why |
|---|---|---|
| Worker count | **8** at once, plus 1 long-running task | The spike-slice cap: matches #6 §3.7's sweep maximum, ~6× the measured-baseline mean concurrency (1.38). Q-0011's real load profile is unresolved; this is the rehearsal's stated load, not a decided cap. |
| Worker prompt | "Count from 1 to 40, one short sentence each" | Long enough that children are mid-turn for the whole window (#6: first assistant event ≈ 19 s with inherited config). |
| Long-running task | Same shape, 1 to 120 | Still in flight after the workers finish. |
| Child flags | `--output-format stream-json --verbose --setting-sources project` | Machine-readable state (#6 §3.2); the setting-sources pin is the ~5.6× cost lever measured in smoke, and pins the spawn's inherited surface for reproducibility. |
| Duration | one ~40 s window per mid-turn run, one ~25 s window per startup run | Two placements of the blocking control: mid-turn silence vs startup-event phase. |

**Spend record (from each child's own `result` row, summed):**

| Step | Spawns | USD |
|---|---|---|
| smoke: inherited config | 1 | 0.450 |
| smoke: `--setting-sources project` | 1 | 0.081 |
| run 1 (8 workers + long task) — superseded, see §3 provenance | 9 | 0.740 |
| run 2 (8 workers + long task) — superseded, see §3 provenance | 9 | 0.366 |
| run 3 (8 workers + long task) — superseded, see §3 provenance | 9 | 0.390 |
| run 4 (8 workers + long task, startup control) — blocking rows and sweep quoted; its intake-latency samples are superseded | 9 | 0.399 |
| run 5 (8 workers + long task, mid-turn control) | 9 | 0.398 |
| **total** | **47** | **2.82** |

Every child in every run exited rc 0, `is_error: false`, `terminal_reason: "completed"` — the load
was real model turns run to completion, not killed mid-flight (a SIGTERM'd child reports
`total_cost_usd: 0`, #6 §3.3, so completion is also what makes the spend recordable).

---

## 3. Records

Verbatim excerpts from `records.jsonl` (harness output, one JSON object per step). Full records
are in each run's `$I16_OUT` directory; they are throwaway with the harness (D-0026).

**Provenance — the harness and stub were corrected across review rounds, and the evidence was
re-run each time.** Runs 1 and 2 were recorded first; a codex review pass then found three
defects: the stub's queue used locks, whose implicit `acquire()` contradicts the very
non-blocking contract under test (the stub is now lock-free and the structural tests assert
lock-freedom outright); the baseline was sampled while a polling consumer contended for the
queue, tilting the comparison against idle (the baseline now runs before the consumer thread
exists); and child stderr was never drained (now drained, untimed, in the sweep). Runs 3 and 4
repeated both placements against that code. A further round added precondition guards (abort when
the judgement stall or the full-cap liveness is not established; invalid-marking on timeout;
stderr kept draining during the blocking control) and then found the latency sampler itself
undercounted: it used the receipt's own timestamps, which exclude receipt construction and
return — at sub-microsecond scale a material share of the path (the reviewer measured the gap at
~0.6 µs). The sampler now brackets the whole `submit()` call externally.

**Run 5 repeats the mid-turn placement against the final code and is the evidence quoted below
for intake latency and the mid-turn blocking control. Run 4 is quoted only for the
startup-placement blocking control and the all-live sweep** — both timed independently of the
sampler and unaffected by the bracketing fix; its intake-latency samples are superseded along
with runs 1–3. The superseded runs are retained in the spend table, and their headline shapes
agree throughout (mid-turn blocking read 12.97 s / 11.55 s / 13.01 s across runs 1, 3, 5; intake
medians ~0.5 µs in-receipt, ~1.1 µs externally bracketed — the difference being exactly the
measurement correction). The quoted runs satisfied every precondition guard, as their records
show (`children_live_at_start=9`, `children_live_at_end=9`, `load_precondition_met=true` in run
5, `judgement_in_flight=true`, `n_stderr_lines: 0` for all children).

### 3.1 The scenario

One process hosts: the stub `SecretaryIntake` in front of its bounded `IntakeQueue`
(`src/claude_org_runtime/secretary/`); a consumer thread that pulls the open **incident** off the
queue and parks inside a stub **AI judgement** (an Event held for the whole window); a supervisor
thread sweeping all children non-blockingly every 100 ms (**worker monitoring**); 8 worker
children plus one **long-running task**, live `claude -p` processes. Intake latency is sampled
from the main thread in bursts. The three load elements are thus the three dependencies item 8
names — monitoring real, the long task real, the judgement a stub (the Dispatcher AI itself is
out of the rehearsal's scope). The baseline is sampled before the consumer thread starts, so the
idle phase carries no contention the loaded phase lacks.

### 3.2 Baseline vs load (run 5)

```
baseline_idle                     : n=400  median 0.001141 ms  p95 0.017162 ms  max 0.047263 ms
under_load_nonblocking_supervisor : n=400  median 0.001270 ms  p95 0.019527 ms  max 0.055481 ms
                                    children_live_at_start=9  children_live_at_end=9
                                    load_precondition_met=true  judgement_in_flight=true
```

The medians differ by ~0.1 µs between idle and load — within noise. No number here is a pass
criterion (`Q-0011`); what the comparison records is that **load did not move the distribution**,
with the worker count stated.

### 3.3 The U6 C2 fold-in: blocking control against live children

#6's control ran after its children had exited and was flagged **explicitly inconclusive**; the
review made re-measuring it against live children a criterion of this task. Two placements:

**Run 5 — control at t≈+8 s, children mid-turn (silent window between `init` and the assistant
event).** The supervisor thread switches to a naive serial blocking read; liveness recorded per
sample:

```
worker-0   13006.747 ms   child_was_live=true    <-- one blocking read, one live child
worker-3    1308.193 ms   child_was_live=true
worker-7, long-task: ~0.01 ms, child_was_live=true (a line already sat in the pipe)
worker-1..2, 4..6: ~0.01 ms, child_was_live=false  (finished; EOF/result already readable
                                                    -- the #6 inconclusive shape, marked as such)
blocking_control_total_s = 14.336
```

**Run 4 — control at t≈+2.5 s, all nine children live (startup-event phase):**

```
worker-0     664.475 ms   child_was_live=true
worker-1..7, long-task: 0.003-0.015 ms, child_was_live=true (a line already sat in the pipe)
blocking_control_total_s = 0.702
```

**The non-blocking readout over the same load, for the fold-in's comparison** (sweeps where all 9
children were live):

```
run 5: whole-sweep median 0.0705 ms  p95 0.1049 ms  (40 all-live sweeps; per-child median 0.0034 ms)
run 4: whole-sweep median 0.0642 ms  p95 0.0902 ms  (133 all-live sweeps)
```

Reading: obtaining state from N children is **not** inherently blocking inside Interlock — the
non-blocking sweep at the cap costs ~0.07 ms, flat, confirming #6 §3.7 on live children. The same
readout written blockingly costs whatever the slowest child's next event costs — **13.01 s** on
one mid-turn child here — a ~10⁵× difference in the same process on the same load. The hazard is
real, lives in supervisor code, and is exactly what the structural tests forbid the intake path
from reaching.

### 3.4 The stall does not propagate

While the run-5 supervisor thread was inside the 14.3 s blocking pass, the main thread kept
sampling the intake:

```
latency_while_supervisor_blocked : n=5720  median 0.001085 ms  p95 0.015699 ms  max 0.397032 ms
```

Same distribution as idle. The queue boundary absorbed every request (`refusals: 0`). The stub
consumer takes nothing further once parked inside its judgement, so the 6,120 requests submitted
after it parked were still queued at teardown (`queue_depth_left`) and were discarded
with the process — in-memory by design; durable intake is the real Secretary's concern (§5).

Two mechanics notes on the sample sets: the sampling loop brackets the blocking pass by up to one
100 ms monitor cycle on each side, which at n=5,720 over 14.3 s is noise; and the burst sampler
runs on the main thread and brackets the whole `submit()` call, so the samples measure the
response path itself, not scheduler fairness under contention.

### 3.5 Children, verbatim summary

```
run 5: 9/9 rc=0, is_error=false, terminal_reason=completed, total_cost_usd_sum=0.3981
run 4: 9/9 rc=0, is_error=false, terminal_reason=completed, total_cost_usd_sum=0.3985
(runs 1-3, superseded: 9/9 rc=0 each, sums 0.7399, 0.3662, 0.3904)
```

Every child's stderr recorded zero lines in runs 3-5 (`n_stderr_lines: 0` across all 27).

---

## 4. Cleanup

- Every child in all five runs completed and was reaped by the harness (`wait` after the drain
  window; the kill-pgid path existed but was not needed). No process of this experiment survived
  it; smoke and the five runs left 47 `.jsonl` transcripts under the scratch project slug, as
  `-p` runs do.
- The scratch directory holds the five `records.jsonl` files; nothing was written into the
  worktree except the harness, this note, the stub package, its tests, and the two document
  updates (`docs/secretary-intake-boundary.md`, `docs/gate-record.md`).

---

## 5. What this rehearses — and what it does not discharge

**Rehearsed, proven on the spike slice:**

- **Structural**: intake and the queue boundary are asynchronous, asserted in code —
  `tests/secretary/test_structural.py` holds the intake package to a stdlib import allowlist (no
  edge to `session/` or `dispatcher/`, with level ≥ 2 relative imports counted as offenders),
  bans blocking primitives from its syntax tree, and holds the package lock-free outright (no
  `with`-block, no lock constructor, no `threading` import — a lock is an implicit wait);
  `tests/secretary/test_behaviour.py` stalls each of the three named dependencies verifiably
  while the intake answers. These tests are the durable half (D-0026).
- **Empirical**: §3.2's baseline-vs-load comparison, worker count stated.
- **U6 fold-in**: §3.3's measured readout latency at the cap, blocking and non-blocking, against
  live children — closing the control #6 left inconclusive.

**Not discharged, by construction (D-0022):**

- The real Secretary does not exist yet; this stub has no durable intake, no roles, no real
  Dispatcher AI judgement behind it. Nothing here claims otherwise.
- The discharge is due **before the canary starts**: the same absence of blocking, shown against
  the real Secretary under genuine worker load, judged against the threshold `Q-0011` settles.
  If that point is reached without the predicate met, it is a **gate failure recorded as such**.
- No number in this note is a threshold, and the spike-slice cap of 8 is this rehearsal's stated
  load, not a decided worker cap.

**Residuals for the discharge to pick up:**

- The blocking control's "finished child" rows in run 5 are the #6 inconclusive shape and are
  marked as such; the live rows carry the finding. A discharge-time measurement should hold load
  longer than the sampling window so every control row is live (run 4 achieves it by placing the
  control earlier).
- The stub judgement is an Event, not a Dispatcher AI turn. The discharge must park a real
  judgement (D-0003's on-demand turn) behind the boundary.
- The lock-free queue's capacity bound is advisory-exact (overshoot ≤ P−1 under P concurrent
  producers, documented in the code). Whether the real Secretary keeps a lock-free boundary or
  buys an exact bound another way is a design decision at the discharge, not here.
- One-machine, one-load, WSL2 figures throughout, as with #6.
