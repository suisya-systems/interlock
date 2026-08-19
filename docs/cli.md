# claude-org-runtime CLI

`claude-org-runtime` exposes a single console entry point with the
subcommand groups `dispatcher`, `settings`, `sandbox` and `attention`,
plus the existing `migrate` module. Each group can also be invoked
directly via `python -m`.

The `org up` / `org adopt` / `org down` and `broker send` groups that this
page used to document are gone: they were built on the terminal-adapter and
pane-control machinery the Discard bucket removes (PORTING_LEDGER.md
D-0014). The sections below cover only what the CLI still exposes.

```sh
pip install claude-org-runtime
claude-org-runtime --version           # 0.1.0
claude-org-runtime --help
```

## `dispatcher delegate-plan`

Computes the deterministic parts of the Dispatcher delegation state
machine (balanced split target selection, name/cwd validation,
instruction-template rendering, worker seed + outbox file writes) and
emits a JSON action plan that Dispatcher Claude reads and executes via
MCP tool calls. The helper does NOT call MCP tools directly.

```sh
claude-org-runtime dispatcher delegate-plan \
    --task-json .state/dispatcher/inbox/<task_id>.json \
    --panes-json panes.json \
    --state-dir .state
```

Equivalent module form:

```sh
python -m claude_org_runtime.dispatcher.runner delegate-plan \
    --task-json ... --panes-json ... --state-dir .state
```

### Flags

| Flag | Description |
|------|-------------|
| `--task-json PATH` | Path to a task JSON file (object with `task_id`, `worker_dir`, `instruction` or `instruction_vars`, etc.). Mutually exclusive with `--task-stdin`. |
| `--task-stdin` | Read the task JSON from stdin. |
| `--panes-json PATH` | Path to a JSON file containing renga `list_panes` output (a list of pane dicts, or `{panes: [...]}`). Under renga 2.0 this is **caller-tab-scoped** and is the geometry source only. |
| `--peers-json PATH` | Path to a JSON file containing `list_peers` output (a list of peer dicts, or `{peers: [...]}`). Peers span every renga tab, so this is the org-wide worker population; `--panes-json` only sees the caller's tab. Omitted -> the population is derived from `--panes-json` alone, which is the renga 1.4 / broker behaviour. |
| `--server-capability TOKEN` | renga protocol capability the server advertises (`caller_scope`, `cross_tab_peers`, `spawn_tab`). Repeatable. Asserted by the caller, never probed -- the renga MCP surface does not report its own capability list. Omitted -> every tab feature fails closed. |
| `--tab SELECTOR` | Place the worker in a specific renga tab: `pane_id:N` (stable anchor, preferred), `index:N` (0-based, shifts when a tab closes), `name:LABEL` (exact match), `new`, or `new:LABEL`. Requires `--server-capability spawn_tab`. Ignored under `--transport broker`. |
| `--overflow-to-new-tab` | Under `--transport renga`, when no balanced-split candidate is left, plan a spawn into a fresh background tab instead of escalating. Requires `--server-capability spawn_tab` **and** `--peers-json`: overflow removes the rect ceiling and the fleet ceiling that replaces it is counted from the peer census, which cannot see workers this flag placed in other tabs unless the census is supplied. Each overflow mints a new tab; it never reuses one. Ignored under `--transport broker`. |
| `--transport {renga,broker}` | Capacity backend: `renga` uses the rect-based balanced split; `broker` uses the explicit `--max-concurrent-workers` ceiling. Default: resolved from `ORG_TRANSPORT`, else the module default (`broker`). |
| `--max-concurrent-workers N\|unlimited` | Worker ceiling: a non-negative int (`0` disables spawning) or `unlimited` (explicit opt-in). Default when omitted: `8`. Ignored under `--transport renga` **unless** `--overflow-to-new-tab` is set -- that mode removes the rect ceiling, so the fleet ceiling becomes the only bound. In that mode the ceiling counts the observed census **plus outstanding reservations** (see below). |

#### Overflow reservations

In `--overflow-to-new-tab` mode the fleet ceiling is counted against
`active_workers + reserved_workers`, and `plan.capacity` reports both.

A *reservation* is a worker this helper already planned that has not shown up
in either snapshot yet. It exists because the pane/peer union cannot cover an
overflow spawn: the new pane lands in a tab of its own, which renga's
caller-tab scoping keeps out of `list_panes` permanently, and its peer bind is
still 10-30s away -- so for the length of that window the worker is invisible
to both inputs and back-to-back delegations each admit another one. Measured
before the fix: a ceiling of `2` admitted three workers, every plan reporting
`free_worker_slots: 2`.

The ledger is the worker seed file the helper already writes on
`ready_to_spawn`. A seed counts as a reservation only while all three hold:

1. its worker is absent from the census (a worker that binds is never counted
   twice),
2. its `Status:` line is `planned` or unreadable -- a status a consumer's
   monitoring loop rewrote (`running`, `pane_closed`, ...) is newer evidence
   than the clock, and that rewrite is itself what refreshed the mtime, so
   without this rule a worker that just finished would block its own
   replacement for the whole window,
3. it is younger than `WORKER_BIND_WINDOW_SECONDS` (45s) -- nothing ever
   deletes these files, so a spawn that died before anything could rewrite it
   frees its slot on the clock, with no cleanup step and no leaked slots.

The mechanism applies to the overflow path only; the broker ceiling and the
non-overflow renga path are unchanged.
| `--state-dir PATH` | State directory root. Default: `.state`. |
| `--template-repo PATH` | Repo root that hosts `.claude/skills/org-delegate/references/instruction-template.md`. Default: try the runtime package's ancestors first, then walk up from CWD. |
| `--locale-json PATH` | Override the English defaults for non-English consumers (e.g. claude-org-ja). The JSON file maps to `LocaleConfig` fields: `constraints_default`, `report_target_default`, `claude_md_filename_default`, `instruction_template`. |
| `--dry-run` | Compute and print the plan without writing the worker seed / outbox files. |

### LocaleConfig

The runtime ships English-only worker instruction copy
(`LocaleConfig.english()`). Consumers whose workers run in another
language can override the locale either programmatically:

```python
from claude_org_runtime.dispatcher import LocaleConfig
from claude_org_runtime.dispatcher.runner import build_plan

ja = LocaleConfig(
    constraints_default="(なし)",
    instruction_template=(
        "# タスク: {task_id}\n"
        "作業ディレクトリ: `{worker_dir}`\n\n"
        "## 指示\n{instruction}\n"
    ),
)
plan = build_plan(task, panes, state_dir, locale=ja)
```

or from the CLI via `--locale-json`:

```sh
claude-org-runtime dispatcher delegate-plan \
    --task-json ... --panes-json ... \
    --locale-json /path/to/locale.ja.json
```

`locale.ja.json` is a flat JSON object whose keys match the
`LocaleConfig` field names; unknown keys are rejected with a clear
error.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | `ready_to_spawn` -- plan emitted, side-effect files written (unless `--dry-run`). |
| `1` | `input_invalid` -- task JSON / panes / peers / cwd validation failed, a malformed `--tab` selector, `--tab` without `--server-capability spawn_tab`, or `--overflow-to-new-tab` without `--peers-json`. A *well-formed* selector the peer census cannot resolve is **not** an error: the census only sees tabs holding at least one peer, so renga stays the authority and answers `tab_not_found` / `tab_ambiguous` itself. |
| `2` | `split_capacity_exceeded` -- no balanced-split candidate, the broker `max_concurrent_workers` ceiling, or renga's tab limit; the `escalate` field tells Dispatcher to notify Secretary for human judgment. |

The exit codes are exactly these three. The renga tab error codes
(`tab_not_found`, `tab_ambiguous`, `tab_limit_reached`,
`target_tab_mismatch`, `pane_not_found`) are **plan-level**, never process
exit codes: they appear as the leading token of an `errors[]` entry / the
`escalate` message, and as the keys of `plan.on_spawn_error`.
`--overflow-to-new-tab` can turn a would-be exit `2` into an exit `0` by
planning the worker into a fresh background tab, which is why it is opt-in
rather than the default.

Note for upgrading consumers: the renga `split_capacity_exceeded` escalation
**message string** grew a measured diagnostics paragraph, on a path that
needs none of the flags above. The pre-#158 sentence is preserved byte for
byte as a literal prefix, so `MIN_PANE` / `max_concurrent_workers` matching
still discriminates the two capacity reasons -- but anything that renders the
message into a fixed-width surface, or matches it in full, should be
re-checked.

## `settings generate`

Renders a per-role `<worker_dir>/.claude/settings.local.json` from the
bundled `role_configs_schema.json` (the SoT now ships with the runtime,
so consumers no longer need a `tools/role_configs_schema.json` copy).

```sh
claude-org-runtime settings generate \
    --role default \
    --worker-dir /path/to/worker \
    --claude-org-path /path/to/claude-org \
    --out /path/to/worker/.claude/settings.local.json
```

Equivalent module form:

```sh
python -m claude_org_runtime.settings.generator \
    --role default --worker-dir ... --claude-org-path ... --out ...
```

### Flags

| Flag | Description |
|------|-------------|
| `--role NAME` | Worker role (`default`, `claude-org-self-edit`, `doc-audit`, ...). |
| `--worker-dir PATH` | Absolute path that `{worker_dir}` resolves to. |
| `--claude-org-path PATH` | Absolute path to the claude-org repo (for hook script paths). |
| `--out PATH` | Output file. Default: stdout. |
| `--schema PATH` | Schema-path override. Default: bundled `role_configs_schema.json`. |
| `--role-kind {worker,org}` | Schema bucket: `worker` (default, `schema['worker_roles']`) or `org` (`schema['roles']`). NOTE: `--role-kind org` is rejected by `settings generate` because org `settings.local.json` files are hand-maintained; use `settings show --role-kind org` for inspection. |
| `--base-clone PATH` | Pattern B context: substituted as `{base_clone}` in entry paths and `additionalDirectories` before realpath evaluation. |
| `--task-id ID` | Pattern B context: substituted as `{task_id}`. |
| `--branch-ref REF` | Pattern B context: substituted as `{branch_ref}`. |
| `--pattern {A,B,C}` | Dispatch pattern. Required when the selected role declares `sandbox_by_pattern`; the renderer then forwards `sandbox_by_pattern[<pattern>]` as the role's sandbox surface (contract SoT: claude-org-ja's `docs/contracts/role-pattern-sandbox-contract.md`, not part of this runtime repo). For roles using the legacy single `sandbox` shape it stays informational and is ignored by the renderer. Free-form values like `b` are rejected by argparse to prevent silent fallthrough. |

## `settings show`

Renders the same per-role settings as `settings generate` and, with
`--explain`, surfaces Phase 3 case E sandbox suppression metadata
(`worker_roles.<role>.sandbox` is described under
`worker_roles.$comment_sandbox` in the bundled schema). The `show` and
`generate` commands share the same renderer, so the deny set you see
under `--explain` is exactly what would be written by `generate`.

```sh
claude-org-runtime settings show \
    --role default \
    --worker-dir /path/to/worker \
    --claude-org-path /path/to/claude-org \
    --explain --json
```

### Flags

| Flag | Description |
|------|-------------|
| `--role NAME` | Same as `settings generate`. |
| `--worker-dir PATH` | Same as `settings generate`. |
| `--claude-org-path PATH` | Same as `settings generate`. |
| `--out PATH` | Output file. Default: stdout. |
| `--schema PATH` | Schema-path override. Default: bundled. |
| `--explain` | Include sandbox suppression metadata: `wsl_detected`, the normalized user-supplied `sandbox_read_roots` (the configured `worker_dir` + `additionalDirectories`, *not* realpath-resolved — the realpath only applies to deny entries during the escape check), and the per-entry `suppressions` list (`layer`, `entry`, `reason`, `realpath`). |
| `--json` | Emit a structured JSON payload instead of the human-readable text. |
| `--role-kind {worker,org}` | Schema bucket: `worker` (default) or `org` (for inspecting secretary / dispatcher / curator sandbox intent). |
| `--base-clone PATH` | Pattern B context: substituted as `{base_clone}` before realpath evaluation. |
| `--task-id ID` | Pattern B context: substituted as `{task_id}`. |
| `--branch-ref REF` | Pattern B context: substituted as `{branch_ref}`. |
| `--pattern {A,B,C}` | Same as `settings generate`: required when the role declares `sandbox_by_pattern`, otherwise informational. |

The runtime applies WSL/realpath suppression at render time: any
`sandbox.filesystem.denyRead / denyWrite` entry whose realpath escapes
the sandbox read roots (`worker_dir` + `additionalDirectories`) is
dropped from the rendered sandbox object — this handles WSL
(`/home/<u>/...` resolving into `/mnt/c/...`) and devcontainer
(`/workspaces` symlink) cases without hard-coding any host path.
`permissions.deny Read(...) / Write(...)` (Layer 2) is **never**
suppressed.

### Symlink canonicalization of deny paths

Suppressing a Layer 3 entry is not enough on its own, because Claude Code
merges **both** layers into the single deny set it hands to bubblewrap
([docs](https://code.claude.com/docs/en/sandboxing): "Paths from both
`sandbox.filesystem` settings and permission rules are merged together
into the final sandbox configuration"). A Layer 2 credential mirror kept
as the compensating control for a suppressed Layer 3 entry — e.g.
`Read(~/.aws/*)` — therefore re-injects the very path that was
suppressed.

That matters because bubblewrap materializes one mount point per deny
path inside a staging newroot *before* the pivot. An **absolute** symlink
anywhere in the chain resolves against a root where the target does not
exist yet, so mount-point creation fails and bwrap aborts the whole
launch:

```
bwrap: Can't create file at /home/<user>/.aws/config: No such file or directory
```

The launch failure is not fail-closed: Claude Code's escape hatch then
retries the command with `dangerouslyDisableSandbox`, so every subsequent
Bash command runs unsandboxed with no standing signal. On WSL2 this fires
whenever a credential directory is a symlink into `/mnt/c`.

So the renderer **rewrites** an escaping deny path to its realpath rather
than dropping it, across both layers:

| Rendered as | Becomes |
|-------------|---------|
| `Read(~/.aws/*)` (with `~/.aws -> /mnt/c/Users/u/.aws`) | `Read(//mnt/c/Users/u/.aws/*)` |
| `sandbox.filesystem.denyRead: ["/home/u/.aws/config"]` | `["/mnt/c/Users/u/.aws/config"]` |

Both guarantees survive: bwrap can bind the realpath form, and the Layer 2
tool-level block still applies to reads issued through the original
symlinked path, because Claude Code resolves symlinks when matching
`Read` / `Edit` deny rules.

Only *absolute* symlinks are rewritten. Relative symlinks resolve
correctly inside bwrap's staging tree, and unanchored globs such as
`Read(**/credentials*)` are never expanded into host paths, so neither
needs canonicalization. Rewrites are reported in
`settings show --explain` (`rewrites`) and appended to the emitted
`$comment` as `; symlink-canonicalized deny paths: [...]` — the
contract-fixed `platform=<linux|wsl>, layer-3 entries suppressed: [`
prefix is left byte-identical.

## `sandbox doctor`

Preflight a rendered `settings.local.json` and fail loudly when the
sandbox would not actually start. The generator canonicalizes what it
renders, but a worker's *effective* deny set is the merge of several
settings scopes (user `~/.claude/settings.json`, project, managed) and
only some come from this runtime — any scope can contribute a path that
takes the sandbox down.

```sh
claude-org-runtime sandbox doctor --settings path/to/settings.local.json
```

| Flag | Description |
|------|-------------|
| `--settings PATH` | Settings file to check. Required; repeat to add scopes. |
| `--no-merge-scopes` | Check only the given files; skip user / managed settings. |
| `--json` | Machine-readable report instead of the text one. |
| `--verbose` | List every deny target, not just failing ones. |
| `--no-probe-bwrap` | Static analysis only; skip the live bwrap canary. |

By default the sibling project scope (`.claude/settings.json` next to a
`settings.local.json`, or vice versa), the user settings
(`~/.claude/settings.json`), and any managed settings are merged in
alongside the given file, because Claude Code unions the deny arrays
across scopes: a symlinked path in *any* scope aborts the launch no
matter how clean the rendered worker file is. Sibling scopes are derived
from each input's own directory rather than a fixed list, so the project
scope is found wherever the file lives.
Checking the worker file alone would report a clean preflight for a
sandbox that cannot start. Each finding names the file that contributed
it, so the fix lands in the right place. `sandbox.enabled` is resolved
conservatively — the gate relaxes only when no scope enables the sandbox
and at least one explicitly disables it.

It does two independent checks:

1. **Static analysis** — collects every deny path the settings contribute
   (Layer 3 `deny{Read,Write}` plus Layer 2 `Read` / `Edit` rules) and
   flags those crossing an absolute symlink, with the realpath rewrite
   that would fix each.
2. **Live canary** — when `bwrap` is on `PATH`, actually launches it with
   those paths bound and reports whether the sandbox comes up. This
   catches unbindable paths whose cause is *not* a symlink.

The canary deliberately passes no `--proc` / `--dev`. Those mount fresh
filesystems *over* the corresponding host trees, and a shadowed region
contains no symlink for bwrap to trip over — it just creates plain
directories and succeeds. Probing with them would blind the canary to any
deny path under the shadowed prefix and make it contradict the static
analysis.

That shadowing is also the only case where the two checks can disagree: a
deny path crossing an absolute symlink binds fine *while* some mount hides
the link. The doctor still reports it as a failure, and says why — a deny
path that works only because something happens to be mounted over it
aborts the launch the moment that stops being true.

Exit status is `0` when the deny paths are usable, `1` when either check
fails, and `2` on a missing / malformed settings file — so it can gate a
worker launch rather than being advisory. The shapes it reads are
validated up front, because a `deny` given as a bare string is iterable
and would otherwise be scanned character by character and reported clean.

Settings that explicitly set `sandbox.enabled: false` pass the gate:
no sandbox launches, so no launch can be aborted. Any finding is still
printed and labelled latent, because the deny arrays merge across
settings scopes and become live as soon as another scope enables the
sandbox. An *absent* `sandbox` key is treated as unknown rather than
off, since user or managed settings can enable it for a role that never
mentions it.

### On `failIfUnavailable` and `allowUnsandboxedCommands`

`sandbox.failIfUnavailable` does **not** cover this failure. Per the
[official docs](https://code.claude.com/docs/en/sandboxing) it governs a
*missing dependency* such as bubblewrap not being installed, which blocks
Claude Code from starting — not a per-command bwrap launch failure on a
machine where bwrap is present and working.

The knob that governs the silent fallback is
`sandbox.allowUnsandboxedCommands: false` (shown as **Strict sandbox
mode** in the `/sandbox` Overrides tab), which makes the
`dangerouslyDisableSandbox` retry be ignored. This runtime does **not**
set it, because the blast radius is fleet-wide: Claude Code's docs list
`docker` as incompatible with the sandbox, and the `default` and
`claude-org-self-edit` worker roles both allow `docker build` while the
runtime ships no `excludedCommands`. Turning strict mode on without first
adding those exclusions would make those workers fail outright rather
than silently lose isolation. `sandbox doctor` is the non-breaking half
of the answer: it makes the loss of isolation visible without changing
what happens when a command cannot be sandboxed.

## Migration from `claude-org-ja`'s `tools/`

If your `claude-org-ja` checkout was previously calling either of the
following in-tree scripts:

- `python tools/dispatcher_runner.py delegate-plan ...`
- `python tools/generate_worker_settings.py ...`

replace them with the runtime equivalents:

```diff
- python tools/dispatcher_runner.py delegate-plan --task-json ... --panes-json ...
+ python -m claude_org_runtime.dispatcher.runner delegate-plan --task-json ... --panes-json ...

- python tools/generate_worker_settings.py --role default --worker-dir ...
+ python -m claude_org_runtime.settings.generator --role default --worker-dir ...
```

The CLI flags are identical; the only behavioural difference is that
`dispatcher_runner` now defaults its instruction-template anchor to the
process's current working directory (the in-tree script anchored to
`<repo>/tools/..`). Pass `--template-repo /path/to/claude-org-ja` to
override if the helper is invoked from somewhere other than the
claude-org-ja repo root.

The bundled `role_configs_schema.json` mirrors
`claude-org-ja/tools/role_configs_schema.json` as of v0.1.0; subsequent
schema edits will land in their own runtime release rather than via
in-place tool edits.
