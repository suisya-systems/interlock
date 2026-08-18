#!/usr/bin/env python3
"""I-02 conversation probe: multi-turn `--resume` continuity and working-tree
ownership for a real `claude -p` worker.

Throwaway harness for suisya-systems/interlock issue #7 (D-0026), a sibling of
investigation/i01_supervisor_probe.py and deliberately self-contained: the
process helpers are copied rather than imported so this file can be read on its
own and deleted on its own.

The supervisor surface here is public-CLI only (D-0010). It never reads the
CLI's per-user config directory, a transcript file or any internal socket. Two
subcommands break that rule ON PURPOSE and are not part of the supervisor
surface -- `transcripts` (which must look at the transcript to say whether it
grew in place or forked) and `u38` (which must move a transcript to answer
whether the claim is keyed to the file). Both say so in their own records via
`internals_observer: true`.

Each subcommand appends one verbatim JSON record per step to
$I02_OUT/records.jsonl and echoes it to stdout.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

CLAUDE = os.environ.get("I02_CLAUDE_BIN", "claude")
OUT_DIR = os.environ.get("I02_OUT", "./results")

# Prepended to every child argv. Used by the internals-free negative so that a
# restricted harness can still hand the child normal access to its own state.
CHILD_WRAPPER = os.environ.get("I02_CHILD_WRAPPER", "").split() or []


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def rec(step, obj):
    obj = dict(obj)
    obj["step"] = step
    obj["ts"] = round(time.time(), 3)
    os.makedirs(OUT_DIR, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, sort_keys=False)
    with open(os.path.join(OUT_DIR, "records.jsonl"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)
    return obj


# --------------------------------------------------------------------------
# process helpers (public /proc only)
# --------------------------------------------------------------------------

def ppid_map():
    m = {}
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % p) as f:
                st = f.read()
            after = st.rsplit(")", 1)[1].split()
            m.setdefault(int(after[1]), []).append(int(p))
        except Exception:
            continue
    return m


def descendants(pid):
    m = ppid_map()
    out, stack = [], [pid]
    while stack:
        cur = stack.pop()
        for c in m.get(cur, []):
            out.append(c)
            stack.append(c)
    return sorted(out)


def cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().decode("utf-8", "replace").replace("\0", " ").strip()
    except Exception:
        return None


def ppid_of(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            st = f.read()
        return int(st.rsplit(")", 1)[1].split()[1])
    except Exception:
        return None


def alive(pid):
    return os.path.exists("/proc/%d" % pid)


def proc_cwd(pid):
    """The child's own cwd, read from /proc. This is item 7's instrument: it is
    how a session that relocates itself into a worktree is caught."""
    try:
        return os.readlink("/proc/%d/cwd" % pid)
    except Exception:
        return None


def proc_snapshot(pid):
    return [
        {"pid": p, "ppid": ppid_of(p), "cmdline": (cmdline(p) or "")[:160],
         "cwd": proc_cwd(p)}
        for p in descendants(pid)
    ]


# --------------------------------------------------------------------------
# spawn helpers
# --------------------------------------------------------------------------

def child_argv(args):
    return CHILD_WRAPPER + [CLAUDE] + args


def spawn(args, cwd, env_extra=None, new_session=True, out_path=None):
    """Spawn the child.

    out_path routes stdout/stderr to files instead of pipes. A detached child
    MUST use it: nobody is left reading its pipes, so a talkative run would
    either block on a full pipe or take EPIPE the moment the supervisor is
    killed -- which would silently change the very lifecycle being observed.
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    if out_path:
        out = open(out_path + ".stdout", "wb")
        err = open(out_path + ".stderr", "wb")
    else:
        out = err = subprocess.PIPE
    try:
        return subprocess.Popen(
            child_argv(args),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            text=False,
            bufsize=0,
            start_new_session=new_session,
        )
    finally:
        if out_path:
            out.close()
            err.close()


class LineReader:
    """Non-blocking, complete-line reader over a raw pipe (see i01's note: a
    select() over a buffered TextIOWrapper loses lines already pulled into user
    space)."""

    def __init__(self, stream):
        self.stream = stream
        self.fd = stream.fileno()
        os.set_blocking(self.fd, False)
        self.buf = b""
        self.eof = False

    def drain(self):
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                self.eof = True
                break
            if not chunk:
                self.eof = True
                break
            self.buf += chunk
        lines = []
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            lines.append(raw.decode("utf-8", "replace"))
        return lines

    def rest(self):
        self.drain()
        out, self.buf = self.buf, b""
        return out.decode("utf-8", "replace")


def cli_pid(p):
    """The pid of the CLI itself. With I02_CHILD_WRAPPER set the spawned pid is
    bwrap, and signalling it would exercise bwrap's termination behaviour and
    not the CLI's (the defect i01's review caught)."""
    if not CHILD_WRAPPER:
        return p.pid
    for d in descendants(p.pid):
        cmd = cmdline(d) or ""
        if not cmd:
            continue
        argv0 = cmd.split()[0]
        if "claude" in cmd and "bwrap" not in argv0:
            return d
    return p.pid


def run_plain(argv, cwd, timeout=120):
    """A non-CLI helper command (git, etc). Not a probe subject."""
    t0 = time.time()
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return {"argv": argv, "rc": p.returncode, "dur_s": round(time.time() - t0, 3),
            "stdout": p.stdout, "stderr": p.stderr}


# --------------------------------------------------------------------------
# working-tree fixture (gate item 7)
# --------------------------------------------------------------------------

SKIP_DIRS = {".git"}


def tree_manifest(root):
    """sha256 of every file in the working tree, excluding .git.

    Item 7 asks for byte-identical, and byte-identical means a recorded hash
    comparison. Symlinks are hashed by their target string, not followed.
    """
    entries = {}
    dirs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for d in dirnames:
            dirs.append(os.path.relpath(os.path.join(dirpath, d), root))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                st = os.lstat(full)
                if os.path.islink(full):
                    digest = hashlib.sha256(
                        os.readlink(full).encode("utf-8", "replace")).hexdigest()
                    kind = "symlink"
                else:
                    h = hashlib.sha256()
                    with open(full, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
                    digest = h.hexdigest()
                    kind = "file"
                entries[rel] = {"kind": kind, "size": st.st_size,
                                "mode": oct(st.st_mode & 0o7777), "sha256": digest}
            except OSError as exc:
                entries[rel] = {"error": "%s %s" % (exc.errno, exc.strerror)}
    return entries, sorted(dirs)


def git_state(root):
    def g(*a):
        try:
            r = subprocess.run(["git"] + list(a), cwd=root, capture_output=True,
                               text=True, timeout=60)
            return r.stdout.strip() if r.returncode == 0 else "RC%d:%s" % (
                r.returncode, r.stderr.strip()[:200])
        except Exception as exc:
            return "ERR:%r" % (exc,)
    return {
        "head": g("rev-parse", "HEAD"),
        "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
        "upstream": g("rev-parse", "--abbrev-ref", "@{u}"),
        "ahead_of_upstream": g("rev-list", "--count", "@{u}..HEAD"),
        "status_porcelain": g("status", "--porcelain=v1", "-uall"),
        "worktree_list": g("worktree", "list"),
    }


def snapshot(root):
    entries, dirs = tree_manifest(root)
    blob = json.dumps({"files": entries, "dirs": dirs}, sort_keys=True)
    return {
        "root": root,
        "n_files": len(entries),
        "n_dirs": len(dirs),
        "tree_hash": hashlib.sha256(blob.encode()).hexdigest(),
        "files": entries,
        "dirs": dirs,
        "git": git_state(root),
    }


def snapshot_diff(before, after):
    bf, af = before["files"], after["files"]
    added = sorted(set(af) - set(bf))
    removed = sorted(set(bf) - set(af))
    changed = sorted(k for k in set(bf) & set(af) if bf[k] != af[k])
    return {
        "tree_hash_before": before["tree_hash"],
        "tree_hash_after": after["tree_hash"],
        "byte_identical": before["tree_hash"] == after["tree_hash"],
        "files_added": added,
        "files_removed": removed,
        "files_changed": changed,
        "dirs_added": sorted(set(after["dirs"]) - set(before["dirs"])),
        "dirs_removed": sorted(set(before["dirs"]) - set(after["dirs"])),
        "git_before": before["git"],
        "git_after": after["git"],
        "git_identical": before["git"] == after["git"],
    }


def cmd_fixture(a):
    """Build the item-7 fixture: a git working tree carrying uncommitted,
    untracked and unpushed work.

    The upstream is established by cloning a seed repository, not by pushing:
    `git push` is denied to this worker by policy, and a clone gives the same
    ahead-of-upstream shape.
    """
    base = os.path.abspath(a.dir)
    seed = os.path.join(base, "seed")
    fixture = os.path.join(base, "fixture")
    for path in (seed, fixture):
        if os.path.exists(path):
            shutil.rmtree(path)
    os.makedirs(seed)
    steps = []

    def g(cwd, *args):
        r = run_plain(["git"] + list(args), cwd)
        steps.append(r)
        if r["rc"] != 0:
            raise SystemExit("git failed: %s\n%s" % (args, r["stderr"]))
        return r

    with open(os.path.join(seed, "tracked_clean.txt"), "w") as f:
        f.write("clean baseline, must never change\n")
    with open(os.path.join(seed, "tracked_dirty.txt"), "w") as f:
        f.write("committed content\n")
    g(seed, "init", "-q", "-b", "main")
    g(seed, "add", "-A")
    g(seed, "-c", "user.email=probe@example.invalid", "-c", "user.name=i02 probe",
      "commit", "-q", "-m", "baseline", "--no-verify")

    g(base, "clone", "-q", seed, fixture)

    # unpushed: a local commit the upstream does not have
    with open(os.path.join(fixture, "tracked_clean.txt"), "a") as f:
        f.write("a line added in an unpushed commit\n")
    g(fixture, "add", "tracked_clean.txt")
    g(fixture, "-c", "user.email=probe@example.invalid", "-c", "user.name=i02 probe",
      "commit", "-q", "-m", "unpushed local work", "--no-verify")

    # uncommitted: a modification to a tracked file, left in the working tree
    with open(os.path.join(fixture, "tracked_dirty.txt"), "a") as f:
        f.write("uncommitted edit, must survive every transition\n")

    # untracked: files git does not know about at all
    with open(os.path.join(fixture, "untracked_note.txt"), "w") as f:
        f.write("untracked work, the case that is easiest to lose\n")
    os.makedirs(os.path.join(fixture, "untracked_dir"), exist_ok=True)
    with open(os.path.join(fixture, "untracked_dir", "deep.txt"), "w") as f:
        f.write("untracked, one level down\n")

    snap = snapshot(fixture)
    rec("fixture", {"base": base, "seed": seed, "fixture": fixture,
                    "git_steps": [{"argv": s["argv"], "rc": s["rc"]} for s in steps],
                    "snapshot": snap})
    print(fixture)


def cmd_snapshot(a):
    rec("snapshot", {"label": a.label, "snapshot": snapshot(os.path.abspath(a.dir))})


# --------------------------------------------------------------------------
# the core primitive: one `-p` turn
# --------------------------------------------------------------------------

def summarise_init(ev):
    """The structured state read, taken from published output (the system/init
    event on --output-format stream-json), not scraped from screen text."""
    if not ev:
        return None
    return {
        "session_id": ev.get("session_id"),
        "model": ev.get("model"),
        "permissionMode": ev.get("permissionMode"),
        "cwd": ev.get("cwd"),
        "claude_code_version": ev.get("claude_code_version"),
        "n_tools": len(ev.get("tools") or []),
        "mcp_servers": [{"name": s.get("name"), "status": s.get("status")}
                        for s in (ev.get("mcp_servers") or [])],
        "n_skills": len(ev.get("skills") or []),
        "n_agents": len(ev.get("agents") or []),
        "output_style": ev.get("output_style"),
        "keys": sorted(ev.keys()),
    }


def summarise_result(ev):
    if not ev:
        return None
    usage = ev.get("usage") or {}
    return {
        "session_id": ev.get("session_id"),
        "is_error": ev.get("is_error"),
        "subtype": ev.get("subtype"),
        "terminal_reason": ev.get("terminal_reason"),
        "stop_reason": ev.get("stop_reason"),
        "num_turns": ev.get("num_turns"),
        "duration_ms": ev.get("duration_ms"),
        "total_cost_usd": ev.get("total_cost_usd"),
        "result": (ev.get("result") or "")[:400],
        "usage": {k: usage.get(k) for k in
                  ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                   "cache_read_input_tokens")},
    }


def do_turn(cwd, prompt, session_id=None, resume=None, kill_after=None,
            extra=None, timeout=300, label="", watch_dir=None, detach=False,
            state_file=None, same_group=False, out_path=None):
    """Spawn one `claude -p` turn and record everything a supervisor can see.

    kill_after: seconds after the system/init event at which SIGTERM is sent to
    the CLI (not to a wrapper). None means run to completion.
    detach: return as soon as the child is spawned (used by the supervisor-kill
    cases, where the point is that the supervisor dies while the child runs).
    same_group: leave the child in the supervisor's own process group instead of
    calling setsid. A supervisor would normally isolate the child (and this
    harness does, by default); the option exists so the "child died with its
    parent" case can be produced at all, by killing the shared group.
    """
    args = ["-p", prompt, "--output-format", "stream-json", "--verbose"]
    if session_id:
        args += ["--session-id", session_id]
    if resume:
        args += ["--resume", resume]
    args += list(extra or [])

    before = snapshot(watch_dir) if watch_dir else None
    t0 = time.time()
    p = spawn(args, cwd, new_session=not same_group,
              out_path=out_path if detach else None)
    target = p.pid
    if CHILD_WRAPPER:
        for _ in range(200):              # resolve the CLI pid under the wrapper
            target = cli_pid(p)
            if target != p.pid:
                break
            time.sleep(0.05)

    if detach:
        state = {"label": label, "argv": child_argv(args), "cwd": cwd,
                 "same_group": same_group, "child_output_prefix": out_path,
                 "spawned_pid": p.pid, "cli_pid": target, "spawn_ts": t0,
                 "requested_session_id": session_id, "resumed": resume,
                 "child_cwd_at_spawn": proc_cwd(target)}
        if state_file:
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state, f)
        rec("turn_detached", state)
        return state

    reader = LineReader(p.stdout)
    err_reader = LineReader(p.stderr)
    err_chunks = []
    events, cwd_samples = [], []
    init_ev = result_ev = None
    first_event_t = init_t = None
    deadline = t0 + timeout
    kill_at = None
    signalled = False
    watched = []
    while True:
        now = time.time()
        if p.poll() is not None and reader.eof and err_reader.eof:
            break
        if now > deadline:
            break
        for ln in reader.drain():
            if first_event_t is None:
                first_event_t = round(now - t0, 3)
            try:
                j = json.loads(ln)
            except Exception:
                events.append({"type": "UNPARSEABLE", "raw": ln[:200]})
                continue
            events.append({"type": j.get("type"), "subtype": j.get("subtype"),
                           "session_id": j.get("session_id"),
                           "t": round(now - t0, 3)})
            if j.get("type") == "system" and j.get("subtype") == "init" and not init_ev:
                init_ev = j
                init_t = round(now - t0, 3)
                if kill_after is not None:
                    kill_at = now + kill_after
            if j.get("type") == "result":
                result_ev = j
        err_chunks.extend(err_reader.drain())
        c = proc_cwd(target)
        if c and (not cwd_samples or cwd_samples[-1]["cwd"] != c):
            cwd_samples.append({"t": round(now - t0, 3), "cwd": c})
        if kill_at and now >= kill_at and not signalled:
            watched = proc_snapshot(p.pid)
            if target != p.pid:
                watched.append({"pid": target, "ppid": ppid_of(target),
                                "cmdline": (cmdline(target) or "")[:160],
                                "cwd": proc_cwd(target)})
            os.kill(target, signal.SIGTERM)
            signalled = True
        time.sleep(0.05)

    try:
        p.wait(timeout=30)
        timed_out = False
    except subprocess.TimeoutExpired:
        # Kill the CLI itself and everything under it, then the spawned process.
        # p.kill() alone signals the spawned pid, which is bwrap when a wrapper
        # is in use, and never reaches the inherited MCP children in either case.
        timeout_kills = descendants(p.pid) + ([target] if target != p.pid else [])
        watched = watched or [{"pid": d, "ppid": ppid_of(d),
                               "cmdline": (cmdline(d) or "")[:160], "cwd": proc_cwd(d)}
                              for d in timeout_kills]
        for pid in timeout_kills:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        p.kill()
        p.wait()
        timed_out = True
    tail = reader.rest()
    err = "\n".join(err_chunks) + err_reader.rest()
    time.sleep(1.0)
    survivors = [d for d in watched if alive(d["pid"])]
    for d in survivors:
        try:
            os.kill(d["pid"], signal.SIGKILL)
        except Exception:
            pass

    after = snapshot(watch_dir) if watch_dir else None
    out = {
        "label": label,
        "argv": child_argv(args),
        "cwd": cwd,
        "requested_session_id": session_id,
        "resumed": resume,
        "spawned_pid": p.pid,
        "cli_pid": target,
        "signalled": signalled,
        "signal": "SIGTERM" if signalled else None,
        "rc": p.returncode,
        "reaped": p.returncode is not None,
        "timed_out": timed_out,
        "dur_s": round(time.time() - t0, 2),
        "first_event_s": first_event_t,
        "init_s": init_t,
        "n_events": len(events),
        "event_types": [e["type"] for e in events][:40],
        "init": summarise_init(init_ev),
        "result": summarise_result(result_ev),
        "child_cwd_samples": cwd_samples,
        "watched_before_signal": watched,
        "survivors_after_reap": survivors,
        "stderr": err.strip()[:2000],
        "tail_unparsed_len": len(tail),
    }
    if before:
        out["fixture"] = snapshot_diff(before, after)
    rec("turn", out)
    return out


def cmd_turn(a):
    do_turn(cwd=os.path.abspath(a.cwd), prompt=a.prompt, session_id=a.session_id,
            resume=a.resume, kill_after=a.kill_after, extra=a.extra,
            timeout=a.timeout, label=a.label,
            watch_dir=os.path.abspath(a.watch_dir) if a.watch_dir else None)


# --------------------------------------------------------------------------
# internals observer -- NOT part of the supervisor surface
# --------------------------------------------------------------------------

def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")


def find_transcripts(sid):
    root = os.path.join(config_dir(), "projects")
    hits = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if sid in name:
                hits.append(os.path.join(dirpath, name))
    return sorted(hits)


def transcript_facts(path):
    n_lines = 0
    sids, types, roles = {}, {}, {}
    first_ts = last_ts = None
    texts = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            n_lines += 1
            try:
                j = json.loads(line)
            except Exception:
                continue
            s = j.get("sessionId")
            if s:
                sids[s] = sids.get(s, 0) + 1
            t = j.get("type")
            if t:
                types[t] = types.get(t, 0) + 1
            msg = j.get("message") or {}
            r = msg.get("role")
            if r:
                roles[r] = roles.get(r, 0) + 1
            ts = j.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            content = msg.get("content")
            if isinstance(content, str):
                texts.append([r, content[:200]])
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        texts.append([r, (c.get("text") or "")[:200]])
    st = os.stat(path)
    return {"path": path, "size": st.st_size, "mtime": round(st.st_mtime, 3),
            "n_lines": n_lines, "session_ids_inside": sids, "types": types,
            "roles": roles, "first_timestamp": first_ts, "last_timestamp": last_ts,
            "message_texts": texts[-16:]}


def cmd_transcripts(a):
    facts = [transcript_facts(p) for p in find_transcripts(a.session_id)]
    rec("transcripts", {"internals_observer": True, "label": a.label,
                        "session_id": a.session_id, "n_files": len(facts),
                        "files": facts,
                        "grep": [{"needle": n,
                                  "hits": sum(1 for f in facts
                                              for pair in f["message_texts"]
                                              if n in (pair[1] or ""))}
                                 for n in (a.needle or [])]})


def cmd_leftovers(a):
    """What one worker leaves behind, enumerated from the outside."""
    root = os.path.join(config_dir(), "projects")
    slugs = {}
    for dirpath, _d, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel == ".":
            continue
        if a.slug_filter and a.slug_filter not in rel:
            continue
        files = []
        for name in sorted(filenames):
            st = os.stat(os.path.join(dirpath, name))
            files.append({"name": name, "size": st.st_size,
                          "mtime": round(st.st_mtime, 3)})
        slugs[rel] = {"n_files": len(files),
                      "total_bytes": sum(f["size"] for f in files),
                      "files": files if a.verbose else files[:5]}
    other = {}
    for sub in ("todos", "shell-snapshots", "statsig", "history.jsonl",
                "file-history"):
        path = os.path.join(config_dir(), sub)
        if os.path.exists(path):
            if os.path.isdir(path):
                names = os.listdir(path)
                recent = [n for n in names
                          if os.stat(os.path.join(path, n)).st_mtime >= (a.since or 0)]
                other[sub] = {"kind": "dir", "n_entries": len(names),
                              "n_touched_since": len(recent),
                              "touched": sorted(recent)[:20]}
            else:
                st = os.stat(path)
                other[sub] = {"kind": "file", "size": st.st_size,
                              "mtime": round(st.st_mtime, 3)}
    rec("leftovers", {"internals_observer": True, "label": a.label,
                      "config_dir": config_dir(), "since": a.since,
                      "project_slugs": slugs, "other_state": other})


# --------------------------------------------------------------------------
# supervisor kill / restart (the C2 lifecycle shape)
# --------------------------------------------------------------------------

def cmd_supervise(a):
    """A minimal supervisor: spawn a child, persist what a restart would need,
    then hold. It is meant to be SIGKILLed from outside."""
    if not (a.session_id or a.resume):
        raise SystemExit("supervise requires --session-id or --resume: a child "
                         "whose id the supervisor never chose cannot be resumed "
                         "from persisted state, which is the whole experiment.")
    state = do_turn(cwd=os.path.abspath(a.cwd), prompt=a.prompt,
                    session_id=a.session_id, resume=a.resume, extra=a.extra,
                    label=a.label, detach=True, state_file=a.state_file,
                    same_group=a.same_group,
                    out_path=a.state_file + ".child")
    rec("supervisor_holding", {"label": a.label, "supervisor_pid": os.getpid(),
                               "state_file": a.state_file, "child": state})
    time.sleep(a.hold)
    rec("supervisor_exited_normally", {"label": a.label})


def cmd_restart(a):
    """Restart from persisted state only.

    Resolve the child BEFORE resuming: a live orphan plus a resume is two
    writers, which the provider will not refuse (U32), and modelling that order
    would be modelling the unsafe one.
    """
    with open(a.state_file, encoding="utf-8") as f:
        state = json.load(f)
    pid = state.get("cli_pid") or state.get("spawned_pid")
    cmd_at_restart = cmdline(pid) or ""
    sid = state.get("requested_session_id") or state.get("resumed") or ""
    if not sid:
        rec("restart_aborted", {"label": a.label,
                                "reason": "persisted state carries no session id; "
                                          "refusing to identify a pid or to resume"})
        return
    # pid reuse is the trap here: only treat it as our child if the command line
    # still carries the session id we spawned it with. Without that predicate the
    # check would accept any live process whose argv mentions claude.
    is_ours = alive(pid) and "claude" in cmd_at_restart and sid in cmd_at_restart
    resolution = {"persisted_pid": pid, "alive_at_restart": alive(pid),
                  "cmdline_at_restart": cmd_at_restart[:200],
                  "identified_as_our_child": bool(is_ours)}
    t0 = time.time()
    if is_ours:
        descs = proc_snapshot(pid)
        os.kill(pid, signal.SIGTERM)
        gone_at = None
        while time.time() - t0 < 60:
            if not alive(pid):
                gone_at = round(time.time() - t0, 3)
                break
            time.sleep(0.1)
        if gone_at is None:
            os.kill(pid, signal.SIGKILL)
            while time.time() - t0 < 90 and alive(pid):
                time.sleep(0.1)
            gone_at = round(time.time() - t0, 3)
            resolution["escalated_to_sigkill"] = True
        resolution.update({"action": "terminated", "gone_after_s": gone_at,
                           "descendants_before_signal": descs,
                           "descendant_survivors": [d for d in descs
                                                    if alive(d["pid"])]})
        for d in resolution["descendant_survivors"]:
            try:
                os.kill(d["pid"], signal.SIGKILL)
            except Exception:
                pass
    else:
        resolution.update({"action": "confirmed_already_gone"
                                     if not alive(pid) else "pid_not_ours"})
    resolution["alive_after_resolution"] = alive(pid) and is_ours
    rec("restart_child_resolution", {"label": a.label, "resolution": resolution,
                                     "persisted_state": state})
    if resolution["alive_after_resolution"]:
        rec("restart_aborted", {"label": a.label,
                                "reason": "child still alive; refusing to resume "
                                          "past a live writer"})
        return
    do_turn(cwd=state["cwd"], prompt=a.prompt, resume=sid,
            label=a.label + ":resume-after-restart",
            watch_dir=os.path.abspath(a.watch_dir) if a.watch_dir else None,
            timeout=a.timeout)


# --------------------------------------------------------------------------
# U38: does removing the transcript release the session-id claim?
# --------------------------------------------------------------------------

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def cmd_u38(a):
    """Operator-authorised follow-up to i01 section 3.8.4.

    Safety rule, enforced in code and not merely intended: this only ever
    touches a transcript whose basename is exactly `<uuid>.jsonl` for a uuid
    this subcommand generated itself in this run. It refuses to touch anything
    else, and it MOVES rather than deletes, so the file can be put back.
    """
    cwd = os.path.abspath(a.cwd)
    quarantine = os.path.abspath(a.quarantine)
    os.makedirs(quarantine, exist_ok=True)
    sid = str(uuid.uuid4())
    rec("u38_plan", {"internals_observer": True, "session_id": sid, "cwd": cwd,
                     "quarantine": quarantine,
                     "rule": "only <this-run's-uuid>.jsonl may be moved; "
                             "move, not delete"})

    do_turn(cwd=cwd, prompt=a.prompt, session_id=sid, label="u38:create")
    rec("u38_after_create", {"internals_observer": True, "session_id": sid,
                             "transcripts": [transcript_facts(p)
                                             for p in find_transcripts(sid)]})

    do_turn(cwd=cwd, prompt=a.prompt, session_id=sid,
            label="u38:reclaim-before-move", timeout=90)

    moved = []
    for path in find_transcripts(sid):
        base = os.path.basename(path)
        if base != sid + ".jsonl":
            rec("u38_refused_to_move", {"path": path,
                                        "reason": "basename is not <this-uuid>.jsonl"})
            continue
        dest = os.path.join(quarantine,
                            os.path.basename(os.path.dirname(path)) + "__" + base)
        shutil.move(path, dest)
        moved.append({"from": path, "to": dest, "size": os.path.getsize(dest)})
    rec("u38_moved", {"internals_observer": True, "session_id": sid, "moved": moved,
                      "remaining": find_transcripts(sid)})

    do_turn(cwd=cwd, prompt=a.prompt, session_id=sid,
            label="u38:reclaim-after-move", timeout=180)
    rec("u38_final_state", {"internals_observer": True, "session_id": sid,
                            "transcripts": [transcript_facts(p)
                                            for p in find_transcripts(sid)],
                            "quarantined": moved})


def cmd_u38_restore(a):
    """Put the quarantined transcripts back where they came from."""
    restored, skipped = [], []
    for name in sorted(os.listdir(a.quarantine)):
        slug, _, base = name.partition("__")
        stem = base[:-6] if base.endswith(".jsonl") else ""
        if not slug or not UUID_RE.match(stem):
            skipped.append(name)
            continue
        dest_dir = os.path.join(config_dir(), "projects", slug)
        dest = os.path.join(dest_dir, base)
        if os.path.exists(dest):
            skipped.append(name + " (destination exists)")
            continue
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(os.path.join(a.quarantine, name), dest)
        restored.append(dest)
    rec("u38_restore", {"internals_observer": True, "restored": restored,
                        "skipped": skipped})


# --------------------------------------------------------------------------
# spawn-cost arms (--setting-sources / --strict-mcp-config)
# --------------------------------------------------------------------------

COST_ARMS = {
    "default": [],
    "strict-mcp": ["--strict-mcp-config"],
    "user-settings-only": ["--setting-sources", "user"],
    "strict-mcp+user-settings": ["--strict-mcp-config", "--setting-sources", "user"],
}


def cmd_costab(a):
    for name in a.arms:
        do_turn(cwd=os.path.abspath(a.cwd), prompt=a.prompt,
                session_id=str(uuid.uuid4()), extra=COST_ARMS[name],
                label="cost:" + name, timeout=180)


# --------------------------------------------------------------------------

def denial_selfcheck(paths):
    out = {}
    for path in paths:
        try:
            if os.path.isdir(path):
                out[path] = {"kind": "dir", "entries": len(os.listdir(path))}
            else:
                with open(path, "rb") as f:
                    out[path] = {"kind": "file", "first_bytes": len(f.read(16))}
        except OSError as exc:
            out[path] = {"errno": exc.errno, "strerror": exc.strerror}
    return out


def cmd_selfcheck(a):
    rec("selfcheck", {"label": a.label, "harness_pid": os.getpid(),
                      "child_wrapper": CHILD_WRAPPER,
                      "harness_view": denial_selfcheck(a.deny_paths or [])})


def cmd_env(a):
    for argv in (["--version"], ["doctor"]):
        r = run_plain([CLAUDE] + argv, os.path.abspath(a.cwd), timeout=120)
        rec("env", {"argv": r["argv"], "rc": r["rc"],
                    "stdout": r["stdout"][:4000], "stderr": r["stderr"][:2000]})
    rec("env_context", {"python": sys.version.split()[0], "cwd": os.getcwd(),
                        "config_dir": config_dir(),
                        "out_dir": os.path.abspath(OUT_DIR)})


def main():
    ap = argparse.ArgumentParser(description="I-02 conversation probe")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("env")
    p.add_argument("--cwd", default=".")
    p.set_defaults(fn=cmd_env)

    p = sub.add_parser("fixture")
    p.add_argument("--dir", required=True)
    p.set_defaults(fn=cmd_fixture)

    p = sub.add_parser("snapshot")
    p.add_argument("--dir", required=True)
    p.add_argument("--label", default="")
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("turn")
    p.add_argument("--cwd", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--session-id")
    p.add_argument("--resume")
    p.add_argument("--kill-after", type=float)
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--label", default="")
    p.add_argument("--watch-dir")
    # one flag per --extra, e.g. --extra=--permission-mode --extra=acceptEdits
    p.add_argument("--extra", action="append", default=[])
    p.set_defaults(fn=cmd_turn)

    p = sub.add_parser("transcripts")
    p.add_argument("--session-id", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--needle", nargs="*", default=[])
    p.set_defaults(fn=cmd_transcripts)

    p = sub.add_parser("leftovers")
    p.add_argument("--label", default="")
    p.add_argument("--slug-filter")
    p.add_argument("--since", type=float)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_leftovers)

    p = sub.add_parser("supervise")
    p.add_argument("--cwd", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--session-id")
    p.add_argument("--resume")
    p.add_argument("--state-file", required=True)
    p.add_argument("--hold", type=float, default=600)
    p.add_argument("--label", default="")
    p.add_argument("--same-group", action="store_true")
    p.add_argument("--extra", action="append", default=[])
    p.set_defaults(fn=cmd_supervise)

    p = sub.add_parser("restart")
    p.add_argument("--state-file", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--watch-dir")
    p.add_argument("--timeout", type=float, default=300)
    p.set_defaults(fn=cmd_restart)

    p = sub.add_parser("u38")
    p.add_argument("--cwd", required=True)
    p.add_argument("--quarantine", required=True)
    p.add_argument("--prompt", default="reply with just: ok")
    p.set_defaults(fn=cmd_u38)

    p = sub.add_parser("u38-restore")
    p.add_argument("--quarantine", required=True)
    p.set_defaults(fn=cmd_u38_restore)

    p = sub.add_parser("costab")
    p.add_argument("--cwd", required=True)
    p.add_argument("--prompt", default="reply with just: ok")
    p.add_argument("--arms", nargs="*",
                   default=["default", "strict-mcp", "strict-mcp+user-settings"])
    p.set_defaults(fn=cmd_costab)

    p = sub.add_parser("selfcheck")
    p.add_argument("--label", default="")
    p.add_argument("--deny-paths", nargs="*", default=[])
    p.set_defaults(fn=cmd_selfcheck)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
