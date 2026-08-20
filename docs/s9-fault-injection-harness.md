# S9 — fault-injection harness design: process contract, two-phase kill barrier, case matrix

**Scope.** Design for S9 (Issue `#15`, plan id I-10), phase 5 of the Agent View spike. This document
resolves the pre-implementation design-review findings **B1, B2 and M1–M6** raised against Issue
`#15` before any implementation is dispatched. It defines the harness that I-11 (`#16`, the
`ACCEPTANCE.md` §2 matrix), I-13 (item 2 crash-window proof) and I-15 (item 11 re-run) all sit on.
**No implementation ships with this document.**

**Status: design for a durable test artifact (D-0026).** The harness core — controller, barrier
protocol, manifest, invariant queries, the tests themselves — is **durable**: D-0014's rescue list
names fault injection and recovery tests as exactly the tests worth carrying. The piece that binds
the harness to today's S6/S7 spike internals is a single **adapter** and is throwaway with them
(§6). Nothing here adds a `D-` entry or closes a `Q-`; where a new ruling would be needed, the path
is escalation, not this file (§10).

**Cross-references** are by stable IDs (`D-00NN`, `Q-00NN`, gate-item numbers, `AC-N`, checkpoint
constant names), never by line number.

---

## 1. What the harness is for, and what it is not

Gate item 5 forbids manual one-shot demonstrations: the gate passes only if every case is automated
and reproducible. The harness is therefore itself a gate artifact. Its obligations, from Issue `#15`:

- deterministic injection at the three `ACCEPTANCE.md` §2 points — **before the durable write**,
  **after the write and before the side effect**, **after the side effect and before its result is
  recorded** — plus the fourth point the outbox rows add (**delivered, before the ack is
  recorded**);
- clock skew, forward and backward, across the lease expiry boundary;
- SIGSTOP (pause a holder, let its lease lapse, resume it);
- each of Supervisor / Dispatcher Core / Secretary killed **separately and in combination**
  (gate item 4);
- a failing case re-runnable in isolation, no human in the loop, and a sane CI wall-clock budget.

What the harness is **not**: it is not the matrix (that is I-11's issue), not the recovery
implementation (the components own their recovery), and not a proof that today's spike components
*are* the real Supervisor / Dispatcher Core / Secretary — §2 is explicit about what is proved now
versus what is re-proved when I-12/I-14 land.

---

## 2. B1 — what S9 runs against now: the role-process contract

**The finding.** Nothing on `main` is a Supervisor, Dispatcher Core, or Secretary *process*. S6 and
S7 are libraries; the real provider (I-12, S2) and the `MessageBus` (I-14, S8) land later. Killing
one in-process function three times under three names would prove nothing about item 4, which is
about three independently-crashing components.

**The resolution** is in two parts: a **process contract** the harness enforces from day one, and an
honest statement of **which claim is proved when**.

### 2.1 The contract

Every component under test is a **role process**:

1. **Independent PID.** Spawned by the controller as a separate OS process
   (`subprocess.Popen([sys.executable, "-m", <driver module>, ...])`), in its own session
   (`start_new_session=True`, see §8). Never a thread, never an in-process call.
2. **Independent SQLite connection.** Each role process opens its own `sqlite3.connect()` against
   the shared database file. Connections are never inherited across `fork`/spawn and never shared
   between roles. A SIGKILL therefore takes down a *connection* mid-transaction, which is the crash
   SQLite's journal has to recover from — the thing an in-process exception can never produce.
3. **Own lease identity.** Each role process acquires and writes under its own lease
   (`resource`/`holder` supplied by the case definition, §4). Which role may hold which resource is
   `Q-0001` and stays open: resource names are **per-case data**, not a role table baked into the
   harness.
4. **Restart entrypoint.** Restart is re-executing the same command line with the same database
   path and role identity plus a `--restart-generation N` argument. The entrypoint's contract: on
   start it must **recover before it proceeds** — reconstruct its view by query from SQLite alone
   (D-0001), re-acquire or re-establish its lease, resume unfinished work (for an outbox writer:
   drive the due/unowned rows to resolution) — and only then continue its operation script. There
   is no warm state handed across the restart; the command line and the database file are the whole
   input.
5. **Scripted, role-asymmetric work.** The three roles do not run the same function under three
   names. Each runs a distinct **operation script** over the S6/S7 surface, shaped like the real
   component's write-set:
   - **Supervisor script** — owns the run/session-binding style writes: acquire its lease, insert
     rows binding an identity, renew, hand off — **plus** one externally-effecting action driven
     through its own outbox `attempt` (a spawn-notification effect to its own destination).
   - **Dispatcher Core script** — the delivery loop: hold the writer lease, take due outbox rows
     through `attempt` (record → effect → result), the path all four checkpoints live on.
   - **Secretary script** — the intake/ack side: enqueue messages, record acks, exercise dedup —
     **plus** one externally-effecting action through `attempt` (an ack-report effect to its own
     destination).

   The scripts touch different tables, rows and leases, so a combination case (§5) exercises a
   cross-role interleaving that a single renamed process could not produce. The added
   `attempt`-driven action in the Supervisor and Secretary scripts is not decoration: gate item 4
   requires **all three** kill windows for **each** of the three components, and the two mid-call
   windows exist only on a record → effect → result path. Every role script must expose all
   mandated windows through at least one of its operations — that is a contract requirement the
   conformance battery (§6.3) checks per role, so I-11 can arm any required (role, window) pair
   without a manifest-validation dead end.

### 2.2 What is proved now, and how it stays valid

Honestly stated: until I-12 and I-14 land, S9 proves that **the harness machinery is sound and the
S6/S7 invariants hold under real kills for three concurrently-writing role processes with disjoint
write-sets**. It does not prove item 4 *of the final components* — that proof belongs to I-11's
matrix run and is re-established when the real processes exist. What keeps S9 valid across that
transition:

- The durable tests speak only to the **fault-runner contract** (§6). Today's role driver is an
  adapter over S6/S7; when the real components exist, adapters over their real entrypoints
  replace it, and the manifest, barrier protocol, invariant observables and tests are unchanged.
  **The re-proof has owners, not a vague future**: the plan runs I-11 (the matrix) *before* I-12
  and I-14, so the first issues in which a real component and this harness coexist are **I-13**
  (item 2 crash-window proof, `depends_on: I-12, I-11` — the real-provider adapter and the
  kill-window re-run against it belong there; the plan already names I-13 as machinery re-run on
  this harness) and **I-15** (item 11 re-run, `depends_on: I-13, I-14` — where the S8
  `MessageBus` adapter obligation lands). Neither issue's current body states the adapter
  deliverable explicitly; this document cannot re-scope another issue, so that gap is flagged for
  escalation in §10 rather than papered over here.
- A **conformance battery** (§6.3) is part of the harness: every adapter — today's spike driver and
  every future real one — must pass it before its cases count. It asserts the contract itself
  (every checkpoint reachable, barrier round-trip works, restart entrypoint recovers, injected
  clock honoured), so "the harness ran" can never silently mean "the adapter faked it".
- Checkpoint names are the S7 constants (`CHECKPOINT_BEFORE_DURABLE_WRITE`,
  `CHECKPOINT_AFTER_RECORD_BEFORE_EFFECT`, `CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD`,
  `CHECKPOINT_DELIVERED_BEFORE_ACK`) mirrored into the contract as **contract vocabulary** (§6.2),
  so a future component that renames its internals still has to expose these four windows by these
  names.

---

## 3. B2 — the two-phase kill barrier: a real SIGKILL, never an exception

**The finding.** S7's `checkpoint` callable was designed so that *raising from it* kills a
delivery. That is fine for S7's unit tests and disqualifying for S9: an exception unwinds the
stack, runs `with`-blocks and `finally` clauses, and leaves the SQLite connection alive to be
closed in an orderly way. None of that happens in a crash. `ACCEPTANCE.md` §2 demands a mid-flight
**kill**; simulating it with control flow proves durability of a process that was never in danger.

**The resolution.** The checkpoint hook never raises and never kills. It implements phase one of a
two-phase barrier; the kill is phase two, and it is a real signal from outside the process.

### 3.1 The protocol

Controller and role process communicate over two inherited pipes dedicated to the protocol
(control in, events out — line-oriented JSON, one object per line). At spawn the controller tells
the role process which checkpoints are **armed** for this case (operation × checkpoint name ×
occurrence index, since a loop passes the same point repeatedly).

**Checkpoints are per-operation windows, not an `Outbox.attempt` internal.** The four S7
constants name *kinds of window*, and every operation a role script performs exposes the windows
that exist for it, through the same hook: the driver invokes the hook at the write boundaries of
**each** API call — `before_durable_write` immediately before the call that commits, the
after-write windows where they exist — so Supervisor and Secretary scripts (lease acquire/renew,
`enqueue`, ack recording) can reach a barrier exactly like the delivery loop can. Inside
`Outbox.attempt` the adapter binds the hook to S7's own `checkpoint` callback, which is the only
place the two mid-call windows (`after_record_before_effect`, `after_effect_before_record`)
physically exist; operations with no side effect (enqueue, ack) have only the before/after-write
windows. The contract carries this **applicability matrix** (operation × checkpoint) as data, and
manifest validation refuses a case that arms a window its operation does not have — a barrier
that cannot be reached is a manifest error caught at collection, never a timeout in CI.

In addition to checkpoints, scripts may declare named **sync points**
(`{"event": "sync", "name": ...}`, e.g. `lease-acquired`) — barrier-capable like checkpoints but
marking script progress rather than a durable-write window. They exist so a fault can be anchored
to a known state ("the holder has its lease and is between operations") when no write window is
the right anchor; §8's SIGSTOP cases require one.

- **Unarmed checkpoint:** the hook returns immediately. No protocol round-trip, no timing
  perturbation on windows the case is not about.
- **Armed checkpoint:** the hook writes
  `{"event": "checkpoint", "name": <constant>, "occurrence": n}` to the event pipe, then **blocks
  reading the control pipe**. It holds no new locks, touches no SQLite state, and allocates
  nothing interesting — the process is frozen mid-window with its transaction exactly as the
  operation script left it.
- **Phase two, kill case:** the controller receives the checkpoint event, waits for the case's
  full barrier condition (§5), then issues `os.kill(pid, signal.SIGKILL)`. No reply is ever
  written to the control pipe; the blocked read is torn down by the kill itself. The controller
  then `Popen.wait()`s and **asserts the exit status is `-SIGKILL`** — a role process that exited
  any other way fails the case as a harness error, not as a component error.
- **Phase two, pass-through case:** for cases that only need synchronisation (e.g. hold a holder
  at a point while a sibling acts, then let it proceed), the controller replies
  `{"cmd": "continue"}` and the hook returns.
- **Clock command:** `{"cmd": "set_clock_offset", "offset_ms": ...}` may be sent while a process
  is blocked at an armed checkpoint (§7).

The protocol carries a `protocol_version` in the spawn handshake; it is part of the fault-runner
contract version (§6.2).

### 3.2 Why this shape

- The **kill is indistinguishable from a crash** by construction: SIGKILL is uncatchable, nothing
  unwinds, the connection dies with the process, and SQLite recovery on the next open is the real
  recovery path.
- The **window is exact**: the process is stopped *inside* the named window, not "somewhere near
  it" by a sleep race. Determinism comes from the barrier, not from timing.
- The **hook stays trivial**: write one line, read one line. It cannot deadlock against the
  operation script's own locks because it takes none, and it cannot corrupt the window it is
  measuring because it does no database work.
- A barrier that is never released is the **timeout/cleanup path's** problem, and that path is
  specified in §8 — the controller never waits unbounded.

---

## 4. M1 — case identity: the matrix is enumerated, the seed is subordinate

**The finding.** Issue `#15` says "the same seed hits the same point", which reads as if the seed
*selects* injection points. If it did, adding a case, reordering an enumeration, or a different
Python hash seed would silently change what every seed means.

**The resolution.** Injection points are never sampled. The matrix is an **explicit, checked-in
enumeration**, and the seed's authority is confined to payload and schedule.

### 4.1 `case_id`

Every case has a stable identifier with the grammar

```
<targets>__<operation>__<checkpoint>__<fault>[__<variant>]
```

- `targets` — ordered role set: `sup`, `disp`, `sec`, `sup+disp`, `disp+sec`, `sup+sec`,
  `sup+disp+sec`.
- `operation` — which operation script step the checkpoint is armed on (e.g. `attempt`,
  `enqueue`, `ack`, `lease-renew`).
- `checkpoint` — the anchor the fault is injected at: one of the four contract checkpoint names
  (§6.2) or a named script **sync point** (§3.1). **Every fault is anchored**; there is no
  unanchored kind. In particular `sigstop-expire` anchors at a sync point such as
  `lease-acquired`: the controller sends SIGSTOP only while the holder is provably blocked at
  that barrier (already holding its lease, between operations), then drives the claimant, then
  `SIGCONT` + `continue` releases the holder — the process, being stopped, cannot consume the
  `continue` until it is resumed, so the pause/takeover/return race is a deterministic sequence,
  not a scheduling accident. Pure clock-skew cases anchor the same way (§7).
- `fault` — the fault kind: `sigkill`, `sigstop-expire`, `clock-fwd`, `clock-back`,
  `drop-delivery`, `dup-delivery`, `lost-ack`, `staggered-sigkill` (§5).
- `variant` — a short slug, present **whenever two cases would otherwise share the first four
  segments**: a different occurrence index of the same checkpoint, a different per-role checkpoint
  assignment in a combination case, a different kill/restart order, or a different clock
  programme each get their own variant slug (e.g. `occ2`, `killorder-ds`, `skew-claimant`).

The four leading segments are a *classification*, not the whole identity; the identity rule is
that **`case_id` is unique across the manifest**, enforced at collection time (a duplicate fails
the run before any case executes). Every field of the case entry that is not derivable from the
`case_id` lives in the manifest entry the `case_id` keys — so `case_id + manifest_version` always
denotes exactly one fully-specified case, which is what the re-run and failure-report contracts
below rely on. `case_id` is the re-run key, the manifest key, and the failure-report key. It never
encodes the seed.

### 4.2 The manifest

The matrix lives in a checked-in **manifest** (data file under the harness tree, §6.1): a literal
list of case entries, each carrying its `case_id`, target set, armed checkpoints per role,
barrier/kill/restart semantics (§5), clock programme (§7), expected-invariant query names, and the
OS lane it belongs to (§8). Two rules keep it honest:

- **No generation at collection time.** A helper may *produce* candidate products
  (targets × operation × checkpoint × fault), but the manifest is the frozen literal, and a test
  asserts the generator's output equals the frozen list — so adding or pruning a case is always an
  explicit, reviewable diff, never a side effect of an enumeration change.
- **Versioned.** The manifest header carries `manifest_version` (integer, bumped on any semantic
  change) and the fault-runner contract version it targets. A failure report always includes both.

The manifest is **not fully populated by S9**. S9 ships the schema, the seed set needed to prove
the harness (at least one case per fault kind, per checkpoint, per lane, plus the §5 combination
seed set), and the generator-freeze test. Populating the full `ACCEPTANCE.md` §2 matrix is I-11's
deliverable, on this schema.

### 4.3 The seed

- One **suite seed** per run (an integer; from CI it is fixed and recorded in the run header, a
  local run may pass any value).
- The **per-case seed** is derived, order-independently and platform-independently, as
  `sha256(manifest_version || case_id || suite_seed)` truncated to 64 bits. Adding cases does not
  shift any other case's stream; Python hash randomisation and OS differences are irrelevant by
  construction.
- The seed's authority is **payload and schedule only**: message payload bytes, dedup-key salt,
  jitter in the operation scripts' step interleaving *where the case declares the order free*.
  It never chooses the checkpoint, the fault, the target set, or the kill/restart order — those
  are the case's identity, and they are fixed in the manifest.

### 4.4 Single-case re-run contract

A failing case prints one reproduction line:

```
S9-REPRO case_id=<...> suite_seed=<...> manifest_version=<...> contract_version=<...> resolved_skew_ms=<...>
```

and the re-run is `pytest <harness path> -k <case_id>` with the suite seed supplied via a single
documented environment variable. Same `case_id` + same suite seed + same manifest version ⇒ same
armed windows, same payloads, same schedule decisions. That is the whole determinism claim, and it
is testable (the conformance battery re-runs one case twice and asserts identical event traces).

---

## 5. M2 — combination semantics: what "in combination" means, fixed in the manifest

**The finding.** "Separately and in combination" is under-specified: 3 components give 7 non-empty
subsets before saying anything about simultaneity, kill order, or who recovers first — and those
choices change both the meaning and the cost of the matrix.

**The resolution.** Combination semantics are **manifest fields with closed vocabularies**, not
conventions:

- `targets` — the ordered non-empty subset under test. All 7 subsets are in scope for the aligned
  mode below.
- `barrier` — per-target armed checkpoint. Mode **`aligned`**: the controller waits until *every*
  target has reported its armed checkpoint and is blocked, **before any kill is issued**. All
  targets are frozen inside their windows simultaneously; the kill set is then applied to a system
  in a known joint state. This is the default meaning of "in combination".
- `kill_order` — the explicit order in which the controller issues SIGKILL after the barrier is
  complete. Because every target is already frozen at its checkpoint, aligned-mode kill order
  cannot change the components' states — it is recorded so the case is fully specified and so the
  same field serves staggered mode, where it does matter.
- Mode **`staggered`** (fault kind `staggered-sigkill`): kills are *not* barrier-simultaneous —
  target A is killed at its checkpoint, the controller then releases or observes target B running
  past A's death, and kills B at a later armed checkpoint. This models "component dies, sibling
  keeps operating against the survivor state, then dies too". Staggered cases are strictly
  enumerated (each one names its full sequence in the manifest); S9 seeds the set with the two
  sequences the acceptance surface cares most about — sender killed after-effect-before-record
  then Secretary killed before recording the ack, and lease holder killed then its successor
  killed mid-first-write — and I-11 extends it deliberately, never by product.
- `restart_order` — explicit ordered list (default: same as `kill_order`). Restart is sequential:
  the controller starts target N+1 only after target N's entrypoint has signalled
  recovery-complete (a protocol event), so each case pins which component recovers into which
  intermediate state. Concurrent-restart cases, if I-11 wants them, are a distinct
  `restart_order: concurrent` value, not an ambiguity.
- `expected` — the invariant observables this case asserts (by name, from the contract's set,
  §6.2), plus the **expected recovery owner**: which restarted role's recovery is asserted to
  have driven each invariant back to health. "Somebody recovered it" is not an assertion. A note
  on the lease invariant: the spike schema keeps **one mutable lease row per resource and no
  history table** (see `docs/lease-fencing.md`), so "at most one live holder across the whole
  timeline" is **not provable by a final-state lease query**, and the contract does not pretend
  it is. The timeline property is asserted through what *is* durable: the epoch attribution the
  fenced writes leave on the rows they touched (`linear-writer-history`: per resource, applied
  writes carry a non-interleaved epoch sequence), the required **recorded refusal** of the stale
  writer's attempt, and the destination observer where the write had an external effect. The
  refusal record is held to `ACCEPTANCE.md` §2's own standard: a control-plane observable is a
  SQLite query or a persisted field, so the contract names a `recorded-refusals` **SQL query**
  and a harness event-trace line is *not* accepted as the evidence (the trace proves the harness
  saw an exception, not that the refusal is durable). Providing the durable record is the
  driver's obligation: the spike driver appends the refusal (resource, holder, stale epoch,
  statement kind, `now_ms`) to a harness-owned, append-only refusal table
  before proceeding — deliberately outside the fence, because it records a *failure to write*
  control state rather than control state itself, and explicitly harness-scope: it is part of the
  throwaway adapter's schema footprint, not a resolution of `Q-0001` or a change to the S5 spike
  schema's control tables. No lease-history table is added.

  **Where that table lives, corrected against the implementation (S9, Issue `#15`).** This
  paragraph originally said "in the same database". It cannot be: `open_control_plane` verifies a
  sha256 fingerprint over *every* object in `sqlite_master` — tables, indices and triggers, with
  their stored DDL text — so a harness table added to the control-plane database makes the next
  open refuse the whole file with `CorruptStateRefused`, and D-0026 promises no migration path to
  repair it with. The ledger is therefore a **sidecar SQLite file beside the control plane**
  (`<workdir>/harness-refusals.sqlite3`, one per case). Everything else this paragraph requires is
  unchanged: append-only, harness-owned, written outside the fence and with its own connection,
  and read back by the `recorded-refusals` **named SQL query** — a persisted, query-answerable
  record, which is the standard `ACCEPTANCE.md` §2 actually sets. It adds no object to
  `spike_schema.sql`, so the fingerprint and every existing database stay valid, and it resolves
  no `Q-`. The sidecar also carries the refusal classes S6 records nowhere (`LeaseHeld`,
  `LeaseNotHeld`, `ClockSkewRefused`) and the S7 `enqueue` refusal, whose `action.kind` is not
  composed by `effect_kind` and therefore cannot be attributed to a resource by query — which is
  the other half of why the driver, and not S6/S7, owns this record.

Scale is controlled by policy, not by product: aligned combination cases cover all 7 subsets ×
a curated set of (operation, checkpoint) pairs chosen where roles genuinely interact (the delivery
loop's four windows against the intake script's enqueue/ack), not the full cross-product. The
pruning rule is recorded in the manifest header; anything pruned is listed, not silent.

---

## 6. M3 — the versioned fault-runner contract: durable tests, throwaway internals

**The finding.** D-0026 makes the tests durable and the S5–S7 implementations throwaway. A harness
that imports `Outbox` internals is destroyed with them — or worse, preserves them by making the
spike schema load-bearing for the gate record.

**The resolution.** One seam, versioned, with the durable side owning the vocabulary.

### 6.1 Layout

```
tests/fault_injection/
  contract.py        # the fault-runner contract: vocabulary, versions, invariant queries — DURABLE
  controller.py      # spawn/barrier/kill/restart/cleanup engine — DURABLE
  manifest.py|.json  # the case matrix (§4.2) — DURABLE
  conformance.py     # the adapter conformance battery (§6.3) — DURABLE
  test_*.py          # the cases — DURABLE
  spike_driver.py    # role driver binding the contract to S6/S7 — THROWAWAY (dies with S5–S7)
```

`spike_driver.py` is the **only** module allowed to import from
`claude_org_runtime.control_plane`; a test enforces that (an import-graph assertion over the
harness tree), so the coupling cannot spread by convenience.

### 6.2 The contract

`contract.py` defines, as data and ABCs, everything a driver must satisfy:

- `FAULT_RUNNER_CONTRACT_VERSION` (integer). The spawn handshake carries it; controller and driver
  refuse a mismatch. Any change to the checkpoint vocabulary, protocol messages, or CLI of the
  driver bumps it.
- The **checkpoint vocabulary**: the four names, owned by the contract. Today they textually equal
  S7's constants and a test in `spike_driver`'s battery asserts that equality; when S7 is
  discarded, the contract names survive and the next adapter maps its internals onto them.
- The **driver CLI**: `--role`, `--db`, `--case-id`, `--suite-seed`, `--armed` (checkpoint ×
  occurrence list), `--clock-base-ms`, `--clock-offset-ms`, `--restart-generation`, plus the two
  protocol file descriptors.
- The **protocol messages** (§3.1) and the recovery-complete event.
- The **invariant observables**, in two kinds, both named and both required:
  1. **Named SQL queries** over the control-plane store (in the same spirit as S5's
     reconstruction queries and S7's unowned-outbox query): the durable tests assert through
     these names; the adapter maps them to the schema of the day. When the schema is thrown away
     with S5, the queries are re-bound, the assertions are not rewritten.
  2. **A destination observer.** `ACCEPTANCE.md` §2 is explicit that for an external effect,
     SQLite alone cannot prove exactly-once — the `after_effect_before_record` window is exactly
     the window where our rows are silent. The contract therefore includes a destination-side
     interface (`effect_count(idempotency_key)`, `attempt_count(idempotency_key)`, mirroring
     S7's `Destination` protocol), and every case that kills inside or after an effect window
     **must** name a destination assertion, not only SQL — manifest validation enforces it. The
     harness destination must be **durable across the role kill and out-of-process relative to
     the killed role**: today's in-process `KeyedDropbox` does not qualify; the spike adapter
     supplies a file- or separate-SQLite-backed dropbox whose store the controller reads
     directly after the kill, so the evidence is the destination's own record, never a re-derivation
     from control-plane rows.

### 6.3 The conformance battery

`conformance.py` is a parametrised suite run against **every** adapter, present and future:
each checkpoint is reachable and blocks; the barrier round-trip works; SIGKILL at each checkpoint
yields exit `-SIGKILL` and a database the invariant queries can be run against; the restart
entrypoint emits recovery-complete and is idempotent (restarting twice changes nothing); the
injected clock is honoured (a `set_clock_offset` visibly moves the driver's reported `now_ms`);
two runs of one case with one seed produce identical event traces. An adapter that has not passed
conformance cannot contribute matrix results — this is the mechanical form of §2.2's "stays
valid" claim, and it is what I-12/I-14 adapters will be built against.

---

## 7. M4 — the clock model: injected, per-role, boundary-relative

**The finding.** "Skew the clock forward and backward" says neither *whose* clock, *which* clock,
nor *by how much* — and a host-clock change is a CI-hostile non-answer.

**The resolution.**

- **The host clock is never touched.** Neither wall nor monotonic, on any lane.
- **The injected clock is fully virtual.** Every S6/S7 API already takes `now_ms` as an argument
  and the database holds no clock of its own; the driver supplies every `now_ms` from a single
  per-process `Clock` object: `now_ms() = clock_base_ms + advance_ms + offset_ms`, where
  `clock_base_ms` is a **fixed constant from the manifest** (`--clock-base-ms`), `advance_ms`
  grows only by **script-declared deterministic increments** (each operation step advances the
  clock by an amount the script states, seeded jitter allowed under §4.3's rules), and
  `offset_ms` starts at `--clock-offset-ms` and moves only via the controller's
  `set_clock_offset` command while the process is blocked at an armed barrier. The driver never
  reads the host wall clock — not as a base, not as a fallback — which is what lets §6.3's
  identical-event-trace conformance requirement (same case + same seed ⇒ byte-identical trace,
  timestamps included) actually hold across re-runs on different days. The operation scripts are
  forbidden (by conformance test) from calling `time.time()`/`datetime.now()` directly.
- **Per-role**: each role process has its own offset. Skew between roles — the case
  `ACCEPTANCE.md` §2's lease row actually needs, a holder whose clock lags the claimant's — is two
  offsets, not a global shift.
- **Monotonic time is harness-internal only.** Controller timeouts and watchdogs run on the host
  monotonic clock and are never skewed; the injected clock models the wall clock, which is the one
  lease expiry arithmetic uses. This asymmetry is deliberate and recorded here so nobody
  "improves" the harness by skewing its own watchdogs.
- **Skew magnitudes are boundary-relative, not raw numbers.** A case's clock programme is recorded
  symbolically against the lease geometry it targets:
  `forward = ttl_ms + guard_ms` (guaranteed to cross `expires_at_ms` from inside the lease),
  `backward = -(elapsed-at-injection + guard_ms)` (guaranteed to land before `acquired_at_ms`),
  with `guard_ms` a single named constant. The resolved millisecond values are computed at run
  time from the case's `ttl_ms` and recorded in the reproduction line (§4.4), so a failure is
  replayable exactly while the manifest stays meaningful when a case's TTL changes.
- **When an offset change takes effect.** The S6/S7 APIs take `now_ms` as a scalar per call: an
  operation already in flight captured its `now_ms` at the call boundary, and a
  `set_clock_offset` delivered at a checkpoint *inside* that call cannot and must not rewrite it —
  a real process does not re-read its clock mid-statement either, so that captured value is the
  honest semantics, not a defect. The contract therefore fixes two rules, both asserted by the
  conformance battery: the driver **sources `now_ms` freshly from its `Clock` at every API call**
  (never caching a value across calls), and a skew case's asserted observation is always made by
  an API call **issued after** the offset change. Concretely, the two supported shapes are:
  (a) **cross-role skew** — the target is blocked at an armed checkpoint while a *sibling's*
  offset is moved and the sibling acts under its new clock (the main case, and unaffected by the
  capture semantics); (b) **same-role skew** — the offset command is delivered at an armed
  checkpoint, the process is released with `continue`, and the skew is observed by the script's
  *next* operation. A case whose expectation depends on an in-flight call seeing a mid-call skew
  is invalid by construction and refused at manifest validation.
- Clock faults compose with the barrier: the canonical skew case blocks the holder at an armed
  checkpoint, moves the claimant's (or holder's) offset across the boundary, lets the claimant
  act, then releases or kills the holder — which is precisely the "expiry discovery is
  insufficient" race the fencing design (see `docs/lease-fencing.md`) is written against.

---

## 8. M5 — OS policy, signal hygiene, and cleanup

**The finding.** CI is a 3-OS × 3-Python matrix; Windows has no SIGSTOP, and a harness that parks
stopped processes or leaks process groups hangs CI for everyone.

**The resolution.**

### 8.1 Lanes

- **Linux is the conformance lane.** Every case kind runs there: SIGKILL barrier cases,
  SIGSTOP-expiry cases, staggered kills, clock skew, process-group semantics. Gate evidence
  (I-11, I-13, I-15) is read from this lane only.
- **The portable lane** runs everywhere (all three OSes): barrier-protocol tests, manifest/seed
  determinism tests, clock-skew cases (no signals involved), and crash cases driven through
  `Popen.kill()` — which is SIGKILL on POSIX and `TerminateProcess` on Windows, both "no unwind,
  connection dies" semantics, sufficient for the invariant assertions even though the exit-status
  assertion is lane-conditional.
- **SIGSTOP cases are Linux-only** (`pytest` skip elsewhere, including macOS: they *would* run
  there, but keeping the conformance claim single-lane means a macOS scheduler flake can never
  block the gate; macOS runs the portable lane only). The skip is by explicit lane marker in the
  manifest entry, so what does not run on an OS is enumerable, never silent.

### 8.2 Process hygiene

- On POSIX, every role process is spawned with `start_new_session=True`: its own session and
  process group (pgid = leader pid), so a stray shell or grandchild cannot be confused with it and
  the group can be signalled as a unit.
- **Teardown is unconditional, layered, and reaps last.** A fixture registered per spawned
  process runs on pass, fail and error alike. On POSIX the ladder is applied to the **process
  group** via `killpg`: `SIGCONT` (a stopped process ignores SIGTERM until continued), then
  `SIGTERM`, then a short grace (2 s) during which leader exit is checked **without reaping** —
  `os.waitid(P_PID, ..., WEXITED | WNOWAIT)` where available, otherwise no exit polling at all
  and the grace is a plain sleep (`Popen.poll()`/`waitpid(WNOHANG)` are forbidden here: they reap
  on success, which would release the pgid mid-ladder) — then `SIGKILL`, and only **after** the
  final group `SIGKILL` is the leader reaped with `wait()`.
  The ordering is the point: an exited-but-unreaped leader is a zombie, a zombie's PID — and
  therefore the group's pgid — **cannot be reused** until it is reaped, so every `killpg` in the
  ladder is guaranteed to address the group we created, even when the leader died at the first
  step while grandchildren survived. Grandchild cleanup is thus never skipped by an early leader
  exit, and no signal is ever sent to a possibly-recycled id.
- **PID-reuse safety**: the controller signals only through the retained `Popen` handle (leader)
  and through `killpg` on the pgid recorded at spawn *while the leader is un-reaped* (see above —
  the reap is deliberately the last step). `os.kill`/`killpg` against any stored id after the
  corresponding leader has been reaped is forbidden.
- **Windows branch (portable lane).** POSIX sessions, `SIGCONT`/`SIGTERM` and `killpg` do not
  exist on Windows, and `start_new_session` has no effect there, so the contract has an explicit
  branch rather than a best-effort mapping: role processes are spawned into a **Job Object**
  created per case with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, crash injection is
  `Popen.kill()` (`TerminateProcess`), and teardown is `TerminateProcess` on the leader followed
  by closing the job handle, which kills the whole tree atomically at the kernel — the moral
  equivalent of the zombie-guarded `killpg`, with the job handle playing the un-reusable-id role.
  If the Job Object API is unavailable to the harness, the fallback is `taskkill /T /F` on the
  leader PID *before* the handle is waited on. Signal-shaped cases (`sigstop-expire`, exit-status
  `-SIGKILL` assertions) never run on this branch (§8.1); only the portable lane's process
  hygiene needs it.
- **Watchdogs at three levels**, all on the host monotonic clock: a barrier timeout per armed
  checkpoint, a per-case timeout, and a suite timeout (§9). A watchdog firing runs the same
  teardown ladder and fails the case with the event trace attached — CI hangs are converted into
  attributable failures.

---

## 9. M6 — the CI budget, in numbers

**The finding.** "Sane wall-clock budget" is not checkable, and I-11/I-13/I-15 inherit whatever S9
sets.

**A boundary first.** `ACCEPTANCE.md` refuses to invent thresholds *for the acceptance surface*
where Issue #740 fixes none. The numbers below are not acceptance thresholds; they are **harness
engineering parameters** — enforced budgets for CI runtime, recorded in the manifest header,
revisable by an ordinary reviewed diff, and requiring no `D-` entry. If anyone proposes reading a
budget number *as* gate evidence, that is a ruling and goes to the secretary (§10).

**Cost model** (basis for the numbers): a typical kill-restart case is 3 process spawns
(~150 ms each interpreted CPython start), one barrier round-trip, one kill + `wait`, one restart
with recovery, and a handful of SQLite assertions — **1–3 s typical, dominated by interpreter
start**; combination cases roughly ×1.5. The budgets assume the ubuntu-latest runner class.

| Parameter | **fast** profile | **full** profile |
|---|---|---|
| Runs on | every PR push, Linux job only | nightly + gate runs (I-11/I-13/I-15), Linux conformance lane |
| Case set | smoke subset: ≤ 25 cases (one per fault kind × checkpoint, singles only, no staggered) | entire manifest, bounded at ≤ 200 cases |
| Per-case timeout | 15 s | 30 s (60 s for staggered/combination cases) |
| Suite timeout | 4 min hard | 25 min hard (expected ≈ 8 min at 200 × ~2.5 s) |
| Off-Linux add-on | portable lane only: ≤ 20 cases, ≤ 3 min added per existing job | same as fast |

Enforcement is mechanical: the per-case and suite watchdogs (§8.2) carry these values from the
manifest header; a manifest whose case count exceeds the profile bound fails collection, so growth
in I-11's matrix forces an explicit budget diff instead of silent CI creep. The fast profile
exists so the 9-job PR matrix never pays for the full matrix: PRs get the smoke subset on one job
plus the cheap portable lane; the full matrix is a scheduled/gate concern.

---

## 10. Boundaries, escalations, and what S9 ships

- **No `D-` entries.** This design adds none and this task may not add any. Rulings this document
  identifies as *someday needed* — promoting any spike artifact (D-0026), reading a budget number
  as gate evidence (§9), a concurrent-restart semantics beyond §5's sequential default if I-11
  wants one as gate evidence — are escalated to the secretary when they become due.
- **One plan gap flagged for escalation now**: §2.2 assigns the real-component adapters and the
  matrix re-run to I-13 (real-provider adapter) and I-15 (`MessageBus` adapter), but neither
  issue's body currently names a fault-runner adapter as a deliverable. Making that explicit is a
  change to those issues, owned by the plan, not by this document — it goes to the secretary with
  this design.
- **Open questions stay open.** `Q-0001` (writer assignment): resource names are per-case data
  (§2.1). `Q-0002`/`Q-0003` (incident collapse semantics, reconcile interval): incident-dedup
  cases must parameterise both, per `ACCEPTANCE.md` §2's dedup row — the manifest schema carries
  the parameter, S9 fixes no value.
- **What S9 (the implementation issue, once dispatched) ships against this design:** the contract,
  controller, protocol, conformance battery, manifest schema + generator-freeze test, the seed
  case set (§4.2, §5), the spike driver adapter, the lane markers and budgets wired into CI.
  **What it does not ship:** the full §2 matrix (I-11), the real-component adapters (I-12/I-14
  follow-ups), any recovery logic (the components').

---

## 11. I-11 addendum — what the matrix run corrected (Issue `#16`)

S9 shipped the harness and the seed set; I-11 populated the `ACCEPTANCE.md` §2 matrix on it, as
§4.2 and §10 say it should. Building the rest of the table established four things this design got
wrong or left open, and they are recorded here rather than in a commit message because the next
adapter (I-13, I-15) inherits them.

**11.1 A SIGKILLed holder cannot return as a stale writer.** §5 and §7 describe the returning
holder's write being refused, and that shape is real — but only for `sigstop-expire`, where the
paused holder keeps its epoch in memory across the takeover. A holder killed with SIGKILL keeps
nothing: it re-executes its command line, re-runs its script from `lease-acquire`, and has no token
left to present. So the lease row's "kill the lease holder without release" injection is discharged
by a refusal at **`acquire`** rather than at a protected write. `LeaseHeld` is persisted nowhere by
S6, so the driver appends it to the same refusal ledger §5 already specifies, and the case asserts
that row. This does not weaken the row: §2 asks that "the returning holder's write attempt is
refused and that refusal is recorded, not silently dropped", and both halves hold — the attempt is
refused at the earliest point it can be, and the refusal is a query-answerable persisted record.
The stale-token half stays where it actually works.

**11.2 Two live writers on one resource are not expressible — but the rejected writer must still
write.** A `writer-race` case cannot arm two writers at their write windows and release them in a
declared order, because the second one never reaches a write window: `acquire`'s upsert only
replaces a lapsed row, so it is refused at the resource boundary. The first version of this case
stopped there and asserted the refusal, which is half of §2's single-writer observable — and only
half. The other half is that "the state item's history in SQLite is a linear sequence with no
interleaving from the rejected writer", and a writer turned away at `acquire` contributes no row for
an interleaving to be visible in. That half was therefore true of every run, including one in which
atomic fencing had stopped working.

The resolution is to let the racer *carry on*: refused at `acquire`, it fabricates the token it was
denied and runs its whole script against the same state item. That is not a way around the refusal —
the refusal is recorded either way — it is the real hazard, a process that has not noticed it lost
its lease. Every write it then makes is refused **at the fence**, inside the write's own
transaction, and the history contains the rejected writer's rows for the assertion to be about. The
case requires a `StaleWriterRefused` specifically, because that is the refusal only a write can
produce; a `LeaseHeld` alone would mean the racer never tried.

The same correction applies to "a write is attempted concurrently from a resumed process and its
replacement". Running the replacement to completion and *then* restarting the killed process leaves
the resumed process meeting a lease row belonging to a process that has already exited, which is not
a concurrent write by any reading. The replacement is held at a barrier instead, alive and holding,
and is released only after the restart has come and gone. `spawn_claimant` therefore takes `armed`
and `behaviours` of its own — the claimant's, never the case's, since a claimant that inherited the
case's behaviours would fence out the writer that is supposed to win.

The general rule this is an instance of, and the one that cost the most rounds to learn: **an
assertion about what a rejected actor did not do is empty unless that actor got far enough to do
it.**

**11.3 A fault injection that every case already performs is not an injection.** The Secretary and
the other role scripts acked twice unconditionally, as standing evidence that acks are idempotent.
That made §2's "duplicate the ack" and "ack an already-acked message" true of all 35 seed cases and
therefore falsifiable by none: a regression in either shape had nowhere to show. Ack multiplicity is
behaviour-driven now and the baseline acks once. The general rule, for whoever extends this matrix
next: **an injection the harness performs by default cannot be a case.**

**11.4 An absence is not evidence unless something could have made it present.** The observation
row's second half — "no termination or restart recommendation is produced from it" — is a count of
rows, and nothing in the harness or the spike composes such a row, so the count was structurally
zero and the assertion could not fail. It is made falsifiable by having each observation case
*declare an escalation policy naming the very fact state its own injection produces*, so the driver
is asked to escalate and must refuse, recording the refusal. The policy is case data and the driver
maps no fact state to any verdict, so `Q-0012` (per-state semantics) stays open; the only rule
encoded is the one D-0006 actually decides. The same shape is the answer whenever a case's
observable is the absence of something: make the thing possible, then assert it did not happen.

**11.5 The dedup row, and how `Q-0002` is carried.** ACCEPTANCE.md §2 requires the incident collapse
rule *and* the re-notification window in absolute time to be parameterised rather than hard-coded —
both halves are `Q-0002` — so S9's own rule that `incident_params` may hold no value at all had to be
relaxed for the matrix, which is a change to a discipline this document set and was taken as a
ruling rather than in passing. The relaxation is narrow and its scope is the point:

- A **case** names its collapse rule, its window, and its dedup key. The driver implements both
  rules and is *told* which to apply; it never picks, and it never composes the dedup key, because a
  driver-side formula would answer `Q-0002`'s "what composes the key" half by inertia — the same way
  a role-to-resource table would have answered `Q-0001`.
- The **matrix** is held to covering the question rather than answering it: manifest validation
  refuses a matrix in which the set of collapse rules is not the whole vocabulary, or in which every
  case declares the same window. One value being load-bearing on a pass is what "hard-coded" means.
- One case declares a window its own raises fall **outside** of and expects no collapse. Without it
  the window would be carried and never change an outcome, and a parameter that changes nothing is
  indistinguishable from a hard-coded one. (Both directions are verified falsifiable: a driver that
  ignores the declared rule fails the increment-in-place cases, and one that ignores the declared
  window fails the outside-the-window case.)
- `reconcile_interval_ms` is **Q-0003**, not `Q-0002`, and is refused a value. The two were conflated
  in an earlier reading of this row; they are labelled apart now.

Nothing here settles `Q-0002`. When it is decided, the manifest keeps every case it has — the
decision removes the obligation to cover both rules, it does not invalidate either.
