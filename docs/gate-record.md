# Gate record — the eleven Agent View gate items

**What this is.** The single place where each of `ACCEPTANCE.md` §1's eleven gate items carries a
verdict, the evidence behind it, the provider that evidence was obtained against, and the D-0022
label — *"proven on the spike slice"* or *"re-proven on the real implementation"*. D-0022 requires
that labelling; without one document holding it, the scoped exception D-0022 grants (two items
deferred, **not waived**) degrades into an unaccountable claim that the gate passed.

This file is bookkeeping. It is the artifact the operator reads when deciding whether implementation
may start, and it is **due whether the gate succeeds or fails** — a gate record that only exists on
success is not a gate record. Issue `#24` (I-19) opens it; the issues listed per item fill their own
rows as evidence lands; it closes when the sequence ends, by discharge or by termination.

**Refs.** `ACCEPTANCE.md` §1 (the items and their verification methods), §2 (fault injection), §3
(canary), §4 (what a new provider forces to be re-run); `DECISIONS.md` D-0019 (gate is a
precondition), D-0020 (Strategy B+), D-0022 (scoped exception, and the labelling this file
implements), D-0023 (item 3's observable), D-0024 (item 2 by experiment), D-0025 (C2 designated),
D-0026 (durable vs throwaway), D-0027 (item 2 fails on C1; C2 adopted);
`docs/plans/spike-issue-decomposition.md` §4 (survival matrix) and §6 (the issue set).

---

## 1. Provider history — read this before the table

The gate was designed against **C1 — Agent View**. On **2026-08-18** gate item 2 **failed on C1**:
the pre-spawn identity experiment came up negative (U1,
`investigation/u1-session-id-bg-experiment.md`) and the fence search D-0024's tail requires came up
**empty** (`investigation/pre-spawn-fence-search.md`). Per D-0024 that opened the `Q-0004` path, and
`Q-0004`'s answer was already on the record: D-0025's designated second spike, **C2 —
Interlock-supervised `claude -p` subprocesses**. D-0027 records the verdict and the adoption.

Three things follow, and every row below is read in their light:

1. **The C1 failure is history, not a dead end to be omitted.** Item 2 has already failed once. A
   record showing only the C2 outcome would present a second attempt as a first one.
2. **It was a failure of the *provider*, not a reclassification of the item** (F6). Item 2's
   predicate is unchanged; softening it would be a reclassification wearing the clothes of a
   mitigation.
3. **The gate is provider-scoped.** Per `ACCEPTANCE.md` §4, items **1, 2, 3, 7, 8 and 10** must be
   proven **in full** against whatever provider ships. Evidence obtained against C1 does not carry
   over to C2, and evidence obtained against C2 does not carry over to a third provider. Every row
   therefore names its provider. Items 4, 5, 6, 9 and 11 are control-plane properties: they are
   re-run for regression, not redesigned.

**Where the sequence stands.** Item 9 is discharged (independently of any provider). **Item 3 is
discharged on C2**, on the weakened observable D-0023 defines — and its residual is stated in §3 in
D-0023's own terms rather than folded into the verdict. Item 2 carries a **failed** verdict on C1
and is **pending** on C2. Every other item is **pending** — the spike is under way on C2 and its
evidence has not landed yet.

---

## 2. The record

Vocabularies are closed. Verdict is one of **`discharged`**, **`failed`**, **`rehearsed — not
discharged`**, **`pending`**. Label is one of **`proven on the spike slice`**, **`re-proven on the
real implementation`**, **`n/a — failed`**, **`pending`**. Provider is one of **`C1 (Agent View)`**,
**`C2 (claude -p subprocesses)`**, **`provider-independent`**, **`pending`**.

| # | Item (short) | Verdict | D-0022 label | Provider | Evidence | Discharge point |
|---|---|---|---|---|---|---|
| 1 | Public CLI alone can start / read state / stop / resume | `pending` | `pending` | `C2 (claude -p subprocesses)` | `#6`, `#7` — not yet landed | The spike (phases 1a/1b) |
| 2 | Unique session↔run re-match across the crash window; no duplicate writer | `failed` on C1; `pending` on C2 | `n/a — failed` (C1); `pending` (C2) | `C1 (Agent View)` → `C2 (claude -p subprocesses)` | D-0027; `investigation/u1-session-id-bg-experiment.md`; `investigation/pre-spawn-fence-search.md`. C2 re-proof: `#18` (provider `#17`) — not yet landed | The spike (phase 6), on C2 |
| 3 | Per-role permission / sandbox / hooks survive restart and fail closed | `discharged` | `proven on the spike slice` | `C2 (claude -p subprocesses)` | `#9`; `docs/per-role-fencing.md`; `src/claude_org_runtime/fencing/`; `tests/fencing/`; `investigation/i04-pretooluse-fence-probe.md` (U15, U35, U42). `#8` closed as **moot** under C2, not passed | The spike (phase 2b) |
| 4 | Supervisor / Dispatcher Core / Secretary resume from SQLite, no double execution | `pending` | `pending` | `pending` | `#12` (S5, the store the resume reads from) and `#13` (S6, the lease and fencing token) landed 2026-08-19; `#14`, `#16` — not yet landed | The spike (phases 4–5) |
| 5 | Lease, outbox resend, ack, dedup, single-writer under fault injection | `pending` | `pending` | `pending` | `#12` (S5) and `#13` (S6, the lease and fencing token) landed 2026-08-19; `#14`, `#15`, `#16` — not yet landed | The spike (phases 4–5) |
| 6 | `MessageBus` delivers and resends independently of the UI | `pending` | `pending` | `pending` | `#19` — not yet landed | The spike (phase 7) |
| 7 | Unsaved artifacts protected from the managed worktree lifecycle | `pending` | `pending` | `C2 (claude -p subprocesses)` | `#7` — not yet landed | The spike (phase 1b) |
| 8 | Secretary window responsiveness under worker load | `pending` rehearsal → **not discharged** | `pending` | `pending` | `#21` (rehearsal) — not yet landed | **Before the canary starts** (D-0022) |
| 9 | Curator output cannot reach a skill without human approval | `discharged` | `proven on the spike slice` | `provider-independent` | `#22`, PR `#27`; `docs/curator-promotion-gate.md`; `tests/curator/`; `investigation/u8-skill-hot-reload-probe.md` (U8) | **Discharged 2026-08-18**, independently of the spike |
| 10 | One-worker canary and run-boundary rollback | `pending` rehearsal → **not discharged** | `pending` | `pending` | `#23` (rehearsal) — not yet landed | **At the canary itself** (D-0022) |
| 11 | Only the `SessionProvider` need be swapped | `pending` | `pending` | `provider-independent` | `#10` (S1) and `#11` (S3) landed 2026-08-19; `#20` — not yet landed | The spike (phase 8) |

All eleven items are present. None is omitted and none is merged into another.

---

## 3. Per-item rows

Each row below is the long form of one table line. A row is complete when its **Verdict** leaves
`pending`; until then its **Evidence** names where the evidence will come from, which is what makes
this file usable at the *start* of the spike rather than only at its end. §6 states how to append.

### Item 1 — the public CLI alone can start, read structured state of, stop, and resume a worker

- **Verdict:** `pending`
- **D-0022 label:** `pending`
- **Provider:** `C2 (claude -p subprocesses)` — `ACCEPTANCE.md` §4 requires this item in full
  against whatever provider ships. Nothing proven on C1 carries over.
- **Evidence:** `#6` (the `claude -p` supervisor surface Interlock will own) and `#7` (multi-turn
  resume on real workers). Neither has landed.
- **Residual:** none recorded yet.
- **Notes:** `#6` is a phase-1a early exit. If the supervisor surface does not work through
  documented flags, the sequence ends here and this file is still due.

### Item 2 — unique session↔run re-matching across the crash window, no duplicate active writer

- **Verdict:** **`failed` on C1 (Agent View)**, 2026-08-18, per D-0027. **`pending` on C2.**
- **D-0022 label:** `n/a — failed` for the C1 attempt; `pending` for C2.
- **Provider:** `C1 (Agent View)` → `C2 (claude -p subprocesses)`.
- **Evidence (C1, the failure):**
  - U1 negative — `investigation/u1-session-id-bg-experiment.md`: a background session's identity
    cannot be chosen before spawn. `--bg --resume` is honoured for *content* and ignored for
    *identity*, exit 0 and no warning, the transcript copied under a new CLI-assigned id.
  - The D-0024 fence search came up **empty** —
    `investigation/pre-spawn-fence-search.md` §3.1 (surfaces searched), §3.2 (twelve candidates;
    rows 1–3, 5, 11 and 12 refuted by experiment rather than by reading). Exhaustiveness is not
    claimed: anything outside §3.1 is *unsearched*, not *absent*.
  - D-0027 records the verdict, its falsification conditions, and the adoption of C2.
- **Evidence (C2, pending):** `#18` proves single-writer re-identification across the crash window
  on C2; `#17` builds the C2 provider it runs against. Neither has landed. `#12` (S5) landed
  2026-08-19 and supplies the durable half the proof is read out of — the session↔run binding is a
  row in `session`, and *at most one active binding per run* is a partial unique index there rather
  than a check-then-insert, since a check-then-insert leaves exactly the window this item injects
  into. Landing the store proves nothing about the item: the kill matrix is `#18`'s.
- **Residual — stated as what it is: the absence of a backstop, not a tolerated violation.**
  Under C2 the provider supplies **no exclusion**:
  - **U27 (negative)** — the `-p` `--session-id` refusal is *not atomic*. Inside an admission window
    of roughly 2–3 s on one machine, 5 of 5 simultaneous trials admitted **both** processes: both
    exited 0, both reported the same `session_id`, and **both wrote to the same transcript**. The
    width is a one-machine, one-load measurement (U34) and must not be designed on as a constant.
  - **U28 (positive)** — after a SIGKILL of the holder the claim held out to ~25 minutes and
    `--resume` returned the same `session_id`. This is the *binding* half of O6 only; it carries no
    single-writer content.
  - **U32 (registered)** — `--resume` carries **no exclusion at all**: two concurrent
    `claude -p --resume <same uuid>` processes were both admitted, simultaneously and at a 5 s
    stagger. **Two is the number tested; no larger count was probed.**

  So nothing but **Interlock's own correctness** — the fencing token, validated atomically as part
  of each protected write (`ACCEPTANCE.md` §2, D-0027 part 3) — stands between a run and an
  interleaved transcript. That is the residual, and it is recorded here rather than left implicit.

  **An interleaved transcript is not an accepted residual.** If two processes were ever concurrently
  live on one session id, **item 2 failed**, and this record says so.
- **Notes:** the exposure is the narrow one item 2 names — the original claimant crashing while
  still inside the admission window, followed by a retry that also lands inside it (F3's crash
  window). O6's grade stays `~` (D-0027); a move to `Y` or to `N` would both be wrong on this
  evidence. General rule fixed by the C1 failure and binding under any provider: **do not treat
  exit 0, or a binding committed before the spawn, as evidence that the identity was accepted** —
  read back what the provider actually assigned.

### Item 3 — per-role permission / sandbox / hooks survive restart and fail closed

- **Verdict:** **`discharged`**, 2026-08-18, on the **weakened** observable D-0023 defines. The
  weakening is not a footnote to this verdict; it is part of it, and it is stated below in D-0023's
  own words.
- **D-0022 label:** `proven on the spike slice` — `src/claude_org_runtime/fencing/` is throwaway
  under D-0026 and `tests/fencing/` is the durable half. Re-proof on the real implementation is
  still owed.
- **Provider:** `C2 (claude -p subprocesses)` — required in full against the shipping provider
  (`ACCEPTANCE.md` §4).
- **Evidence:** `#9` (S10) — `docs/per-role-fencing.md`; the renderer, fail-closed spawn
  precondition, `PreToolUse` deny hook and breach-probe battery in
  `src/claude_org_runtime/fencing/`; the durable suite in `tests/fencing/`; and the live
  measurements in `investigation/i04-pretooluse-fence-probe.md` (nine `claude -p` children, CLI
  `2.1.234`).
- **The four criteria, and what each rests on.**
  1. **Restart preserves the fence.** Under C2 the only restart is Interlock respawning a `-p`
     child from persisted state. The battery denies every rule on both sides and the rendered-input
     diff is identical (`tests/fencing/test_restart_preserves_fence.py`). Neither alone suffices:
     a fence that comes back one rule short still *passes* the battery, because the battery can
     only probe the rules it was given — the diff is what catches the loss.
  2. **Every rule has a probe, and every probe is denied.** 44 rules across four roles, one
     forbidden operation each, coverage asserted as a set equality against the rendered fence and
     re-asserted by adding a rule at runtime and requiring the battery to grow with it. A
     hand-maintained probe list would pass the first check and fail the second, which is why the
     second exists.
  3. **A broken configuration refuses the spawn.** All three classes `#9` names — config deleted,
     hook path unresolvable, sandbox profile absent — plus a self-check for a fence that does not
     deny its own probes. The load-bearing assertion is negative: **the spawner is never invoked**,
     and no fence or settings file is published.
  4. **The deny hook is proven to deny, not merely to run.** By effect, never by exit code:
     `investigation/i04-pretooluse-fence-probe.md` measured a JSON `deny` at exit 2 stopping the
     operation, and a hook exiting **1** being absorbed while the operation went through — the same
     shape A6/U35 found on `WorktreeCreate`, now reproduced on `PreToolUse`. **All nine cases
     exited 0.**
- **U15 — answered.** `PreToolUse` fires and its `deny` is honoured under `bypassPermissions`; the
  mode does not skip the hook. The answer is reflected in the rendering as a **refusal** of that
  mode (`RefusalReason.PERMISSION_MODE_BYPASS`): U15 removes the reason to *avoid*
  `bypassPermissions` and leaves the reason to *refuse* it — under it the hook is the only remaining
  layer, and that layer has a measured absorption mode which is silent and exits 0.
- **How C2 changed the item's shape.** D-0023 part 3 made a supervisor-initiated restart fence a
  **terminal** exit condition: if no handle mediating restarts started by the provider's own
  supervisor existed, item 3 failed on Agent View. **That hole was removed by the provider switch,
  not closed by evidence** — under C2 no other party can restart a worker, so Interlock's
  fail-closed spawn precondition covers every start. It was **never proven closed on Agent View**,
  and `#8`, the issue that would have probed it, was **closed as moot rather than passed**. The
  distinction matters: a hole that stopped existing when the provider changed will exist again the
  moment a provider with its own supervisor is adopted.
- **Residual — in D-0023's own terms.** No public surface returns a session's *effective*
  configuration, so item 3's equality check is not runnable as written. The substitute is a
  behavioural **breach-probe battery** (one forbidden operation per *rule* in the role's fence, not
  one per role) plus a diff of Interlock's own rendered inputs. **This substitution is a deliberate
  weakening of item 3, accepted by a human — not an equivalent method.** Diffing our own rendered
  inputs proves **what we wrote, not what the provider loaded**, and that gap is exactly what item 3
  exists to close. Probing every rule narrows it; it does not close it.
- **Additional residuals recorded rather than absorbed into the verdict:**
  - **U42 (new).** An *unresolvable* `PreToolUse` hook fails open or closed depending on the
    launcher's exit code — `python3` exits 2 and blocks, `bash` exits 127 and does not. The
    "fail-closed" outcome is a coincidence of one interpreter's convention. Handled by validating
    hook paths before the spawn; it remains a property of the provider, not of Interlock.
  - **U43 (open).** Every absorption mode measured is exit-code-shaped. A hook that **times out**,
    or one writing malformed JSON at exit 0, was **not probed**, and the battery does not cover it.
  - **One machine, one CLI build, one load, one run per case** (U34). No case was repeated, so this
    is not a flakiness measurement.
  - **No single run exercises the rendered fence end to end.** The attempt is recorded as
    **inconclusive** (`investigation/i04-pretooluse-fence-probe.md` §5b): the control was blocked
    too, because `permission_mode: default` stops bash writes regardless of any rule, so neither run
    attributes anything to the fence. The hook's decision and the CLI's honouring of a `deny` are
    two separate measurements and are **not** stitched into one claim.
- **Notes:** fail-closed is **Interlock's own obligation** under D-0017 regardless of provider
  (D-0023 part 2), so that work is not wasted under any `Q-0004` outcome. A third provider would
  revert this row to `pending` (`ACCEPTANCE.md` §4) and would restore the D-0023 part 3 hole with
  it.

### Item 4 — Supervisor / Dispatcher Core / Secretary resume from SQLite with no double execution

- **Verdict:** `pending`
- **D-0022 label:** `pending`
- **Provider:** `pending` — a control-plane property; re-run for regression against a new provider,
  not redesigned (`ACCEPTANCE.md` §4).
- **Evidence:** `#13` (lease/fencing token), `#14` (outbox), `#16` (the `ACCEPTANCE.md` §2 matrix as
  an automated suite). Not yet landed. `#12` (S5) landed 2026-08-19:
  `src/claude_org_runtime/control_plane/`, tests `tests/control_plane/`. It is the store this item
  resumes *from* — the five recovery reads are `RECONSTRUCTION_QUERIES`, and the suite proves the
  reconstruction is answerable by query alone by dropping the interpreter and re-reading it from a
  fresh subprocess. Corrupt state is refused rather than recovered as empty (R3), so a damaged
  database cannot present itself as "nothing was in flight".
- **Residual:** none recorded yet. The known structural limit is stated in `ACCEPTANCE.md` §2:
  SQLite alone cannot distinguish a completed side effect from one that never started, so each
  action handler must **name** its exactly-once mechanism (destination idempotency key, or effect
  transactional with its record) or route to a human gate (D-0004).

### Item 5 — lease, outbox resend, ack, dedup, single-writer confirmed by fault injection

- **Verdict:** `pending`
- **D-0022 label:** `pending`
- **Provider:** `pending` — control-plane property, as item 4.
- **Evidence:** `#14`, `#15` (deterministic kill points), `#16`. Not yet landed. `#13` (S6) landed
  2026-08-19: `src/claude_org_runtime/control_plane/lease.py`, tests
  `tests/control_plane/test_lease.py`, written record `docs/lease-fencing.md`. It supplies the
  exclusion half of this item — the fencing token is validated **atomically as part of** each
  protected write, a stale-token write is refused and the refusal recorded as an `action` row, and
  the single-writer property is read back by query from the epoch every fenced write is stamped
  with. **Landing the lease discharges nothing here:** the injection matrix is `#16`'s and the
  deterministic kill points are `#15`'s, and this item is a verdict about those cases, not about
  the mechanism they exercise.
- **Residual:** two, both stated in `docs/lease-fencing.md` §5 rather than assumed away. **(1)** The
  spike schema keeps one lease row per resource and no history table (`Q-0001`), so a wall-clock
  timeline is reconstructed from observed row states while the durable evidence is the epoch stamped
  on each fenced write. **(2)** Under clock skew two holders can overlap in *true* time and the rows
  cannot show it — each claimant stamps its acquisition in its own frame — which is precisely why a
  protected write validates the epoch and not the expiry. Per `ACCEPTANCE.md` §2 a case certifying
  exactly-once for an **external** effect from our own rows alone does not count, and every case must
  be automated and reproducible — no manual one-shot demonstrations.
- **Notes:** D-0027 moves the fencing token and its tests from "belt and braces" to **the only
  exclusion in the system**. Item 5's single-writer cases are therefore where item 2's residual is
  actually carried.

### Item 6 — `MessageBus` delivers and resends independently of the Agent View UI

- **Verdict:** `pending`
- **D-0022 label:** `pending`
- **Provider:** `pending` — control-plane property.
- **Evidence:** `#19` (S8 as a worker-outbound MCP endpoint, plus the static no-edge assertion).
  Not yet landed.
- **The F1 caveat, in its now-stronger form — and it is a caveat, not a strength.** Per F1 there is
  no non-interactive path to deliver a message *into* a running background session, so the transport
  is necessarily **worker-outbound**: the worker connects to Interlock's bus as a client and
  delivery is a pull, not a push. Item 6's "with no Agent View UI attached" condition is therefore
  **trivially satisfied — the UI is not on the delivery path at all**. And under C2 there is **no
  Agent View UI to attach in the first place**. The condition is free for two independent reasons.
  **Two reasons a condition is free is not a stronger result; it is the same result, twice
  unearned.** Claiming item 6 as a strong result would be overclaiming twice over.
- **Residual:** the "UI attached but session state deliberately stale" case must be **translated,
  not skipped**, under C2 — the stale readout becomes a provider readout that is stale or wrong (a
  session id whose child is gone, a state read returning "could not observe"). The part of item 6
  that is *not* free is the static no-dependency-edge assertion, enforced in CI.

### Item 7 — unsaved artifacts protected from the managed worktree lifecycle

- **Verdict:** `pending`
- **D-0022 label:** `pending`
- **Provider:** `C2 (claude -p subprocesses)` — required in full against the shipping provider.
- **Evidence:** `#7` (working-tree ownership on real `claude -p` workers). Not yet landed.
- **Residual:** none recorded yet.
- **Notes:** `#7` is a phase-1b early exit and can fail item 1 or item 7. C2 was chosen partly on
  O8 — under C2 nobody else owns the working tree — but that is a *reason to expect* the item to
  pass, not evidence that it did.

### Item 8 — Secretary window responsiveness while workers are loaded

- **Verdict:** `pending` rehearsal → **explicitly not discharged**
- **D-0022 label:** `pending` (the rehearsal, when it lands, is labelled *proven on the spike slice*
  and is **not** a discharge; the discharge carries *re-proven on the real implementation*)
- **Provider:** `pending` — and note `ACCEPTANCE.md` §4 lists item 8 among the items that must be
  proven **in full** against whatever provider ships.
- **Evidence:** `#21` — a stub Secretary intake with an explicit queue boundary under a load
  generator at the worker cap, asserting structurally that intake and queue boundary are
  asynchronous and recording baseline-vs-load latency. Not yet landed.
- **Discharge point:** **before the canary starts** (D-0022, D-0013).
- **Discharge point reached:** `no` — the canary has not started. A Secretary that blocks under
  load would invalidate the canary's own measurements.
- **Residual:** the numeric latency threshold is **unresolved** — `Q-0011`. The gate check is the
  absence of blocking dependencies plus a recorded baseline-vs-load comparison, and the real proof
  is against the real Secretary under genuine worker load.
- **Scoped exception:** see §4. If the discharge point is reached without the predicate met, that is
  a **gate failure recorded as such**. Deferred, not waived.

### Item 9 — Curator output cannot reach a skill without human approval

- **Verdict:** **`discharged` in full**, 2026-08-18.
- **D-0022 label:** `proven on the spike slice`.
- **Provider:** `provider-independent` — item 9 tests nothing about the session backend, and
  `ACCEPTANCE.md` §4 deliberately omits it from the re-run list. **Uniquely among the eleven, it was
  untouched by the provider switch** — it was the one issue exempt from the phase-0 block, ran in
  parallel from day 1, and the C2 ruling did not touch it. If the gate fails outright on C2 as well,
  this result still stands.
- **Discharged independently of the spike** (D-0022): in parallel, with no dependency on any other
  issue and none on the provider verdict.
- **Evidence:** `#22`, merged as PR `#27`. `docs/curator-promotion-gate.md` (the design and the path
  audit); `tests/curator/` (the five negatives, the positive control, and the build-failing path
  audit); `investigation/u8-skill-hot-reload-probe.md` (U8).
- **What was proven.** U8 is answered **affirmative** — a running session re-reads skill material
  from disk, and a mid-session directory is loadable *before* it is listed. So **writing the file is
  the promotion**, and the gate sits at the filesystem write rather than at a `promote()` policy
  step. Against that boundary all five negatives are refused **and the refusal recorded**: approval
  absent, forged-but-unrecorded, revoked, candidate mutated after approval (the content digest),
  and a valid approval replayed against a different candidate. A path audit shows no route from
  Curator output to skill material that bypasses the gate, and a negative test **fails the build**
  if such a route is added later.
- **Residual:** none. The item is discharged in full.
- **Notes:** the tests are **durable** (D-0026); the gate implementation is throwaway by default.

### Item 10 — one-worker canary and run-boundary rollback

- **Verdict:** `pending` rehearsal → **explicitly not discharged**
- **D-0022 label:** `pending` (rehearsal → *proven on the spike slice*; discharge → *re-proven on
  the real implementation*)
- **Provider:** `pending` — `ACCEPTANCE.md` §4 requires item 10 re-run in full against whatever
  provider ships.
- **Evidence:** `#23` — a run-start routing point, a run→owning-system ledger and a writer audit
  over both stores against a **synthetic** counterparty, plus a rehearsed rollback that changes only
  the routing decision. Not yet landed.
- **Discharge point:** **at the canary itself** (D-0022).
- **Discharge point reached:** `no` — the canary has not run. The item passes when canary runs complete
  with exactly one owner per run, no record written by both systems, and a real rollback that
  changes only routing.
- **Residual:** canary duration, sample size and numeric go/no-go criteria are **unresolved** —
  `Q-0005`, which also holds the undecided case of runs already in flight *on Interlock* at
  rollback. Item 10's real proof needs v1 as a live counterparty, which needs the implementation to
  be running — which is why it is deferred rather than discharged up front.
- **Scoped exception:** see §4. Deferred, not waived.

### Item 11 — even if the provider does not hold, only the `SessionProvider` need be swapped

- **Verdict:** `pending`
- **D-0022 label:** `pending`
- **Provider:** `provider-independent` — by construction: the item measures the *absence* of
  provider detail in the control plane.
- **Evidence:** `#10` (S1, the provisional `SessionProvider` interface), `#11` (S3, the stub
  provider over local child processes), `#20` (re-run the control-plane suite unchanged against S3).
  `#10` and `#11` landed 2026-08-19; `#20` — the half that actually measures the item — has not.
  Both halves are needed: a stub that exists proves nothing until an unmodified suite runs against it.
- **Residual:** none recorded yet. Any test that has to be **modified** to run against S3 marks a
  leak of session-backend detail into the control plane and must be fixed before the item passes.
- **Notes:** D-0020's B+ ordering — S3 written before S2 — exists so that item 11 measures a
  structural property rather than a retrofit. The C1→C2 switch is the first real test of D-0019's
  promise that a gate failure costs a provider and not a design, and item 11 is where that promise
  is measured rather than asserted.

---

## 4. The scoped exception (items 8 and 10 only)

D-0022 is a **scoped exception to D-0019, limited to items 8 and 10**. Both are **rehearsed on
substitutes during the spike and explicitly not discharged** before implementation starts:

| Item | Rehearsed on | Real proof | Discharged at |
|---|---|---|---|
| 8 | Stub Secretary intake with an explicit queue boundary, under a load generator at the worker cap | The same absence of blocking against the real Secretary under genuine worker load, at a threshold settled by `Q-0011` | **Before the canary starts** |
| 10 | Run-start routing, run→owner ledger and writer audit against a synthetic counterparty; a rehearsed rollback | The same audit with v1 as the live counterparty, under the numeric criteria settled by `Q-0005` | **At the canary itself** |

Three consequences, stated so they cannot be read past:

1. **Implementation may begin with items 8 and 10 outstanding — and only those two. Any further item
   slipping past the gate is a new decision, not an extension of this one.**
2. **This exception defers the two items; it does not waive them.** If either discharge point is
   reached without its predicate met, that is a **gate failure recorded as such**, in this file.
3. D-0019 keeps its ID and its `accepted` status. It is not superseded.

---

## 5. Artifact classification (D-0026)

The spike's **durable** output is the `SessionProvider` interface (S1) and the tests. **Every
implementation the spike produces is throwaway by default, including S5's schema.** Promotion into
the real implementation requires a **new `D-` entry** that says so; nothing in this record promotes
anything.

| Artifact | Class (D-0026) | Where |
|---|---|---|
| S1 — the provisional `SessionProvider` interface | **durable (contract)** | `#10` — marked provisional in the file itself (D-0021); promoted to a settled contract only by a later `D-` entry. Landed 2026-08-19: `src/claude_org_runtime/session/provider.py`, tests `tests/session/`. Being written does not promote it |
| Tests — fault injection, recovery, accident-derived fixtures, the control-plane suite | **durable (tests)** | `#15`, `#16`, `#18`, `#19`, `#20`, `tests/curator/`, `tests/fencing/` |
| S2 — the C2 `SessionProvider` | throwaway | `#17` |
| S3 — the stub provider | throwaway | `#11` — landed 2026-08-19: `src/claude_org_runtime/session/stub_provider.py`, tests `tests/session/test_stub_provider.py`. Local child processes only: no Claude CLI, no network. Throwaway under D-0026, but it survives a C2 switch untouched |
| S4 — the probe harnesses | throwaway | `#6`, `#7` |
| **S5 — the spike SQLite schema** | **throwaway — named explicitly by D-0026** | `#12` — landed 2026-08-19: `src/claude_org_runtime/control_plane/spike_schema.sql`, tests `tests/control_plane/`. The file carries its own note, at the top, that it is a spike schema and that **no migration path is promised from it**, and the loader refuses DDL whose marking has been removed. A database at another revision is **refused, never migrated**. `Q-0001` stays open and is not answered by inertia: no column, CHECK or index names a component or a role, and `Q-0002` stays open too — `dedup_key` is indexed but not unique, so neither collapse rule is forced |
| S6 / S7 — lease and outbox implementations | throwaway (their tests are durable) | `#13` — landed 2026-08-19: `src/claude_org_runtime/control_plane/lease.py`, tests `tests/control_plane/test_lease.py`, written record `docs/lease-fencing.md`. Throwaway under D-0026 and it survives a C2 switch untouched — it is Interlock's own obligation regardless of provider (D-0024), and after the fence search it is the only exclusion there is. `Q-0001` stays open: `holder` is an opaque claimant identity and never a role. `#14` — not yet landed |
| S8 — `MessageBus` MCP endpoint | throwaway (the no-edge assertion is durable) | `#19` |
| S10 — per-role fencing renderer, `PreToolUse` deny hook, breach-probe battery | throwaway implementation, durable tests | `#9`; implementation `src/claude_org_runtime/fencing/`, tests `tests/fencing/`. Landed 2026-08-18; nothing here is promoted by having discharged item 3 |
| Curator promotion gate implementation | throwaway | PR `#27`, `src/claude_org_runtime/curator/` |
| Investigation records (U-register, fence search, U1, U8, U15/U35/U42) | evidence, not spike output — kept as the basis of the verdicts above | `investigation/` |

---

## 6. If the gate fails on C2 — what that costs

**A gate record that only exists on success is not a gate record.** So, plainly:

- **`Q-0004` is resolved and spent.** D-0025 resolved it by designating C2; D-0027 spent that
  designation by adopting C2 when item 2 failed on C1. The question cannot be drawn on twice.
- **No current `D-` entry designates a third provider.** D-0025 records **C3 — the Claude Agent
  SDK — as a genuine second choice**, not a weak one, but **adopting it would take a new decision**.
  Until that decision exists, a failure on C2 leaves the spike with no designated provider.
- **The design still survives the failure.** D-0019's promise is unchanged: a gate failure replaces
  only the `SessionProvider`. `ACCEPTANCE.md` §4 lists what stays intact (SQLite SoT, fact states,
  incident contract, `MessageBus` separation, role boundaries, cutover method) and what must be
  re-established against a new provider — items 1, 2, 3, 7, 8 and 10, in full.
- **The rows already filled do not all survive.** Item 9 does, being provider-independent. Every
  row whose Provider column names C2 would revert to `pending` against a third provider, and the C2
  results would join item 2's C1 result as **history retained, not evidence carried**.
- **Any failing predicate closes this record.** The sequence has early exits at `#6` (phase 1a),
  `#7` (phase 1b) and item 2 on C2 (`#18`), and every issue carrying a gate predicate can end it:
  `#9` can fail item 3, `#16` items 4 and 5, `#18` item 2, `#19` item 6, `#20` item 11, `#22`
  item 9. In every one of those branches the downstream evidence never arrives — and in every one
  this record is still due, closed by termination instead of by discharge.

---

## 7. How to append a row

Later issues fill **one item at a time**. To close out an item:

1. Edit that item's row in §2 and its section in §3 — nothing else. Rows are independent; a failing
   item does not block the others from being recorded.
2. Set **Verdict** from the closed vocabulary in §2, **Provider** to the provider the evidence was
   actually obtained against, and the **D-0022 label** to `proven on the spike slice` or `re-proven
   on the real implementation`. A failure takes `n/a — failed` and states the failing predicate.
3. Name the **evidence** as issue/PR numbers plus committed artifacts. A verdict with no artifact
   behind it is not a verdict.
4. State the **residual** in the terms of the decision that created it, not in softer terms of your
   own. If there is none, write "none".
5. **Never overwrite history.** Item 2's C1 failure is retained alongside its C2 outcome; anything
   later that supersedes a row is appended next to it, not in place of it.
6. Items 8 and 10 may not be set to `discharged` before their named discharge points (§4). When a
   discharge point is reached, flip that item's **Discharge point reached** field to `yes` and set
   the verdict in the same edit: `discharged` if its predicate was met, **`failed` if it was not**.
   The flag is what lets the record move at the discharge point without letting it move early; a
   `discharged` verdict while the flag reads `no` is the widening D-0022 forbids.

`tests/gate_record/test_gate_record.py` enforces the structural half of these rules — eleven items
present and distinct, closed vocabularies, table and sections agreeing, the D-0022 exception not
quietly widened, and item 2's C1 failure not deleted.
