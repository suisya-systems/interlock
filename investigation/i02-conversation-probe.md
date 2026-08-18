# I-02 — multi-turn `--resume` and working-tree ownership on real `claude -p` workers

**Status:** answered — the item 1 cycle runs on a real multi-turn worker, item 7's negative is executed, and U38 is closed with a hazard
**Date:** 2026-08-18
**Task:** `interlock-i02-conversation-probe-20260818` (issue #7, phase 1b, scaffold S4)
**Scope:** propose-only. This note records experiments and *proposes* readings for gate items 1 and 7.
It decides nothing. No `D-` entry is created here and no design document is edited.
**Refs:** issue #7; `D-0010` (public CLI only, fail closed), `D-0025`, `D-0026` (throwaway),
`D-0027` (C2 is the spike's `SessionProvider`); `ACCEPTANCE.md` §1 items 1 and 7;
`investigation/i01-supervisor-probe.md` (H1-H4, U34/U36, U3),
`investigation/pre-spawn-fence-search.md` (U28, U32, U33, A4, A6),
`investigation/u1-session-id-bg-experiment.md`.
**Harness:** `investigation/i02_conversation_probe.py`, `investigation/i02_internals_free_negative.sh`.

---

## 1. Verdict

Under C2 a session is a session id plus a transcript, and re-entering it means spawning a fresh
`-p` process with `--resume`. Driven that way on a real multi-turn worker, **the item 1 cycle
closes and item 7's negative comes back clean** — with one new hazard that changes what a session
id is worth.

The positive half, all of it measured on CLI 2.1.234:

| | Result |
|---|---|
| **The whole cycle on one real worker** | start → structured state read from published output → **stop** (SIGTERM mid-turn, rc 143) → resume, documented flags only, on a worker holding a genuine four-turn conversation (§3.1). |
| **Continuity across three successive resumes** | Three fresh processes, one `session_id` throughout, **one transcript growing in place** (11 → 18 → 26 → 34 lines, same path, one `sessionId` inside). **No fork-like behaviour** of the U33 class was observed on `-p` (§3.2). |
| **Continuity across the stopped turn** | The fourth process recalled both the codeword *and* that the previous turn had been interrupted at item 20 of 40. A SIGTERM'd turn is persisted, partial output and a `[Request interrupted by user]` marker included (§3.2). |
| **Supervisor kill and restart, both child cases** | Child that died with its parent (shared process group, `SIGKILL` to the group) and child that outlived it (reparented to pid 837, still counting). In both, the restarted supervisor read **persisted state only**, resolved the child first, and then resumed. The live orphan was terminated and its exit confirmed at **2.6 s**, its MCP descendants with it (§3.3). |
| **Internals-free negative, on this half and the right way round** | The restriction is on **the harness**, never the child. With the config directory an empty tmpfs and every socket an empty file *for the harness*, create-and-resume behaved identically and the child recalled its codeword; the child's transcript landed in the **real** config directory (§3.4). |
| **Working-tree ownership** | Across every transition on a fixture carrying uncommitted, untracked **and** unpushed work, the tree stayed **byte-identical by recorded hash** — baseline `0b13bdcc…` at the start, `0b13bdcc…` at the end, `git status` unchanged. The child never moved its `cwd` and created no worktree, **including when given an edit-forcing task** (§3.5). |

The negative half is one hazard and one economics fact:

| # | Hazard | Evidence |
|---|--------|----------|
| **H5** | **Removing the transcript releases the `--session-id` claim, and the id is then re-claimable as if new.** The same id was refused (`rc 1`, *already in use*) while its transcript existed and **admitted (`rc 0`) once the file was moved away**, writing a *fresh* 11-line transcript at the same path. So the claim is a file-existence check: anything that can unlink the file — a cleanup sweep, a disk-full eviction, another tool, an operator — silently clears it. A session id is therefore **not durable identity**; it is a name whose uniqueness lasts exactly as long as a file nobody is guarding. This answers **U38** and it closes U36's open half (§3.6). |

| # | Fact | Evidence |
|---|------|----------|
| **Spawn cost is dominated by prompt-cache state, not by configuration** | The *same* arm, run twice, cost **USD 0.379 cold and 0.038 warm** — a 10× swing with identical argv. `--strict-mcp-config` cut the child's tool list from 107 to 26 and its cold cache-creation tokens from 17 949 to 15 186 (−15 %), which is real but an order of magnitude smaller than the cache effect (§3.7). i01 §5.4's "spawn cost is governed by inherited configuration" needs this correction: it is governed by *cache locality first*, configuration second. |

**Worker count and quota.** 12 distinct sessions, **22 model-backed `-p` invocations** (19 foreground
plus 3 detached supervisor children). 17 captured a `total_cost_usd`, summing to **USD 3.61**; the
detached children, the refused claim and the aborted turn did not, so 3.61 is a floor. Every process
started was terminated and reaped; the roster shows **zero rows** from this experiment (§4).

---

## 2. Environment, and which side of the boundary each restriction was applied to

```
$ claude --version
2.1.234 (Claude Code)

$ claude doctor
Running: native (2.1.234)
Commit: 7215ba60b06d
Platform: linux-x64
Path: /home/happy_ryo/.local/share/claude/versions/2.1.234
Config install method: native
No installation issues found.
```

Same build as `u1-session-id-bg-experiment.md`, `pre-spawn-fence-search.md` and
`i01-supervisor-probe.md`, so all four notes compose without a version caveat.

**The working directory is deliberately different from the three prior notes.** They spawned into a
non-git scratch directory; item 7 requires the opposite, so every spawn here ran with `cwd` set to a
**scratch git working tree outside the Interlock repository**:

```
/tmp/claude-1000/-home-happy-ryo-work-org-workers-interlock--worktrees-interlock-i02-conversation-probe-20260818/
  2a5932c4-bf92-4e4e-98e6-c1c897ecb9b1/scratchpad/i02/real/fixture
```

The fixture is built by `i02_conversation_probe.py fixture` and carries all three states item 7 names:

```
$ git status --porcelain=v1 -uall
M tracked_dirty.txt          # uncommitted: a tracked file edited in the working tree
?? untracked_dir/deep.txt    # untracked, one level down
?? untracked_note.txt        # untracked

$ git rev-list --count @{u}..HEAD
1                            # unpushed: one local commit the upstream does not have
```

The upstream is established by **cloning a seed repository, not by pushing** — `git push` is denied
to this worker by policy, and a clone produces the same ahead-of-upstream shape. A second, identical
fixture (`real2/fixture`) exists solely for §3.5's edit-forcing probe, so that the deliberate write
lands somewhere other than the tree the other transitions are graded on.

**Sandbox statement, as issue #7 requires.** Two different restrictions are in play and must not be
confused.

1. **This worker's own sandbox was lifted for every model-backed command in this note.** The fence
   search (§2 of that note) established that under the worker sandbox a `-p` run succeeds and returns
   a `session_id` while **no transcript is ever written**. Since the whole subject here is the
   transcript — continuity, growth in place, and §3.6's claim release — running any of it under that
   sandbox would have manufactured false results throughout. Every command below ran with the
   sandbox disabled, deliberately, and no other permission was bypassed.
2. **The internals-free negative applies its restriction to the harness process only** (§3.4), with
   the child handed its state back through a nested `bwrap`. Denying the *child* its own transcript
   is the trap the fence search fell into; under C2 it would also deny the provider its documented
   function, and is not what was done.

**Public surface only, with two declared exceptions.** The supervisor path in the harness touches no
path under the CLI's config directory. Two subcommands read (and in one case move) transcripts **on
purpose** and are not part of the supervisor surface: `transcripts`, which must look at the file to
say whether it grew in place or forked, and `u38`, which must move it to answer §3.6. Both stamp
`internals_observer: true` into their own records.

**Standing safety rule for §3.6, enforced in code.** The `u38` subcommand generates its own uuid and
will only touch a file whose basename is exactly `<that uuid>.jsonl`; anything else is refused and
the refusal is recorded. It **moves** the file to a scratch quarantine rather than deleting it. No
transcript belonging to a real session was touched, and the classifier did not block the operation
this time (contrast i01 §3.8.4, where it did).

---

## 3. Transcript

Records are written verbatim, one JSON object per step, by `i02_conversation_probe.py` to
`$I02_OUT/records.jsonl`. Excerpts below are quoted from those records; long arrays are elided at
`…` and nothing else is edited.

**Harness self-test before any quota was spent.** Every instrument — the SIGTERM path, the CLI-pid
resolution under a wrapper, the child-`cwd` sampler, the tree-hash differ, both supervisor-restart
branches, and the negative's `bwrap` plumbing — was first exercised against a stub `claude` binary
that emits the same stream-json framing and no model turns. Two defects were found and fixed there
rather than in a paid run (the `--extra` passthrough, and a `same_group` option the "died with its
parent" case needs at all).

### 3.1 E1 — the whole item 1 cycle, on one real multi-turn worker

One session id (`dd4f6ff0-d0e3-4f7c-a9c2-6d0ed01a7344`), four processes, in the fixture directory.

```
$ claude -p "Remember this codeword: TANGERINE-7. Reply with just: stored" \
    --output-format stream-json --verbose \
    --session-id dd4f6ff0-d0e3-4f7c-a9c2-6d0ed01a7344
```

| # | Verb | argv | rc | `init` at | result |
|---|------|------|----|-----------|--------|
| 1 | **start** + **structured read** | `-p … --session-id <U>` | 0 | 3.06 s | `stored` |
| 2 | resume | `-p … --resume <U>` | 0 | 2.96 s | `TANGERINE-7` |
| 3 | resume + **stop** | `-p <40-item counting prompt> --resume <U>`, SIGTERM 8 s after `init` | **143** | 2.36 s | `is_error: true`, `terminal_reason: "aborted_streaming"` |
| 4 | resume | `-p … --resume <U>` | 0 | 2.46 s | recalled the codeword *and* the interruption |

**The structured state read is machine-parseable from published output**, not scraped from rendered
screen text: it is the `system/init` event on `--output-format stream-json`, whose keys are exactly
the set i01 §3.9 recorded —

```
agents, analytics_disabled, apiKeySource, capabilities, claude_code_version, cwd,
fast_mode_disabled_reason, fast_mode_state, mcp_servers, memory_paths, messaging_socket_path,
model, output_style, permissionMode, plugins, product_feedback_disabled, session_id, skills,
slash_commands, subtype, terminal_slash_commands, tools, type, uuid
```

and which on the resumed processes reported `session_id` = the resumed id, `cwd` = the fixture,
`model: claude-fable-5`, `permissionMode: auto`, 128 tools, 56 skills, 5 agents, 7 MCP servers all
`connected`.

**The stop verb behaves as i01's H3 predicts on the interrupt path, with one difference worth
recording.** Signalled with SIGTERM, the CLI exited **143** (not 0), while its own `result` event
carried `is_error: true`, `subtype: "error_during_execution"`, `terminal_reason: "aborted_streaming"`
and `total_cost_usd: 0`. i01's H3 case exited 0 on SIGINT with the same JSON. So the exit code
disagrees with the payload in one direction on SIGINT and agrees in the other on SIGTERM: **read the
payload, and never infer the payload from the code.**

The two MCP children the CLI inherited (`renga mcp-peer`, a `bun` server) were enumerated *before*
the signal — the i01 correction — and both were gone when re-checked afterwards. No survivors.

### 3.2 E2 — continuity, and whether the transcript grows or forks

The codeword probe of A4's shape (fence search §3.3.1), repeated across three resumes in fresh
processes. After each, an observer read the transcript directly:

| After | files carrying the id | size | lines | `sessionId` values inside | user/assistant |
|-------|----------------------|------|-------|---------------------------|----------------|
| create | 1 | 56 330 | 11 | 1 | 1 / 1 |
| resume 1 | 1 | 59 674 | 18 | 1 | 2 / 2 |
| resume 2 (stopped) | 1 | 65 655 | 26 | 1 | 4 / 4 |
| resume 3 | 1 | 71 095 | 34 | 1 | 6 / 6 |

```
$ find ~/.claude/projects -name "*dd4f6ff0-d0e3-4f7c-a9c2-6d0ed01a7344*"
…/-tmp-…-scratchpad-i02-real-fixture/dd4f6ff0-d0e3-4f7c-a9c2-6d0ed01a7344.jsonl   71095 bytes
```

**One file, one id, monotonically growing — no fork.** This is the direct negative of U33: under
`--bg`, `--resume` silently behaved as `--fork-session`, copying the history into a new transcript
under a new id. On `-p` it does not. The identity handle is honoured as an identity handle, and the
`session_id` reported in both `init` and `result` was the resumed one in every process.

The stopped turn is persisted in full, which matters because the next resume inherits it:

```
assistant  TANGERINE-7
user       Count from 1 to 40, writing one short sentence about each number. Take your time.
assistant  1. 1 はすべての始まりであり、唯一の乗法単位元です。\n2. 2 は最小の素数で… (partial)
user       [Request interrupted by user]
```

and the fourth process answered:

```
コードワードは TANGERINE-7 で、中断時は 1〜40 のカウントで 20 番目の文を書いている途中でした。
```

So a stopped turn is not a hole in the conversation: the partial assistant output and an explicit
interruption marker are both replayed to the next process. A supervisor that stops a worker mid-turn
is handing its successor a conversation that *knows it was stopped*.

### 3.3 E3 — resume across a supervisor restart, with the child resolved first

The order is the point. Resuming past a live orphan creates a second writer the provider will not
refuse (U32), so the restarted supervisor resolves the child **before** issuing `--resume`. Both
cases the issue names were produced.

**Case A — a child that died with its parent.** The supervisor was run under `setsid` with the child
left in its process group (`--same-group`; the harness isolates children by default, so this option
exists only to make the case producible at all), then the **group** was SIGKILLed 12 s in:

```
before group kill: supervisor=2613506 alive=yes  child=2613508 alive=yes
                   child_pgid=2613506  sup_pgid=2613506
after  group kill: supervisor alive=no           child alive=no
```

A new process then read `stateA.json` — pid, argv, cwd, session id — and nothing else:

```json
{"persisted_pid": 2613508, "alive_at_restart": false, "identified_as_our_child": false,
 "action": "confirmed_already_gone", "alive_after_resolution": false}
```

then resumed: `rc 0`, same `session_id`, result `CINNABAR-33`.

**Case B — a child that outlived its parent.** Same shape, child isolated normally, and only the
supervisor SIGKILLed:

```
supervisor(only) killed: sup alive=no  child=2618539 alive=yes  reparented_ppid=837
```

H2 reconfirmed. The restarted supervisor identified the orphan from persisted state — and **not by
pid alone**: it requires the live command line to still carry the session id it spawned with, because
pid reuse would otherwise let it signal a stranger —

```json
{"persisted_pid": 2618539, "alive_at_restart": true,
 "cmdline_at_restart": "claude -p Remember this codeword: OBSIDIAN-58. … --session-id c1f9f880-47e2-4…",
 "identified_as_our_child": true, "action": "terminated", "gone_after_s": 2.606,
 "descendants_before_signal": [{"pid": 2618623, "cmdline": "… renga mcp-peer"},
                               {"pid": 2618628, "cmdline": "bun …/claude-peers-mcp/server.ts"}],
 "descendant_survivors": [], "alive_after_resolution": false}
```

— terminated it with SIGTERM, confirmed its exit at **2.606 s**, confirmed its MCP descendants gone
with it, and only then resumed: `rc 0`, same `session_id`, result `OBSIDIAN-58`.

The harness refuses the unsafe order structurally: if the child is still alive after resolution it
records `restart_aborted` and does not resume. That branch did not fire in either case.

**A third run is disclosed because it is evidence about timing, not a failure.** The first attempt at
case B killed the supervisor and then took about a minute to reach the restart, by which time the
orphan had **finished its turn and exited on its own**. The restart correctly found it gone and
resumed (`BASALT-19`, `rc 0`). That is a real and probably common shape — an orphan that completes
unsupervised — and it is why the two deliberate cases were then produced in single, tightly timed
runs. It also underlines H2 from the other side: an orphan does not merely survive, it *finishes the
work and writes it into the transcript* the supervisor will later resume.

### 3.4 E4 — the internals-free negative, on the conversation half

`i02_internals_free_negative.sh`. The restriction is applied to **the harness**; the child is handed
its state back:

```
### deny paths (applied to the harness only):
    /home/happy_ryo/.claude
    /tmp/claude-http-25896304fed559d7.sock
    /tmp/claude-http-67ececdf5ea6c18e.sock

### harness runs under:
    bwrap --dev-bind / / --bind <config-dir> <alias> --tmpfs <config-dir>
          --bind <sock> <alias-sock> --bind <empty-file> <sock>   (per socket)

### child argv is prefixed with:
    bwrap --dev-bind / / --bind <alias> <config-dir> --bind <alias-sock> <sock> …
```

**A — control, harness unrestricted, codeword `ZEPHYR-41`:**

```json
{"label":"negative:unrestricted","child_wrapper":[],
 "harness_view":{"/home/happy_ryo/.claude":{"kind":"dir","entries":34},
                 "/tmp/claude-http-…sock":{"errno":6,"strerror":"No such device or address"}}}
create : rc=0  session_id=bed4b888-96f2-43a1-9763-4ac5c30880ca  n_tools=128  result="stored"
resume : rc=0  session_id=bed4b888-96f2-43a1-9763-4ac5c30880ca  n_tools=128  result="ZEPHYR-41"
```

**B — negative, harness denied and child not, codeword `MARJORAM-92`:**

```json
{"label":"negative:restricted","child_wrapper":["bwrap","--dev-bind","/","/", …],
 "harness_view":{"/home/happy_ryo/.claude":{"kind":"dir","entries":0},
                 "/tmp/claude-http-…sock":{"kind":"file","first_bytes":0}}}
create : rc=0  session_id=2753e035-2fcf-4b85-afb9-5dadc2992bb8  n_tools=107  result="stored"
resume : rc=0  session_id=2753e035-2fcf-4b85-afb9-5dadc2992bb8  n_tools=128  result="MARJORAM-92"
```

The harness sees **0 entries** where it saw 34, and each socket is an empty regular file rather than a
socket. Spawn, the structured read, the resume, the recalled codeword, `rc 0` on all four, and a
byte-identical fixture on all four — identical on both sides of the restriction. Under restriction the
spawned pid is `bwrap` and the CLI is its child, so the harness resolves the CLI's own pid
(`spawned != cli` is true only in B), the correction i01's review forced.

**C — observer, outside the harness: did the restricted run's child keep normal access?**

```
restricted session id: 2753e035-2fcf-4b85-afb9-5dadc2992bb8
~/.claude/projects/…-scratchpad-i02-real-fixture/2753e035-….jsonl   60354 bytes
unrestricted session id: bed4b888-96f2-43a1-9763-4ac5c30880ca
~/.claude/projects/…-scratchpad-i02-real-fixture/bed4b888-….jsonl   59664 bytes
```

Yes. The restricted run's child wrote its transcript to the **real** config directory and then read it
back on resume — which is the whole point on this half: the conversation was reconstructed *by the
child from state the harness could not see*. Without this control the run would be a silent false
negative.

The 107-vs-128 tool counts are the artefact i01 §3.9 already named: one MCP server was `pending` at
`init` in one run and `connected` in the other. It is independent corroboration that a naive tool-list
diff is unsound, and it is **not** an effect of the restriction — the restricted resume reported 128.

**What this negative does and does not prove** is unchanged from i01 §3.10 and is not re-argued here:
the documented internal paths are denied to the harness at their real locations, the child demonstrably
keeps them, and the grant is not unforgeable, because every unprivileged kernel restriction available
in this environment is inherited by children.

### 3.5 E5 — working-tree ownership (item 7)

**Every transition, against a recorded hash.** The fixture's manifest is a sha256 per file (symlinks
by target string), rolled into one tree hash, plus the git triple. Baseline and final, after twelve
foreground transitions and three detached-child spawn-and-kill cycles:

```
BASELINE tree_hash: 0b13bdcc99fbc796aa194058620866159ac829a0b5ac3796225cf8e17fca130a
FINAL    tree_hash: 0b13bdcc99fbc796aa194058620866159ac829a0b5ac3796225cf8e17fca130a
byte_identical across the whole run: True
files added/removed/changed: []  []  []      dirs added/removed: []  []
git identical: True   (HEAD unchanged, ahead_of_upstream=1, same porcelain status)
```

Per-file baseline hashes, for the record:

```
tracked_clean.txt        a2df0051315e0812…   69 B   (carries the unpushed commit's line)
tracked_dirty.txt        41fa05a7002d5d1c…   66 B   (uncommitted edit)
untracked_note.txt       088717f48b03fb98…   49 B   (untracked)
untracked_dir/deep.txt   2d4e86f342c16e49…   26 B   (untracked, nested)
```

and per transition, from the records:

```
p1:create                    byte_identical=True  git_identical=True
p1:resume1                   byte_identical=True  git_identical=True
p1:resume2-stopped           byte_identical=True  git_identical=True   <- stop, mid-turn SIGTERM
p1:resume3                   byte_identical=True  git_identical=True
p2A:resume-after-restart     byte_identical=True  git_identical=True
p2B:resume-after-restart     byte_identical=True  git_identical=True
p2B2:resume-after-restart    byte_identical=True  git_identical=True
negative:unrestricted:create/resume   byte_identical=True  git_identical=True
negative:restricted:create/resume     byte_identical=True  git_identical=True
```

The three **detached** children (spawn, then the supervisor is killed, then the child is either killed
with it or terminated at restart) are not covered by a per-turn pair, because at spawn time the
supervisor is about to die. They are covered by the **cumulative** baseline-to-final comparison above,
which brackets every one of them. No transition was refused, and none needed to be: none of them
touched the tree at all. The untracked case is included explicitly and survived unchanged.

**The child never moved.** The harness samples `/proc/<cli-pid>/cwd` every 50 ms for the whole life of
every foreground run. Across all of them the sampler recorded exactly one value — the fixture — and
`git worktree list` reported one worktree before and after.

**The negative is executed, not argued from "we did not pass `--worktree`".** A6 of the fence search
watched an Agent View session relocate into `.claude/worktrees/probe` unasked, and it did so at the
moment Claude first needed to write a file. So the same trigger was reproduced on `-p`, in a second
identical fixture, with a prompt that forces a file write and a permission mode that lets it happen:

```
$ claude -p "Create a file called probe.txt containing the word hello, then reply with just: done" \
    --output-format stream-json --verbose --session-id … --permission-mode acceptEdits
rc=0  permissionMode=acceptEdits  num_turns=2  result="done"
child cwd samples: ['real2/fixture']            <- one value, the whole run
files_added: ['probe.txt']   dirs_added: []     <- written in place, at the top of the fixture
worktree_list before: …/real2/fixture  aa0b1df [main]
worktree_list after : …/real2/fixture  aa0b1df [main]
status after: 'M tracked_dirty.txt\n?? probe.txt\n?? untracked_dir/deep.txt\n?? untracked_note.txt'
```

**The `-p` child edited the working tree it was given and created no worktree of its own.** The
uncommitted, untracked and unpushed states all survived the edit untouched; the only difference is the
file the prompt asked for. A6's relocation is therefore an **Agent View behaviour, not a CLI-wide
one**, and it does not reproduce on the surface C2 uses.

### 3.6 E6 — U38: does removing the transcript release the claim?

i01 §3.8.4 left this half of U36 open because the step was blocked by this worker's own auto-mode
classifier. The operator authorised the follow-up on 2026-08-18, limited to a session the experiment
created itself. It ran, on `88b913f0-a9e9-4913-acb9-84b594a0361c`, created in the fixture directory:

```
1. create        --session-id 88b913f0-…      rc=0   transcript: 56162 B, 11 lines
2. re-claim      --session-id 88b913f0-…      rc=1   1.45 s
                 stderr: Error: Session ID 88b913f0-a9e9-4913-acb9-84b594a0361c is already in use.
3. move the transcript out of the config directory into a scratch quarantine
                 moved:     …/projects/<slug>/88b913f0-….jsonl  (56162 B)
                 remaining: []            <- no path anywhere under the config dir carries the id
4. re-claim      --session-id 88b913f0-…      rc=0   6.93 s   ADMITTED
                 result: "ok",  session_id = 88b913f0-…
                 transcript at the same path again: 56146 B, 11 lines — a NEW conversation
```

**U38 is answered: removing the transcript releases the claim.** The refusal is not a record of the id
having ever been used; it is a stat of one file. Move the file and the id is fresh again — same path,
same name, a conversation with no history in it.

Three consequences, and none of them is small:

1. **The `-p` refusal is not a lock.** It has no owner, no lease and no lifetime; it is a side effect
   of a file existing. i01's H4 already showed it can be side-stepped by changing `cwd`. H5 shows it
   can be *cleared outright* by anything with write access to the config directory — a cleanup sweep,
   a disk-pressure eviction, an unrelated tool, a person. Combined with U32 (`--resume` excludes
   nothing) there is no configuration of documented flags in which the provider refuses a second
   writer reliably.
2. **A session id is not durable identity.** Anything in Interlock that treats "the id is claimed" as
   evidence that the conversation still exists is reading a file's existence and calling it a
   guarantee. After a sweep the id re-binds silently to an empty conversation — no error, `rc 0`, the
   same id echoed back in `init` and `result`. That is exactly the shape of the "lost binding" failure
   U33 produced by another route.
3. **The cleanup Interlock must perform is also a claim-releasing operation.** §4 shows a worker's
   only durable residue is its transcript, so cleaning up *is* deleting the thing that holds the
   claim. Cleanup and identity are therefore coupled on this provider, and a design that garbage-
   collects transcripts on one schedule while treating ids as reserved on another will collide.

The quarantined file is left in the experiment's scratch directory and **not** restored: step 4 wrote a
new transcript at its original path, so restoring it would overwrite a live file. Both are experiment
artefacts and neither is a real session.

### 3.7 E7 — spawn cost, and what narrowing inheritance actually buys

i01 §5.4 recorded a per-run cost of 0.037–0.76 USD for trivial prompts and attributed it to inherited
configuration. The brief asked whether `--setting-sources` and friends move it. Four runs of the same
trivial prompt (`reply with just: ok`), in order:

| Arm | tools | MCP | cache **create** | cache **read** | `total_cost_usd` |
|-----|-------|-----|------------------|----------------|------------------|
| `default` | 107 | 7 | 17 949 | 19 572 | **0.3788** |
| `--strict-mcp-config` | 26 | 0 | 15 186 | 19 572 | 0.3235 |
| `--strict-mcp-config --setting-sources user` | 26 | 0 | **0** | 34 758 | 0.0350 |
| `default` **again** | 128 | 7 | **0** | 37 829 | **0.0380** |

The fourth row is the control, and it dissolves the naive reading of the third: **the same arm that
cost 0.3788 cold cost 0.0380 warm, with identical argv.** The third row's ten-fold drop is a
prompt-cache hit, not a configuration saving.

What survives as a real measurement is the cold-prefix cache-creation figure: `--strict-mcp-config`
removes 81 tools and **15 %** of the cold tokens (17 949 → 15 186). Useful, bounded, and nothing like
an order of magnitude.

The load-bearing fact for Interlock's economics is the other one: **back-to-back spawns that share a
prompt prefix are roughly 10× cheaper than cold ones.** A supervisor that keeps worker configuration
identical and spawns in bursts pays a fraction of what one that varies configuration per worker pays,
and any future cost figure quoted for a `-p` spawn is meaningless without saying whether the prefix
was warm. i01's 0.037–0.76 range is best re-read as exactly this axis: its low end is warm, its high
end cold.

Two flags were **not** measured and are recorded as untested rather than implied: `--bare`, which
documents that OAuth and keychain are never read and therefore cannot authenticate this
subscription-based worker at all, and `--no-session-persistence`, which is incompatible with the
subject of this note (no transcript means nothing to resume).

---

## 4. Cleanup, and what each worker leaves behind

- **Background sessions started: zero.** Every spawn was `-p`. `claude agents --json` afterwards
  returns 4 rows: one long-standing `background` row in another org's directory, two interactive
  sessions belonging to other workers, and **this worker's own interactive session**. No row from any
  of the 22 invocations. `-p` runs hold no roster row, which is also why nothing enumerates them.
- **Stray processes: none.** No `claude` process with this experiment's scratch path in its argv or
  `cwd` survived the run. Every foreground child was reaped by the harness; the three detached
  children were resolved as §3.3 describes (one killed with its parent, one terminated at restart with
  its exit confirmed, one having exited on its own and confirmed gone). Inherited MCP children were
  enumerated before each signal and re-checked after: zero survivors in every case.
- **On disk, per worker, the residue is the transcript and nothing else.** After the whole run:

  ```
  …/projects/<slug for real/fixture>/     11 files   637 046 bytes
  …/projects/<slug for real2/fixture>/     1 file     60 077 bytes
  …/projects/<slug>/memory/                0 files    (an empty directory, created per slug)
  ```

  Twelve transcripts totalling ~697 KB for 22 invocations — resumes append to the existing file rather
  than adding one, so files track *sessions*, not runs. Roughly 45–70 KB per session for conversations
  of a handful of trivial turns; the floor is set by the system prompt the CLI records, not by the
  conversation.
- **Nothing else in the config directory was touched.** `shell-snapshots` (14 entries) and
  `file-history` (90 entries) had **0 entries** modified since the experiment started; `history.jsonl`
  was last written well before it. Those are interactive-session artefacts, not `-p` ones.
- **So the cleanup Interlock must perform is:** delete the session's transcript, and remove the
  per-`cwd` project slug directory (and its empty `memory/`) when the last worker for that directory is
  retired. There is no roster row to clear, no process to reap that the supervisor did not itself
  spawn, and no provider-side handle to release. Per §3.6 this same deletion also releases the session
  id, so the cleanup step and the identity model are not independent.
- **Working tree:** byte-identical at the end of the run (§3.5). The second fixture holds the one file
  §3.5's edit-forcing probe deliberately created. Both fixtures, the quarantine directory and all
  records live under the session scratchpad, outside the repository. Nothing was written inside the
  Interlock worktree except this note and the two harness files.

---

## 5. What this means — proposed reading

Propose-only, per the scope statement. Items 1 and 7 are named because issue #7 supplies their
provider-side inputs. Nothing here discharges a gate item by itself; that is the gate record's call.

### 5.1 Item 1 — the cycle closes on a real conversation, and #6's pass did not cover it

**Proposed: satisfied for C2, on the conversation half as well as the supervisor half.** The full
cycle — start, machine-parseable structured read from published output, stop, resume — was driven on a
worker holding a genuine multi-turn conversation, through documented flags only, with the
internals-free negative executed on this half and the right way round. Continuity is real: three
successive resumes in fresh processes, one id, one transcript, and recall across a turn that was
killed mid-stream.

Two conditions belong in the reading, not as caveats but as things a supervisor must do:

- **Resume is not idempotent with respect to live writers.** U32 says the provider will admit two
  concurrent resumes; §3.3 shows an orphan can be alive and *still working* when the supervisor comes
  back. The order — resolve, then resume — is load-bearing, and pid alone is not enough to resolve
  with, because pid reuse would make the supervisor signal a stranger. Matching the persisted session
  id against the live command line is the cheap fix and is what the harness does.
- **The id is not the durable thing; the transcript is** (§3.6). Item 1's verbs all work, but a
  control plane that persists only the id has persisted a name that a file deletion invalidates.

### 5.2 Item 7 — the `-p` child does not take the working tree

**Proposed: satisfied, and the negative is executed rather than argued.** Across every transition
Interlock performs, on a tree carrying uncommitted, untracked and unpushed work, the content was
byte-identical by recorded hash and the git state unchanged. The child never relocated its `cwd` and
created no worktree — including under the exact trigger that made an Agent View session relocate in
A6, a task that forces a file write, with edits permitted.

`ACCEPTANCE.md` §1 item 7's tail — *if the provider can reclaim a worktree without an interlock the
control plane can observe or veto, that is a gate failure, not a workaround* — is not engaged on this
surface. On Agent View it is (A6); this is one more concrete difference in C2's favour, on top of
D-0027's.

The honest limit: this shows the child does not take the tree **unasked**. It says nothing about a
child instructed to run `git worktree add`, or one whose configuration enables a worktree workflow.
Those are Interlock's own policy surface, not the provider's, and item 7 is about the latter.

### 5.3 What this does *not* claim

- It does not touch item 2. D-0027 already failed it on Agent View, and `ACCEPTANCE.md` §2 assigns the
  single-writer half to Interlock's own fencing token. §3.6 makes the provider's refusal *less* of a
  substitute for that token, not more — it now has a third bypass (delete the file) alongside the race
  (U27) and the different-`cwd` route (H4). **#18 owns the single-writer verdict** and nothing here
  pre-empts it.
- It does not re-establish #6's supervisor findings; it consumes them.
- It measures one machine, one build, one load. Every timing here (2.6 s to terminate an orphan,
  ~3 s to `init`, the cost figures) is a one-machine figure, not a provider constant.

---

## 6. New unverified items this raises

| # | Question |
|---|----------|
| **U39** | Does a *concurrent* resume of the same session interleave both turns into the one transcript, as the interactive documentation says, and what does each process's `result` report? U32 established both are admitted; what the file looks like afterwards on `-p` was not examined here. |
| **U40** | Does the CLI itself ever remove transcripts — a retention sweep, a size cap, an eviction under disk pressure? §3.6 makes this the difference between a stable id and one that silently frees itself. Nothing in this run deleted anything the harness did not delete on purpose. |
| **U41** | After §3.6's re-claim, the id names a *new* conversation. Is there any published field that distinguishes a re-claimed-empty id from a fresh one — `num_turns`, a resumed-at marker in `init` — that a control plane could check before trusting a resume? |
| **U42** | How does resume behave when the transcript exists but is truncated or corrupt (a crash mid-append)? The stopped-turn case shows partial turns are persisted cleanly; a torn write was not tested. |
| **U43** | Is the ~10× cold/warm spawn-cost ratio (§3.7) stable across models and prefix sizes, and what exactly invalidates the prefix — `cwd`, the MCP set, CLAUDE.md, all three? A supervisor that batches spawns is paying for this whether or not it knows the rule. |

---

## 7. Bottom line

On `-p`, the C2 shape of a worker works: the conversation survives across processes, the id survives
with it, the transcript grows in place instead of forking, a supervisor can die and come back and pick
the conversation up from persisted state alone, and the working tree it was given comes back
byte-identical — even when the worker is told to write into it.

What does not survive is the idea that the session id is a claim. It is a filename. Deleting the file
frees it, another directory ignores it, a concurrent resume never consults it. Interlock's identity and
its exclusion have to come from Interlock; the provider supplies continuity, and continuity is what
this probe found.
