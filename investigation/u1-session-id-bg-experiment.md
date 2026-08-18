# U1 — Does `--session-id <uuid>` compose with `--bg`?

**Status:** answered — **negative**
**Date:** 2026-08-18
**Task:** `interlock-u1-experiment-20260818` (Decision 6a, phase 0)
**Scope:** propose-only. This note records an experiment and *proposes* a gate reading. It does not
decide the gate. No `D-` entry is created here and no design document is edited; confirmation is left
to a human and to a subsequent `D-` entry.
**Refs:** `#740`; `docs/proposals/agent-view-gate-scaffold.md` Appendix A/U1, §1.5 F3/F4/F6, §6
Decision 6, §3.3 item 2, §5.3 O6; `ACCEPTANCE.md` §1 item 2.

---

## 1. Verdict

**U1 is answered NEGATIVE.** `--session-id` does **not** compose with `--bg`. On CLI 2.1.234 the flag
is not rejected, not honoured, and not partially applied — it is **explicitly discarded with a
warning**, and the session receives a CLI-assigned identity instead:

```
warning: --bg manages the session id; ignoring --session-id (use --resume <id> to continue an existing session)
```

The exit code is **0**. The spawn succeeds. Only the identity the caller asked for is thrown away.

Three secondary results were obtained in the same run and are reported here because two of them
change what the negative result *means*:

| # | Result | Bearing |
|---|---|---|
| **A** | Under `--bg`, the UUID-collision case is **not detected at all** — the flag is discarded before any collision check runs, so a colliding UUID behaves identically to a fresh one (warning, exit 0, brand-new id). | There is no `--bg` collision behaviour to design against, because there is no `--bg` identity input. |
| **B** | On the `-p` surface `--session-id` **is** honoured exactly: the requested UUID comes back as `session_id` in `--output-format json`. | The flag is functional. The discard is specific to `--bg`, not a broken flag. |
| **C** | On the `-p` surface a **second** process asking for an in-use UUID is **refused**: `Error: Session ID <uuid> is already in use.`, exit **1**, no session started. Tested both against a *finished* session (E5) and against a **live, mid-turn** one (E7), where the first process kept the UUID and completed normally. | Pre-spawn identity choice plus refusal of a second claimant — the ingredients F6's fence is built from — exist on `-p`, and are simply not reachable from `--bg` (V18, re-confirmed below). This **removes U13's stated objection** for the `-p` **CLI** surface. It is *not* the full fence F6 specifies (§5.3), *not* evidence about the SDK (U29), and *not* evidence about the crash window (U27/U28). |

Result **C** is the most consequential finding beyond U1 itself, and it is a *new* fact: the proposal
recorded (§5.3, O6) that "no source states a second process using the same UUID is **refused**". One
now does — this experiment. See §5.

---

## 2. Environment

```
$ claude --version
2.1.234 (Claude Code)

$ which claude
/home/happy_ryo/.local/bin/claude
```

Working directory for every spawn was a scratch directory outside the repository, deliberately **not**
a git repository, so that background-session worktree isolation (V10) could not touch the Interlock
worktree:

```
/tmp/claude-1000/-home-happy-ryo-work-org-workers-interlock--worktrees-interlock-u1-experiment-20260818/f0ac0d8e-9ee4-446b-9bbf-1473ec0c010b/scratchpad/u1
```

Relevant `claude --help` entries, verbatim (2.1.234):

```
  --bg, --background                    Start the session as a background agent
                                        and return immediately (manage with
                                        `claude agents`)

  --session-id <uuid>                   Use a specific session ID for the
                                        conversation (must be a valid UUID)
```

Neither entry mentions the other. The discard is documented **nowhere in the help text** — it surfaces
only at runtime, as the warning in §3.2.

---

## 3. Transcript

All commands and raw output, verbatim and in execution order. Six model-backed invocations were made
in total: two real `--bg` sessions (E2, E3) and four `-p` runs (E4, E5, and E7's two). Both background sessions were
stopped and removed afterwards (§4).

### 3.1 E1 — baseline roster

```
$ claude agents --json --all
[
  {
    "id": "1b5c7f25",
    "cwd": "/home/happy_ryo/work/org/claude-org-ja/.dispatcher",
    "kind": "background",
    "startedAt": 1781839887949,
    "sessionId": "1b5c7f25-7831-4564-9be3-7697eff61e2b",
    "name": "mcp__org-broker__check_messages メッセージ確認",
    "state": "failed"
  },
  {
    "id": "f41a4ced",
    "cwd": "/home/happy_ryo/work/org/claude-org-ja/.dispatcher",
    "kind": "background",
    "startedAt": 1781844142830,
    "sessionId": "f41a4ced-99f6-4826-9573-f4c6dfd477a2",
    "name": "i appreciate the context, but i'm unabl…",
    "state": "blocked"
  },
  {
    "id": "45841c01",
    "cwd": "/home/happy_ryo/work/org/claude-org-ja",
    "kind": "background",
    "startedAt": 1786081002984,
    "sessionId": "45841c01-d7a8-43da-bcdb-92529a4cf525",
    "name": "renga 2.0 リリース情報",
    "state": "done"
  }
]
```

Note in passing: every row here carries `sessionId == id + "-" + <suffix>`, i.e. the short `id` is the
first UUID group of `sessionId`. That relationship holds for every session observed in this
experiment too, and is worth recording as an observed regularity — not a documented contract.

### 3.2 E2 — the U1 experiment proper

First attempt was blocked by this worker's own sandbox, not by the CLI. Recorded for honesty because
it is the only failure in the run that is **not** a finding about the provider:

```
$ claude --bg --session-id 1ef3dc35-cfdb-4dbc-8b55-e032d4de2ea5 "reply with ok"
Couldn't start the session — EROFS: read-only file system, mkdir '/home/happy_ryo/<config-dir>/jobs/d7560ef3'
--- EXIT CODE: 1 ---
```

(The CLI's per-user job directory is outside this worker's writable set. Incidentally this shows a
**job directory** being allocated under an id — `d7560ef3` — that is neither the requested UUID nor,
as below, the eventual session id.)

Re-run with the sandbox lifted for that command only:

```
$ claude --bg --session-id 1ef3dc35-cfdb-4dbc-8b55-e032d4de2ea5 "reply with ok"
warning: --bg manages the session id; ignoring --session-id (use --resume <id> to continue an existing session)
Starting background service…
backgrounded · 3afc043c
  claude agents             list sessions
  claude attach 3afc043c    open in this terminal
  claude logs 3afc043c      show recent output
  claude stop 3afc043c      stop this session
--- EXIT CODE: 0 ---
```

Roster readback for that session:

```
$ claude agents --json --all     # filtered to the new session
{
  "pid": 885585,
  "id": "3afc043c",
  "cwd": "/tmp/.../scratchpad/u1",
  "kind": "background",
  "startedAt": 1787023485238,
  "sessionId": "3afc043c-3dae-421b-a242-beff7604d024",
  "name": "reply with confirmation",
  "status": "idle",
  "state": "done"
}

$ claude agents --json --all | grep -c "1ef3dc35"
0
```

The requested UUID `1ef3dc35-cfdb-4dbc-8b55-e032d4de2ea5` appears **nowhere** in the roster. The
session's identity is `3afc043c-3dae-421b-a242-beff7604d024`, assigned by the CLI.

This is a **real `--bg` Claude session, not an `--exec` job** — the distinction F4 insists on. It has a
conversation (`name` was model-generated from the prompt: `"reply with confirmation"`), it reached
`state: done` with `status: idle`, and it was attachable via `claude attach`.

### 3.3 E3 — the collision case under `--bg`

Requesting the UUID of the session created in E2, which was live in the roster at the time:

```
$ claude --bg --session-id 3afc043c-3dae-421b-a242-beff7604d024 "reply with ok"
warning: --bg manages the session id; ignoring --session-id (use --resume <id> to continue an existing session)
backgrounded · a2c05ef7
  claude agents             list sessions
  claude attach a2c05ef7    open in this terminal
  claude logs a2c05ef7      show recent output
  claude stop a2c05ef7      stop this session
--- EXIT CODE: 0 ---
```

Identical warning, exit 0, and a **new** unrelated id `a2c05ef7`. Neither refused nor overwritten: the
collision is never evaluated, because the input that could collide was discarded first.

### 3.4 E4 — control: is the flag functional at all?

```
$ claude -p --session-id 02be33b3-d084-4f9a-b4f8-a39e2d8cfe71 "reply with ok" --output-format json
{"is_error":false,"duration_api_ms":2406,"num_turns":1,"stop_reason":"end_turn",
 "session_id":"02be33b3-d084-4f9a-b4f8-a39e2d8cfe71", ... "result":"ok", ...}
--- EXIT CODE: 0 ---
```

(Response elided at `...` for length; the `session_id` field is verbatim and is the whole point.)

The requested UUID is honoured exactly. So E2/E3 are **not** a broken or inert flag — `--bg`
specifically discards a flag that works elsewhere.

### 3.5 E5 — collision on the surface that honours the flag

Re-running E4's command with the **same** UUID, now in use:

```
$ claude -p --session-id 02be33b3-d084-4f9a-b4f8-a39e2d8cfe71 "reply with ok" --output-format json
--- stdout ---
(empty)
--- stderr ---
Error: Session ID 02be33b3-d084-4f9a-b4f8-a39e2d8cfe71 is already in use.
--- EXIT CODE: 1 ---
```

Refused, exit 1, nothing on stdout, no session started.

**Read this narrowly.** E4's process had already exited when E5 ran, so on its own E5 shows only that a
*persisted* session id cannot be re-claimed. That is not the same as excluding a **live** second
writer, which is what U13 asks and what gate item 2 needs. E7 was run to close that gap.

### 3.6 E6 — re-confirming V18 on this build

```
$ claude --bg -p --session-id 02be33b3-d084-4f9a-b4f8-a39e2d8cfe71 "reply with ok"
--bg and --print conflict: --print never starts the interactive session that `claude agents` attaches to,
so the job would be unattachable. The prompt is the positional — drop --print: `claude --bg '<task>'`
--- EXIT CODE: 1 ---
```

V18 holds on 2.1.234, and the message states the *reason*: `--print` never starts the interactive
session that `claude agents` attaches to. The two surfaces are mutually exclusive by construction, not
by oversight — which matters for §5.

### 3.7 E7 — concurrent claimant against a **live** session

*Run after the others, to close the gap E5 leaves.* A first process was started with a chosen UUID
and a prompt long enough to keep it mid-turn; while it was still running (verified with `kill -0` immediately before launching the second), a second process
claimed the same UUID:

```
UUID_C=c54a9db9-a4d1-4445-940d-05d84408d385

# P1, backgrounded by the shell, still running:
$ claude -p --session-id $UUID_C "Count from 1 to 30, writing one short sentence about each number. Take your time." --output-format json &
P1 pid=979196

CONFIRMED: P1 still running at moment of P2 launch

# P2, launched ~3s later while P1 is alive and mid-turn:
$ claude -p --session-id $UUID_C "reply with ok" --output-format json
--- P2 stdout ---
(empty)
--- P2 stderr ---
Error: Session ID c54a9db9-a4d1-4445-940d-05d84408d385 is already in use.
P2 EXIT CODE: 1

# P1 then completed normally, keeping the id:
P1 EXIT CODE: 0
P1 session_id: c54a9db9-a4d1-4445-940d-05d84408d385
```

**The live second writer is excluded, and the first writer is unaffected** — it neither aborted nor
lost the id. One claimant proceeds and the other is refused rather than admitted alongside it — a
necessary property for item 2, though not on its own a sufficient one (§5.3).

Still not shown by E7: behaviour when the two processes start *simultaneously* (a genuine race on the
claim itself, rather than a second claim arriving after the first is established), and behaviour after
the holder is killed ungracefully. See U27/U28.

---

## 4. Cleanup

Both background sessions started by this experiment were stopped and removed:

```
$ claude stop 3afc043c   → stopped 3afc043c
$ claude rm   3afc043c   → removed 3afc043c
$ claude stop a2c05ef7   → stopped a2c05ef7
$ claude rm   a2c05ef7   → removed a2c05ef7

$ claude agents --json --all | grep -E "3afc043c|a2c05ef7"
(no matches — neither test session remains in the roster)
```

No worktrees were created (the scratch cwd was not a git repository, so V10's isolation did not
engage). The three pre-existing sessions in E1 were left untouched. The `-p` runs leave transcripts on
disk under the CLI's per-user project directory but hold no roster row and no process.

---

## 5. What this means — proposed reading

The proposal's Decision 6a has an explicit tail: *if the experiment fails, search for any other
**pre-spawn** fence, and if none exists, fail item 2 and open the `Q-0004` path rather than
substituting post-hoc adoption (F6).* The experiment failed. The tail therefore applies, and this
section proposes — but does not decide — how.

### 5.1 Gate item 2 on Agent View: the fence search is **triggered**, and is not done here

`ACCEPTANCE.md` §1 item 2 requires that after a kill at each injection point, re-identification yields
exactly one session per run, and that a single-writer violation at any injection point is a gate
failure. §3.3 states the dependency plainly: *"If U1 fails, this item fails"* unless another
**pre-spawn** fence is found.

On the `--bg` surface there is now no identity input at all. The crash window F3 describes — between
"spawn issued" and "identity known" — cannot be closed by construction, because the caller has no way
to commit a binding before the spawn. And per F6, post-hoc attribute matching on `cwd` / `startedAt` /
`name` does not rescue it: `startedAt` is only knowable after the spawn, `name` is a display name and
not an identity (V20, and E2 shows the CLI *generates* it from the prompt — `"reply with
confirmation"` for the prompt `"reply with ok"`, so it is not even caller-controlled), and a
crash-then-retry can leave two live sessions matching one intent before any reconciler runs.

**But that is only the `--session-id` route.** Decision 6a's tail permits failing item 2 *after* a
search for **any other pre-spawn fence** comes up empty, and this experiment did not conduct that
search — it tested one hypothesis, `--session-id` with `--bg`, and refuted it. An independent token
that Interlock itself mints and that protected writes carry, or some other provider handle not
examined here, is not excluded by anything above.

So the accurate statement is narrower than a verdict:

> **U1 is negative, which triggers Decision 6a's fence search for C1. That search is outstanding. If
> it comes up empty, item 2 fails on Agent View and routes to `Q-0004`; if it finds a pre-spawn fence,
> item 2 survives on that fence instead.**

What this note *can* say without the search is that the search must be for a **pre-spawn** handle:
per F6, post-hoc adoption is not an available answer, so a search that returns only a reconciliation
rule has come up empty for these purposes. Both the search and the resulting verdict are a human's,
in a `D-` entry.

### 5.2 The failure mode is *soft*, and that is a finding in its own right

E2 and E3 both **exit 0**. A caller that passes `--session-id`, checks the exit code, and proceeds to
write a binding row to SQLite would record an identity the session does not have — silently, for every
spawn. The only signal is an unstructured warning line on the CLI's output, which is not in
`--output-format json` (unavailable under `--bg` anyway, per E6) and not in `claude agents --json`.

Any Interlock code path that ever passes `--session-id` alongside `--bg` must therefore treat exit 0
as **insufficient** and read the identity back from the roster. This is worth stating regardless of
the `Q-0004` outcome, because it is the kind of fail-open the charter's `D-0010` posture exists to
catch, and it is adjacent to F2's evidence (V15/V16) that this codebase's habit under bad input is to
ignore-and-continue rather than refuse.

### 5.3 The fence F6 asked for exists — one surface away

This is the result that should shape the `Q-0004` conversation. F6 asks what would rescue item 2:

> some identifier or token committed to SQLite before the spawn that the second writer's protected
> writes must carry and that the first commit invalidates

E4, E5 and E7 show that the `-p` surface has the **first two ingredients** of that: the caller chooses
the UUID before the spawn (E4), a second process claiming a finished session's id is refused (E5), and
a second process claiming a **live** session's id is refused with exit 1 while the first writer
proceeds unharmed (E7).

**That is not yet the fence F6 specifies, and the difference should not be blurred.** F6 asks for a
token that *protected writes carry* and that *the first commit invalidates*. E7 shows something
weaker: refusal of a late claimant against an already-established holder. It does not show that two
simultaneous claims resolve atomically, and it does not make the UUID a fencing token on the write
path — nothing here demonstrates that a write is checked against the claim at the moment it lands.
Those are the two properties item 2's kill-and-retry predicate actually turns on.

So the honest formulation is: **what this removes is an objection, not the remaining work.**

1. **`U13`'s stated objection no longer stands for the `-p` CLI surface.** §5.3 grades C2 as `~` on O6
   with the reason "no source states a second process using the same UUID is **refused**". One now
   does, for a live session. That specific reason for the `~` is discharged.

   **What should *not* follow.** The grade should **not** move to `Y` on this evidence. `Y` on O6
   would assert identity across the crash window, and the crash window is exactly what remains
   untested (U27, U28). C2 stays at `~` with a better-founded reason: refusal is observed, atomicity
   and crash behaviour are not. Nor is this evidence about the SDK — C3's O6 grade should not move at
   all, because although `W1a` records that the SDK's default arrangement is a spawned CLI child,
   which makes inheritance plausible, plausibility is not what this document trades in (§1.1). See
   U29.

2. **The exclusion is unreachable from Agent View, and E6 says why.** `--bg` and `-p` conflict because
   `--print` never starts the interactive session `claude agents` attaches to. So this is not a gap
   that a future flag combination is likely to close by accident — the surfaces are disjoint by
   design. Agent View does not fail item 2 for want of a fence in the product; it fails because the
   fence lives on the surface Agent View is defined in opposition to.

### 5.4 What is *not* claimed

- This says nothing about gate items 1, 3–11. Only item 2's U1 dependency was under test.
- It does not select a `Q-0004` candidate. It supplies one input (O6 evidence for C1 and C2) to a
  decision that weighs seven candidates against six obligations.
- It does not propose softening item 2. Per F6, an adoption rule that picks a winner without proving
  the loser never wrote would be a reclassification wearing the clothes of a mitigation; on a negative
  U1 the honest move is to look for a real pre-spawn fence and, failing that, change providers — not
  to redefine the predicate.
- **It does not conduct Decision 6a's search for another pre-spawn fence, and so does not deliver an
  item 2 verdict.** It refutes one hypothesis (`--session-id` under `--bg`) and thereby triggers that
  search. See §5.1.
- The `-p` refusal was observed against a finished holder (E5) and a live one (E7), both in the same
  directory. It was **not** tested across directories, under a simultaneous race on the claim itself,
  or after an ungraceful kill of the holder — and that last one is the injection point item 2 actually
  names. See U27/U28.

---

## 6. New unverified items this raises

Proposed additions to the Appendix A register, in its style. None was tested here.

| # | Question | Why it matters |
|---|---|---|
| **U27** | Is the `-p` refusal **atomic** under a genuine race — two processes issuing the claim simultaneously — or a check-then-create with a window? E7 shows exclusion of a claimant arriving *after* the holder is established, which is weaker. | Item 2's predicate is a kill-and-retry race. A non-atomic check does not discharge it. Highest-value follow-up. |
| **U28** | After an **ungraceful kill** (SIGKILL) of the holder: does `--resume <uuid>` still succeed, *and* is a fresh `--session-id <uuid>` claimant still refused? Both halves must hold together. E5 covered the cleanly-finished case, E7 the live case; neither covers SIGKILL. | This is the crash-window probe, and it must be framed around `--resume`, not around re-claiming the id: under C2 Interlock resumes a persisted session with `--resume`, so continued refusal of `--session-id` is the *desired* durable-identity behaviour, not an availability bug. The failure that matters is the opposite one — the claim releasing on crash, so a retry can create a second live writer. |
| **U29** | Is the refusal enforced by the shared session store or only by the `-p` entry path? If the former, a provider built on the SDK (`W1a`: a spawned CLI child) inherits it; if the latter, it may not. | Determines whether C3 inherits the E5/E7 property or only C2 has it. §5.3 deliberately does **not** move C3's O6 grade pending this. |
| **U30** | Does `--session-id` compose with `--bg --exec`? Not tested — and per F4 a job has no conversation, so a positive result there would be a false positive for U1 and must not be read as one. | Guards against a later probe "re-answering" U1 the wrong way. |
| **U31** | Is the E2/E3 warning emitted on stdout or stderr, and is it stable enough to detect programmatically? Not separated in this run (output was captured combined). | Only matters as a stopgap; §5.2's roster readback is the sound approach regardless. |

Also observed, not a question but not a contract either: `sessionId`'s first UUID group equals the
short `id` in every row seen (E1, E2). Convenient, undocumented, and should not be depended on.

---

## 7. Bottom line

`--session-id` does not compose with `--bg`; it is discarded with a warning and exit 0, and the
collision case does not arise because there is no identity input to collide. U1 is **negative**.
Following Decision 6a's tail, this note **proposes** that the search for another pre-spawn fence on
Agent View is now triggered, and that item 2 fails and the `Q-0004` path opens if that search comes up
empty. It also reports that on the `-p` surface identity can be chosen pre-spawn and a second
claimant — live or finished — is refused. Those are **ingredients** of F6's fence, not the fence
itself — F6 additionally requires a token that
protected writes carry and that the first commit invalidates, and neither write-time fencing nor
atomicity under a simultaneous race was tested (U27/U28). What they do remove is `U13`'s stated
objection for C2. They do not rescue C1 either way, since E6 confirms the two surfaces cannot be
combined.

For C1 itself, the negative U1 result **triggers** Decision 6a's search for some other pre-spawn
fence; that search is outstanding, and the item 2 verdict follows from it rather than from this note.

The gate verdict itself is left to a human and to a subsequent `D-` entry.
