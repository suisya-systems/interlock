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

**Where the sequence stands.** Items 9, 11 and 6 are discharged (each a provider-independent,
control-plane property — item 6 with the F1 caveat its §3 row states). **Item 3 is
discharged on C2**, on the weakened observable D-0023 defines — and its residual is stated in §3 in
D-0023's own terms rather than folded into the verdict. Item 2 carries a **failed** verdict on C1
and is **discharged on C2** (2026-08-21, `#18`) — the C1 failure stays on the record beside it.
**Items 1 and 7 are discharged on C2** (verdicts recorded 2026-08-21, on the S4 probe records of
`#6` and `#7`, both landed 2026-08-18). **Items 4 and 5 are discharged** as control-plane
properties (verdicts recorded 2026-08-21, on the S9 harness and the `ACCEPTANCE.md` §2 matrix
suite — `#15` and `#16`, landed 2026-08-20). **Item 8 is rehearsed on C2 — explicitly not discharged** (D-0022): its
discharge point remains before the canary starts, against the real Secretary. **Item 10 is
rehearsed against a synthetic counterparty — explicitly not discharged** (D-0022): its discharge
point remains at the canary itself, with live v1 as the counterparty. No item is `pending` any
longer: every row carries a verdict, and what stands between this record and the gate's close is
the two D-0022 deferrals, whose discharge points have not been reached.

---

## 2. The record

Vocabularies are closed. Verdict is one of **`discharged`**, **`failed`**, **`rehearsed — not
discharged`**, **`pending`**. Label is one of **`proven on the spike slice`**, **`re-proven on the
real implementation`**, **`n/a — failed`**, **`pending`**. Provider is one of **`C1 (Agent View)`**,
**`C2 (claude -p subprocesses)`**, **`provider-independent`**, **`pending`**.

| # | Item (short) | Verdict | D-0022 label | Provider | Evidence | Discharge point |
|---|---|---|---|---|---|---|
| 1 | Public CLI alone can start / read state / stop / resume | `discharged` | `proven on the spike slice` | `C2 (claude -p subprocesses)` | `#6` (PR `#31`) — `investigation/i01-supervisor-probe.md`; `#7` (PR `#33`) — `investigation/i02-conversation-probe.md`: the full cycle on one real multi-turn worker, three successive resumes, the internals-free negative executed on both halves. Landed 2026-08-18; verdict recorded 2026-08-21 | The spike (phases 1a/1b) — reached 2026-08-18 |
| 2 | Unique session↔run re-match across the crash window; no duplicate writer | `failed` on C1; `discharged` on C2 | `n/a — failed` (C1); `proven on the spike slice` (C2) | `C1 (Agent View)` → `C2 (claude -p subprocesses)` | D-0027; `investigation/u1-session-id-bg-experiment.md`; `investigation/pre-spawn-fence-search.md`. C2 proof: `#18` landed 2026-08-21 — `docs/crash-window-orchestration.md`, `tests/gate_item2/`, the `session-start` fault cases, `investigation/i18-crash-window-characterisation.md`; its provider (`#17`, S2) landed 2026-08-21 | The spike (phase 6), on C2 — reached 2026-08-21 |
| 3 | Per-role permission / sandbox / hooks survive restart and fail closed | `discharged` | `proven on the spike slice` | `C2 (claude -p subprocesses)` | `#9`; `docs/per-role-fencing.md`; `src/claude_org_runtime/fencing/`; `tests/fencing/`; `investigation/i04-pretooluse-fence-probe.md` (U15, U35, U42). `#8` closed as **moot** under C2, not passed | The spike (phase 2b) |
| 4 | Supervisor / Dispatcher Core / Secretary resume from SQLite, no double execution | `discharged` | `proven on the spike slice` | `provider-independent` | `#12` (S5), `#13` (S6) and `#14` (S7) landed 2026-08-19; `#15` (PRs `#50`, `#51` — the S9 harness) and `#16` (PR `#52` — the §2 matrix) landed 2026-08-20 — `tests/fault_injection/`, `docs/s9-fault-injection-harness.md`: three role processes killed separately and in combination at points straddling the durable write, recovery by query from SQLite alone. Verdict recorded 2026-08-21 | The spike (phases 4–5) — reached 2026-08-20 |
| 5 | Lease, outbox resend, ack, dedup, single-writer under fault injection | `discharged` | `proven on the spike slice` | `provider-independent` | `#13` (S6) and `#14` (S7) landed 2026-08-19; `#15` (PRs `#50`, `#51`) and `#16` (PR `#52`) landed 2026-08-20 — all six `ACCEPTANCE.md` §2 targets automated in `tests/fault_injection/` (55 cases at `#16`'s landing, 59 with `#18`'s `session-start` cases), external effects proven against the destination's own record. Verdict recorded 2026-08-21 | The spike (phases 4–5) — reached 2026-08-20 |
| 6 | `MessageBus` delivers and resends independently of the UI | `discharged` | `proven on the spike slice` | `provider-independent` | `#19` — `src/claude_org_runtime/messagebus/`; `tests/messagebus/`; `docs/messagebus-carry-drop.md`; D-0028 | The spike (phase 7) |
| 7 | Unsaved artifacts protected from the managed worktree lifecycle | `discharged` | `proven on the spike slice` | `C2 (claude -p subprocesses)` | `#7` (PR `#33`) — `investigation/i02-conversation-probe.md` §3.5: a fixture carrying uncommitted, untracked and unpushed work byte-identical by recorded hash across every lifecycle transition, the A6 edit-forcing negative executed. Landed 2026-08-18; verdict recorded 2026-08-21 | The spike (phase 1b) — reached 2026-08-18 |
| 8 | Secretary window responsiveness under worker load | `rehearsed — not discharged` | `proven on the spike slice` | `C2 (claude -p subprocesses)` | `#21` — `tests/secretary/`; `docs/secretary-intake-boundary.md`; `investigation/i16-item8-rehearsal.md` | **Before the canary starts** (D-0022) |
| 9 | Curator output cannot reach a skill without human approval | `discharged` | `proven on the spike slice` | `provider-independent` | `#22`, PR `#27`; `docs/curator-promotion-gate.md`; `tests/curator/`; `investigation/u8-skill-hot-reload-probe.md` (U8) | **Discharged 2026-08-18**, independently of the spike |
| 10 | One-worker canary and run-boundary rollback | `rehearsed — not discharged` | `proven on the spike slice` | `provider-independent` | `#23` — `tests/canary/`; `docs/canary-routing-rehearsal.md`; `src/claude_org_runtime/canary/` (synthetic counterparty rehearsal) | **At the canary itself** (D-0022) |
| 11 | Only the `SessionProvider` need be swapped | `discharged` | `proven on the spike slice` | `provider-independent` | `#10` (S1) and `#11` (S3) landed 2026-08-19; `#20` landed 2026-08-20 — `tests/gate_item11/`, and the control-plane suite in CI with a provider bound. Zero test modifications; the S2 half of `#20`'s fourth criterion was discharged 2026-08-21 when `#17` landed the S2 registry row (see §3, item 11) | The spike (phase 8) |

All eleven items are present. None is omitted and none is merged into another.

---

## 3. Per-item rows

Each row below is the long form of one table line. A row is complete when its **Verdict** leaves
`pending`; until then its **Evidence** names where the evidence will come from, which is what makes
this file usable at the *start* of the spike rather than only at its end. §6 states how to append.

### Item 1 — the public CLI alone can start, read structured state of, stop, and resume a worker

- **Verdict:** `discharged` — recorded 2026-08-21, on evidence landed 2026-08-18. The two probe
  records propose this reading (each is scoped propose-only); entering the verdict is this
  record's call, made here.
- **D-0022 label:** `proven on the spike slice` — the probe harnesses are S4 throwaways (D-0026);
  the durable half is the two investigation records with their verbatim argv, output and exit
  codes. Re-proof on the real implementation is still owed, and `ACCEPTANCE.md` §4 keeps item 1
  on the re-run-in-full list for any provider change.
- **Provider:** `C2 (claude -p subprocesses)` — CLI 2.1.234, one machine, one load (U34). Nothing
  proven on C1 carries over, and nothing from C1 was used.
- **Evidence:** `#6` (PR `#31`) — `investigation/i01-supervisor-probe.md`, the supervisor half:
  spawn, machine-parseable state read from published output (the `system/init` event under
  `--output-format stream-json --verbose` — the default `text` format is not a supervisor
  surface), signal-terminate and reap through documented flags only, the exit-code table with
  exit 0 recorded as evidence of nothing, the capability/version probe recorded verbatim, and the
  internals-free negative executed against the harness with the child unrestricted. `#7` (PR
  `#33`) — `investigation/i02-conversation-probe.md`, the conversation half: the whole cycle on
  one real four-turn worker (start → structured read → stop, a mid-turn SIGTERM at rc 143 →
  resume, the stopped turn persisted and recalled), continuity across three successive resumes in
  fresh processes with one `session_id` and one transcript growing in place (no fork-like
  behaviour of the U33 class), resume across a supervisor kill-and-restart from persisted state
  only with the child resolved before the resume — both the died-with-parent and the live-orphan
  case — and the internals-free negative re-run on this half the right way round: the restriction
  on the harness, never the child.
- **Residual:** three, each a provider fact recorded rather than absorbed. **(1) H5/U38** — the
  `--session-id` refusal is a file-existence check, not a lock: removing the transcript releases
  the claim and the id is then re-claimable as if new, so a session id is **not durable
  identity** (i02 §3.6). U40 (whether the CLI itself ever removes transcripts) and U41 (whether a
  re-claimed-empty id is distinguishable from a fresh one) stay open. **(2)** "Stop and reap"
  means the **process group**: a pid-only stop satisfies the letter of the item while leaving the
  child's children running (i01 §3.5). **(3)** Resume is not idempotent with respect to live
  writers (U32): resolve-then-resume is a supervisor obligation, not a provider guarantee — the
  exclusion stays with the lease, and item 2's residual is where it is carried.
- **Notes:** `#6` was a phase-1a early exit, and it did not fire — the supervisor surface works
  through documented flags. The cycle's verbs are C2-shaped: under C2 "resume" is continuity
  across invocations (a fresh `-p` process with `--resume`), not reattachment to a running
  process, and that is the shape the evidence demonstrates.

### Item 2 — unique session↔run re-matching across the crash window, no duplicate active writer

- **Verdict:** **`failed` on C1 (Agent View)**, 2026-08-18, per D-0027. **`discharged` on C2**, 2026-08-21.
- **D-0022 label:** `n/a — failed` for the C1 attempt; `proven on the spike slice` for C2 — the orchestration and binding are throwaway under D-0026, the tests are the durable half, and re-proof on the real implementation is still owed (`ACCEPTANCE.md` §4 keeps item 2 on the re-run-in-full list for any provider change).
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
- **Evidence (C2, the discharge):** `#18` landed 2026-08-21; `#17` (the C2 provider it runs
  against) landed the same day, and `#12` (S5, landed 2026-08-19) supplies the durable store the
  proof is read out of — the session↔run binding is a row in `session`, *at most one active binding
  per run* is a partial unique index there rather than a check-then-insert (a check-then-insert
  leaves exactly the window this item injects into), and `#18` gave the row its staged
  `binding_phase` (`prepared → spawned → identity_confirmed`) so a pre-spawn commit is expressible
  without claiming an observation that never happened. What `#18` demonstrates, each part durable
  and automated:
  - **Commit-before-spawn, killed at all four injection points.** The `--session-id` UUID is
    generated by the orchestrator, committed under the fence before the process exists, and passed
    verbatim to the provider; `tests/fault_injection/` (the `session-start` cases over
    `session_driver.py`) SIGKILLs a real supervisor process at each armed point — before the
    binding commit, between commit and spawn, between spawn and the read-back commit, and after the
    read-back commit ("after the read-back" is defined as after that commit) — restarts it, and
    asserts from the reopened database that re-identification yields exactly one session for the
    run (`one-binding-per-run`: the index's at-most-one, plus a non-empty read after recovery,
    plus the surviving row confirmed — a recovery that never committed its read-back fails). The
    kill's placement is itself evidenced: the destination's spawn ledger is sampled between the
    kill and the restart, and a kill claimed inside a window whose spawn count disagrees fails
    the case. One granularity note: the armed "between commit and spawn" anchor sits after *both*
    admission writes (the prepare and the write-ahead mark commit back to back, with no anchor
    between them), so the narrower `prepared`-committed-mark-not state is exercised by
    constructed rows in `tests/gate_item2/`, not by a real kill.
  - **The mediated crash-and-retry shapes.** `tests/gate_item2/` runs the U27 shape (claimant dies
    inside the admission window, retry lands inside it) and the U32 shape (recovery of a dead
    holder's session) through the control plane, against providers that refuse nothing by
    construction: the losing claimant is refused at a fenced write and never becomes a process; a
    second resume is never issued while a claimant holds the lease; recovery resolves the surviving
    process first and goes through `--resume`, never a fresh claim (U28); a supervisor-only kill
    ends in adoption of the surviving child with no second spawn; orphans a binding does not name
    are never adopted.
  - **Refusals are rows.** Every second-writer refusal is an `action` row written by the lease
    module inside the refused attempt's own transaction; no assertion anywhere reads an exit code
    (under U27 both racers exit 0, so exit codes prove nothing here).
  - **The transcript-level statement.** The destination observables
    (`live-processes-per-session`, `transcript-single-writer`) are read from the destination's own
    records — real process counts and the captured event streams — never from our rows; every
    mediated stream names exactly the committed identity, with no doubled turn.
  - **The unmediated characterisation**, re-measured on this machine rather than taken from the
    report's figure: `investigation/i18-crash-window-characterisation.md` (CLI 2.1.238) reproduces
    U27 (3/3 both-admitted, both wrote; window edge between 2 s and 3 s today, refusal ~0.34 s and
    pre-model) and U32 (both concurrent resumes admitted; interleaved transcript, 3 user + 6
    assistant turns under one id). No measured figure is a constant in any test (U34).
  - The written protocol — the spawn-admission critical section, and the recovery ordering — is
    `docs/crash-window-orchestration.md`; it keeps the two claims separate: the fencing token
    proves protected writes, the spawn gate prevents a second model turn.
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
  The C2 discharge narrows it but does not remove it, and adds one honest bound of its own:

  - **The admission→exec window is open by construction.** A process creation cannot be made
    transactional with a SQLite commit, so a claimant that loses its lease *between* its fenced
    admission commit and its `exec` can bring a process into existence. What is guaranteed — and
    tested — is that the very next fenced write detects the stale token, and that the loser then
    acts coordinated with the holder rather than blind: while no takeover writer has confirmed the
    binding nor left an epoch-stamped gate row (the gate fires before the provider verb, so a
    winner that has reached the provider always has a durable trace even before its confirm), the
    loser's child is ordered stopped at once — serialised against the winner's writes by the
    database write lock; otherwise a session-level stop could kill the winner's adopted worker, so
    the loser stands down and its possibly-rogue process is surfaced as an unresolved hazard on
    the refusal (`stop_attempted=False`) for the holder to reconcile. The refusal carries the measured detection→stop latency **and the
    provider's own stop verdict** (`LoserTerminated.stop_confirmed`) — a stop the provider could
    not confirm is surfaced as unconfirmed, never dressed up as a termination that happened. Two scope
    caveats, stated: the stopped-claimant shapes are exercised deterministically — the pause is
    simulated by advancing the injected clock and raising the epoch at the exact seam a stopped
    process would resume from, on both sides of the admission write and during the read-back
    stall — no real `SIGSTOP` is delivered in this suite (the harness's real signals are the
    `SIGKILL` cases). And against a live model-backed child, termination speed bounds the
    exposure; it does not re-classify it — if such a child ever took a model turn on a session id
    it did not own, that would be the violation this item names, not an accepted cost.
  - The window's *mechanism* was answered by `investigation/i01-supervisor-probe.md`: U34 — the
    window is bounded by session persistence, the transcript's creation (~2.9 s there), not the
    first API response — and U36 — the refusal is keyed to the persisted transcript in the
    cwd-derived project directory, with the deletion half re-proposed as U38. What stays open is the
    *number*: the width is a one-machine, one-load measurement (U45), so no retry delay is designed
    on it, and the re-measurement here confirms the hazard is live on the current CLI, not a new
    explanation.

  **An interleaved transcript is not an accepted residual.** If two processes were ever concurrently
  live on one session id, **item 2 failed**, and this record says so.
- **Notes:** the exposure is the narrow one item 2 names — the original claimant crashing while
  still inside the admission window, followed by a retry that also lands inside it (F3's crash
  window). O6's grade stays `~` (D-0027); a move to `Y` or to `N` would both be wrong on this
  evidence. General rule fixed by the C1 failure and binding under any provider: **do not treat
  exit 0, or a binding committed before the spawn, as evidence that the identity was accepted** —
  read back what the provider actually assigned. The C2 walk enforces this: a binding is confirmed
  only after a positive read-back is itself committed, and a disagreeing read-back impounds the
  session rather than confirming it. One document correction rode with the discharge and is
  disclosed here as in the PR: `ACCEPTANCE.md`'s item 2 row previously said "between spawn and
  commit", which D-0024's commit-before-spawn ordering had already superseded; the row was
  re-synchronised to the four injection points, with the change noted in the row itself. The same
  row's "enumerate sessions via the public CLI" became "via the provider's public surface", and
  that is a **weakening, stated as one**: under C2 the CLI lists no `-p` children (i01 §3.6), so
  the roster is the provider's own durable per-session records — Interlock-supervised state, not
  an independent surface. The orphan/no-double-adoption assertions therefore rest on the provider's
  record discipline (record-before-`Popen`, validated against directory name and identity on
  discovery) rather than on a second observer.

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

- **Verdict:** `discharged` — recorded 2026-08-21, on evidence landed 2026-08-20 (`#16`, closing
  the matrix its own issue names as discharging items 4 and 5).
- **D-0022 label:** `proven on the spike slice` — and the slice caveat is the load-bearing one on
  this item: the three role processes the matrix kills are harness adapters running distinct,
  role-asymmetric operation scripts over the S6/S7 surface, **not** the real Supervisor /
  Dispatcher Core / Secretary — `docs/s9-fault-injection-harness.md` §2.2 states this honestly,
  and this record repeats it rather than reading past it. What is proven now is that three
  independently-crashing role processes with disjoint write-sets recover from SQLite alone with
  no double execution under real kills; *re-proven on the real implementation* will mean the same
  manifest and cases run through adapters over the real components' entrypoints, and the
  conformance battery every adapter must pass is what keeps the harness valid across that
  transition.
- **Provider:** `provider-independent` — a control-plane property: the item 4/5 cases bind no
  session provider, and `tests/fault_injection/test_import_graph.py` holds every harness module
  but the two adapters free of any `claude_org_runtime` import, on the syntax tree. Re-run for
  regression against a new provider, not redesigned (`ACCEPTANCE.md` §4).
- **Evidence:** `#15` (PR `#50` — `docs/s9-fault-injection-harness.md`, the design; PR `#51` —
  the harness) and `#16` (PR `#52` — the matrix), both landed 2026-08-20, on top of `#12` (S5),
  `#13` (S6) and `#14` (S7), landed 2026-08-19. What the suite demonstrates for this item: each
  of the three roles is SIGKILLed **separately** at all four deterministic points straddling the
  durable write (before the write; after the record, before the effect; after the effect, before
  its record; delivered, before the ack), and **in combination** — all three pairs and the
  triple, staggered kill orders included — at two of them, *before the durable write* and *after
  the effect, before its record*; the other two windows are exercised by single-role cases only,
  and that narrower combined coverage is stated here rather than implied away. On restart each
  role entrypoint **recovers before it proceeds** —
  reconstructing its view by query from SQLite alone, with no warm state across the restart (the
  command line and the database file are the whole input), re-establishing its lease and driving
  unfinished work to resolution; and exactly-once is evidenced by the dedup record on our side
  **and** the destination's own effect record, never by the absence of a visible duplicate. The
  store side stands as recorded when it landed: `#12`'s `RECONSTRUCTION_QUERIES` prove the
  reconstruction answerable by query alone from a fresh subprocess, and corrupt state is refused
  rather than recovered as empty (R3); `#14`'s `HandlerRegistry` refuses a handler that does not
  declare its exactly-once mechanism.
- **Residual:** three. **(1)** The role-process caveat above: item 4 *of the final components* is
  owed when the real processes exist — that debt is exactly what the D-0022 label carries, and it
  is stated here so the label cannot be read as a formality. **(2)** The `ACCEPTANCE.md` §2
  structural limit is unchanged and not claimed closed: SQLite alone cannot distinguish a
  completed side effect from one that never started, so each handler names its exactly-once
  mechanism (enforced mechanically since `#14`) or routes to a human gate (D-0004). **(3)**
  `Q-0001` and `Q-0002` are **parameterised, not answered**: resource names and collapse rules
  are per-case data, and manifest validation rejects at collection time any matrix that settles
  either question.

### Item 5 — lease, outbox resend, ack, dedup, single-writer confirmed by fault injection

- **Verdict:** `discharged` — recorded 2026-08-21, on evidence landed 2026-08-20.
- **D-0022 label:** `proven on the spike slice` — the mechanisms under test (S6/S7) and the spike
  adapter are throwaway; the durable half is the manifest, the barrier protocol, the invariant
  queries and the cases themselves (D-0014's rescue list, D-0026). The role-process caveat item
  4's row states applies here unchanged.
- **Provider:** `provider-independent` — control-plane property, as item 4.
- **Evidence:** `#15` (PRs `#50`, `#51`) and `#16` (PR `#52`), landed 2026-08-20, over `#13` (S6)
  and `#14` (S7), landed 2026-08-19, which carry one half of this item each — the exclusion half
  (the fencing token validated atomically as part of each protected write, refusals recorded as
  `action` rows, single-writer read back by query from the stamped epoch) and the outbox resend /
  ack / dedup half (lost ack resends without losing the message; duplicate and late acks change
  nothing; `retry_count` monotonic across a real process restart; no unfinished row left without
  a live owner, exported as `UNOWNED_OUTBOX_QUERY`). What `#16` adds is the verdict-bearing part:
  **all six §2 targets — Lease, Outbox resend, Ack, Dedup, Single-writer, Observation outage —
  automated** (55 cases at its landing, 59 with the `session-start` cases `#18` added), the §2
  table enforced as an injection-phrase checklist with zero exemptions. Kill points are
  deterministic and re-runnable in isolation; clock skew is driven both directions across the
  lease-expiry boundary and SIGSTOP through lapse-and-resume; every assertion is against a
  durable record — a SQLite query or a persisted incident field — and every external-effect case
  is additionally proven against the **destination's own** effect record. The observation-outage
  rows keep D-0006's two fact states distinct: an unreadable observation is classified
  `OBSERVATION_UNAVAILABLE` and a silent-but-readable one `NO_ACTIVITY_EVIDENCE` — neither is
  ever an anomaly, and neither produces a termination or restart recommendation. No manual one-shots
  anywhere: the suite runs in CI (full, fast and portable profiles).
- **Residual:** the three recorded before `#16` landed, one of them updated, plus one new pair.
  **(1)** The spike schema keeps one lease row per resource and no history table (`Q-0001`), so a
  wall-clock timeline is reconstructed from observed row states while the durable evidence is the
  epoch stamped on each fenced write (`docs/lease-fencing.md` §5). **(2)** Under clock skew two
  holders can overlap in *true* time and the rows cannot show it — each claimant stamps its
  acquisition in its own frame — which is precisely why a protected write validates the epoch and
  not the expiry. **(3) — updated with `#16`:** the destination is still the **spike stand-in**
  (`KeyedDropbox` and the refusing / duplicate-delivering wrappers built over it), and `#16` ran
  the whole matrix against that stand-in; re-proving the item against a **real** destination
  therefore now rests on the canary alone, no longer partly on `#16`. **(4) — new with `#16`,
  disclosed in its PR:** the incident-collapse "open-linked" reading (chain root = the row with
  NULL `related_incident_id`) is a harness-local convention — the schema assigns that column no
  semantics while `Q-0002` stays open — and `sigkill-expire` observes the returning holder
  refused at *acquire*, with the stale-token *write* refusal carried by `sigstop-expire`.
- **Notes:** D-0027 moves the fencing token and its tests from "belt and braces" to **the only
  exclusion in the system**. Item 5's single-writer cases are therefore where item 2's residual is
  actually carried — and the same harness is what `#18` re-armed for item 2's `session-start`
  cases, so the two verdicts share machinery, not evidence.

### Item 6 — `MessageBus` delivers and resends independently of the Agent View UI

- **Verdict:** `discharged`
- **D-0022 label:** `proven on the spike slice`
- **Provider:** `provider-independent` — by construction: the evidence is a delivery path with no
  import edge to any provider, and the stale-readout case was driven against the S3 stub (`#11`)
  precisely because item 6 is buildable against the stub alone — that is the no-edge property
  demonstrated, not a shortcut around C2.
- **Evidence:** `#19` (S8) — `src/claude_org_runtime/messagebus/` (the bus over the S7 outbox and
  the worker-outbound stdio MCP endpoint); `tests/messagebus/test_messagebus.py` and
  `tests/messagebus/test_endpoint.py` (send, first delivery dropped — in-process and over real
  stdio via `INTERLOCK_MESSAGEBUS_FAULT=drop-first-poll` — outbox resends, **exactly one** ack
  recorded, destination effect count 1); `tests/messagebus/test_stale_readout.py` (the translated
  stale case, below); `tests/messagebus/test_import_graph.py` (the static assertion, run by CI both
  in the full suite and as its own named step); `docs/messagebus-carry-drop.md` and D-0028 (the
  Q-0023 disposition this item was gated on).
- **The F1 caveat, in its now-stronger form — and it is a caveat, not a strength.** Per F1 there is
  no non-interactive path to deliver a message *into* a running background session, so the transport
  is necessarily **worker-outbound**: the worker connects to Interlock's bus as a client and
  delivery is a pull, not a push. Item 6's "with no Agent View UI attached" condition is therefore
  **trivially satisfied — the UI is not on the delivery path at all**. And under C2 there is **no
  Agent View UI to attach in the first place**. The condition is free for two independent reasons.
  **Two reasons a condition is free is not a stronger result; it is the same result, twice
  unearned.** Claiming item 6 as a strong result would be overclaiming twice over. What this
  discharge actually earns is the pair of non-free clauses: the stale-readout invariance and the
  CI-enforced no-edge assertion.
- **How the stale case was translated, not skipped.** The "UI attached but session state
  deliberately stale" wording names a UI that no longer exists, so the staleness is a provider
  readout that is stale or wrong: a session id whose child is gone (stub session stopped,
  `read_state` answering `exited-*` for a roster entry with no process), and a `read_state`
  answering "could not observe" (a running child that has announced nothing). Under each, the whole
  acceptance sequence — send, dropped first delivery, resend, ack, duplicate ack — records a
  transcript compared `==` against the same sequence run with no session backend in the process at
  all. Equality, not similarity, is the assertion.
- **How a later edge fails the build.** `tests/messagebus/test_import_graph.py` reads imports from
  the AST of every file under `src/claude_org_runtime/messagebus/` (absolute and relative, aliases
  included), refuses any name that reaches `claude_org_runtime.session` or a provider module, and
  guards its own vacuity (the package must exist and must import the control plane). It pairs with
  item 11's assertion: item 11 pins that the control plane knows no provider; this pins that the
  delivery layer built on it doesn't either — D-0009's split, structural on both sides.
- **Residual:** one failing specification (`xfail(strict=True)`) stands in
  `tests/messagebus/test_carried_specifications.py` — recipient aliasing, carried from the
  quarantined `test_store.py` — and the `carried-deferred` rows of `docs/messagebus-carry-drop.md`
  stay quarantined until their non-MessageBus successor surfaces exist (D-0028). The spike endpoint
  trusts its env-configured recipient identity; a real deployment needs the authenticated binding
  Q-0001/Q-0007 leave open. Per D-0026 the implementation is throwaway; the durable output is the
  suite and the assertion.

### Item 7 — unsaved artifacts protected from the managed worktree lifecycle

- **Verdict:** `discharged` — recorded 2026-08-21, on evidence landed 2026-08-18.
- **D-0022 label:** `proven on the spike slice` — the probe is an S4 throwaway; the durable half
  is `investigation/i02-conversation-probe.md` §3.5's recorded hashes and per-transition table.
  Re-proof against the shipping provider's real lifecycle is still owed (`ACCEPTANCE.md` §4 keeps
  item 7 on the re-run-in-full list).
- **Provider:** `C2 (claude -p subprocesses)` — CLI 2.1.234, one machine, one load.
- **Evidence:** `#7` (PR `#33`) — `investigation/i02-conversation-probe.md` §3.5. A git fixture
  carrying all three states the item names — uncommitted, untracked and unpushed — was driven
  through every lifecycle transition Interlock performs: thirteen foreground transitions (create,
  the resumes, a mid-turn stop, both supervisor kill-and-restart cases, the internals-free
  negative on both sides), each bracketed by a per-transition hash pair, plus four detached
  spawn-and-kill cycles bracketed by the cumulative baseline-to-final comparison. The tree came
  back **byte-identical by recorded hash** (`0b13bdcc…` at baseline and at the end), git state
  unchanged, the child's `cwd` sampled every 50 ms recording exactly one value, and no worktree
  created. The negative is **executed, not argued from "we did not pass `--worktree`"**: the A6
  trigger — a task forcing a file write, with edits permitted — was reproduced on `-p` in a
  second identical fixture, and the child edited the tree in place, relocating nothing and
  creating no worktree.
- **Residual:** two. **(1)** The negative covers the child acting **unasked**: a child instructed
  to run `git worktree add`, or configured into a worktree workflow, is Interlock's own policy
  surface, not the provider's — and item 7 is about the latter. **(2)** Under C2 the "cleanup"
  transition is Interlock's own deletion of the transcript, which per H5 also releases the
  session id — the cleanup/identity coupling is recorded under item 1's residual and binds the
  real cleanup design; nothing about it threatens the working tree, but it is the one lifecycle
  step this evidence could not exercise against a provider-owned implementation, because none
  exists.
- **Notes:** the item's tail clause — a provider able to reclaim a worktree without an interlock
  the control plane can observe or veto is a gate failure — is **not engaged on this surface**:
  under C2 there is no provider-managed worktree lifecycle at all, and A6's unasked relocation is
  an Agent View behaviour that does not reproduce on `-p`. Like item 3's D-0023 hole, that clause
  would re-engage the moment a provider that manages worktrees is adopted. `#7` was a phase-1b
  early exit that could have failed item 1 or item 7; it fired for neither. C2 was chosen partly
  on O8 — under C2 nobody else owns the working tree — and what was a *reason to expect* the item
  to pass is now backed by the executed negative rather than standing in for it.

### Item 8 — Secretary window responsiveness while workers are loaded

- **Verdict:** `rehearsed — not discharged` (2026-08-21). The rehearsal is **not** the discharge;
  the discharge carries *re-proven on the real implementation* and is still owed.
- **D-0022 label:** `proven on the spike slice`.
- **Provider:** `C2 (claude -p subprocesses)` — and note `ACCEPTANCE.md` §4 lists item 8 among the
  items that must be proven **in full** against whatever provider ships, so the discharge is
  against the shipping provider regardless of this rehearsal.
- **Evidence:** `#21` — a stub Secretary intake behind an explicit bounded queue boundary
  (`src/claude_org_runtime/secretary/`, throwaway per D-0026;
  `docs/secretary-intake-boundary.md` is the contract). **Structural:** `tests/secretary/` holds
  the intake package to a stdlib import allowlist (no dependency edge to `session/` or
  `dispatcher/`), bans blocking primitives from its syntax tree, holds the package lock-free
  outright (a lock is an implicit wait), and stalls each of the three named dependencies (worker
  monitoring, long-running work, AI judgement) while the intake answers. **Empirical**
  (`investigation/i16-item8-rehearsal.md`): with 8 workers at the spike-slice cap plus a
  long-running task — live `claude -p` children — and an incident parked awaiting a stub
  judgement, intake latency was unchanged from idle (medians ~0.001 ms both sides, recorded, not
  a threshold), and stayed unchanged while the supervisor thread was deliberately blocked for
  14.3 s. **The #6 blocking-`readline()` control, inconclusive there, is re-measured against live
  children:** one blocking read on a mid-turn child cost 13.01 s against a ~0.07 ms non-blocking
  whole-sweep of all nine — the U6 C2 fold-in measured rather than argued. Spend recorded:
  USD 2.82 across 47 spawns.
- **Discharge point:** **before the canary starts** (D-0022, D-0013).
- **Discharge point reached:** `no` — the canary has not started. A Secretary that blocks under
  load would invalidate the canary's own measurements.
- **Residual:** the numeric latency threshold is **unresolved** — `Q-0011`; nothing in the
  rehearsal states one and its numbers are not criteria. The real proof is against the real
  Secretary (durable intake, real Dispatcher AI judgement, genuine worker load) — the stub
  judgement here is an Event, and the spike-slice cap of 8 is the rehearsal's stated load, not a
  decided cap.
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

- **Verdict:** `rehearsed — not discharged` (2026-08-21). A **synthetic counterparty rehearsal**:
  the rehearsal is **not** the discharge, and the discharge carries *re-proven on the real
  implementation* and is still owed at the canary.
- **D-0022 label:** `proven on the spike slice`.
- **Provider:** `provider-independent` — the routing layer imports no provider (or any other
  Interlock module), asserted on the syntax tree; and `ACCEPTANCE.md` §4 requires item 10 re-run
  in full against whatever provider ships regardless, with live v1 as the counterparty.
- **Evidence:** `#23` — a run-start routing point above both systems and the provider, a
  run→owning-system ledger (separate store; append-only routing policy, insert-only run ledger,
  mid-flight owner change refused by trigger) and a writer audit over both stores, against a
  **synthetic** counterparty (`src/claude_org_runtime/canary/`, throwaway per D-0026;
  `docs/canary-routing-rehearsal.md` is the contract; `tests/canary/` the durable half). The
  end-to-end scenario routes **exactly one** new run to Interlock between a baseline run and a
  post-rollback run on the stand-in, finishes a v1-started run mid-canary with its owner
  untouched, shows a writer audit with **no record written by both systems**, and rehearses a
  rollback whose entire footprint is one appended `routing_decision` row — both run stores and
  the run ledger canonically byte-identical across it. Every output carries the rehearsal marking
  naming the canary as the discharge point.
- **Discharge point:** **at the canary itself** (D-0022).
- **Discharge point reached:** `no` — the canary has not run. The item passes when canary runs complete
  with exactly one owner per run, no record written by both systems, and a real rollback that
  changes only routing.
- **Residual:** canary duration, sample size and numeric go/no-go criteria are **unresolved** —
  **Q-0005 remains open**; nothing in the rehearsal states one and none of its numbers is a
  criterion. Q-0005 also holds the undecided case of runs already in flight *on Interlock* at
  rollback: the rehearsal shows only that the rollback itself does not touch such runs, and
  deliberately provides no API in which a policy about them could be expressed. The counterparty
  is synthetic, so nothing here exercises v1's real write paths, load or failure modes — item
  10's real proof needs v1 live, which needs the implementation to be running, which is why it is
  deferred rather than discharged up front.
- **Scoped exception:** see §4. Deferred, not waived.

### Item 11 — even if the provider does not hold, only the `SessionProvider` need be swapped

- **Verdict:** `discharged`
- **D-0022 label:** `proven on the spike slice`
- **Provider:** `provider-independent` — by construction: the item measures the *absence* of
  provider detail in the control plane.
- **Evidence:** `#10` (S1, the provisional `SessionProvider` interface), `#11` (S3, the stub
  provider over local child processes), `#20` (re-run the control-plane suite unchanged against S3).
  `#10` and `#11` landed 2026-08-19; `#20` landed 2026-08-20. Both halves are needed: a stub that
  exists proves nothing until an unmodified suite runs against it.
- **What `#20` measured.** `tests/gate_item11/` runs the control-plane suite
  (`tests/control_plane/`, 184 cases) twice as subprocesses — once plain, once with
  `tests/gate_item11/provider_plugin.py` binding a live S3 session for the whole run — and compares
  the collected test ids, the per-phase outcome of every one of them, and the SHA-256 of every file
  each run read. All three are identical, which is `#20`'s fourth criterion (*the same suite
  artifact, differing only in provider fixture*) evidenced rather than asserted. The provider is
  **qualified before collection starts** — the plugin binds a session, writes it into S5 under a
  fencing token through the adapter and delivers one acked effect about it, aborting the run if it
  cannot — so the comparison is not one a provider the control plane could not use would also pass.
  **Zero test modifications were required**: no file under `tests/control_plane/` or
  `src/claude_org_runtime/control_plane/` is touched by the commit that discharges this item.
  `tests/gate_item11/test_substitution_scenarios.py` drives the other direction — sessions really
  started by S3, bound into S5's source of truth through the one adapter that knows both
  vocabularies — so that "the suite does not need a provider" is not satisfied by a control plane
  that could not use one.
- **How a later leak fails the build.** The CI workflow runs the control-plane suite a second time
  with the provider bound, and `tests/gate_item11/test_no_provider_detail_leaks.py` widens
  `tests/control_plane/test_lease.py`'s import-edge assertion from S6 alone to the whole
  control-plane package and the whole suite, adds *nothing under `src/` may import both a session
  backend and the control plane*, and discovers `SessionProvider` implementations so that one
  shipping without a registry entry fails the build rather than silently narrowing the measurement
  back to the provider it was already known to pass.
- **Residual — discharged 2026-08-21:** the S2 half of `#20`'s fourth criterion. `#17` landed and
  the registry tripwire forced the S2 row exactly as designed: `tests/gate_item11/registry.py` now
  carries S2, so the unchanged-suite comparison and the substitution scenarios both run against S3
  *and* S2 from the same artifact. No test under `tests/control_plane/` was modified; the only edits
  were the registry entry and a backend-availability skip in the two parameterised fixtures — on a
  machine without the claude CLI the whole S2 row skips (as the bwrap-dependent sandbox tests do),
  never an individual test under a bound provider. The leak clause below still has nothing recorded
  against it.
- **Notes:** D-0020's B+ ordering — S3 written before S2 — exists so that item 11 measures a
  structural property rather than a retrofit. The C1→C2 switch is the first real test of D-0019's
  promise that a gate failure costs a provider and not a design, and item 11 is where that promise
  is measured rather than asserted. Any test that has to be **modified** to run against a provider
  marks a leak of session-backend detail into the control plane and must be fixed — not annotated,
  not skipped, not marked expected-fail — before the item passes.

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
| Tests — fault injection, recovery, accident-derived fixtures, the control-plane suite | **durable (tests)** | `#15`, `#16`, `#18`, `#19`, `#20`, `tests/curator/`, `tests/fencing/`. `#15` and `#16` landed 2026-08-20: `tests/fault_injection/` — the controller, barrier protocol, manifest, conformance battery and the §2 matrix cases are the durable half; the spike driver is the throwaway adapter its design names. `#20` landed 2026-08-20: `tests/gate_item11/` is durable test material — the provider registry, the substitution adapter and the two plugins are the fixture half a second provider re-uses, not an implementation to throw away with S3 |
| S2 — the C2 `SessionProvider` | throwaway | `#17` — landed 2026-08-21: `src/claude_org_runtime/session/claude_cli_provider.py`, tests `tests/session/test_claude_cli_provider.py` (hermetic, against a fake CLI — the failure shapes a live healthy CLI will not produce on demand), plus the S2 registry row in `tests/gate_item11/`. Identity read-back and refusal only: the `already in use` refusal is never a lock (U27/U38), `--resume` is unguarded (U32), and exclusion stays with the lease — orchestration and the crash-window proof are `#18`'s |
| S3 — the stub provider | throwaway | `#11` — landed 2026-08-19: `src/claude_org_runtime/session/stub_provider.py`, tests `tests/session/test_stub_provider.py`. Local child processes only: no Claude CLI, no network. Throwaway under D-0026, but it survives a C2 switch untouched |
| S4 — the probe harnesses | throwaway | `#6`, `#7` — landed 2026-08-18: `investigation/i01_supervisor_probe.py`, `investigation/i02_conversation_probe.py` and the two `*_internals_free_negative.sh` drivers are throwaway; their records `investigation/i01-supervisor-probe.md` and `investigation/i02-conversation-probe.md` are evidence, kept as the basis of the item 1 and item 7 verdicts |
| **S5 — the spike SQLite schema** | **throwaway — named explicitly by D-0026** | `#12` — landed 2026-08-19: `src/claude_org_runtime/control_plane/spike_schema.sql`, tests `tests/control_plane/`. The file carries its own note, at the top, that it is a spike schema and that **no migration path is promised from it**, and the loader refuses DDL whose marking has been removed. A database at another revision is **refused, never migrated**. `Q-0001` stays open and is not answered by inertia: no column, CHECK or index names a component or a role, and `Q-0002` stays open too — `dedup_key` is indexed but not unique, so neither collapse rule is forced |
| S6 / S7 — lease and outbox implementations | throwaway (their tests are durable) | `#13` — landed 2026-08-19: `src/claude_org_runtime/control_plane/lease.py`, tests `tests/control_plane/test_lease.py`, written record `docs/lease-fencing.md`. Throwaway under D-0026 and it survives a C2 switch untouched — it is Interlock's own obligation regardless of provider (D-0024), and after the fence search it is the only exclusion there is. `Q-0001` stays open: `holder` is an opaque claimant identity and never a role. `#14` — landed 2026-08-19: `src/claude_org_runtime/control_plane/{outbox,handlers,destination}.py` is throwaway, `tests/control_plane/test_outbox.py` is the durable half |
| S8 — `MessageBus` MCP endpoint | throwaway (the no-edge assertion is durable) | `#19` |
| Stub Secretary intake (item 8 rehearsal) | throwaway implementation, durable tests and boundary contract | `#21` — landed 2026-08-21: `src/claude_org_runtime/secretary/` is throwaway; `tests/secretary/` (the structural and behavioural assertions) and `docs/secretary-intake-boundary.md` (the contract the real Secretary and `#29` build against) are the durable half; `investigation/i16_item8_rehearsal.py` is an S4-style throwaway harness and `investigation/i16-item8-rehearsal.md` its record |
| Routing point, run-owner ledger, writer audit (item 10 rehearsal) | throwaway implementation, durable tests and contract | `#23` — landed 2026-08-21: `src/claude_org_runtime/canary/` (the routing point, the ledger with its own SQLite file, the audit and the synthetic counterparty) is throwaway; `tests/canary/` and `docs/canary-routing-rehearsal.md` (the contract the real canary cutover builds against) are the durable half. The ledger stays out of Q-0001's territory: `owning_system` names a system, never a component, role or lease holder |
| S10 — per-role fencing renderer, `PreToolUse` deny hook, breach-probe battery | throwaway implementation, durable tests | `#9`; implementation `src/claude_org_runtime/fencing/`, tests `tests/fencing/`. Landed 2026-08-18; nothing here is promoted by having discharged item 3 |
| Session-binding phases and lease-before-spawn orchestration (item 2, crash window) | throwaway implementation, durable tests and protocol contract | `#18` — landed 2026-08-21: `src/claude_org_runtime/control_plane/session_binding.py` and `src/claude_org_runtime/supervisor/session_orchestrator.py` are throwaway (the `binding_phase` column is a spike expression of the injection seams, not an answer to `Q-0001`); the durable half is `tests/gate_item2/`, the schema constraints in `tests/control_plane/test_spike_schema.py`, the `session-start` contract vocabulary, cases and assertion arms in `tests/fault_injection/` (with `session_driver.py` as the second adapter, replaceable like the first), and `docs/crash-window-orchestration.md` (the protocol the real supervisor builds against). `investigation/i18_recharacterisation.py` is an S4-style throwaway probe and `investigation/i18-crash-window-characterisation.md` its record |
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
