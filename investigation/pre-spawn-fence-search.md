# Pre-spawn fence search on the `--bg` surface, and U27 / U28

**Status:** Part A — **search came up empty**; Part B — **U27 negative**, **U28 positive**
**Date:** 2026-08-18
**Task:** `interlock-fence-search-20260818` (triggered by `D-0024`)
**Scope:** propose-only. This note records a search and two experiments, and *proposes* readings. It
decides nothing: no `D-` entry is created here, no design document is edited, and the gate verdict is
left to a human and to a subsequent `D-` entry.
**Refs:** `#740`; `DECISIONS.md` `D-0024`, `D-0025`, `D-0023`, `D-0010`;
`investigation/u1-session-id-bg-experiment.md` (U1, and the U27/U28 definitions in its §6);
`docs/proposals/agent-view-gate-scaffold.md` §1.5 F3/F4/F6, §3.3 item 2, §5.3 O6, Appendix A;
`ACCEPTANCE.md` §1 item 2 and §2.

---

## 1. Verdict

Two independent results, one per part of the task.

### Part A — the pre-spawn fence search on Agent View

**The search came up empty.** On CLI 2.1.234 no public `--bg` surface lets a caller commit a session
identity, or acquire an exclusive claim, *before* the spawn. Ten candidate handles were examined
(§3.2); every one is either discarded at spawn, documented as explicitly *not* deduplicated, a
post-spawn artifact, or out of scope under `D-0025`'s local-execution pre-filter.

Per `D-0024`'s tail, this note therefore **proposes** — and does not enact — that gate item 2 **fails
on Agent View (C1)** and that the `Q-0004` path opens. See §5.1 for the exact proposed wording and for
what would overturn it.

Exhaustiveness cannot be proved, so §3.1 states the **surfaces searched** rather than claiming none
exists anywhere. The claim this note is willing to defend is: *no such handle is reachable from the
documented CLI surface of 2.1.234, and the two handles that looked most promising were refuted by
experiment rather than by reading.*

The strongest new fact in Part A is **A1/A4**: under `--bg`, `--resume <uuid>` is honoured as a
*content* handle but **not** as an *identity* handle. The conversation really is carried over — a
resumed background session answered a question that only the earlier turn could answer — but it is
registered under a **new CLI-assigned session id**, with the original transcript left untouched and a
copy written under the new id. In other words `--bg --resume` behaves like `--fork-session`, silently:
exit 0, no warning of any kind. A caller that committed the requested UUID before the spawn would hold
an identity that names *no running session*, while the work it asked for runs under an id it never
saw.

### Part B — U27 and U28

| # | Question | Result |
|---|----------|--------|
| **U27** | Is the `-p` `--session-id` refusal atomic under a genuine race? | **NEGATIVE.** There is an admission window of roughly **2 to 3 seconds** on this machine. In 5 of 5 simultaneous trials **both** processes were admitted, **both** exited 0, **both** reported the same `session_id`, and **both wrote to the same transcript**. |
| **U28** | After a SIGKILL of the holder, does `--resume` still succeed *and* is a fresh `--session-id` claimant still refused? | **POSITIVE, both halves.** The claim held through every probe taken — refused at t+2 s, at t+65 s, again after a successful resume, and again about 25 minutes later — and `--resume` returned the same `session_id` with exit 0. |

A third result was obtained while probing U28 and is reported here because it changes what U28's
positive is worth:

> **U32 (new).** `--resume` carries **no exclusion at all**. Two concurrent `claude -p --resume <same
> uuid>` processes were both admitted — simultaneously **and** at a 5-second stagger, far outside
> U27's window. Exit 0 for both, same `session_id` for both.

So the exclusion U1 §5.3 found on the `-p` surface is narrower than it looked in two ways at once: it
is not atomic at the front (U27), and it does not exist at all on the resume path (U32) — which is the
path `D-0025`'s C2 would actually use after a crash, per U28's own framing.

**What survives.** `-p` still refuses a *late* second claimant (U1 E5/E7, re-confirmed here as control
C1), and identity is still durable across an ungraceful kill (U28). Those are real, and they are
useful. They are not a single-writer guarantee.

---

## 2. Environment

```
$ claude --version
2.1.234 (Claude Code)

$ which claude
/home/happy_ryo/.local/bin/claude
```

Same build as U1, so the two notes compose without a version caveat.

Working directory for every spawn was a scratch directory outside the repository, deliberately **not**
a git repository (so worktree isolation could not touch the Interlock worktree):

```
/tmp/claude-1000/-home-happy-ryo-work-org-workers-interlock--worktrees-interlock-fence-search-20260818/c02e9ef7-5e99-4c33-8dd7-7923290c6856/scratchpad/fence
```

**One environmental control matters and is stated up front.** Under this worker's sandbox, a `-p` run
succeeds and returns its `session_id`, but its transcript is **never written to disk** — after the
sandboxed run of A1 step 1, no file matching `8f3b1c02*` existed anywhere under the CLI's per-user
config directory. Since the "already in use" refusal appears to be keyed to persisted session state, a
sandboxed experiment could have produced a false negative on every refusal probe. **Every Part B
command was therefore run with the sandbox lifted**, and control C1 (§4.2) confirms the refusal
mechanism was live in the environment where the U27 races ran. The `--bg` spawns needed the sandbox
lifted anyway, for the same reason U1's E2 did.

Sessions started: five `--bg` sessions (A1, A2a, A2b, A3, A4), all stopped and removed (§6), plus a
number of short `-p` runs, which hold no roster row and no process.

---

## 3. Part A — the search

### 3.1 Surfaces searched

Exhaustiveness is not provable, so this is an enumeration of what was read, not a claim about what
exists. Anything outside this list is *unsearched*, not *absent*.

**On the running build (2.1.234), read in full:**

- `claude --help` — all 242 lines; every option inspected for a caller-supplied identity, key, or
  claim.
- `claude agents --help` — the whole Agent View management surface.
- `claude project --help`.
- The four undocumented background commands, whose usage lines were read verbatim: `claude stop`,
  `claude rm`, `claude attach`, `claude logs`.
- Probes for a hypothetical create-with-identity subcommand:
  `claude spawn|start|new|run|exec|dispatch|session|sessions --help`. **None exists** — every one falls
  through to root help, so the background lifecycle surface is exactly `--bg` + `agents` +
  `stop`/`rm`/`attach`/`logs`.

**Official documentation (`code.claude.com/docs/en/`), read for this question:**
`agent-view`, `cli-reference`, `worktrees`, `sessions`, `env-vars`.

**Deliberately not searched, with reasons:**

- **CLI internals** (bundle strings, on-disk job/state formats). Excluded by `D-0010`: a handle found
  only by reading internals is not a documented capability and could not be built on. On-disk state was
  read once, as *diagnostic evidence* about behaviour already observed (§4.1), never as a candidate.
- **Cloud and remote surfaces** — `--cloud`, `--environment`, `--teleport`, `--remote-control`.
  Excluded by `D-0025` part 1 (local execution is mandatory).
- **The Agent SDK.** A different candidate (C3) with its own open question (U29).
- **`--exec` jobs.** Per F4 a job has no conversation, so an identity handle found there would be a
  false positive for this question — the same trap U30 guards against.

### 3.2 Candidates and verdicts

Every candidate is a public handle that could conceivably be committed to SQLite *before* the spawn.

| # | Candidate | Source | Verdict |
|---|-----------|--------|---------|
| 1 | `--session-id <uuid>` with `--bg` | `claude --help`; cli-reference | **No.** Discarded with a warning, exit 0, CLI-assigned identity (U1 E2/E3). |
| 2 | `--resume <uuid>` with `--bg` | cli-reference; *and the U1 warning text itself suggests it*: "use `--resume <id>` to continue an existing session" | **No — and silently.** A1/A4 below: exit 0, **no warning of any kind**, and the requested UUID appears nowhere in the roster. The *conversation* is resumed (A4 proves it), but into a **new** CLI-assigned session id — a silent fork. The caller cannot commit the identity, which is what a fence needs. |
| 3 | `-n, --name <name>` | `claude --help`; cli-reference; sessions | **No.** The sessions page states outright that Claude Code "doesn't check the `--name` of a background or `-p` session at startup". A2 confirms it on the build: two live background sessions, same caller-set name, both exit 0, no variant suffix, no refusal. |
| 4 | `-w, --worktree <name>` | worktrees | **No.** "Passing `--worktree` a name whose directory already exists **opens that existing worktree** instead of creating a new one" — reuse, not refusal. The `git worktree lock` Claude Code takes is worse than useless as a fence here: it is taken *after* the session starts, its stated purpose is to stop *cleanup* from removing the worktree, and the periodic sweep "releases a lock Claude Code set for a session whose process has exited" — i.e. it **releases on crash**, the exact opposite of what a fence must do. Not tested on the build; see §5.4. |
| 5 | An environment variable fixing the session id | env-vars page; plus a direct probe of the plausible name | **No.** No such variable is documented. A3 probes `CLAUDE_SESSION_ID=<uuid> claude --bg` anyway: ignored, new identity. Even a positive would have failed `D-0010`'s documented-capability posture. |
| 6 | `--fork-session` | cli-reference | **No.** Documented to "create a new session ID instead of reusing the original" — CLI-assigned by construction. |
| 7 | `cwd` (or `--add-dir`) as an exclusive resource | roster observation | **No.** Two background sessions coexist in one `cwd` — visible in U1's E1 baseline (two rows under the dispatcher directory) and again in A2 (two rows under the scratch dir). |
| 8 | `claude agents` dispatch flags | `claude agents --help` | **No.** The subcommand sets *defaults* for dispatched sessions (`--agent`, `--model`, `--settings`, ...). It has no create verb and no identity argument. |
| 9 | `--exec` job identity | agent-view; F4 | **Out of scope by construction** — a job has no conversation (F4/U30). |
| 10 | `--cloud` / `--environment` / `--teleport` / `--remote-control [name]` | `claude --help`; cli-reference | **Out of scope** under `D-0025` part 1 (local execution mandatory). |

Rows 1-3 and 5 were refuted by experiment; rows 4, 6 and 8 by documentation plus the help text; rows
7, 9 and 10 by observation or by prior decision.

### 3.3 A1 — `--bg --resume <uuid>`, the most promising candidate

Step 1, create a session whose identity the caller chose (the `-p` surface honours it, U1 E4):

```
$ claude -p --session-id 8f3b1c02-4d71-4e0a-9a55-6c1d2e3f4a5b "reply with ok" --output-format json
{"is_error":false,...,"session_id":"8f3b1c02-4d71-4e0a-9a55-6c1d2e3f4a5b",...,"result":"ok",...}
--- EXIT CODE: 0 ---
```

Step 2, first attempt blocked by this worker's own sandbox, not by the CLI (recorded for honesty; the
same failure U1 E2 hit):

```
$ claude --bg --resume 8f3b1c02-4d71-4e0a-9a55-6c1d2e3f4a5b "reply with ok"
Couldn't start the session - EROFS: read-only file system, mkdir '<config-dir>/jobs/fd9e3bbc'
--- EXIT CODE: 1 ---
```

(Note in passing, as U1 did: the job directory is allocated under `fd9e3bbc`, which is neither the
requested UUID nor the identity the eventual session received.)

Re-run with the sandbox lifted for that command only:

```
$ claude --bg --resume 8f3b1c02-4d71-4e0a-9a55-6c1d2e3f4a5b "reply with ok"
Starting background service…
backgrounded · 92f1437a
  claude agents             list sessions
  claude attach 92f1437a    open in this terminal
  claude logs 92f1437a      show recent output
  claude stop 92f1437a      stop this session
--- EXIT CODE: 0 ---
```

**There is no warning.** Roster readback:

```
$ claude agents --json --all      # filtered to the new session
{"pid": ..., "id": "92f1437a",
 "cwd": "/tmp/.../scratchpad/fence", "kind": "background",
 "startedAt": 1787026493875,
 "sessionId": "92f1437a-2b33-403f-8707-58fc23d979a1",
 "name": "reply with ok", "state": "working"}

$ claude agents --json --all | grep -c "8f3b1c02"
0
```

A new identity, unrelated to the one requested. This session later reached `state: failed` and its logs
could not be read (`Couldn't read logs for 92f1437a - connect ENOENT
/tmp/cc-daemon-1000/60743f15/control.sock`), which left one question open: was the conversation
resumed at all, or was a fresh one started under a new id? A1 cannot tell, so **A4 was run to settle
it** — the distinction matters, because "the flag was ignored" and "the flag worked but re-registered
the session" are different facts about the provider.

### 3.3.1 A4 — what `--bg --resume` actually does

A `-p` session was created with a chosen UUID and a codeword, then resumed under `--bg` with a
question only that conversation could answer:

```
$ claude -p --session-id 7c9e5510-1a2b-4c3d-8e9f-0a1b2c3d4e5f \
    "Remember this codeword: PLUMBAGO. Reply with just: stored" --output-format json
session_id: 7c9e5510-1a2b-4c3d-8e9f-0a1b2c3d4e5f
result: stored

$ claude --bg --resume 7c9e5510-1a2b-4c3d-8e9f-0a1b2c3d4e5f \
    "What codeword did I ask you to remember? Reply with just the word."
backgrounded · ef3e58d6
--- EXIT CODE: 0 ---
```

Roster, and the session's own screen via `claude logs ef3e58d6` (ANSI stripped):

```
[('done', 'ef3e58d6-b776-4488-a860-efc85ee81915')]

❯ Remember this codeword: PLUMBAGO. Reply with just: stored
● stored
❯ What codeword did I ask you to remember? Reply with just the word.
● PLUMBAGO
```

The two transcripts on disk complete the picture:

```
7c9e5510-....jsonl   11 lines   user "Remember this codeword: PLUMBAGO…" / assistant "stored"
ef3e58d6-....jsonl   24 lines   the same two turns, re-stamped sessionId ef3e58d6,
                                plus  user "What codeword…" / assistant "PLUMBAGO"
```

**So `--resume` under `--bg` is honoured as a content handle and ignored as an identity handle.** The
history is copied into a new transcript under a new session id, the original transcript is left
untouched, and the background session is registered as `ef3e58d6-…`. That is the documented behaviour
of `--fork-session` — applied without being asked for, and without a word of output. (The original
UUID also stays claimed afterwards: a later `-p --session-id 7c9e5510-…` was refused with `already in
use`.) **U33 is therefore answered here**, and the answer makes the candidate's verdict firmer, not
weaker: there is no way to name the identity the background session will have.

Compared with U1's `--session-id` case, this is the more dangerous shape. There, a caller who reads
the CLI's output at least sees a warning. Here a caller passing `--resume` gets exit 0, a management
banner that looks entirely normal, and a *different session running its work* — with **nothing at all**
to read. A control plane that committed the requested UUID would hold an identity naming no live
session while the work proceeded under an id it never saw: not a lost spawn, a lost *binding*. §5.2 of
the U1 note ("treat exit 0 as insufficient and read the identity back from the roster") applies with
more force, not less.

### 3.4 A2 — duplicate `--name` under `--bg`

```
$ claude --bg -n interlock-fence-dup "reply with ok"
backgrounded · 214f1076 · interlock-fence-dup
--- EXIT CODE: 0 ---

$ claude --bg -n interlock-fence-dup "reply with ok"
backgrounded · 3811ccb3 · interlock-fence-dup
--- EXIT CODE: 0 ---
```

Roster:

```
{"pid": 1152342, "id": "214f1076", ..., "sessionId": "214f1076-685a-4c67-bb5c-4f1ff97f66eb",
 "name": "interlock-fence-dup", "state": "done"}
{"pid": 1152341, "id": "3811ccb3", ..., "sessionId": "3811ccb3-4431-4e95-ac95-4f45d8e85dc8",
 "name": "interlock-fence-dup", "state": "done"}
```

Two distinct live sessions, one name, no refusal and no variant suffix — matching the documented
carve-out exactly. This is the documented behaviour of the *only* caller-controlled pre-spawn string
on the surface, and it is an anti-fence: names collide silently. It also re-confirms V20 / `D-0024`'s
"`name` is a display name rather than an identity" from the other direction — the U1 note showed the
CLI *generates* names, and this shows the CLI *does not deduplicate* the ones you set.

### 3.5 A3 — undocumented environment variable probe

```
$ CLAUDE_SESSION_ID=11112222-3333-4444-5555-666677778888 claude --bg "reply with ok"
backgrounded · 4f459d6f
--- EXIT CODE: 0 ---

# roster
{"pid": 1152519, "id": "4f459d6f", ..., "sessionId": "4f459d6f-0918-4ef0-872a-080be575466b",
 "name": "reply interaction", "state": "done"}
requested uuid present: False
```

Ignored. Recorded mainly to close the "did you try an env var?" question explicitly; per `D-0010` an
undocumented variable could not have been built on even if it had worked.

---

## 4. Part B — U27 and U28

### 4.1 U27 — is the `-p` claim atomic?

Method: five trials, each with a fresh UUID. Two `claude -p --session-id <uuid> "reply with ok"`
processes were released from a common `threading.Barrier`, so both `exec`ed within a fraction of a
millisecond of each other (measured launch skew 0.0-0.1 ms). Driver: `u27.py`, kept with the raw
results in the scratch directory.

**Result: 5 of 5 trials `BOTH-ADMITTED`.** Representative trial, verbatim from the driver's record:

```json
{
  "trial": 2,
  "uuid": "540f6d3d-b91d-4ff4-8275-3541b8e77065",
  "launch_skew_ms": 0.1,
  "P1": {"rc": 0, "session_id": "540f6d3d-b91d-4ff4-8275-3541b8e77065", "stdout_empty": false},
  "P2": {"rc": 0, "session_id": "540f6d3d-b91d-4ff4-8275-3541b8e77065", "stdout_empty": false},
  "verdict": "BOTH-ADMITTED"
}
```

```
SUMMARY: {"1":"BOTH-ADMITTED","2":"BOTH-ADMITTED","3":"BOTH-ADMITTED",
          "4":"BOTH-ADMITTED","5":"BOTH-ADMITTED"}
```

**Both processes also wrote.** Admission and writing are different claims, so the transcript for trial
2 was read: one file, 22 lines, carrying **two** user turns and **two** assistant turns with distinct
message UUIDs under one `sessionId`:

```
# <config-dir>/projects/<scratch-project>/540f6d3d-b91d-4ff4-8275-3541b8e77065.jsonl
lines: 22
user      | 540f6d3d | cca3c0d0 | reply with ok
user      | 540f6d3d | f8ff819b | reply with ok
assistant | 540f6d3d | 61ea036e | ok
assistant | 540f6d3d | ee6222c9 | ok
```

This is a **demonstrated single-writer violation at the provider's own identity level**, not an
inference from two exit codes.

### 4.2 Control C1 — the refusal mechanism was live

The result above would be worthless if the refusal simply did not function in this environment. Taking
trial 2's now-finished UUID and re-claiming it sequentially:

```
$ claude -p --session-id 540f6d3d-b91d-4ff4-8275-3541b8e77065 "reply with ok" --output-format json
--- stdout ---
(empty)
--- stderr ---
Error: Session ID 540f6d3d-b91d-4ff4-8275-3541b8e77065 is already in use.
--- EXIT CODE: 1 ---
```

U1's E5 reproduces exactly. The refusal is present; the race gets past it.

### 4.3 How wide is the window?

Same shape as U1's E7 (a long-running holder, a short second claimant) with the stagger swept.
Driver: `u27b.py`.

| Stagger | Second claimant | Time to result |
|---------|-----------------|----------------|
| 0.5 s | **admitted**, exit 0 | 5.8 s (a real model turn) |
| 1.0 s | **admitted**, exit 0 | 6.9 s |
| 2.0 s | **admitted**, exit 0 | 5.3 s |
| 3.0 s | refused, exit 1, `Error: Session ID ... is already in use.` | 0.4 s |
| 5.0 s | refused, exit 1, same message | 0.4 s |

The holder (`P1`) completed normally with exit 0 in every row.

So the window on this machine is roughly **2 to 3 seconds** wide, and refusal — when it happens — is
fast and pre-model (0.4 s). **U1's E7 was measured at about a 3-second stagger, i.e. it landed just
outside this window.** E7's positive result was correct and is not contradicted; it simply did not
probe the region where the property fails.

The window's mechanism is not established here, only its width on one machine under one load. Do not
treat "about 2 to 3 seconds" as a constant (U34).

### 4.4 U28 — after an ungraceful kill

Method: a holder was started with a chosen UUID and a long prompt, left running for 10 s (well past
the U27 window), then killed with `SIGKILL` on its process group. Both halves of U28 were then probed,
twice for the claim half to see whether the claim ever releases. Driver: `u28.py`.

```
U28 uuid = 6ed0a0e3-ef90-4e5e-9bdf-460ac9b1b584
{"step":"holder_alive_before_kill","pid":1216664,"alive":true}
{"step":"holder_killed","poll":-9}

{"step":"claim_t+2s",  "rc":1,"dur":0.4,"stdout":"",
 "stderr":"Error: Session ID 6ed0a0e3-ef90-4e5e-9bdf-460ac9b1b584 is already in use."}

{"step":"claim_t+65s", "rc":1,"dur":0.4,"stdout":"",
 "stderr":"Error: Session ID 6ed0a0e3-ef90-4e5e-9bdf-460ac9b1b584 is already in use."}

{"step":"resume",      "rc":0,"dur":6.2,
 "stdout":"{...,\"session_id\":\"6ed0a0e3-ef90-4e5e-9bdf-460ac9b1b584\",...}"}

{"step":"claim_after_resume","rc":1,"dur":0.4,"stdout":"",
 "stderr":"Error: Session ID 6ed0a0e3-ef90-4e5e-9bdf-460ac9b1b584 is already in use."}

SUMMARY: {"claim_t+2s":1,"claim_t+65s":1,"resume":0,"claim_after_resume":1}
```

**Both halves hold.** The identity survives an ungraceful kill: `--resume` reaches the crashed session
and returns the same `session_id`, and a fresh `--session-id` claimant stays refused — at t+2 s, at
t+65 s, and again after the resume. This is exactly the direction U28 named as *desired*: the claim
did **not** release on crash within any interval probed, so a prompt retry cannot create a second live
writer *by re-claiming*. Note the bound: expiry, a cleanup sweep, or manual state removal at some
longer horizon was not tested (U36).

### 4.5 U32 (new) — `--resume` excludes nothing

U28's framing says that under C2 Interlock resumes a persisted session with `--resume`. So the resume
path's own concurrency was probed, against the session U28 had just left on disk. Driver: `u28b.py`.

```
resuming uuid = 6ed0a0e3-ef90-4e5e-9bdf-460ac9b1b584

{"case":"simultaneous",
 "R1":{"rc":0,"dur":10.9,"session_id":"6ed0a0e3-...","stderr":""},
 "R2":{"rc":0,"dur":5.3, "session_id":"6ed0a0e3-...","stderr":""},
 "verdict":"BOTH-ADMITTED"}

{"case":"staggered_5s",
 "R1":{"rc":0,"dur":12.3,"session_id":"6ed0a0e3-...","stderr":""},
 "R2":{"rc":0,"dur":5.2, "session_id":"6ed0a0e3-...","stderr":""},
 "verdict":"BOTH-ADMITTED"}
```

Two concurrent resumes of one session, both admitted, both exit 0, both carrying the same
`session_id` — and at a 5-second stagger, which is **outside** U27's window, so this is not the same
race. There is simply no exclusion on this path. The documentation says as much for the interactive
case, in a sentence that is easy to read past: *"If you resume the same session in two terminals
without forking, messages from both interleave into one transcript."*

The refusal, then, guards **creation of a session id**, not **use of a session**.

---

## 5. What this means - proposed reading

### 5.1 Gate item 2 on Agent View: proposed **fail**, and the `Q-0004` path opens

`D-0024` says: if U1 fails, "search for any other **pre-spawn** idempotent identity or fence; if none
exists, **gate item 2 fails and the Q-0004 path opens**." U1 failed. §3 conducted that search. It
found nothing.

The proposed statement, for a human to accept or reject in a `D-` entry:

> **The pre-spawn fence search triggered by `D-0024` came up empty on the documented CLI surface of
> 2.1.234. Gate item 2 therefore fails on Agent View (C1), and the `Q-0004` path — already resolved by
> `D-0025` to C2 as the designated second spike — opens.**

What would overturn it, stated so the proposal is falsifiable: a documented handle on a surface not in
§3.1's list; a future CLI release that honours `--session-id` under `--bg`, or that adopts the id named
by `--resume` as the background session's own identity rather than forking, or that refuses a
duplicate `--name` for background sessions; or a `WorktreeCreate` hook arrangement that
turns worktree creation into a genuine pre-spawn exclusive claim (§5.4). None of these is available
today, and `D-0024` asks for the verdict on what exists, not on what might.

Note that this is a failure of the *provider*, not of the gate's design. Item 2's predicate is
unchanged and, per F6, softening it would be a reclassification rather than a mitigation.

### 5.2 The C2 evidence moves — and not in C2's favour

U1 §5.3 proposed that C2's O6 grade stay at `~` while its *reason* changed, because refusal had been
observed but the crash window had not been probed. That reasoning was right and its conclusion still
holds, but the reason must change again:

| Property | U1's finding | After this note |
|----------|--------------|-----------------|
| Identity chosen pre-spawn | yes (E4) | unchanged |
| Late second claimant refused | yes (E5, E7) | unchanged — control C1 reproduces it |
| Claim atomic under a race | untested (U27) | **no** — ~2-3 s window; both writers admitted *and both wrote* |
| Identity durable across SIGKILL | untested (U28) | **yes** — claim held through every probe, `--resume` works |
| Second *user* of a session excluded | not asked | **no** (U32) — concurrent `--resume` is unrestricted |

**Proposed: C2's O6 grade stays `~`, on a materially worse footing than U1 left it.** What is now
established is a *durable identity* (the binding half of O6, plus crash survival), with **no** usable
exclusion primitive: the create path has a multi-second admission window, and the path C2 would
actually use after a crash has none at all. A move to `Y` on O6 would be wrong on this evidence; so
would a move to `N`, because the binding and crash-survival halves are real and were the reason C2 was
designated. C3's grade should still not move (U29 is untouched by this note).

This does not disturb `D-0025`'s designation of C2. It sharpens what spiking C2 has to prove.

### 5.3 The one conclusion that holds under every candidate

`D-0024`'s last consequence already said it, and both experiments now support it from opposite
directions: **the single-writer half of O6 must come from Interlock's own fencing token, validated
atomically as part of each protected write (`ACCEPTANCE.md` §2).**

- On C1 there is no identity input at all, so there is nothing to fence with (Part A).
- On C2 there *is* an identity, and it is durable across a crash (U28) — but it admits two writers
  within a ~2-3 s window at creation (U27) and any number of concurrent writers via `--resume` (U32).

The dangerous shape is worth spelling out, because it is exactly item 2's predicate: a supervisor
crashes, is restarted promptly, and retries the spawn. A retry issued *within a couple of seconds* —
which is what a healthy supervisor does — lands **inside** U27's window. The provider will admit both.
Nothing outside Interlock will stop the second writer, and per `D-0023` part 3 fail-closed is
Interlock's own obligation regardless.

The practical reading: Interlock's fencing token is not a belt-and-braces addition to a provider
guarantee. On the evidence available today it is **the only exclusion in the system**, and its tests
(`D-0014`'s rescue list, fault injection at the injection points) are the tests that matter.

### 5.4 On the worktree candidate specifically

Candidate 4 is the one a reader is most likely to want re-opened, since `git worktree` really does
have an exclusion primitive. Three documented facts close it *as a pre-spawn fence for the incumbent*,
and all three would have to be overturned together:

1. Reusing a name **opens** the existing worktree rather than refusing.
2. The lock Claude Code takes is taken **after** the session starts, and exists to stop cleanup, not
   to stop a second session.
3. The sweep **releases** that lock when the owning process has exited — release-on-crash, the
   opposite of a fence's requirement.

A `WorktreeCreate` hook could in principle be made to acquire a real exclusive claim before returning
a path. That is not a pre-spawn fence *for Agent View* though: it would be Interlock's own fence,
executed by a hook, which is §5.3's answer wearing a different hat — and it would have to be reasoned
about as a design of Interlock's, with its own `D-` entry. It was not tested here; recorded as U35.

---

## 6. Cleanup

All five background sessions started by this experiment were stopped and removed:

```
$ claude stop 92f1437a → stopped;  claude rm 92f1437a → removed
$ claude stop 214f1076 → stopped;  claude rm 214f1076 → removed
$ claude stop 3811ccb3 → stopped;  claude rm 3811ccb3 → removed
$ claude stop 4f459d6f → stopped;  claude rm 4f459d6f → removed
$ claude stop ef3e58d6 → stopped;  claude rm ef3e58d6 → removed
```

Roster verification afterwards:

```
$ claude agents --json --all      # background rows only
1b5c7f25 failed  .../claude-org-ja/.dispatcher
f41a4ced blocked .../claude-org-ja/.dispatcher
45841c01 done    .../claude-org-ja
bg rows: 3
experiment ids present: []

$ ls <config-dir>/jobs
1b5c7f25  45841c01  f41a4ced  pins.json
```

Exactly the three pre-existing sessions from U1's E1 baseline remain, and no job directory from this
experiment survives. No worktrees were created (the scratch cwd was not a git repository). As in U1,
the `-p` runs leave transcripts on disk under the CLI's per-user project directory (15 files under the
scratch project) and hold no roster row and no process.

---

## 7. New unverified items this raises

Proposed additions to the Appendix A register, continuing U1's numbering. Except where marked, none
was tested here.

| # | Question | Why it matters |
|---|----------|----------------|
| **U32** | *(answered here, listed for the register)* Does `--resume` exclude a second concurrent user of the same session? **No** — both admitted, simultaneously and at a 5 s stagger. | This is the path C2 uses after a crash. It means U28's positive is about identity durability only, and carries no single-writer content. |
| **U33** | *(answered here, listed for the register)* Under `--bg --resume <uuid>`, is the named conversation resumed, or is a fresh one started? **Resumed** — A4 (§3.3.1): the history is copied into a **new** session id, the original transcript is untouched, and the original UUID stays claimed. `--bg --resume` behaves like `--fork-session`, without saying so. | Fixes how the fail-open must be described: the flag is not inert, it is honoured for content and ignored for identity. A control plane keyed on the requested UUID would hold a binding to no live session while the work ran elsewhere. |
| **U34** | What sets the width of U27's admission window, and is ~2-3 s stable across machines, load, and CLI versions? Is it bounded by transcript-file creation, by the first API response, or by something else? | A supervisor's retry delay is a design parameter. If it can be placed reliably outside the window, the exposure shrinks (it does not vanish — the window still exists). Do **not** design on the measured figure without this. |
| **U35** | Can a `WorktreeCreate` hook be made to acquire a genuine exclusive claim *before* the CLI proceeds, and does the CLI honour a hook failure by refusing to start? | The only route by which a worktree could become a real pre-spawn fence (§5.4). Note it would be Interlock's fence, not the provider's. |
| **U36** | Is the "already in use" refusal keyed to the persisted transcript, to a lock file, or to a live process? Evidence is mixed: it survives SIGKILL (U28) and outlives a finished process (E5, C1), and under this worker's sandbox — where transcripts are never written — no state persisted at all. | Determines whether the refusal is inherited by other entry paths (relates to U29) and whether it can be cleared or spoofed by state on disk. |
| **U37** | Does the U27 race exist on the interactive and Agent-View dispatch paths, or only on `-p`? | Not directly actionable for C1 (no identity input) but bears on any provider that shells out to the CLI. |

---

## 8. Bottom line

The pre-spawn fence search that `D-0024` triggered **came up empty**: on CLI 2.1.234 the `--bg`
surface offers no way to commit an identity or acquire an exclusive claim before the spawn. The two
best candidates were refuted by experiment — under `--bg`, `--resume` silently forks the conversation
into a **new** CLI-assigned identity instead of adopting the one named, and a duplicate `--name` is
accepted for two live background sessions exactly as the documentation says it will be. This note therefore **proposes** that gate item 2 fails on Agent View
and that the `Q-0004` path opens, and leaves the enactment to a human and a subsequent `D-` entry.

On the `-p` surface, **U27 is negative**: the "already in use" refusal has an admission window of
roughly 2 to 3 seconds, and inside that window two processes were admitted to one session
id, both exiting 0 and both writing to one transcript. **U28 is positive on both halves**: after a
SIGKILL the claim held through every probe taken (out to about 25 minutes) and `--resume` still
reaches the session. But `--resume` itself
(U32) excludes nothing, so the property that survives the crash is *durable identity*, not *single
writer*.

Taken together, the two parts point one way. No provider surface examined here supplies exclusion that
item 2 could rest on. `ACCEPTANCE.md` §2's fencing token, validated atomically on every protected
write, is not a supplement to a provider guarantee — on this evidence it is the only one there is.
