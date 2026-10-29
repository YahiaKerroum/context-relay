"""Night shift: queue tasks before bed, let the agents work through them unattended, get a morning report.

Each task runs as a chain of headless sessions (the same autopilot that hands off at 65% context), in order,
within limits you set: a time budget, a stop at Claude's usage limit (the remaining Claude tasks then go to
Gemini), sessions per task, and which tools may run without asking. Dangerous commands still go through the
danger gate, so a risky step waits for your OK on Telegram instead of happening while you sleep.
"""
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import relay
from . import smart
FILE = relay.HOME / "night.json"
LOGS = relay.HOME / "night"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_thread = None
_cancel = threading.Event()

WRAP = ("You are working unattended overnight; the user is asleep and cannot answer questions. Make steady, "
        "careful progress on the task below. If you are blocked or unsure, take the safest reasonable path and "
        "record the assumption in your final message. Keep changes small and the project working. Do not commit "
        "or push unless the task says so. When you are done, finish with a short summary of what you did and "
        "what is left.\n\nTASK\n")


def load():
    d = relay.read_json(FILE, None) or {}
    d.setdefault("tasks", [])
    d.setdefault("state", "idle")
    return d


def save(d):
    relay.write_json(FILE, d)


def update(fn):
    smart.update_json(FILE, fn, {"tasks": [], "state": "idle"})


def add(kind, prompt, cwd, spec=None):
    task = {"id": uuid.uuid4().hex[:5], "kind": kind, "prompt": prompt.strip(), "cwd": str(cwd), "spec": spec or {},
            "status": "queued"}
    update(lambda d: d["tasks"].append(task))
    return task


def parse_time(s):
    m = re.fullmatch(r"\s*(\d{1,2})[:h.](\d{2})\s*", s or "")
    if not m:
        return None
    now = datetime.now()
    t = now.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)), second=0, microsecond=0)
    return (t + timedelta(days=1) if t <= now else t).timestamp()


def start(at=None):
    def fn(d):
        for t in d["tasks"]:
            if t["status"] in ("done", "failed", "stopped", "skipped"):
                t["status"] = "queued"
        d.update(state="scheduled", start_at=at or time.time() + 5, report=None)
    update(fn)
    smart.event("night", "night shift scheduled" if at else "night shift starting")


def stop():
    _cancel.set()
    update(lambda d: d.update(state="idle", start_at=None))


def clear():
    stop()
    update(lambda d: d.update(tasks=[], state="idle", start_at=None))


def tick():
    """Called by the daemon every few seconds."""
    global _thread
    d = load()
    if d["state"] == "scheduled" and time.time() >= d.get("start_at", 0) and not (_thread and _thread.is_alive()):
        if not any(t["status"] == "queued" for t in d["tasks"]):
            update(lambda dd: dd.update(state="idle"))
            return
        _cancel.clear()
        _thread = threading.Thread(target=run, daemon=True)
        _thread.start()


def _kill(proc):
    try:
        relay.kill_tree(proc.pid)
    except OSError:
        pass


def _set_task(tid, **kw):
    def fn(d):
        for t in d["tasks"]:
            if t["id"] == tid:
                t.update(kw)
    update(fn)


def run_task(task, cfg, deadline):
    """Returns (status, note). Runs the task as an autopilot chain and watches the clock and the limits."""
    from . import limits
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{time.strftime('%m%d-%H%M')}-{task['id']}.log"
    cwd = task["cwd"]
    kind = task["kind"]
    if kind == "annotate":
        from . import delegate
        jid = delegate.new_dataset_job(cwd=cwd, **task["spec"])
        while delegate.status(jid).get("state") not in (*delegate.TERMINAL, "paused"):
            if _cancel.is_set():
                delegate.cancel(jid)
                return "stopped", "cancelled"
            if time.time() > deadline:
                delegate.cancel(jid)
                return "stopped", "out of time"
            time.sleep(5)
        st = delegate.status(jid)
        log.write_text(delegate.summary(jid), encoding="utf-8")
        return ("done" if st.get("state") == "done" else "failed"), (st.get("message") or st.get("state"))
    tool = "claude" if kind == "claude" else "agy"
    args = [*relay.cli_cmd("run", tool, WRAP + task["prompt"]), "--cwd", cwd,
            "--max", str(cfg["max_sessions_per_task"])]
    if tool == "claude" and cfg["allowed_tools"]:
        args += ["--", "--allowedTools", ",".join(cfg["allowed_tools"])]
    env = dict(os.environ, CONTEXT_RELAY_NIGHT="1", PYTHONIOENCODING="utf-8")
    with open(log, "wb") as f:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        why = None
        while proc.poll() is None:
            if _cancel.is_set():
                why = "stopped by you"
            elif time.time() > deadline:
                why = "out of time"
            elif tool == "claude" and limits.active():
                why = "Claude's usage limit"
            elif smart.offline():
                time.sleep(5)  # nothing to do without a connection, but don't burn the clock killing it
            if why:
                _kill(proc)
                return "stopped", why
            time.sleep(5)
    text = log.read_text(encoding="utf-8", errors="replace")
    sessions = len(re.findall(r"=== relay \w+: session \d+", text))
    return ("done" if proc.returncode == 0 else "failed"), f"{sessions} session(s)"


def run():
    from . import guard
    cfg = relay.load_config()["night"]
    deadline = time.time() + cfg["max_hours"] * 3600
    update(lambda d: d.update(state="running", started=time.time()))
    results = []
    fallback = False
    for t in [t for t in load()["tasks"] if t["status"] == "queued"]:
        if _cancel.is_set():
            break
        if time.time() > deadline:
            _set_task(t["id"], status="skipped", note="out of time")
            continue
        if fallback and t["kind"] == "claude" and cfg["fallback_to_gemini"]:
            t["kind"] = "agy"
            _set_task(t["id"], kind="agy", note="moved to Gemini")
        snap = None
        try:
            snap = guard.snapshot(t["cwd"], f"night: {t['prompt'][:60]}", "night", t["id"])
        except Exception as e:  # noqa: BLE001
            relay.log(f"night snapshot: {e!r}")
        _set_task(t["id"], status="running", started=time.time())
        try:
            status, note = run_task(t, cfg, deadline)
        except Exception as e:  # noqa: BLE001
            status, note = "failed", repr(e)[:150]
        changed = []
        try:
            changed = guard.changes_since(t["cwd"], snap) if snap else []
        except Exception:  # noqa: BLE001
            pass
        if status == "stopped" and "usage limit" in note:
            fallback = True
        _set_task(t["id"], status=status, note=note, finished=time.time(), files=len(changed), snap=snap)
        smart.event("night", f"task {status}: {t['prompt'][:60]}")
    report = build_report()
    update(lambda d: d.update(state="done", finished=time.time(), report=report))
    relay.notify("night", report, always=True, desktop=True)


def build_report():
    d = load()
    started, finished = d.get("started", time.time()), time.time()
    lines = [f"🌅 <b>Night shift report</b> · {(finished - started) / 3600:.1f} h"]
    icons = {"done": "✅", "failed": "❌", "stopped": "⏹", "skipped": "⏭", "queued": "⏳", "running": "🏃"}
    for t in d["tasks"]:
        dur = (t.get("finished", finished) - t.get("started", finished)) / 60 if t.get("started") else 0
        lines.append(f"{icons.get(t['status'], '•')} {relay.html_escape(t['prompt'][:70])} "
                     f"<i>({'Gemini' if t['kind'] == 'agy' else 'Claude' if t['kind'] == 'claude' else t['kind']}, "
                     f"{dur:.0f} min, {t.get('files', 0)} file(s) changed"
                     + (f", {relay.html_escape(str(t['note']))}" if t.get("note") else "") + ")</i>")
    waiting = [a for a in (relay.read_json(relay.HOME / "away_waiting.json", []) or [])]
    done = sum(1 for t in d["tasks"] if t["status"] == "done")
    lines.append(f"\n{done}/{len(d['tasks'])} tasks finished. Review with git diff; /undo rolls a turn back.")
    return "\n".join(lines)


def telegram(arg, cwd):
    parts = arg.split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    d = load()
    if sub == "add":
        if not cwd:
            return "I don't know which project yet; use the editor once, then add again."
        kind = "claude"
        if rest.lower().startswith("gemini "):
            kind, rest = "agy", rest[7:]
        if not rest.strip():
            return "Usage: /night add [gemini] <task>"
        t = add(kind, rest, cwd)
        return f"🌙 Queued #{len([x for x in load()['tasks']])} for {Path(cwd).name} ({'Gemini' if kind == 'agy' else 'Claude'}): {rest[:80]}\n/night start 23:00 when you're ready."
    if sub == "start":
        at = parse_time(rest) if rest else None
        if rest and not at:
            return "Time like 23:00, or just /night start to begin now."
        if not any(t["status"] in ("queued", "done", "failed", "stopped", "skipped") for t in d["tasks"]):
            return "The queue is empty. /night add <task> first."
        start(at)
        return ("🌙 Night shift starts at " + datetime.fromtimestamp(at).strftime("%H:%M") + ". Sleep well; I'll send the report."
                if at else "🌙 Night shift starting now.")
    if sub == "stop":
        stop()
        return "⏹ Night shift stopped."
    if sub == "clear":
        clear()
        return "Queue cleared."
    if sub == "report":
        return d.get("report") or "No report yet."
    if not d["tasks"]:
        return ("🌙 Night shift: the queue is empty.\n/night add <task> · /night add gemini <task> · "
                "/night start 23:00 · /night report")
    icons = {"done": "✅", "failed": "❌", "stopped": "⏹", "skipped": "⏭", "queued": "⏳", "running": "🏃"}
    sched = ""
    if d["state"] == "scheduled":
        sched = "\nStarts " + datetime.fromtimestamp(d["start_at"]).strftime("%H:%M")
    return "🌙 <b>Night shift</b> · " + d["state"] + "\n" + "\n".join(
        f"{icons.get(t['status'], '•')} {relay.html_escape(t['prompt'][:70])} ({'Gemini' if t['kind'] == 'agy' else t['kind']})"
        for t in d["tasks"]) + sched


def cli(a):
    sub = a[1] if len(a) > 1 else "list"
    def opt(name, default=None):
        return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default
    cwd = opt("--cwd", os.getcwd())
    if sub == "add":
        kind = opt("--tool", "claude")
        print(add({"gemini": "agy"}.get(kind, kind), a[2], cwd)["id"])
    elif sub == "add-annotate":
        spec = dict(input=opt("--input"), instructions=opt("--instructions"), fields=opt("--fields"),
                    calibration=opt("--calibration"), model=opt("--model"), runner=opt("--runner"))
        print(add("annotate", f"annotate {Path(opt('--input')).name}", cwd, {k: v for k, v in spec.items() if v})["id"])
    elif sub == "start":
        at = parse_time(opt("--at")) if opt("--at") else None
        start(at)
        print("scheduled" if at else "starting")
    elif sub == "stop":
        stop()
    elif sub == "clear":
        clear()
    elif sub == "remove":
        update(lambda d: d.__setitem__("tasks", [t for t in d["tasks"] if t["id"] != a[2]]))
    else:
        print(telegram("", cwd))
    return 0
