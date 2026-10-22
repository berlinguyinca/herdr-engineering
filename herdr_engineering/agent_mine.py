#!/usr/bin/env python3
"""
agent_mine.py — cross-agent tool-usage miner for claude / pi / codex / opencode.

Reads each agent's session transcripts (different formats), normalizes every
tool call -> (tool, command-signature, success) triple, and produces:
  * a redacted TSV (agent, session, ts, tool, family, ok, sig) for pandas/agg
  * a report: per-agent stats, tool mix, failure hotspots, and the shell
    signatures that RECUR ACROSS AGENTS (the "build a better tool" candidates).

Why: to design better tool calls / tools from observed agent behavior.
Default output is REDACTED: signatures carry only the program (and subcommand for known CLIs),
never flags or arguments. --full adds truncated command bodies (sensitive; files are mode 0600).
"""
import json, os, glob, time, argparse, collections, sys, re, socket

HOME = os.path.expanduser("~")
STORES = {
    "claude":   os.path.join(HOME, ".claude/projects"),
    "pi":       os.path.join(HOME, ".pi/agent/sessions"),
    "codex":    os.path.join(HOME, ".codex/sessions"),
    "opencode": os.path.join(HOME, ".local/share/opencode/storage"),
}

# Coarse tool "family" so we can compare across agents despite different names.
FAMILY = {
    "bash": "shell", "shell": "shell", "local_shell": "shell", "exec": "shell",
    "terminal": "shell", "run": "shell", "run_command": "shell", "exec_command": "shell",
    "read": "file_read", "read_file": "file_read", "view": "file_read", "cat": "file_read",
    "grep": "search", "glob": "search", "search": "search", "rg": "search",
    "toolsearch": "search", "listdir": "search", "ls": "search",
    "edit": "file_write", "write": "file_write", "apply_patch": "file_write",
    "patch": "file_write", "edit_file": "file_write", "multiedit": "file_write",
    "agent": "agent", "mission": "agent", "task": "agent", "agenttool": "agent",
    "subagent": "agent",
    "webfetch": "web", "websearch": "web", "web_search": "web", "browser": "web",
    "sendmessage": "coordination", "monitor": "coordination", "taskstop": "coordination",
    "schedulewakeup": "coordination",
}
def family(name):
    if not name: return "other"
    return FAMILY.get(name.lower(), "other")

def shell_cmd(tool, args):
    """Pull a shell command string out of a tool's args, if it is a shell tool."""
    if family(tool) != "shell" or not isinstance(args, dict):
        return None
    for k in ("command", "cmd", "script", "code"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return " ".join(v)
    return None

# Programs whose first plain-word arguments are subcommands, not data. For every
# other program only the program name is kept: flags and arguments are where
# tokens, passwords, hostnames and paths live.
_SUBCOMMAND_PROGRAMS = frozenset((
    "git", "gh", "npm", "npx", "pnpm", "yarn", "cargo", "go", "docker", "podman", "kubectl",
    "systemctl", "journalctl", "pip", "pip3", "uv", "make", "apptainer", "brew", "apt", "apt-get",
    "sbatch", "squeue", "scontrol", "sacct", "sacctmgr", "ansible-playbook", "herdr-eng",
))
_WRAPPERS = frozenset(("sudo", "time", "env", "nohup", "nice", "exec", "command"))
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROGRAM = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")


def sig_of(cmd):
    """A comparable, redacted signature: the program, plus its subcommand for known CLIs.

    Never contains flags, assignments, paths, hosts or other arguments, so it cannot
    carry a secret that was typed into the command line.
    """
    if not cmd: return None
    lines = cmd.strip().splitlines()
    toks = lines[0].split() if lines else []
    while toks and (_ASSIGNMENT.match(toks[0]) or toks[0] in _WRAPPERS):
        toks.pop(0)
    if not toks: return None
    prog = os.path.basename(toks[0].strip("'\"")) or "?"
    if not _PROGRAM.match(prog): return "?"
    sig = [prog]
    if prog in _SUBCOMMAND_PROGRAMS:
        for t in toks[1:3]:
            if not _SUBCOMMAND.match(t): break
            sig.append(t)
    return " ".join(sig)

ERR_HINTS = ("command not found", "no such file", "permission denied", "traceback (most recent",
             "exit status 1", "exit code 1", "error:", "fatal:", "panic:", "segmentation fault",
             "not found", "syntaxerror", "commanderror", "process exited with")
def looks_failed(text):
    if not text: return None
    t = str(text).lower()[:4000]
    return any(h in t for h in ERR_HINTS)

def result_text(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if isinstance(content, dict): return json.dumps(content)[:2000]
    return ""

def codex_custom_cmd(inp):
    """Extract a shell command from a codex custom_tool_call input string."""
    if isinstance(inp, dict):
        return shell_cmd("exec", inp)
    if not isinstance(inp, str):
        return None
    m = re.search(r'(?:cmd|command)\s*:\s*"(.*?)"', inp)
    if m: return m.group(1)
    m = re.search(r"(?:cmd|command)\s*:\s*\[([^\]]*)\]", inp)
    if m: return m.group(1)
    return inp.strip() or None

def codex_out_text(out):
    if isinstance(out, str): return out
    if isinstance(out, list):
        return " ".join(b.get("text", "") for b in out if isinstance(b, dict))
    if isinstance(out, dict): return json.dumps(out)[:2000]
    return ""

# ---- per-agent extractors: yield normalized events -----------------------------
# event = dict(tool, sig, fam, ok(bool|None), dur_ms(int|None), cmd(str|None), ts(str))

def ev(agent, tool, cmd, ok, dur=None, ts=None):
    return {"agent": agent, "tool": tool if isinstance(tool, str) else None, "sig": sig_of(cmd),
            "fam": family(tool if isinstance(tool, str) else None), "ok": ok,
            "dur": dur if isinstance(dur, (int, float)) else None, "cmd": cmd,
            "ts": ts if isinstance(ts, str) else None}

def _lines(path):
    """Yield a transcript's lines; an unreadable file yields nothing instead of aborting the run."""
    try:
        with open(path, errors="replace") as fh:
            yield from fh
    except OSError:
        return


def mine_claude(path):
    pending = {}
    for line in _lines(path):
        if '"tool_use"' not in line and '"tool_result"' not in line:
            continue
        try: d = json.loads(line)
        except Exception: continue
        if not isinstance(d, dict): continue
        m = d.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list): continue
        ts = d.get("timestamp")
        for b in c:
            if not isinstance(b, dict): continue
            if b.get("type") == "tool_use":
                cmd = shell_cmd(b.get("name"), b.get("input"))
                pending[b.get("id")] = (b.get("name"), cmd)
            elif b.get("type") == "tool_result":
                name, cmd = pending.pop(b.get("tool_use_id"), ("?", None))
                yield ev("claude", name, cmd, (not b.get("is_error")), ts=ts)

def mine_pi(path):
    calls = {}
    for line in _lines(path):
        if '"toolCall"' not in line and '"toolResult"' not in line:
            continue
        try: d = json.loads(line)
        except Exception: continue
        if not isinstance(d, dict): continue
        m = d.get("message")
        if not isinstance(m, dict): continue
        ts = d.get("timestamp") or m.get("timestamp")
        if m.get("role") == "assistant":
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "toolCall":
                        calls[b.get("id")] = (b.get("name"), shell_cmd(b.get("name"), b.get("arguments")))
        elif m.get("role") == "toolResult":
            tid = m.get("toolCallId")
            name = m.get("toolName"); cmd = None
            if tid in calls:
                name, cmd = calls.pop(tid)
            yield ev("pi", name, cmd, (not m.get("isError")), dur=m.get("durationMs"), ts=ts)

def mine_codex(path):
    fifo = collections.deque(); byid = {}
    for line in _lines(path):
        if "_call" not in line:          # matches function_call(_output) + custom_tool_call(_output)
            continue
        try: d = json.loads(line)
        except Exception: continue
        if not isinstance(d, dict): continue
        p = d.get("payload", d)
        if not isinstance(p, dict): continue
        t = p.get("type"); ts = d.get("timestamp")
        if t == "function_call":
            args = p.get("arguments")
            if isinstance(args, str):
                try: args = json.loads(args)
                except Exception: args = {}
            cmd = shell_cmd(p.get("name"), args) if isinstance(args, dict) else (args if isinstance(args, str) else None)
            fifo.append((p.get("name"), cmd))
        elif t == "custom_tool_call":
            byid[p.get("call_id")] = (p.get("name"), codex_custom_cmd(p.get("input")))
        elif t == "function_call_output":
            name, cmd = fifo.popleft() if fifo else ("?", None)
            yield ev("codex", name, cmd, (not looks_failed(codex_out_text(p.get("output", "")))), ts=ts)
        elif t == "custom_tool_call_output":
            name, cmd = byid.pop(p.get("call_id"), ("?", None))
            yield ev("codex", name, cmd, (not looks_failed(codex_out_text(p.get("output", "")))), ts=ts)

EXTRACTORS = {"claude": mine_claude, "pi": mine_pi, "codex": mine_codex}

def discover(agent, days):
    root = STORES.get(agent)
    if not root or not os.path.isdir(root):
        return [], False
    if agent == "opencode":
        files = glob.glob(os.path.join(root, "**", "*.json"), recursive=True)
        files = [f for f in files if "node_modules" not in f]
        # opencode has no message transcript store in this layout
        return files, True
    cutoff = time.time() - days * 86400 if days else 0
    out = []
    for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        if days:
            try:
                if os.path.getmtime(f) < cutoff: continue
            except OSError: continue
        out.append(f)
    return out, True

# --- HPC-ssh adoption (mirrors ssh_probe.py — keep the regexes in sync) ---------
HPC_SSH_RE = re.compile(r"hpc-ssh\.sh\s+(?:hive\b|farm\b|franklin\b|fry\.|-)")


def _is_hpc_hand(cmd):
    if not cmd or not re.search(r"\bssh\b", cmd):
        return False
    return bool(re.search(r"\b(hive|farm|franklin|fry\.|fry@)\b", cmd))


def _since_hit(ts, since):
    return bool(ts) and (ts[:10] >= since if "T" not in since else ts >= since)


def _default_since_install():
    try:
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssh-install-date")
        if os.path.isfile(f):
            v = open(f).read().strip()
            if v:
                return v
    except Exception:
        pass
    return "2026-10-21"


def build_summary(agents, events, stats, days, since_install):
    per = collections.defaultdict(list)
    for e in events:
        per[e["agent"]].append(e)
    per_agent = {}
    for a in agents:
        evs = per.get(a, [])
        tools = collections.Counter(e["tool"] for e in evs)
        sigs = collections.Counter(e["sig"] for e in evs if e["sig"])
        fails = collections.Counter(e["sig"] for e in evs if e["ok"] is False and e["sig"])
        per_agent[a] = {
            "events": len(evs),
            "files": stats.get(a, {}).get("files", 0),
            "top_tools": tools.most_common(12),
            "top_signatures": sigs.most_common(8),
            "top_failures": fails.most_common(5),
        }
    hand = hpc = hand_s = hpc_s = 0
    for e in events:
        cmd = e.get("cmd")
        if not cmd:
            continue
        sh = _since_hit(e.get("ts"), since_install)
        if HPC_SSH_RE.search(cmd):
            hpc += 1
            hpc_s += 1 if sh else 0
        elif _is_hpc_hand(cmd):
            hand += 1
            hand_s += 1 if sh else 0

    def pct(n, d):
        return round(100.0 * n / (n + d), 1) if (n + d) else 0.0

    tss = sorted(e["ts"] for e in events if e.get("ts"))
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": socket.gethostname(),
        "agents_requested": agents,
        "agents_with_data": [a for a in agents if per_agent[a]["events"]],
        "window_days": days,
        "window_from": tss[0] if tss else None,
        "window_to": tss[-1] if tss else None,
        "totals": {"events": len(events),
                   "agents_with_data": sum(1 for a in per_agent.values() if a["events"])},
        "per_agent": per_agent,
        "hpc_ssh": {
            "hand_ssh_hpc": hand,
            "hpc_ssh": hpc,
            "adoption_pct": pct(hpc, hand),
            "since_install_date": since_install,
            "hand_ssh_hpc_since": hand_s,
            "hpc_ssh_since": hpc_s,
            "adoption_since_pct": pct(hpc_s, hand_s),
        },
    }


def fleet_rollup(summaries):
    """Aggregate a list of per-site summary dicts into a fleet-wide roll-up.

    `summaries` is a list of the objects `build_summary` returns (one per site).
    Entries that are not valid summaries (e.g. an {"error": ...} stub) are skipped.
    """
    hand = hpc = hand_s = hpc_s = events = 0
    sites = 0
    per_agent = collections.Counter()
    for s in summaries:
        if not isinstance(s, dict) or "hpc_ssh" not in s:
            continue
        sites += 1
        h = s.get("hpc_ssh") or {}
        hand += int(h.get("hand_ssh_hpc", 0) or 0)
        hpc += int(h.get("hpc_ssh", 0) or 0)
        hand_s += int(h.get("hand_ssh_hpc_since", 0) or 0)
        hpc_s += int(h.get("hpc_ssh_since", 0) or 0)
        events += int((s.get("totals") or {}).get("events", 0) or 0)
        for a, info in (s.get("per_agent") or {}).items():
            if isinstance(info, dict):
                per_agent[a] += int(info.get("events", 0) or 0)

    def pct(n, d):
        return round(100.0 * n / (n + d), 1) if (n + d) else 0.0

    return {
        "sites": sites,
        "events": events,
        "hand_ssh_hpc": hand,
        "hpc_ssh": hpc,
        "adoption_pct": pct(hpc, hand),
        "hand_ssh_hpc_since": hand_s,
        "hpc_ssh_since": hpc_s,
        "adoption_since_pct": pct(hpc_s, hand_s),
        "per_agent": dict(per_agent),
    }


def run(agents, days, full, outdir, limit_files=None, json_summary=False, since_install=None, summary_out=None):
    os.makedirs(outdir, exist_ok=True)
    events = []
    stats = {}
    for agent in agents:
        files, ok = discover(agent, days)
        if not ok:
            stats[agent] = {"found": False, "files": 0, "events": 0, "note": "store not found"}
            continue
        if agent == "opencode":
            stats[agent] = {"found": True, "files": len(files), "events": 0,
                            "note": "no session transcript store at this path (tool-output/logs only)"}
            continue
        fn = EXTRACTORS[agent]
        use = files if limit_files is None else files[:limit_files]
        n = 0
        for f in use:
            sess = os.path.basename(f)
            try:
                for e in fn(f):
                    e["session"] = sess
                    events.append(e)
            except Exception as exc:  # one odd transcript must not discard the host's other data
                print(f"  [{agent}] skipped {sess}: {type(exc).__name__}", file=sys.stderr)
            n += 1
        stats[agent] = {"found": True, "files": len(files), "scanned": n, "events": sum(1 for e in events if e["agent"] == agent)}
        print(f"  [{agent}] {len(files)} files in window, scanned {n} -> {stats[agent]['events']} tool events", file=sys.stderr)

    if json_summary:
        summary = build_summary(agents, events, stats, days, since_install)
        js = json.dumps(summary, indent=2)
        if summary_out:
            d = os.path.dirname(os.path.abspath(summary_out))
            os.makedirs(d, exist_ok=True)
            with os.fdopen(os.open(summary_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
                fh.write(js + "\n")
            print(f"wrote JSON summary -> {summary_out}", file=sys.stderr)
        else:
            print(js)
        return events, stats

    # write redacted TSV
    tsv = os.path.join(outdir, "events.tsv")
    cols = ["agent", "session", "ts", "tool", "fam", "ok", "sig"] + (["cmd"] if full else [])
    with os.fdopen(os.open(tsv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write("\t".join(cols) + "\n")
        for e in events:
            row = [e["agent"], e.get("session", ""), e.get("ts") or "", e["tool"] or "", e["fam"],
                   ("" if e["ok"] is None else ("1" if e["ok"] else "0")), e["sig"] or ""]
            if full:
                c = (e["cmd"] or "").replace("\n", " ").replace("\t", " ")[:240]
                row.append(c)
            fh.write("\t".join(str(x) for x in row) + "\n")

    report(agents, events, stats, tsv, days)
    return events, stats

def report(agents, events, stats, tsv, days):
    line = "=" * 72
    print(line)
    print(f"AGENT TOOL-USAGE MINER   (window: {'all' if days==0 else str(days)+' days'})")
    print(line)
    # overview
    print(f"\n{'agent':10} {'files':>7} {'events':>8} {'success%':>9} {'median_ms':>10}")
    per = collections.defaultdict(list)
    for e in events: per[e["agent"]].append(e)
    for a in agents:
        evs = per.get(a, [])
        ok = [e["ok"] for e in evs if e["ok"] is not None]
        rate = (100.0 * sum(1 for x in ok if x) / len(ok)) if ok else float("nan")
        durs = sorted(e["dur"] for e in evs if isinstance(e.get("dur"), (int, float)))
        med = durs[len(durs)//2] if durs else None
        s = stats.get(a, {})
        files = s.get("files", 0)
        print(f"{a:10} {files:>7} {len(evs):>8} {(f'{rate:.1f}' if ok else '  n/a'):>9} {(f'{med:.0f}' if med is not None else '  -'):>10}")

    # tool mix per agent
    for a in agents:
        evs = per.get(a, [])
        if not evs: continue
        c = collections.Counter(e["tool"] for e in evs)
        top = ", ".join(f"{k}={v}" for k, v in c.most_common(8))
        print(f"\n{a} tool mix: {top}")

    # failure hotspots (only events with a known outcome)
    fail = collections.Counter()
    tot = collections.Counter()
    for e in events:
        if e["ok"] is None: continue
        key = (e["agent"], e["tool"], e["sig"] or "-")
        tot[key] += 1
        if not e["ok"]: fail[key] += 1
    print(f"\n{line}\nTOP FAILURE HOTSPOTS  (agent / tool / command-signature -> failed/total)")
    print(line)
    for (a, tool, sig), nfail in fail.most_common(18):
        t = tot[(a, tool, sig)]
        print(f"  {nfail:>5}/{t:<5} {100.0*nfail/t:5.1f}%  [{a}] {tool}  `{sig}`")

    # cross-agent recurring shell signatures -> "build a better tool" candidates
    fam_sig = collections.defaultdict(collections.Counter)
    for e in events:
        if e["fam"] == "shell" and e["sig"]:
            fam_sig[e["sig"]][e["agent"]] += 1
    print(f"\n{line}\nSHELL SIGNATURES RECURRING ACROSS >=2 AGENTS  (best tool candidates)")
    print(line)
    rows = [(sig, dict(agents_)) for sig, agents_ in fam_sig.items() if len(agents_) >= 2]
    rows.sort(key=lambda r: -sum(r[1].values()))
    for sig, cnt in rows[:18]:
        detail = ", ".join(f"{a}:{n}" for a, n in sorted(cnt.items()))
        print(f"  {sum(cnt.values()):>6}  `{sig}`   ({detail})")
    print(f"\n(redacted events -> {tsv})")

def _build_arg_parser():
    ap = argparse.ArgumentParser(description="mine this host's agent transcripts (redacted by default)")
    ap.add_argument("--agents", default="claude,pi,codex,opencode")
    ap.add_argument("--days", type=int, default=45, help="0 = all time (slower; codex is 18GB)")
    ap.add_argument("--full", action="store_true", help="include truncated command bodies (may be sensitive)")
    ap.add_argument("--out", default=os.path.join(HOME, ".cache", "agent-mine"))
    ap.add_argument("--limit", type=int, default=None, help="max files per agent (debug)")
    ap.add_argument("--json-summary", action="store_true",
                    help="emit a compact JSON summary (for the herdr dashboard); skips the text report and TSV")
    ap.add_argument("--summary-out", default=None,
                    help="with --json-summary, write the JSON to this file instead of stdout")
    ap.add_argument("--since", default=None,
                    help="since-install date (YYYY-MM-DD or ISO ts) for the adoption-since metric")
    return ap


def agent_mine_main(args) -> int:
    """Entry point for `herdr-eng agent-mine` (args is an argparse.Namespace)."""
    agents = [x.strip() for x in args.agents.split(",") if x.strip()]
    run(agents, args.days, args.full, args.out, args.limit,
        json_summary=args.json_summary,
        since_install=(args.since or _default_since_install()),
        summary_out=args.summary_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(agent_mine_main(_build_arg_parser().parse_args()))
