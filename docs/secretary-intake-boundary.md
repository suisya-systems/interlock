# The Secretary intake boundary — contract of the item 8 rehearsal

**Status:** rehearsal artifact (Issue #21, gate item 8, D-0022). **This is a rehearsal, not a
discharge.** The stub in `src/claude_org_runtime/secretary/` is throwaway by default (D-0026); the
durable outputs are the boundary contract below and the tests in `tests/secretary/`. The discharge
point for item 8 is the same absence of blocking shown **against the real Secretary under genuine
worker load, before the canary starts** (D-0013), against a latency threshold settled by `Q-0011`.

**Refs.** `ACCEPTANCE.md` §1 item 8; `DECISIONS.md` D-0016 (Secretary is the single non-blocking
window), D-0022 (scoped exception: rehearse now, prove at the discharge point), D-0002; `Q-0011`
(unresolved — no numeric threshold exists and none is invented here);
`investigation/i16-item8-rehearsal.md` (the empirical record);
`investigation/i01-supervisor-probe.md` §3.7/§5.3 (the provider-side input and the U6 fold-in).

## Why a boundary contract exists at all

Gate item 8 asks for the absence of blocking dependencies to be shown **structurally**, not
inferred from a good latency number — a passing number proves nothing on its own while Q-0011 is
unresolved. A structural claim needs a structure to point at. This document names it once, so later
work builds against it rather than re-deciding it (the Secretary Web interface, Issue #29, was
explicitly told not to pre-empt this boundary).

## The contract

Three parties, two sides, one crossing.

```
requester ──▶ SecretaryIntake.submit() ──▶ IntakeQueue ──▶ consumers (pull)
                     │                      (bounded)        worker monitoring,
                     ◀── IntakeReceipt ──┘                   long-running work,
                         (always, immediately)               AI judgement
```

1. **The intake path performs no blocking call.** `submit()` stamps a receipt, offers the request
   to the queue without waiting, and answers. It never joins a thread, waits on an event, reads a
   pipe, sleeps, or calls into any consumer. Enforced on the syntax tree by
   `tests/secretary/test_structural.py`, which bans the blocking primitives from the intake module
   and holds its imports to a stdlib allowlist.

2. **The queue boundary is explicit, bounded, one-way — and lock-free.** `IntakeQueue` is the only
   object the two sides share. A `with lock:` is a blocking `acquire()` whenever the holder is
   descheduled, so the package takes **no lock at all** (asserted on the syntax tree: no
   `with`-block, no synchronisation-object constructor, no `threading` import); shared state is a
   single deque whose operations are atomic in CPython. The price is stated in the code: the
   capacity check is exact under one producer and may overshoot by at most P−1 under P concurrent
   producers — bounded by the thing D-0017 already caps. Consumers **pull**; nothing on the
   consumer side is ever invoked, signalled, or waited for by the intake side.

3. **No dependency edge exists from intake to supervision or judgement.** The intake package
   imports no other Interlock module — in particular not `session` (the C2 supervisor side) and
   not `dispatcher`. Worker monitoring and AI judgement cannot block a code path that cannot
   reach them.

4. **Backpressure is a refusal, not a wait.** When the bounded queue is full, the request is
   refused immediately and the refusal is **recorded** (on the receipt and in the refusal log).
   Whether a refusal at a given depth is acceptable is a real-Secretary design question, out of
   scope here; the contract fixes only that the alternative to acceptance is an immediate recorded
   refusal, never a block.

## What the rehearsal showed empirically

Full record: `investigation/i16-item8-rehearsal.md`. In brief, on the spike slice: intake
request→response latency was unchanged between idle and a load of 9 live `claude -p` children
(8 workers at the spike-slice cap plus one long-running task) with an open incident parked awaiting
a stub AI judgement — and stayed unchanged while the supervisor thread was deliberately made
*blocking* and spent ~13 s serialised on one child's next stream event. The U6 C2 fold-in is
thereby measured, not argued: a blocking per-child read serialises the **supervisor** by seconds
per live child; the structural boundary keeps that stall out of the intake path.

## What the real Secretary must preserve

The stub is disposable; these properties are not:

- the intake path free of blocking primitives and of dependency edges to supervision/judgement;
- an explicit bounded queue as the only crossing, with pull-only consumers;
- refusal-with-record as the full backpressure behaviour;
- durable intake (an SQLite-backed inbox, D-0001) — deliberately **not** rehearsed here, and the
  first thing the real implementation adds on the intake side of the same boundary.

The discharge measurement repeats the baseline-vs-load comparison against the real Secretary under
genuine worker load, with the numeric acceptance threshold that Q-0011 settles.
