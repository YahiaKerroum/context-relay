"""Hook-side smarts shared by the Claude and Gemini hooks.

events      an append-only log of what happened (feeds the away card, the dashboard and /missed)
flags       pause / stop an agent from the phone (the PreToolUse hook honours them)
inbox       ideas and messages queued from the phone or the editor, delivered between steps and tasks
rules       pinned standing rules, and protected paths derived from them
stuck       repeated errors and edit loops are nudged, then escalated to a fresh session
checks      "done means green": tests and lint after a turn, failures go back to the agent
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import relay
H = relay.HOME
FLAGS = H / "flags"
INBOX = H / "inbox.json"
RULES = H / "rules.json"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# ---------------------------------------------------------------- small stores

def update_json(path, fn, default):
    """Read-modify-write under a lock file, so hooks and the daemon never clobber each other."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    end = time.time() + 5
    held = False
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 10:
                    lock.unlink()
            except OSError:
                pass
            if time.time() > end:
                break  # proceed unlocked rather than hang a hook
            time.sleep(0.05)
        except OSError:
            break
    try:
        data = relay.read_json(path)
        if data is None:
            data = json.loads(json.dumps(default))
        new = fn(data)
        data = data if new is None else new
        relay.write_json(path, data)
        return data
    finally:
        if held:
            lock.unlink(missing_ok=True)


def event(kind, text, **kw):
    try:
        H.mkdir(parents=True, exist_ok=True)
        with open(H / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, "text": text, **kw}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def events_since(ts, kinds=None):
    out = []
    try:
        for line in relay.tail_lines(H / "events.jsonl", 2_000_000):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ts", 0) >= ts and (not kinds or e.get("kind") in kinds):
                out.append(e)
    except OSError:
        pass
    return out


def trim_events(keep=4000):
    p = H / "events.jsonl"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > keep * 1.5:
            p.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- flags: pause, stop

def _flag(name, sid=None):
    return FLAGS / (name if not sid else f"{name}-{sid[:8]}")


def set_flag(name, sid=None, on=True):
    p = _flag(name, sid)
    if on:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(time.time()))
    else:
        p.unlink(missing_ok=True)


def has_flag(name, sid=None):
    return _flag(name).exists() or bool(sid and _flag(name, sid).exists())


def clear_flags(name):
    for p in FLAGS.glob(name + "*"):
        p.unlink(missing_ok=True)


STOP_MSG = ("The user pressed STOP on their phone. Do not run this tool. End your turn now with one line saying "
            "where you stopped, and wait for new instructions.")
NETWORK_CMD = re.compile(r"\b(pip3?|uv|npm|pnpm|yarn|bun|git\s+(push|pull|fetch|clone)|curl|wget|kaggle|huggingface-cli"
                         r"|hf\s|ollama\s+pull|docker\s+pull|gh\s|winget|choco|scoop)\b", re.I)
NETWORK_TOOLS = {"WebFetch", "WebSearch", "search_web", "read_url_content"}


def offline():
    o = relay.read_json(H / "offline.json", {}) or {}
    return bool(o.get("offline")) and time.time() - o.get("ts", 0) < 120


def hold(sid, tool, cmd, cfg):
    """Wait while the agent is paused or (for network tools) the connection is down.
    Returns a deny reason when the user pressed STOP, else None."""
    if has_flag("stop", sid):
        return STOP_MSG
    needs_net = tool in NETWORK_TOOLS or bool(cmd and NETWORK_CMD.search(cmd))
    end = time.time() + 840  # the hook timeout is 900 s
    noted = False
    while time.time() < end:
        if has_flag("stop", sid):
            return STOP_MSG
        paused = has_flag("pause", sid)
        down = needs_net and offline() and cfg.get("recovery", {}).get("enabled", True)
        if not paused and not down:
            return None
        if not noted:
            noted = True
            event("hold", "paused by you" if paused else "waiting for the connection", sid=sid, tool=tool)
        time.sleep(1)
    return None


# ---------------------------------------------------------------- pinned rules and protected paths

PROTECT_RE = re.compile(
    r"(?:never|don'?t|do not|must not|should not|shouldn'?t|not allowed to|no)\s+(?:to\s+)?"
    r"(?:touch|edit|modify|change|alter|delete|remove|overwrite|rewrite|write(?:\s+to)?|commit|rename|move)\s+"
    r"(?:anything\s+(?:in|inside|under)\s+|files?\s+(?:in|inside|under)\s+|the\s+|my\s+|any\s+)?"
    r"([\w./\\*-]+)", re.I)
MUTATE_RE = re.compile(
    r"(^|[\s;&|(])(rm|mv|cp|del|erase|rd|rmdir|sed\s+-i|tee|touch|mkdir|truncate|chmod|dd|"
    r"git\s+(checkout|restore|rm|mv|clean|reset|stash)|Remove-Item|Move-Item|Copy-Item|Set-Content|Out-File|"
    r"Add-Content|New-Item|Rename-Item|unlink|shutil\.(rmtree|move|copy))\b|>>?\s*\S", re.I)
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "write_to_file", "replace_file_content",
              "multi_replace_file_content", "edit_file", "write_file"}
BASH_TOOLS = {"Bash", "PowerShell", "run_command"}


def load_rules():
    r = relay.read_json(RULES, None) or {}
    r.setdefault("next", 1)
    r.setdefault("global", [])
    r.setdefault("projects", {})
    return r


def extract_protect(text, root):
    out = []
    for m in PROTECT_RE.finditer(text):
        tok = m.group(1).strip(".,;:!?)").replace("\\", "/")
        if not tok or tok.lower() in ("it", "that", "this", "them", "any", "all", "files", "file", "folder", "code"):
            continue
        looks_like_path = "/" in tok or "." in tok or "*" in tok
        if looks_like_path or (root and (Path(root) / tok).exists()):
            out.append(tok.lstrip("./"))
    return list(dict.fromkeys(out))


def add_rule(text, root=None, scope="project"):
    protect = extract_protect(text, root)
    box = {}

    def fn(d):
        rule = {"id": d["next"], "text": text.strip(), "ts": time.time(), "protect": protect,
                "scope": scope, "root": str(root) if root and scope == "project" else ""}
        d["next"] += 1
        if scope == "global" or not root:
            d["global"].append(rule)
        else:
            d["projects"].setdefault(relay.norm(root), []).append(rule)
        box["rule"] = rule
    update_json(RULES, fn, {"next": 1, "global": [], "projects": {}})
    return box["rule"]


def remove_rule(rid):
    hit = []

    def fn(d):
        for lst in [d["global"], *d["projects"].values()]:
            for r in list(lst):
                if r["id"] == rid:
                    lst.remove(r)
                    hit.append(r)
    update_json(RULES, fn, {"next": 1, "global": [], "projects": {}})
    return hit[0] if hit else None


def rules_for(cwd):
    r = load_rules()
    c = relay.norm(cwd)
    items = list(r["global"])
    for key, lst in r["projects"].items():
        if c == key or c.startswith(key + "/"):
            items += lst
    return items


def rules_text(cwd):
    items = rules_for(cwd)
    if not items:
        return ""
    return ("Standing rules the user pinned (always follow them; if one blocks what you need, ask the user first):\n"
            + "\n".join(f"- {r['text']}" for r in items))


def _abs(cwd, p):
    return relay.norm(str((Path(cwd) / p).resolve() if not os.path.isabs(p) else Path(p).resolve()))


def protected_violation(cwd, tool, args):
    """The first pinned rule this tool call would break, or None."""
    rules = [r for r in rules_for(cwd) if r.get("protect")]
    if not rules:
        return None
    if tool in FILE_TOOLS:
        paths = [v for k, v in args.items() if isinstance(v, str) and
                 ("file" in k.lower() or "path" in k.lower() or k == "TargetFile")]
        targets = [_abs(cwd, p) for p in paths if p]
    elif tool in BASH_TOOLS:
        cmd = (args.get("command") or args.get("CommandLine") or "")
        norm_cmd = cmd.replace("\\", "/").lower()
        if not MUTATE_RE.search(cmd):
            return None
        for r in rules:
            for p in r["protect"]:
                core = p.rstrip("/").replace("*", "").lower()
                if core and re.search(r"(^|[\s/\"'=:])" + re.escape(core) + r"(/|\s|\"|'|$)", norm_cmd):
                    return r
        return None
    else:
        return None
    for r in rules:
        base = r.get("root") or cwd
        for p in r["protect"]:
            core = p.rstrip("/").replace("*", "")
            if not core:
                continue
            t = _abs(base, core)
            for a in targets:
                if a == t or a.startswith(t.rstrip("/") + "/") or (not r.get("root") and core.lower() in a):
                    return r
    return None


# ---------------------------------------------------------------- the PreToolUse decision

def before_tool(who, st, cfg, tool, args, cwd, sid):
    """Everything that can stop a tool call. Returns a deny reason, or None to allow."""
    from . import guard
    cmd = args.get("command") or args.get("CommandLine") or ""
    why = hold(sid, tool, cmd, cfg)
    if why:
        return why
    rule = protected_violation(cwd, tool, args)
    if rule:
        ok_until = st.setdefault("rule_ok", {})
        if ok_until.get(str(rule["id"]), 0) < time.time():
            ok, by = guard.ask("rule", f"edit that breaks your rule: {rule['text']}",
                               cmd or json.dumps(args, ensure_ascii=False)[:400], who, cwd)
            event("rule", f"rule #{rule['id']} {'overridden' if ok else 'held'}: {rule['text']}", sid=sid)
            if not ok:
                return (f"This would break the user's pinned rule #{rule['id']}: \"{rule['text']}\". "
                        f"They did not approve it ({by or 'no answer'}). Do not work around it; explain what you "
                        f"needed and ask the user.")
            ok_until[str(rule["id"])] = time.time() + 600
    if cmd:
        ok, why = guard.check_command(cmd, who, cwd)
        if not ok:
            return why
    return None


# ---------------------------------------------------------------- inbox: ideas and messages

def inbox_add(text, cwd, kind="idea", source="telegram", target=None):
    item = {"id": uuid.uuid4().hex[:6], "ts": time.time(), "kind": kind, "text": text.strip(),
            "cwd": str(cwd or ""), "target": target, "status": "queued", "source": source}
    update_json(INBOX, lambda d: d["items"].append(item), {"items": []})
    event("inbox", f"{kind} queued: {text.strip()[:80]}", id=item["id"])
    return item


def inbox_list(status=None):
    items = (relay.read_json(INBOX, {"items": []}) or {"items": []})["items"]
    return [i for i in items if not status or i["status"] == status]


def _same_project(item_cwd, cwd):
    if not item_cwd:
        return True
    a, b = relay.norm(item_cwd), relay.norm(cwd)
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


def inbox_take(cwd, kinds, sid, mark="delivered"):
    if not any(i["status"] == "queued" for i in inbox_list()):
        return []
    taken = []

    def fn(d):
        for it in d["items"]:
            if it["status"] != "queued" or it["kind"] not in kinds or not _same_project(it["cwd"], cwd):
                continue
            if it.get("target") and not str(sid).startswith(str(it["target"])[:8]):
                continue
            it.update(status=mark, delivered_to=sid, delivered_ts=time.time())
            taken.append(dict(it))
    update_json(INBOX, fn, {"items": []})
    for it in taken:
        event("inbox", f"{it['kind']} delivered: {it['text'][:80]}", id=it["id"], sid=sid)
    return taken


def inbox_set(item_id, **kw):
    def fn(d):
        for it in d["items"]:
            if it["id"] == item_id:
                it.update(kw)
    update_json(INBOX, fn, {"items": []})


def inbox_remove(item_id):
    def fn(d):
        d["items"] = [i for i in d["items"] if i["id"] != item_id]
    update_json(INBOX, fn, {"items": []})


def messages_text(items):
    body = "\n".join(f"- {i['text']}" for i in items)
    return (f"{relay.MARK} The user sent this from their phone while you were working. Take it into account now; "
            f"if it changes the task, adapt, otherwise carry on:\n{body}")


def ideas_text(items):
    body = "\n".join(f"{n}. {i['text']}" for n, i in enumerate(items, 1))
    return (f"{relay.MARK} Before you finish: the user dropped these ideas from their phone to be done next. Do them "
            f"now, one at a time, in order, then say in one line what you did for each:\n{body}")


# ---------------------------------------------------------------- stuck detector

def _sig(tool, err):
    e = re.sub(r"0x[0-9a-f]+|\b\d+\b", "#", (err or "").lower())
    return f"{tool}|{re.sub(r'[^a-z# ]+', ' ', e)[:140].split()[:18]}"


def _first_line(err):
    for line in (err or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return "an error"


def edited_path(tool, args):
    if tool not in FILE_TOOLS:
        return ""
    for k, v in args.items():
        if isinstance(v, str) and ("file" in k.lower() or "path" in k.lower() or k == "TargetFile"):
            return relay.norm(v)
    return ""


def stuck_check(st, cfg, tool, args, ok, err):
    sc = cfg["stuck"]
    if not sc["enabled"]:
        return None
    rec = st.setdefault("recent", [])
    path = edited_path(tool, args)
    # an error with no text is told apart by what was run, so unrelated failures never add up
    sig = None if ok else _sig(tool, err or json.dumps(args, sort_keys=True, default=str)[:200])
    rec.append({"t": tool, "f": path, "s": sig})
    del rec[:-24]
    seen = st.setdefault("stuck_seen", {})
    if sig:
        n = sum(1 for r in rec[-14:] if r["s"] == sig)
        lvl = seen.get(sig, 0)
        if n >= sc["escalate_after"] and lvl < 2:
            seen[sig] = 2
            return "escalate", f"the same error {n} times: {_first_line(err)}"
        if n >= sc["after_errors"] and lvl < 1:
            seen[sig] = 1
            return "nudge", f"the same error {n} times: {_first_line(err)}"
    if path:
        n = sum(1 for r in rec[-14:] if r["f"] == path)
        key = "file|" + path
        lvl = seen.get(key, 0)
        if n >= sc["loop_escalate"] and lvl < 2:
            seen[key] = 2
            return "escalate", f"editing {Path(path).name} {n} times without getting anywhere"
        if n >= sc["loop_edits"] and lvl < 1:
            seen[key] = 1
            return "nudge", f"editing {Path(path).name} {n} times"
    return None


def nudge_text(why):
    return (f"{relay.MARK} You're going in circles: {why}. Stop repeating the same approach. Re-read the error, "
            f"state a different hypothesis, and try something genuinely different. If you are blocked, say so and "
            f"ask the user (ask_user) instead of retrying.")


def escalate(who, st, cfg, why):
    """Hand this chat to a fresh session (optionally the other model) and tell the user once."""
    sc = cfg["stuck"]
    st["stuck_note"] = why
    other = "agy" if who == "claude" else "claude"
    if sc["escalate"] == "other_model":
        st["switch_to"] = other
    if sc["escalate"] != "notify":
        relay_force(who, st["sid"])
    name = st.get("title") or f"{'Claude' if who == 'claude' else 'Gemini'} chat"
    action = {"notify": "I'm leaving it running", "fresh": "handing it to a fresh session",
              "other_model": f"handing it to {'Gemini' if other == 'agy' else 'Claude'}"}.get(sc["escalate"],
                                                                                              "handing it to a fresh session")
    relay.notify("stuck", f"🧱 <b>{relay.html_escape(name)}</b> is stuck: {relay.html_escape(why)}.\n"
                          f"Nothing is broken; {action} with a note about what failed.",
                 sid=st["sid"], desktop=True)
    event("stuck", f"{name}: {why}", sid=st["sid"])


def relay_force(who, sid):
    f = relay.STATE / "force" / who / sid
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(str(time.time()))


# ---------------------------------------------------------------- after a tool ran

KAGGLE_PUSH = re.compile(r"kaggle\.com/code/([\w.-]+/[\w.-]+)", re.I)


def after_tool(who, st, cfg, tool, args, ok, err, response, cwd, sid):
    """Run after every tool call. Returns text to put in front of the model, or None."""
    parts = []
    hit = stuck_check(st, cfg, tool, args, ok, err)
    if hit:
        level, why = hit
        parts.append(nudge_text(why))
        if level == "escalate":
            escalate(who, st, cfg, why)
    cmd = args.get("command") or args.get("CommandLine") or ""
    if cmd and "kaggle" in cmd and re.search(r"kernels\s+push", cmd):
        m = KAGGLE_PUSH.search(response or "")
        if m:
            from . import watchers
            watchers.add_kaggle(m.group(1), cwd, auto=True)
    msgs = inbox_take(cwd, {"message"}, sid)
    if msgs:
        parts.append(messages_text(msgs))
    return "\n\n".join(parts) or None


# ---------------------------------------------------------------- done means green

CODE_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".cs",
            ".rb", ".php", ".vue", ".svelte")


def project_python(root):
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python", "venv/bin/python"):
        if (Path(root) / rel).exists():
            return str(Path(root) / rel)
    return sys.executable


def detect_checks(root, cfg):
    p = Path(root)
    over = relay.read_json(p / ".context-relay" / "checks.json", None)
    if over and over.get("commands"):
        return over["commands"]
    mapped = cfg["checks"].get("commands", {}).get(relay.norm(root))
    if mapped:
        return mapped
    cmds = []
    py = project_python(root)
    tests = p / "tests"
    if tests.is_dir() and any(tests.glob("test_*.py")):
        has_pytest = subprocess.run([py, "-c", "import pytest"], capture_output=True,
                                    creationflags=NO_WINDOW).returncode == 0
        cmds.append(f'"{py}" -m pytest -x -q -p no:cacheprovider' if has_pytest
                    else f'"{py}" -m unittest discover -s tests')
    pkg = relay.read_json(p / "package.json", None)
    if pkg and (pkg.get("scripts") or {}).get("test") and "no test specified" not in pkg["scripts"]["test"]:
        cmds.append("npm test --silent")
    pyproject = (p / "pyproject.toml").read_text(encoding="utf-8", errors="ignore") if (p / "pyproject.toml").exists() else ""
    if (p / "ruff.toml").exists() or "[tool.ruff" in pyproject:
        ruff = shutil.which("ruff")
        if ruff:
            cmds.append(f'"{ruff}" check .')
    return cmds


def turn_changes(cwd, sid):
    from . import guard
    try:
        for s in guard.snapshots(cwd, 60):
            if s.get("sid") == sid and not s["label"].startswith("before undo"):
                return guard.changes_since(cwd, s["sha"])
    except Exception as e:  # noqa: BLE001
        relay.log(f"turn_changes: {e!r}")
    return None


def _failing(out):
    names = set(re.findall(r"^FAILED\s+(\S+)", out, re.M))
    names |= {m.strip() for m in re.findall(r"^(?:FAIL|ERROR):\s+(.+)$", out, re.M)}
    return names


def _baseline():
    return relay.read_json(H / "checks_baseline.json", {}) or {}


def _accept(root, failing):
    """The failures present when a turn is accepted are the project's known state; later turns are judged
    against them, so old red tests do not block unrelated work."""
    b = _baseline()
    b[relay.norm(root)] = sorted(failing)
    relay.write_json(H / "checks_baseline.json", b)


def run_checks(cwd, sid, cfg):
    """None when there is nothing to check; else {ok, cmd, report, new, failing, root}."""
    from . import guard
    import hashlib
    root = guard.project_root(cwd)
    ch = cfg["checks"]
    if not root or not ch["enabled"]:
        return None
    changed = turn_changes(cwd, sid)
    if not changed or not any(f.lower().endswith(CODE_EXT) for _, f in changed):
        return None
    cmds = detect_checks(root, cfg)
    if not cmds:
        return None
    known = set(_baseline().get(relay.norm(root), []))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", CONTEXT_RELAY_DISABLE="1")
    failing, first = set(), None
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, cwd=str(root), shell=True, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=ch["timeout_s"], env=env, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            event("checks", f"timed out after {ch['timeout_s']}s: {cmd[:80]}")
            continue
        if r.returncode == 0:
            continue
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        names = _failing(out)
        tail = "\n".join(out.strip().splitlines()[-40:])[-3500:]
        sig = set() if names else {"sig:" + hashlib.md5(tail[-300:].encode("utf-8", "ignore")).hexdigest()[:10]}
        failing |= names | sig
        if first is None:
            first = (cmd, tail)
    new = failing - known
    if not new:
        if failing:
            event("checks", "same failures as before this turn; not blocking")
        _accept(root, failing)
        return {"ok": True, "root": str(root)}
    return {"ok": False, "cmd": first[0], "report": first[1], "new": sorted(new)[:12],
            "failing": sorted(failing), "root": str(root)}


def on_stop(who, st, cfg, cwd, sid):
    """Called when the agent is about to finish. Returns text that sends it back to work, or None."""
    ch = cfg["checks"]
    res = None
    if st.get("check_rounds", 0) < ch["max_rounds"]:
        res = run_checks(cwd, sid, cfg)
    elif st.get("check_last"):  # out of tries: accept the state, tell the user once
        last = st.pop("check_last")
        _accept(last["root"], last["failing"])
        relay.notify("checks", f"🧪 {relay.html_escape(st.get('title') or 'A chat')} finished with checks still failing "
                               f"after {ch['max_rounds']} tries ({len(last['failing'])} failing). Worth a look.",
                     sid=sid, desktop=True, actions=["tests"])
        event("checks", "still failing after the agent's tries", sid=sid)
    if res and not res["ok"]:
        st["check_rounds"] = st.get("check_rounds", 0) + 1
        st["check_last"] = {"root": res["root"], "failing": res["failing"]}
        event("checks", f"failed: {res['cmd'][:60]}", sid=sid)
        names = f" ({len(res['new'])} new failing: {', '.join(res['new'][:5])})" if res["new"] else ""
        return (f"{relay.MARK} Done isn't done yet: the project's checks fail after your changes{names}.\n"
                f"Command: {res['cmd']}\n```\n{res['report']}\n```\nFix what you broke before you finish. If a failure "
                f"was already there and is unrelated to your change, say so in one line instead of touching "
                f"unrelated code.")
    if res and res["ok"]:
        event("checks", "passed", sid=sid)
        st.pop("check_last", None)
        st["checks_green"] = time.time()
    items = inbox_take(cwd, {"message", "idea"}, sid)
    if items:
        msgs = [i for i in items if i["kind"] == "message"]
        ideas = [i for i in items if i["kind"] == "idea"]
        return "\n\n".join([messages_text(msgs)] * bool(msgs) + [ideas_text(ideas)] * bool(ideas))
    return None
