# i18 — Unmediated characterisation: U27 re-measured, U32 reproduced (issue #18)

**Layer.** This is the *unmediated* half of issue #18: the provider driven
directly, Interlock deliberately out of the path, in a scratch working
directory that never touches a real run's session. It exists to prove the
hazard is live on the machine the gate runs on — it is the only place two
concurrent processes on one session id may appear. The mediated proof
(`tests/gate_item2/`, `tests/fault_injection/`) imports nothing from here, and
no figure below is a design constant anywhere (U34).

**Conditions.** One machine (`allegro`, WSL2), one load, CLI `2.1.238
(Claude Code)`, 2026-08-21. Driver: `investigation/i18_recharacterisation.py`
(barrier-released simultaneous pairs; stagger sweep; SIGKILL + concurrent
resume — the same method as `pre-spawn-fence-search.md` §4). Sample sizes are
what the tables say and nothing larger is claimed.

## U27 re-run — the `-p --session-id` claim is still not atomic

Three trials, each a fresh UUID and two `claude -p --session-id <uuid>`
processes released from a common barrier:

| Trial | Result | Exit codes | Reported ids | Transcript |
|---|---|---|---|---|
| 1 | **both admitted** | 0, 0 | one id, both | 24 lines, **2 user + 2 assistant turns**, one `sessionId` |
| 2 | **both admitted** | 0, 0 | one id, both | 24 lines, 2 + 2, one `sessionId` |
| 3 | **both admitted** | 0, 0 | one id, both | 24 lines, 2 + 2, one `sessionId` |

3/3 BOTH-ADMITTED, and the transcripts show **both wrote** — the same
single-writer violation at the provider's own identity level that
`pre-spawn-fence-search.md` §4.1 records, reproduced on the current CLI.

## U27 window sweep — the edge on this machine, today

Long-running holder plus a second claimant at increasing stagger:

| Stagger | Outcome | Claimant duration |
|---|---|---|
| 0.5 s | admitted, exit 0 | 5.52 s |
| 1.0 s | admitted, exit 0 | 4.66 s |
| 2.0 s | admitted, exit 0 | 4.95 s |
| 3.0 s | **refused**, exit 1 | 0.34 s |
| 5.0 s | **refused**, exit 1 | 0.34 s |

The edge lies between 2 s and 3 s here, today — consistent with the original
~2–3 s measurement, and still a one-machine, one-load, one-day figure. The
refusal, when it comes, is fast (~0.34 s) and pre-model. **This width is not
a constant and nothing in the mediated proof sleeps on it** (U34; D-0027:
"a supervisor retry delay must not be designed against the measured 2–3 s
figure").

## U32 re-run — `--resume` still excludes nothing

Holder established with a chosen UUID, left 10 s (past the window's measured
ballpark), SIGKILLed on its process group; then two concurrent
`claude -p --resume <uuid>`:

| Resumer | Exit | Same id? |
|---|---|---|
| R1 | 0 | yes |
| R2 | 0 | yes |

**Both admitted.** The shared transcript ends at 28 lines with 3 user and 6
assistant turns under one `sessionId` — an interleaved transcript, which the
gate record names as exactly the thing that is *not* an accepted residual.
Two is the number tested; no larger count was probed.

## What follows

Nothing in the provider stands between a crash-and-retry inside the admission
window (F3) — or any concurrent resume at all — and an interleaved
transcript. The exclusion has to be Interlock's own fencing token, ahead of
the spawn: that mediated argument is tested (not restated) in
`tests/gate_item2/` and the fault-injection harness, all of which pass with
the provider's refusal assumed absent. Raw results:
`i18_recharacterisation.py --out` JSON (scratch); the numbers above are the
complete set, not excerpts.
