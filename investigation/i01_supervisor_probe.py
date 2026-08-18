#!/usr/bin/env python3
"""I-01 supervisor probe: what a parent process must know about a `claude -p` child.

Throwaway harness for suisya-systems/interlock issue #6 (D-0026). It uses the
public CLI only (D-0010): it never reads the CLI's per-user config directory,
transcript files or any internal socket. The one deliberate exception is the
`observe` subcommand, which exists to answer U34/U36 and is NOT part of the
supervisor surface -- see the note in investigation/i01-supervisor-probe.md.

Each subcommand writes one verbatim JSON record per step to $I01_OUT (default
./results) and echoes it to stdout.

Usage:
    python3 i01_supervisor_probe.py <subcommand> [options]

Subcommands:
    env          CLI version / capability probe / documented-flag inventory
    spawn        spawn matrix: output formats, --settings, --permission-mode
    streams      stream-json framing, arrival times, flush on abnormal exit
    exits        exit-code table against causes
    signals      SIGTERM / SIGINT / SIGKILL, process group, what is left behind
    orphan       SIGKILL the supervisor, watch the children
    window       admission-window sweep at --session-id creation
    u36          what the refusal is keyed to
    concurrency  N concurrent children, state-readout latency
    readback     effective-configuration readback on public surfaces
    scenario     spawn -> structured read -> signal -> reap (used by the
                 internals-free negative; runs restricted and unrestricted)
    observe      U34 bounding probe. Reads internal paths ON PURPOSE. Not part
                 of the supervisor harness.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid

CLAUDE = os.environ.get("I01_CLAUDE_BIN", "claude")
OUT_DIR = os.environ.get("I01_OUT", "./results")
SHORT_PROMPT = "reply with ok"
LONG_PROMPT = (
    "Count from 1 to 40, writing one short sentence about each number. Take your time."
)

# Optional wrapper the harness prepends to every child argv. Used by the
# internals-free negative so a restricted harness can still hand the child
# normal access to the CLI's own state. Format: shell-free, space separated.
CHILD_WRAPPER = os.environ.get("I01_CHILD_WRAPPER", "").split() or []


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def rec(step, obj):
    obj = dict(obj)
    obj["step"] = step
    os.makedirs(OUT_DIR, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    with open(os.path.join(OUT_DIR, "records.jsonl"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)
    return obj


# --------------------------------------------------------------------------
# process helpers (public /proc only; no CLI internals)
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


def proc_snapshot(pid):
    return [
        {"pid": p, "ppid": ppid_of(p), "cmdline": (cmdline(p) or "")[:160]}
        for p in descendants(pid)
    ]


# --------------------------------------------------------------------------
# spawn helpers
# --------------------------------------------------------------------------

def child_argv(args):
    return CHILD_WRAPPER + [CLAUDE] + args


def spawn(args, cwd, env_extra=None, new_session=True):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        child_argv(args),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,          # binary: see LineReader, and codex review P2
        bufsize=0,
        start_new_session=new_session,
    )


class LineReader:
    """Non-blocking, complete-line reader over a raw pipe.

    A select() on a buffered TextIOWrapper is unsound: one readline() can pull
    several lines into user space, after which the fd is no longer readable
    while complete lines still sit in the buffer. This reads the raw fd
    non-blockingly and does its own line framing, so drain() really does return
    everything currently available.
    """

    def __init__(self, stream):
        self.stream = stream
        self.fd = stream.fileno()
        os.set_blocking(self.fd, False)
        self.buf = b""
        self.eof = False

    def drain(self):
        """Return every complete line available right now (no blocking)."""
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
        """Whatever is left, complete lines or not."""
        self.drain()
        out, self.buf = self.buf, b""
        return out.decode("utf-8", "replace")


def cli_pid(p):
    """The pid of the CLI itself.

    Without a wrapper that is the spawned pid. With I01_CHILD_WRAPPER set the
    spawned pid is the wrapper (bwrap), so signalling it would exercise the
    wrapper's termination behaviour and not the CLI's (codex review P1).
    """
    if not CHILD_WRAPPER:
        return p.pid
    for d in descendants(p.pid):
        cmd = cmdline(d) or ""
        if "claude" in cmd and "bwrap" not in cmd.split()[0]:
            return d
    return p.pid


def comm(p, timeout=180):
    """communicate() on a binary pipe, decoded. Every direct caller must use
    this: spawn() opens binary pipes, and rec() cannot serialise bytes.
    """
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    return (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace")


def run(args, cwd, timeout=180, env_extra=None):
    t0 = time.time()
    p = spawn(args, cwd, env_extra=env_extra)
    try:
        out, err = p.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        timed_out = True
    return {
        "argv": child_argv(args),
        "cwd": cwd,
        "rc": p.returncode,
        "dur_s": round(time.time() - t0, 2),
        "timed_out": timed_out,
        "stdout": out.decode("utf-8", "replace"),
        "stderr": err.decode("utf-8", "replace"),
    }


def session_id_of(stdout):
    try:
        return json.loads(stdout).get("session_id")
    except Exception:
        return None


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_env(a):
    for probe in (["--version"], ["doctor"]):
        r = subprocess.run(
            [CLAUDE] + probe, capture_output=True, text=True, cwd=a.cwd, timeout=120
        )
        rec("capability_probe", {"argv": [CLAUDE] + probe, "rc": r.returncode,
                                 "stdout": r.stdout, "stderr": r.stderr})
    # `capabilities` is what D-0010 asks for first; record that it does not exist.
    r = subprocess.run([CLAUDE, "capabilities"], capture_output=True, text=True,
                       cwd=a.cwd, timeout=120)
    rec("capabilities_subcommand", {"rc": r.returncode, "stdout": r.stdout[:2000],
                                    "stderr": r.stderr[:2000]})


def cmd_spawn(a):
    """What must a parent supply for a spawn to be reproducible?"""
    settings_path = os.path.join(a.cwd, "i01-settings.json")
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump({"permissions": {"deny": ["Bash"]}}, f)

    cases = [
        ("text", ["-p", SHORT_PROMPT]),
        ("json", ["-p", SHORT_PROMPT, "--output-format", "json"]),
        ("stream-json", ["-p", SHORT_PROMPT, "--output-format", "stream-json",
                         "--verbose"]),
        ("settings-file", ["-p", SHORT_PROMPT, "--output-format", "json",
                           "--settings", settings_path]),
        ("settings-inline", ["-p", SHORT_PROMPT, "--output-format", "json",
                             "--settings", '{"permissions":{"deny":["Bash"]}}']),
        ("permission-mode-plan", ["-p", SHORT_PROMPT, "--output-format", "json",
                                  "--permission-mode", "plan"]),
        ("session-id", ["-p", SHORT_PROMPT, "--output-format", "json",
                        "--session-id", str(uuid.uuid4())]),
        ("empty-env", ["-p", SHORT_PROMPT, "--output-format", "json"]),
    ]
    for name, args in cases:
        if name == "empty-env":
            # minimal environment: what does a spawn actually need inherited?
            t0 = time.time()
            p = subprocess.Popen(
                child_argv(args), cwd=a.cwd,
                env={"PATH": os.environ.get("PATH", ""),
                     "HOME": os.environ.get("HOME", "")},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True)
            out, err = comm(p, timeout=180)
            r = {"argv": child_argv(args), "cwd": a.cwd, "rc": p.returncode,
                 "dur_s": round(time.time() - t0, 2), "stdout": out, "stderr": err,
                 "env": "PATH+HOME only"}
        else:
            r = run(args, a.cwd)
        r["case"] = name
        r["session_id"] = session_id_of(r["stdout"])
        rec("spawn", r)


def cmd_streams(a):
    """stream-json framing, arrival times, and flush on abnormal termination."""
    # 1. framing + arrival times over a normal run
    p = spawn(["-p", SHORT_PROMPT, "--output-format", "stream-json", "--verbose"], a.cwd)
    t0 = time.time()
    lines = []
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace")
        lines.append({"t": round(time.time() - t0, 3), "len": len(line),
                      "ends_nl": line.endswith("\n"), "raw": line.rstrip("\n")})
    err = p.stderr.read().decode("utf-8", "replace")
    p.wait()
    parsed = []
    for ln in lines:
        try:
            j = json.loads(ln["raw"])
            parsed.append({"t": ln["t"], "type": j.get("type"),
                           "subtype": j.get("subtype")})
        except Exception as exc:
            parsed.append({"t": ln["t"], "type": "UNPARSEABLE", "err": str(exc)})
    rec("stream_framing", {"rc": p.returncode, "n_lines": len(lines),
                           "all_line_delimited_json":
                               all(x["type"] != "UNPARSEABLE" for x in parsed),
                           "arrival": parsed, "stderr": err,
                           "lines_verbatim": [l["raw"] for l in lines]})

    # 2. flush on abnormal termination: SIGKILL mid-turn, read what survived
    for sig, label in ((signal.SIGKILL, "SIGKILL"), (signal.SIGTERM, "SIGTERM")):
        p = spawn(["-p", LONG_PROMPT, "--output-format", "stream-json", "--verbose"],
                  a.cwd)
        t0 = time.time()
        got = []
        reader = LineReader(p.stdout)
        deadline = t0 + a.kill_after
        while time.time() < deadline and not reader.eof:
            for ln in reader.drain():
                got.append({"t": round(time.time() - t0, 3), "raw": ln,
                            "complete_line": True})
            time.sleep(0.05)
        os.kill(cli_pid(p), sig)
        time.sleep(1.5)
        tail = "".join(x + "\n" for x in reader.drain()) + reader.rest()
        err = p.stderr.read().decode("utf-8", "replace")
        p.wait()
        rec("abnormal_exit_flush", {
            "signal": label, "kill_after_s": a.kill_after, "rc": p.returncode,
            "lines_before_signal": got,
            "tail_after_signal": tail,
            "tail_is_complete_line": tail.endswith("\n") if tail else None,
            "stderr": err})

    # 3. stderr-only content: the already-in-use refusal
    u = str(uuid.uuid4())
    first = run(["-p", SHORT_PROMPT, "--output-format", "json", "--session-id", u], a.cwd)
    rec("stderr_only_holder", dict(first, case="holder"))
    time.sleep(5)
    second = run(["-p", SHORT_PROMPT, "--output-format", "json", "--session-id", u],
                 a.cwd)
    rec("stderr_only_refusal", dict(second, case="second-claimant",
                                    stdout_empty=(second["stdout"] == "")))


def cmd_exits(a):
    """Enumerate exit codes against causes."""
    cases = [
        ("success", ["-p", SHORT_PROMPT, "--output-format", "json"]),
        ("unknown-flag", ["-p", SHORT_PROMPT, "--no-such-flag-i01"]),
        ("bad-flag-value", ["-p", SHORT_PROMPT, "--permission-mode", "nonsense"]),
        ("bad-model", ["-p", SHORT_PROMPT, "--output-format", "json",
                       "--model", "claude-does-not-exist-i01"]),
        ("bad-settings-path", ["-p", SHORT_PROMPT, "--output-format", "json",
                               "--settings", "/nonexistent/i01-settings.json"]),
        ("malformed-settings", ["-p", SHORT_PROMPT, "--output-format", "json",
                                "--settings", "{not json"]),
        ("bad-session-id", ["-p", SHORT_PROMPT, "--output-format", "json",
                            "--session-id", "not-a-uuid"]),
        ("bad-cwd-flag", ["-p", SHORT_PROMPT, "--output-format", "json",
                          "--add-dir", "/nonexistent/i01-dir"]),
        # documented as "only works with --print and stream-json": is it refused
        # or silently ignored? (exit 0 as evidence of nothing)
        ("ignored-flag-combo", ["-p", SHORT_PROMPT, "--output-format", "json",
                                "--include-partial-messages"]),
        ("budget-zero", ["-p", SHORT_PROMPT, "--output-format", "json",
                         "--max-budget-usd", "0.000001"]),
    ]
    for name, args in cases:
        r = run(args, a.cwd, timeout=180)
        r["case"] = name
        rec("exit_code", r)

    # already-in-use refusal
    u = str(uuid.uuid4())
    h = run(["-p", SHORT_PROMPT, "--output-format", "json", "--session-id", u], a.cwd)
    rec("exit_code", dict(h, case="holder-for-refusal"))
    time.sleep(5)
    r = run(["-p", SHORT_PROMPT, "--output-format", "json", "--session-id", u], a.cwd)
    rec("exit_code", dict(r, case="already-in-use"))

    # interrupted run: SIGINT and SIGTERM delivered to the child
    for sig, label in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM"),
                       (signal.SIGKILL, "SIGKILL")):
        p = spawn(["-p", LONG_PROMPT, "--output-format", "json"], a.cwd)
        time.sleep(a.kill_after)
        os.kill(cli_pid(p), sig)
        out, err = comm(p, timeout=60)
        rec("exit_code", {"case": "interrupted-" + label, "rc": p.returncode,
                          "stdout": out, "stderr": err,
                          "stdout_empty": out == ""})


def cmd_signals(a):
    """Signals and process topology, including what the child leaves behind."""
    mcp_path = os.path.join(a.cwd, "i01-mcp-server.py")
    with open(mcp_path, "w", encoding="utf-8") as f:
        f.write(
            "#!/usr/bin/env python3\n"
            "# Minimal stdio MCP server stub: it never answers. Its only job is to\n"
            "# be a child process the CLI starts, so the supervisor can see what\n"
            "# survives termination of the CLI itself.\n"
            "import sys, time\n"
            "sys.stderr.write('i01-mcp-stub up\\n'); sys.stderr.flush()\n"
            "while True:\n"
            "    line = sys.stdin.readline()\n"
            "    if not line:\n"
            "        time.sleep(3600)\n"
        )
    mcp_cfg = os.path.join(a.cwd, "i01-mcp.json")
    with open(mcp_cfg, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"i01stub": {"command": sys.executable,
                                              "args": [mcp_path]}}}, f)

    for sig, label, to_group in (
        (signal.SIGTERM, "SIGTERM", False),
        (signal.SIGINT, "SIGINT", False),
        (signal.SIGKILL, "SIGKILL", False),
        (signal.SIGKILL, "SIGKILL", True),
    ):
        p = spawn(["-p", LONG_PROMPT, "--output-format", "json",
                   "--mcp-config", mcp_cfg, "--strict-mcp-config"], a.cwd)
        time.sleep(a.kill_after)
        before = proc_snapshot(p.pid)
        pgid = os.getpgid(p.pid)
        if to_group:
            os.killpg(pgid, sig)
        else:
            os.kill(p.pid, sig)
        t0 = time.time()
        out, err = comm(p, timeout=30)
        time.sleep(2.0)
        survivors = [d for d in before if alive(d["pid"])]
        rec("signal", {
            "signal": label, "delivered_to": "process group" if to_group else "child pid",
            "child_pid": p.pid, "pgid": pgid, "harness_pid": os.getpid(),
            "descendants_before": before,
            "rc": p.returncode,
            "reap_dur_s": round(time.time() - t0, 2),
            "stdout": out, "stderr": err,
            "survivors_after_2s": survivors,
            "child_alive_after": alive(p.pid),
        })
        # cleanup any survivor so the next case starts clean
        for s in survivors:
            try:
                os.kill(s["pid"], signal.SIGKILL)
            except Exception:
                pass


def cmd_orphan(a):
    """SIGKILL the supervisor; do the -p children survive and keep writing?"""
    driver = os.path.join(a.cwd, "i01-parent.py")
    driver_src = """#!/usr/bin/env python3
import json, os, subprocess, sys, time
cwd, outdir, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
new_session = sys.argv[4] == '1'
prompt = __PROMPT__
kids = []
for i in range(n):
    of = open(os.path.join(outdir, 'orphan-' + str(i) + '.out'), 'w')
    ef = open(os.path.join(outdir, 'orphan-' + str(i) + '.err'), 'w')
    p = subprocess.Popen([__CLAUDE__, '-p', prompt, '--output-format', 'json'],
                         cwd=cwd, stdin=subprocess.DEVNULL, stdout=of,
                         stderr=ef, start_new_session=new_session)
    kids.append(p.pid)
print(json.dumps({'parent': os.getpid(), 'children': kids}), flush=True)
time.sleep(600)
"""
    driver_src = (driver_src.replace("__PROMPT__", repr(LONG_PROMPT))
                            .replace("__CLAUDE__", repr(CLAUDE)))
    with open(driver, "w", encoding="utf-8") as f:
        f.write(driver_src)

    outdir = os.path.join(a.cwd, "orphan-out")
    os.makedirs(outdir, exist_ok=True)

    for new_session in (True, False):
        parent = subprocess.Popen(
            [sys.executable, driver, a.cwd, outdir, "2", "1" if new_session else "0"],
            stdout=subprocess.PIPE, text=True)
        info = json.loads(parent.stdout.readline())
        time.sleep(a.kill_after)
        pre = {str(k): {"alive": alive(k), "ppid": ppid_of(k)} for k in info["children"]}
        sizes_before = {f: os.path.getsize(os.path.join(outdir, f))
                        for f in sorted(os.listdir(outdir))}
        os.kill(info["parent"], signal.SIGKILL)
        parent.wait()
        time.sleep(3)
        mid = {str(k): {"alive": alive(k), "ppid": ppid_of(k), "cmd": cmdline(k)}
               for k in info["children"]}
        time.sleep(45)
        post = {str(k): {"alive": alive(k), "ppid": ppid_of(k)} for k in info["children"]}
        sizes_after = {f: os.path.getsize(os.path.join(outdir, f))
                       for f in sorted(os.listdir(outdir))}
        outs = {}
        for f in sorted(os.listdir(outdir)):
            with open(os.path.join(outdir, f), encoding="utf-8") as fh:
                outs[f] = fh.read()[:1200]
        rec("orphan", {
            "child_new_session": new_session,
            "parent_pid": info["parent"], "children": info["children"],
            "children_before_kill": pre,
            "children_3s_after_parent_sigkill": mid,
            "children_48s_after_parent_sigkill": post,
            "output_sizes_before_kill": sizes_before,
            "output_sizes_after": sizes_after,
            "kept_writing": sizes_after != sizes_before,
            "child_output": outs,
        })
        for k in info["children"]:
            if alive(k):
                try:
                    os.kill(k, signal.SIGKILL)
                except Exception:
                    pass
        for f in os.listdir(outdir):
            os.remove(os.path.join(outdir, f))


def cmd_window(a):
    """Admission-window sweep at --session-id creation."""
    for stagger in a.staggers:
        u = str(uuid.uuid4())
        holder = spawn(["-p", LONG_PROMPT, "--output-format", "json",
                        "--session-id", u], a.cwd)
        t0 = time.time()
        time.sleep(stagger)
        second = run(["-p", SHORT_PROMPT, "--output-format", "json",
                      "--session-id", u], a.cwd, timeout=180)
        hout, herr = comm(holder, timeout=300)
        rec("window", {
            "stagger_s": stagger, "uuid": u,
            "second_rc": second["rc"], "second_dur_s": second["dur_s"],
            "second_stdout_empty": second["stdout"] == "",
            "second_session_id": session_id_of(second["stdout"]),
            "second_stderr": second["stderr"].strip(),
            "verdict": "admitted" if second["rc"] == 0 else "refused",
            "holder_rc": holder.returncode,
            "holder_session_id": session_id_of(hout),
            "holder_dur_s": round(time.time() - t0, 2),
            "holder_stderr": herr.strip(),
        })


def cmd_u36(a):
    """What is the refusal keyed to?"""
    # (a) a session created with --no-session-persistence: is its id re-claimable?
    u = str(uuid.uuid4())
    r1 = run(["-p", SHORT_PROMPT, "--output-format", "json", "--session-id", u,
              "--no-session-persistence"], a.cwd)
    rec("u36_nopersist_first", dict(r1, uuid=u, session_id=session_id_of(r1["stdout"])))
    time.sleep(5)
    r2 = run(["-p", SHORT_PROMPT, "--output-format", "json", "--session-id", u], a.cwd)
    rec("u36_nopersist_reclaim", dict(r2, uuid=u,
                                      verdict="admitted" if r2["rc"] == 0 else "refused"))

    # (b) a normally persisted session, re-claimed as a control
    u2 = str(uuid.uuid4())
    r3 = run(["-p", SHORT_PROMPT, "--output-format", "json", "--session-id", u2], a.cwd)
    rec("u36_persist_first", dict(r3, uuid=u2, session_id=session_id_of(r3["stdout"])))
    time.sleep(5)
    r4 = run(["-p", SHORT_PROMPT, "--output-format", "json", "--session-id", u2], a.cwd)
    rec("u36_persist_reclaim", dict(r4, uuid=u2,
                                    verdict="admitted" if r4["rc"] == 0 else "refused"))
    print(json.dumps({"note": "uuid for the transcript-removal probe", "uuid": u2}))

    # (c) same id, different cwd: is the claim per-project or global?
    other = os.path.join(a.cwd, "other-cwd")
    os.makedirs(other, exist_ok=True)
    r5 = run(["-p", SHORT_PROMPT, "--output-format", "json", "--session-id", u2], other)
    rec("u36_reclaim_other_cwd", dict(r5, uuid=u2, cwd=other,
                                      verdict="admitted" if r5["rc"] == 0 else "refused"))


def cmd_concurrency(a):
    """N concurrent children; how long does the supervisor take to read state?"""
    children = []
    t_start = time.time()
    for i in range(a.n):
        p = spawn(["-p", SHORT_PROMPT, "--output-format", "stream-json", "--verbose"],
                  a.cwd)
        children.append({"i": i, "p": p, "lines": [], "done": False,
                         "reader": LineReader(p.stdout)})
    spawn_dur = time.time() - t_start

    # Readout A: liveness + exit-status poll (non-blocking).
    def readout_liveness():
        lat = []
        for c in children:
            t = time.time()
            c["p"].poll()
            alive(c["p"].pid)
            lat.append((time.time() - t) * 1000.0)
        return lat

    # Readout B: structured-state readout -- drain whatever complete stream-json
    # lines are buffered for each child, without blocking on any of them.
    def readout_structured():
        lat = []
        for c in children:
            t = time.time()
            c["lines"].extend(c["reader"].drain())
            c["done"] = c["reader"].eof
            lat.append((time.time() - t) * 1000.0)
        return lat

    samples_live, samples_struct = [], []
    deadline = time.time() + a.load_seconds
    while time.time() < deadline and any(c["p"].poll() is None for c in children):
        samples_live.append(readout_liveness())
        samples_struct.append(readout_structured())
        time.sleep(0.1)

    # Blocking-readout control: a naive supervisor that calls readline() per
    # child. Only meaningful while a child is still live -- on a finished child
    # readline() hits EOF at once and measures nothing, so record liveness with
    # each sample rather than reporting a bare number.
    block_lat = []
    for c in children:
        was_live = c["p"].poll() is None
        t = time.time()
        try:
            os.set_blocking(c["p"].stdout.fileno(), True)
            c["p"].stdout.readline()
        except Exception:
            pass
        block_lat.append({"child_was_live": was_live,
                          "ms": round((time.time() - t) * 1000.0, 3)})

    for c in children:
        try:
            c["p"].wait(timeout=180)
        except subprocess.TimeoutExpired:
            c["p"].kill()

    def stats(rows):
        flat = sorted(x for row in rows for x in row)
        if not flat:
            return None
        return {"n": len(flat), "min_ms": round(flat[0], 4),
                "median_ms": round(flat[len(flat) // 2], 4),
                "p95_ms": round(flat[int(len(flat) * 0.95)], 4),
                "max_ms": round(flat[-1], 4),
                "sum_per_sweep_ms": round(sum(flat) / max(1, len(rows)), 4)}

    rec("concurrency", {
        "workers": a.n, "spawn_all_dur_s": round(spawn_dur, 2),
        "sweeps": len(samples_live),
        "liveness_readout": stats(samples_live),
        "structured_readout": stats(samples_struct),
        "blocking_readline_control": block_lat,
        "rcs": [c["p"].returncode for c in children],
        "lines_per_child": [len(c["lines"]) for c in children],
    })


def cmd_readback(a):
    """Is the effective permission / sandbox / hook configuration readable?"""
    settings = {
        "permissions": {"deny": ["Bash", "WebFetch"], "allow": ["Read"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "true"}]}]},
    }
    spath = os.path.join(a.cwd, "i01-readback-settings.json")
    with open(spath, "w", encoding="utf-8") as f:
        json.dump(settings, f)

    for label, extra in (
        ("baseline", []),
        ("settings+plan", ["--settings", spath, "--permission-mode", "plan"]),
        ("disallowed-tools", ["--disallowed-tools", "Bash", "--permission-mode",
                              "acceptEdits"]),
    ):
        p = spawn(["-p", SHORT_PROMPT, "--output-format", "stream-json", "--verbose"]
                  + extra, a.cwd)
        out, err = comm(p, timeout=180)
        init = None
        for ln in out.splitlines():
            try:
                j = json.loads(ln)
            except Exception:
                continue
            if j.get("type") == "system" and j.get("subtype") == "init":
                init = j
                break
        rec("readback", {"case": label, "rc": p.returncode, "argv_extra": extra,
                         "init_event": init, "stderr": err,
                         "init_keys": sorted(init.keys()) if init else None})


def denial_selfcheck(paths):
    """What does THIS process see at each named internal path?"""
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
        except Exception as exc:  # pragma: no cover
            out[path] = {"error": repr(exc)}
    return out


def cmd_scenario(a):
    """spawn -> structured state read -> signal-terminate -> reap.

    This is the end-to-end supervisor scenario used by the internals-free
    negative. It touches no path under the CLI's config directory.
    """
    denied = a.deny_paths or []
    rec("scenario_selfcheck", {"label": a.label, "harness_pid": os.getpid(),
                               "child_wrapper": CHILD_WRAPPER,
                               "harness_view": denial_selfcheck(denied)})
    u = str(uuid.uuid4())
    p = spawn(["-p", LONG_PROMPT, "--output-format", "stream-json", "--verbose",
               "--session-id", u], a.cwd)
    t0 = time.time()
    events = []
    first_event_t = None
    reader = LineReader(p.stdout)
    while time.time() - t0 < a.kill_after and not reader.eof:
        for ln in reader.drain():
            if first_event_t is None:
                first_event_t = round(time.time() - t0, 3)
            try:
                j = json.loads(ln)
                events.append({"type": j.get("type"), "subtype": j.get("subtype"),
                               "session_id": j.get("session_id")})
            except Exception:
                events.append({"type": "UNPARSEABLE"})
        time.sleep(0.05)

    # Who gets the signal, and what has to be watched afterwards. Both are
    # recorded so the restricted and unrestricted runs can be compared on the
    # same terms: the signal goes to the CLI itself even when a wrapper process
    # was spawned in front of it, and the descendants are enumerated BEFORE the
    # signal, because once the parent is reaped they are reparented away and
    # can no longer be found from its pid.
    target = cli_pid(p)
    watched = proc_snapshot(p.pid) + ([{"pid": target, "ppid": ppid_of(target),
                                        "cmdline": (cmdline(target) or "")[:160]}]
                                      if target != p.pid else [])
    os.kill(target, signal.SIGTERM)
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    tail = "".join(x + "\n" for x in reader.drain()) + reader.rest()
    err = p.stderr.read().decode("utf-8", "replace")
    time.sleep(2.0)
    survivors = [d for d in watched if alive(d["pid"])]
    for d in survivors:                      # leave nothing behind either way
        try:
            os.kill(d["pid"], signal.SIGKILL)
        except Exception:
            pass
    rec("scenario", {
        "label": a.label,
        "requested_session_id": u,
        "spawned_pid": p.pid,
        "signalled_pid": target,
        "signalled_the_wrapper": target == p.pid and bool(CHILD_WRAPPER),
        "n_events_before_signal": len(events),
        "event_types": [e["type"] for e in events],
        "init_session_id": next((e["session_id"] for e in events
                                 if e["type"] == "system"), None),
        "first_event_s": first_event_t,
        "rc": p.returncode,
        "reaped": p.returncode is not None,
        "watched_before_signal": watched,
        "survivors_2s_after_reap": survivors,
        "stderr": err.strip(),
        "tail_len": len(tail),
        "tail_first_line": tail.splitlines()[0][:300] if tail.strip() else "",
    })


def cmd_observe(a):
    """U34 bounding probe. READS INTERNAL PATHS ON PURPOSE.

    Not part of the supervisor harness: this exists only to say what bounds the
    admission window, which cannot be answered from the public surface.
    """
    cfg = a.config_dir
    u = str(uuid.uuid4())
    p = spawn(["-p", LONG_PROMPT, "--output-format", "stream-json", "--verbose",
               "--session-id", u], a.cwd)
    t0 = time.time()
    seen = {}                 # internal path carrying the uuid -> first-seen offset
    first_stream_event = None
    first_init_event = None
    first_assistant = None
    claims = []
    reader = LineReader(p.stdout)
    next_claim = t0 + a.claim_every if a.claim_every > 0 else float("inf")
    while time.time() - t0 < a.observe_seconds:
        for root, _dirs, files in os.walk(cfg):
            for fn in files:
                if u in fn:
                    fp = os.path.join(root, fn)
                    if fp not in seen:
                        seen[fp] = {"first_seen_s": round(time.time() - t0, 3),
                                    "size_at_first_seen": os.path.getsize(fp)}
        for ln in reader.drain():
            if first_stream_event is None:
                first_stream_event = round(time.time() - t0, 3)
            try:
                j = json.loads(ln)
            except Exception:
                j = {}
            if (j.get("type") == "system" and j.get("subtype") == "init"
                    and first_init_event is None):
                first_init_event = round(time.time() - t0, 3)
            if j.get("type") == "assistant" and first_assistant is None:
                first_assistant = round(time.time() - t0, 3)
        if a.claim_every > 0 and time.time() >= next_claim:
            # Launched asynchronously and collected at the end. A synchronous
            # claimant would stop the scan for several seconds -- inside the
            # very interval being measured -- and an admitted one would itself
            # create a file carrying this uuid, so the landmark times could no
            # longer be attributed to the holder (codex review P1).
            claims.append({"issued_at_s": round(time.time() - t0, 3),
                           "proc": spawn(["-p", SHORT_PROMPT, "--output-format",
                                          "json", "--session-id", u], a.cwd)})
            next_claim = time.time() + a.claim_every
        time.sleep(0.02)
    comm(p, timeout=300)
    collected = []
    for c in claims:
        cp = c.pop("proc")
        _out, cerr = comm(cp, timeout=180)
        c["rc"] = cp.returncode
        c["stderr"] = cerr.strip()[:200]
        c["verdict"] = "admitted" if cp.returncode == 0 else "refused"
        collected.append(c)
    rec("observe_u34", {
        "uuid": u,
        "claims_were_issued": bool(collected),
        "config_dir_paths_carrying_the_uuid": seen,
        "first_stream_event_s": first_stream_event,
        "first_init_event_s": first_init_event,
        "first_assistant_event_s": first_assistant,
        "claims": collected,
    })


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="I-01 supervisor probe (issue #6). Public CLI only.")
    ap.add_argument("subcommand", choices=[
        "env", "spawn", "streams", "exits", "signals", "orphan", "window", "u36",
        "concurrency", "readback", "scenario", "observe"])
    ap.add_argument("--cwd", default=os.getcwd(),
                    help="working directory for every child (use a non-git scratch)")
    ap.add_argument("--kill-after", type=float, default=6.0,
                    help="seconds to let a child run before signalling it")
    ap.add_argument("--n", type=int, default=8, help="concurrent workers")
    ap.add_argument("--load-seconds", type=float, default=20.0)
    ap.add_argument("--staggers", type=float, nargs="+",
                    default=[0.5, 1.0, 2.0, 2.5, 3.0, 4.0, 5.0])
    ap.add_argument("--label", default="unrestricted")
    ap.add_argument("--config-dir", default=os.path.expanduser("~/.claude"),
                    help="observe only: CLI per-user config directory")
    ap.add_argument("--observe-seconds", type=float, default=40.0)
    ap.add_argument("--claim-every", type=float, default=0.0,
                    help="observe only: seconds between second-claimant probes. "
                         "0 (the default) issues none at all, which is what the "
                         "landmark measurement needs: an admitted claimant can "
                         "itself create the first file carrying the uuid.")
    ap.add_argument("--deny-paths", nargs="*", default=[],
                    help="scenario only: internal paths to self-check for denial")
    a = ap.parse_args()
    globals()["cmd_" + a.subcommand](a)


if __name__ == "__main__":
    main()
