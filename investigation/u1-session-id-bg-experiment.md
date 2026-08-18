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
| **C** | On the `-p` surface a **second** process asking for an in-use UUID is **refused**: `Error: Session ID <uuid> is already in use.`, exit **1**, no session started. | This is exactly the pre-spawn fence F6 asks for — and it exists. It is simply not reachable from `--bg` (V18, re-confirmed below). It closes the single-writer half of **U13** for the `-p`/SDK surface. |

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

All commands and raw output, verbatim and in execution order. Four model-backed invocations were made
in total: two real `--bg` sessions (E2, E3) and two `-p` runs (E4, E5). Both background sessions were
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

Refused, exit 1, nothing on stdout, no session started. **The second writer is excluded.**

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

### 5.1 Gate item 2 on Agent View: **proposed FAIL**

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

**Proposed verdict: gate item 2 fails on Agent View (C1), and this routes to `Q-0004` per Decision
6a's tail.** Confirmation is a human's, in a `D-` entry.

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

E4 and E5 together show the CLI already implements precisely that, on the `-p` surface: the caller
chooses the UUID before the spawn (E4), and a second process claiming it is **refused with exit 1**
(E5). That is a pre-spawn fence with second-writer exclusion — the exact shape item 2 needs.

Two consequences, both proposals:

1. **`U13` is answered for the `-p`/SDK surface, and O6 should be re-scored.** §5.3 grades C2 as `~`
   on O6 with the reason "no source states a second process using the same UUID is **refused**". One
   now does. If a human accepts this experiment as evidence, C2's O6 grade moves toward `Y` — which
   materially changes the `Q-0004` comparison, because O6 was named as *the* decisive obligation.
   Whether the refusal is a property of the shared session store (and so would apply to any surface
   that let you name an id) or only of the `-p` code path is **not** established here; see §6.

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
  the loser never wrote would be a reclassification wearing the clothes of a mitigation; the honest
  move on a negative U1 is to fail the item and change providers, not to redefine the predicate.
- The `-p` refusal was observed once, against a session created seconds earlier in the same directory.
  It was not tested across directories, across concurrent live processes racing for the same id, or
  after an ungraceful kill — the injection points item 2 actually names. See §6.

---

## 6. New unverified items this raises

Proposed additions to the Appendix A register, in its style. None was tested here.

| # | Question | Why it matters |
|---|---|---|
| **U27** | Is the `-p` "already in use" refusal (E5) atomic against *concurrent* claimants, or is it a check-then-create with a race window? Item 2's predicate is about a kill-and-retry race, so a non-atomic check does not discharge it. | Decides whether §5.3's rescue is real for C2 or only apparent. This is the single highest-value follow-up. |
| **U28** | Does the refusal survive the injection points item 2 names — a kill between claim and first write, a stale session file from an ungracefully-killed process, an id whose session is `done` vs still live? E5 tested only the live-and-healthy case. | A fence that releases on crash is not a fence for the crash window. |
| **U29** | Is the refusal enforced by the shared session store or only by the `-p` entry path? If the former, a provider built on the SDK (`W1a`: a spawned CLI child) inherits it; if the latter, it may not. | Determines whether C2 and C3 both inherit the E5 property or only C2. |
| **U30** | Does `--session-id` compose with `--bg --exec`? Not tested — and per F4 a job has no conversation, so a positive result there would be a false positive for U1 and must not be read as one. | Guards against a later probe "re-answering" U1 the wrong way. |
| **U31** | Is the E2/E3 warning emitted on stdout or stderr, and is it stable enough to detect programmatically? Not separated in this run (output was captured combined). | Only matters as a stopgap; §5.2's roster readback is the sound approach regardless. |

Also observed, not a question but not a contract either: `sessionId`'s first UUID group equals the
short `id` in every row seen (E1, E2). Convenient, undocumented, and should not be depended on.

---

## 7. Bottom line

`--session-id` does not compose with `--bg`; it is discarded with a warning and exit 0, and the
collision case does not arise because there is no identity input to collide. U1 is **negative**.
Following Decision 6a's tail, this note **proposes** that gate item 2 fails on Agent View and that the
`Q-0004` path opens. It also reports that the pre-spawn fence F6 specifies demonstrably exists on the
`-p` surface, with second-writer exclusion — which strengthens the `Q-0004` alternatives rather than
rescuing C1, since E6 confirms the two surfaces cannot be combined.

The gate verdict itself is left to a human and to a subsequent `D-` entry.
