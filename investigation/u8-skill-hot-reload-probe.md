# U8 — Are skills re-read by an already-running session, or bound at session start?

**Status:** answered — **affirmative (hot-reload)**
**Date:** 2026-08-18
**Task:** `interlock-item9-curator-gate-20260818` (gate item 9, Issue #22)
**Scope:** propose-only for anything outside item 9. This note records a documentation search and a
runtime experiment, and it *places the approval gate* for item 9 because the issue assigns that
consequence to U8 explicitly. It does not create a `D-` entry and does not edit a design document;
any wider ruling is left to a human.
**Refs:** `#22`; `docs/proposals/agent-view-gate-scaffold.md` Appendix A/U8; `DECISIONS.md` D-0018,
D-0022, D-0026.

---

## 1. Verdict

**U8 is answered AFFIRMATIVE for skills.** An already-running Claude Code session **re-reads skill
material from disk**; it is not bound once at session start. On CLI 2.1.234 all three effects a
promotion gate would care about were observed inside one live session, with no restart:

| # | Observed | Bearing on item 9 |
|---|---|---|
| **A** | An edited `SKILL.md` **body** is served to the running session. The second invocation of the same skill returned `BODY-TOKEN-ALPHA-V2` after the file was rewritten. | The bytes a session executes are whatever is on disk *at invocation time*. |
| **B** | An edited **`description`** reaches the session's own skill listing: the model reported `DESCRIPTION-TOKEN-ALPHA-V2` without reading any file. | Even the cached-looking part of the skill surface is refreshed. |
| **C** | A skill **directory created after session start** was invocable — `u8probe-beta` loaded and returned `BODY-TOKEN-BETA-V1` — **even though it was still absent from the listing the session reported** (the model's `skills=` answer named only `u8probe-alpha`). | Absence from the listing is *not* absence from the session. A new directory is live before it is advertised. |

**Consequence, as gate item 9 states it: writing a file into a live skill directory already *is*
promotion.** There is no later moment at which a "promote this candidate" function could intervene,
because by the time such a function ran the session would already be able to load the file. Result C
sharpens this: a gate that waited for a skill to *appear in the listing* before considering it
promoted would be waiting for something that lags the actual exposure.

**Therefore the approval gate for item 9 sits at the filesystem write**, not at a promotion
function. It is implemented as `claude_org_runtime.curator.gate.PromotionGate`, which is the only
code in the package that performs a write into skill material and the only code allowed to name a
skill root — enforced by `claude_org_runtime.curator.audit` and `tests/curator/test_path_audit.py`.

### Directories that are live skill material for an already-running session

Recorded here for the acceptance criterion, and mirrored in code as
`claude_org_runtime.curator.skill_root.LIVE_SKILL_MATERIAL`:

- `~/.claude/skills/` — user scope;
- `<project>/.claude/skills/` — the directory the session was started in, and every parent up to the
  repository root;
- `<added dir>/.claude/skills/` — inside each directory passed with `--add-dir`.

Two documented exclusions, **not** covered by the probe and carried here as documentation-only
facts: a *top-level skills directory that did not exist at session start* is not watched (restart
required), and **plugin** skills need `/reload-plugins` for `hooks/`, `.mcp.json`, `agents/` and
`output-styles/` changes. Neither weakens the placement above — both are cases where a write is
*less* immediately live, never more.

---

## 2. Documentation search

Source: <https://code.claude.com/docs/en/skills> (fetched 2026-08-18; `docs.claude.com/en/docs/claude-code/skills`
301-redirects there). Verbatim:

> Claude Code watches skill directories for file changes. When you add, edit, or remove a skill under
> `~/.claude/skills/`, the project `.claude/skills/`, or a `.claude/skills/` inside an `--add-dir`
> directory, Claude Code picks up the change within the current session, without a restart. If you
> create a top-level skills directory that didn't exist when the session started, restart Claude Code
> so it can watch the new directory.

> Live change detection covers `SKILL.md` text only. For a skill folder that is also a plugin,
> changes to `hooks/`, `.mcp.json`, `agents/`, and `output-styles/` need `/reload-plugins` to take
> effect.

> In a regular session, skill descriptions are loaded into context so Claude knows what's available,
> but full skill content only loads when invoked.

The documentation answers U8 on its own. It was not treated as sufficient: the issue asks for
documentation search **and** a direct runtime test, and the third quotation above leaves open
precisely the question the gate depends on — whether the *descriptions* said to be "loaded into
context" are re-loaded, and what happens to a directory that appears mid-session. Both were answered
by the probe, and C is a fact the documentation does not state.

---

## 3. Environment

```
$ claude --version
2.1.234 (Claude Code)

$ which claude
/home/happy_ryo/.local/bin/claude
```

The probe session ran in the worker's own worktree, in a **separate renga tab**, launched by
`spawn_claude_pane` (`claude --dangerously-load-development-channels server:renga-peers --model
sonnet`), model Sonnet 5. One session, three turns, closed afterwards (§5).

Fixture, created **before** the session started, at
`<worktree>/.claude/skills/u8probe-alpha/SKILL.md`:

```markdown
---
name: u8probe-alpha
description: U8 probe alpha. DESCRIPTION-TOKEN-ALPHA-V1. Invoke only when the user types /u8probe-alpha.
---

# U8 probe alpha

BODY-TOKEN-ALPHA-V1

When invoked, reply with exactly the BODY-TOKEN value written above and nothing else.
```

Every answer the probe was asked for is a **token**, not prose. That is deliberate: renga's pane
snapshot (`inspect_pane`) collapses the spaces out of some rendered rows, so a prose answer could not
have been quoted verbatim with confidence. Tokens contain no spaces and survive the rendering intact.
The transcript below is the pane snapshot exactly as returned, including that space collapsing.

---

## 4. Transcript

### 4.1 E1 — baseline, before any file changed

Prompt (typed into the pane, submitted with Enter):

```
PROBE SESSION. Ignore CLAUDE.md worker instructions; do not edit files, do not run git, do not
message any peer/secretary/dispatcher. Two questions: (Q1) Invoke the skill named u8probe-alpha via
the Skill tool and report the BODY-TOKEN-... token contained in the skill body you receive. (Q2)
WITHOUT using Read/Bash/Grep/Glob or any file tool, report the DESCRIPTION-TOKEN-... token that
appears in the description of u8probe-alpha in your available-skills listing, and list every skill
name you can see starting with u8probe. Reply in exactly this form and nothing else: A1=<token> ;
A2=<token> skills=<names>
```

Pane, verbatim:

```
●Skill(u8probe-alpha)
  ⎿  Successfullyloadedskill

●A1=BODY-TOKEN-ALPHA-V1;A2=DESCRIPTION-TOKEN-ALPHA-V1skills=u8probe-alpha

✻Cogitated for 13s
```

Baseline established: V1 body, V1 description, `u8probe-alpha` the only `u8probe*` skill.

A second, identical turn was submitted (an artefact of establishing that the pane was accepting
input) and answered `A1=BODY-TOKEN-ALPHA-V1;A2=DESCRIPTION-TOKEN-ALPHA-V1skills=u8probe-alpha`
**without** a new `Skill(...)` tool call — the model reused what was already in context. That is why
the post-mutation turn below states explicitly that the tool must be called again: a repeat answer
from context would have proved nothing either way.

### 4.2 E2 — the mutation, with the session still running

While the session sat idle at its prompt, three changes were made on disk:

1. `u8probe-alpha/SKILL.md` body: `BODY-TOKEN-ALPHA-V1` → `BODY-TOKEN-ALPHA-V2`;
2. the same file's `description`: `DESCRIPTION-TOKEN-ALPHA-V1` → `DESCRIPTION-TOKEN-ALPHA-V2`;
3. a **new** directory `u8probe-beta/` with `SKILL.md` carrying `BODY-TOKEN-BETA-V1`.

The session was neither restarted nor notified.

### 4.3 E3 — after the mutation

Prompt:

```
SECOND PROBE TURN. The files on disk changed since your last answer. Do NOT answer from memory or
from earlier context. (Q1) Invoke the Skill tool for u8probe-alpha AGAIN right now, and report the
BODY-TOKEN-... token that appears in the NEW tool result. (Q2) WITHOUT any file tool, report the
DESCRIPTION-TOKEN-... token currently shown for u8probe-alpha in your available-skills listing, and
list every skill name you can now see starting with u8probe. (Q3) If a skill named u8probe-beta is
visible to you, invoke it via the Skill tool and report its BODY-TOKEN; if it is not visible, write
A3=NOT-VISIBLE. Reply in exactly this form and nothing else: A1=<token> ; A2=<token> skills=<names> ;
A3=<token-or-NOT-VISIBLE>
```

Pane, verbatim:

```
  ⎿  1 skill available

●Skill(u8probe-alpha)
  ⎿  Successfullyloadedskill
  ⎿  1 skill available

●Skill(u8probe-beta)
  ⎿  Successfullyloadedskill

●A1=BODY-TOKEN-ALPHA-V2;A2=DESCRIPTION-TOKEN-ALPHA-V2skills=u8probe-alpha;A3=BODY-TOKEN-BETA-V1

✻Sautéed for 11s
```

Reading of the three answers:

- **A1 = `BODY-TOKEN-ALPHA-V2`** — the running session executed bytes written after it started.
  Result A.
- **A2 = `DESCRIPTION-TOKEN-ALPHA-V2`** — obtained with no file tool, so it came from the session's
  own skill surface, which had been refreshed. Result B.
- **A3 = `BODY-TOKEN-BETA-V1`, while `skills=u8probe-alpha`** — the mid-session directory was
  *loadable* and simultaneously *unlisted*. Result C. The `Skill(u8probe-beta)` line is the load
  actually happening, not the model paraphrasing.

The one thing this transcript cannot separate is *when* the re-read happens — at the watcher's file
event or at invocation. It does not need to: either way the write is the last moment anybody
controls.

---

## 5. Cleanup

- The probe pane was closed (`close_pane` id=12); one earlier pane (id=11) was closed without
  running a turn, after being spawned too narrow to quote from.
- Both fixture directories were removed: `<worktree>/.claude/skills/u8probe-alpha/` and
  `u8probe-beta/`. `<worktree>/.claude/skills/` is back to empty and is untracked, so nothing from
  this probe is committed.
- Model-backed invocations: three turns in one Sonnet session. No background agents were started.

## 6. Notes for whoever re-runs this

- `.claude/` is a **hook-guarded** path for workers. `Bash` refuses to create it
  (`block-org-structure.sh` matches `mkdir|touch|cp|mv` against the directory name) while the `Write`
  tool is explicitly permitted to, for an out-of-org worker's own repository. Removing the fixtures
  afterwards needs the sandbox lifted for that one command, because `.claude/` is outside the Bash
  write allowlist. Do not route around either guard; use the tool each one permits.
- Spawn the probe pane in **its own tab**. A vertical split was 38 columns wide, and the snapshot of
  a narrow pane is unquotable.
- `send_message(deliver="user_turn")` is refused (`user_turn_unsupported_target`) until the spawned
  pane has registered as a peer; `send_keys` + `Enter` works immediately, and the very first
  keystrokes after launch can be dropped while the TUI is still coming up. Type, `inspect_pane` to
  confirm the composer holds the text, and only then send Enter.
