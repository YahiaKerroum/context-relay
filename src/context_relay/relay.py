#!/usr/bin/env python3
"""Context Relay: hand a long LLM chat to a fresh session before quality drops, and keep vibe-coding calm.

Everything is a subcommand of `context-relay` (or `python -m context_relay`):

  setup | install | uninstall | status | doctor      install and inspect (see `context-relay setup --help`)
  telegram setup | pair | status | unpair             connect your phone
  run claude|agy "task" [--cwd DIR] [--max N] [-- extra CLI args]
                                                      autopilot: headless sessions back to back
  annotate --input F --instructions T --fields "label:pos|neg, score:number" [--output F]
           [--calibration F] [--runner cli|ide] [--model M] [--limit N] [--inter-rater] [--cwd DIR]
  delegate-task --task T [--cwd DIR]                  delegate a tedious task to Gemini in Antigravity
  job list | status ID | wait ID | cancel ID | resume ID
  inbox | rule | watch | night | stats                ideas, pinned rules, watchers, night shift, dashboard data
  snapshots [DIR] | changes DIR SHA | undo DIR SHA    undo snapshots of agent turns
  handoff-now claude|agy SESSION_ID                   (called by the editor's "Hand off now")
  daemon                                              the background helper (Telegram, network, presence)
  mcp [ask]                                           MCP server: delegation tools and ask_user
  claude-hook | claude-statusline | agy-hook EVENT    (called by Claude Code / Antigravity)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("CONTEXT_RELAY_HOME", Path.home() / ".context-relay"))
STATE = HOME / "state"
OUTBOX = HOME / "outbox"
RUNS = HOME / "runs"
LOG = HOME / "relay.log"
MARK = "[CONTEXT RELAY]"

DEFAULTS = {
    "threshold_pct": 65,
    # 0 = auto: the window Claude Code reports to the status line, else 1M for
    # Opus (what this account runs) and 200k for anything else.
    "claude_window_tokens": 0,
    "agy_window_tokens": 1048576,
    "handoff_dir": ".context-relay/handoffs",
    "auto_open_new_chat": True,
    "auto_submit": True,
    "auto_submit_delay_ms": 2500,
    "warn_at_pct": 50,
    # where the next chat runs: "same", "claude" or "agy"
    "continue_in": "same",
    # Antigravity app / CLI chats have no editor of their own: open their next chat in the IDE
    "open_app_handoffs_in_ide": True,
    "required_sections": ["## Goal", "## Status", "## Next steps"],
    "remind_every": 8,
    "max_stop_blocks": 3,
    "pending_pointer_hours": 2,
    "autopilot": {
        "max_sessions": 10,
        "claude_args": ["--permission-mode", "acceptEdits"],
        "agy_args": ["--mode", "accept-edits"],
    },
    "guard": {
        "danger_gate": True,
        "data_guard": True,
        "approval_timeout_min": 10,
        "extra_danger_patterns": [],
    },
    "network": {"mode": "auto", "data_ssids": []},  # auto: metered connection or a listed hotspot = data
    "snapshots": {"enabled": True, "max_file_mb": 20, "keep_days": 7},
    "telegram": {"notify": "away", "approvals": "away"},  # away | always | off
    "away_after_min": 10,
    "locked_is_away": True,
    "keep_awake": True,
    # when Claude hits its usage limit, continue in Gemini (and build the handoff from the transcript)
    "auto_switch": {"enabled": True},
    "stuck": {"enabled": True, "after_errors": 3, "escalate_after": 6, "loop_edits": 6, "loop_escalate": 10,
              "escalate": "fresh"},  # fresh | other_model | notify
    "checks": {"enabled": True, "timeout_s": 240, "max_rounds": 2, "commands": {}},
    "inbox": {"auto_run": True, "idle_wait_s": 20},
    "phone": {"permission_mode": "acceptEdits", "resume_mode": "fork", "timeout_min": 30,
              "default_tool": "claude", "agy_permissions": "edits",
              "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git status:*)", "Bash(git diff:*)",
                                "Bash(git log:*)", "Bash(git add:*)", "Bash(git commit:*)", "Bash(python:*)",
                                "Bash(python -m unittest:*)", "Bash(pytest:*)", "Bash(npm test:*)",
                                "Bash(npm run:*)", "Bash(ls:*)", "Bash(cat:*)"]},
    "live": {"every_s": 6, "every_s_data": 20, "max_min": 4, "max_min_data": 2},
    "ask": {"timeout_min": 30, "telegram_after_s": 120},
    "recovery": {"enabled": True, "grace_s": 90},
    "kaggle": {"auto_fetch": "wifi", "file_pattern": "(.*\\.log|results/.*\\.(json|csv|txt))"},
    "night": {"max_hours": 8, "max_sessions_per_task": 6, "fallback_to_gemini": True,
              "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git status:*)", "Bash(git diff:*)",
                                "Bash(git log:*)", "Bash(python:*)", "Bash(python -m unittest:*)",
                                "Bash(pytest:*)", "Bash(npm test:*)", "Bash(ls:*)", "Bash(cat:*)"]},
    "delegation": {
        "inter_rater": {"enabled": False, "fraction": 0.05, "min": 10, "max": 100, "model": "sonnet",
                        "manual_seconds_per_record": 20},
        "runner": "cli",             # "cli": Antigravity CLI, parallel, headless; "ide": batches open in the Antigravity IDE
        "model": "gemini-3.8-flash-medium",
        "batch_size": 20,
        "concurrency": 3,
        "max_retries": 2,
        "calibration_per_batch": 3,
        "calibration_min_accuracy": 0.8,
        "batch_timeout_s": 600,
        "task_timeout_s": 3600,
        # headless Antigravity cannot ask for permission: "edits" lets Gemini edit files but not run
        # commands; "all" approves everything (--dangerously-skip-permissions)
        "task_permissions": "edits",
    },
}


# ---------------------------------------------------------------- utilities

def log(msg):
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    p = HOME / "config.json"
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (OSError, ValueError) as e:
            log(f"config unreadable, using defaults: {e}")
    if os.environ.get("CONTEXT_RELAY_THRESHOLD"):
        cfg["threshold_pct"] = float(os.environ["CONTEXT_RELAY_THRESHOLD"])
    return cfg


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def notify(kind, text, sid=None, actions=None, buttons=None, always=False, desktop=False):
    """Queue a notification. The daemon sends it to Telegram (when you are away, or always if `always`);
    `desktop` also shows it in the editor. `actions` are one-tap buttons for chat `sid`, `buttons` are
    rows of (label, callback_data)."""
    try:
        name = f"{time.time():.3f}-{uuid.uuid4().hex[:4]}.json"
        write_json(HOME / "notify" / name, {"kind": kind, "text": text, "ts": time.time(), "sid": sid,
                                            "actions": actions, "buttons": buttons, "always": always})
        if desktop:
            write_json(HOME / "desktop" / name, {"kind": kind, "text": text, "ts": time.time()})
    except OSError:
        pass


def cli_cmd(*args):
    """The command line that runs a context-relay subcommand with the interpreter we are running in."""
    return [sys.executable, "-m", "context_relay", *map(str, args)]


def kill_tree(pid):
    """Stop a process and its children (a headless agent session may have spawned some)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def spawn_bg(*args):
    """Fire and forget a subcommand (snapshots must not slow the chat down)."""
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    subprocess.Popen(cli_cmd(*args), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)


def html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def read_stdin_json():
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def emit(obj):
    if obj:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False))
        sys.stdout.flush()


def tail_lines(path, max_bytes=4_000_000):
    """Last lines of a JSONL file without reading a multi-hundred-MB transcript."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]  # first line is probably cut in half
    return lines


def norm(p):
    return str(p).replace("\\", "/").rstrip("/").lower()


def session_state_path(tool, sid):
    return STATE / tool / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', sid)}.json"


def handoff_path_for(cwd, sid, cfg):
    d = Path(cwd) / cfg["handoff_dir"]
    d.mkdir(parents=True, exist_ok=True)
    git = Path(cwd) / ".git"
    if git.is_dir():  # keep handoffs out of the user's commits without touching .gitignore
        excl = git / "info" / "exclude"
        try:
            text = excl.read_text(encoding="utf-8") if excl.exists() else ""
            if ".context-relay/" not in text:
                excl.parent.mkdir(parents=True, exist_ok=True)
                with open(excl, "a", encoding="utf-8") as f:
                    f.write(("" if text.endswith("\n") or not text else "\n") + ".context-relay/\n")
        except OSError:
            pass
    return str(d / f"{datetime.now():%Y%m%d-%H%M%S}-{sid[:8]}.md")


def handoff_written(st):
    p = st.get("handoff_path")
    if not p or not os.path.exists(p):
        return False
    try:
        return os.path.getsize(p) > 300 and os.path.getmtime(p) >= st.get("requested_at", 0) - 5
    except OSError:
        return False


def missing_sections(st, cfg):
    try:
        text = Path(st["handoff_path"]).read_text(encoding="utf-8").lower()
    except OSError:
        return cfg["required_sections"]
    return [s for s in cfg["required_sections"] if s.lower() not in text]


def take_force(tool, sid):
    """'Hand off now' from the editor leaves a flag for the session."""
    f = STATE / "force" / tool / sid
    if f.exists():
        try:
            f.unlink()
        except OSError:
            pass
        return True
    return False


# ---------------------------------------------------------------- prompts

def handoff_instructions(st, cfg):
    if st.get("stuck_note"):
        why = (f"This session is going in circles ({st['stuck_note']}), so it is being handed to a fresh session "
               f"that will take a different approach. In the handoff's Gotchas, record exactly what you tried and "
               f"why it failed, so the next session does not repeat it. This session is ending; write the handoff now.")
    elif st.get("forced"):
        why = (f"The user asked to hand this conversation off now (context is at {st.get('pct', 0):.0f}%). "
               f"This session is ending and a fresh session will take over from a handoff you write now.")
    else:
        why = (f"This conversation has used {st['pct']:.0f}% of its context window ({st['tokens']:,} of "
               f"{st['window']:,} tokens). The relay threshold is {cfg['threshold_pct']:.0f}%. Past this point "
               f"answer quality degrades, so this session is ending and a fresh session will take over from a "
               f"handoff you write now.")
    return f"""{MARK} {why}

Do this now:
1. Do not start anything new. If you are in the middle of a single edit, finish only that edit; otherwise stop where you are.
2. Write the handoff file to exactly this path: {st['handoff_path']}
3. Then reply with one short line saying the handoff is saved, and end your turn. Do nothing else.

The handoff is the ONLY thing the next session will see. Write it for a capable engineer who has zero memory of this chat. Use these sections:
# Handoff: <short title>
## Goal: what the user ultimately wants, in their words where possible, plus standing preferences and constraints they gave.
## Status: what is finished (with file paths), what is half done, what is verified and what is not.
## Next steps: numbered, concrete, in order. Step 1 must be something the next session can start immediately.
## Key decisions: choices made and why, so they are not relitigated.
## Gotchas: failed approaches, errors hit and how they were fixed, traps to avoid.
## Files and commands: the files that matter (paths) and the commands to build, run or verify.
## Open questions: anything that needs the user's input.
Be specific: exact paths, names, numbers, error messages. No filler. Aim for 400 to 1500 words."""


def reminder(st):
    return (f"{MARK} Reminder: context is at {st['pct']:.0f}%. Stop the current work and write the "
            f"handoff to {st['handoff_path']} as instructed, then end your turn.")


def missing_sections_message(st, missing):
    return (f"{MARK} The handoff at {st['handoff_path']} is missing these sections: {', '.join(missing)}. "
            f"Add them (the next session sees nothing else), then end your turn.")


def next_session_prompt(st, req_id=None, max_chars=None):
    tag = f"[CONTEXT RELAY #{req_id}]" if req_id else MARK
    who = {"claude": "Claude Code", "agy": "Gemini in Antigravity"}.get(st.get("tool"), "another agent")
    try:
        body = Path(st["handoff_path"]).read_text(encoding="utf-8")
    except OSError:
        body = None
    head = (f"{tag} You are continuing work from a previous session ({who}) that was handed off at "
            f"{st['pct']:.0f}% context. The handoff below was written by that session. Treat it as your "
            f"briefing: read it, quickly re-check the current state where it matters (files may have "
            f"changed), then carry on with the next steps. Do not ask the user to repeat anything the "
            f"handoff already says.\nHandoff file: {st['handoff_path']}\n\n")
    try:
        from . import smart
        rules = smart.rules_text(st.get("cwd", ""))
    except Exception:  # noqa: BLE001
        rules = ""
    tail = ("\n\n" + rules) if rules else ""
    if body is None or (max_chars and len(head) + len(body) + len(tail) > max_chars):
        return head + "Read the handoff file above first; it is your briefing." + tail
    return head + body + tail


def pending_pointer(tool, cwd, cfg):
    """Latest finished handoff for this project, if recent: a fallback for chats opened by hand."""
    best = None
    for p in (STATE / tool).glob("*.json"):
        st = read_json(p, {})
        if st.get("status") == "done" and st.get("cwd") and norm(st["cwd"]) == norm(cwd):
            if not best or st.get("done_at", 0) > best.get("done_at", 0):
                best = st
    if not best or time.time() - best.get("done_at", 0) > cfg["pending_pointer_hours"] * 3600:
        return None
    return (f"{MARK} A previous session in this project handed off its work "
            f"{int((time.time() - best['done_at']) / 60)} min ago. Handoff file: {best['handoff_path']}. "
            f"If the user asks to continue, or their message is a {MARK} handoff, read that file and "
            f"carry on from its next steps. Otherwise ignore this note.")


# ---------------------------------------------------------------- shared state machine

def measure(st, tokens, window, cfg):
    st["tokens"] = int(tokens)
    st["window"] = int(window)
    st["pct"] = 100.0 * tokens / window if window else 0.0
    st["updated"] = time.time()
    return st["pct"] >= cfg["threshold_pct"]


def request_handoff(st, cfg):
    st["status"] = "requested"
    st["requested_at"] = time.time()
    st["handoff_path"] = handoff_path_for(st["cwd"], st["sid"], cfg)
    st["since_request"] = 0
    st["stop_blocks"] = 0
    st.setdefault("pct", 0.0)
    st.setdefault("tokens", 0)
    st.setdefault("window", 0)
    log(f"{st['tool']} {st['sid'][:8]} at {st['pct']:.1f}% -> handoff requested: {st['handoff_path']}")


def next_target(st, cfg):
    """Which editor opens the next chat, or None when nothing can."""
    if st.get("switch_to"):  # stuck and told to try the other model
        return st["switch_to"]
    if cfg["continue_in"] in ("claude", "agy"):
        return cfg["continue_in"]
    if cfg["auto_switch"]["enabled"] and st["tool"] == "claude":
        from . import limits
        if limits.active():
            notify("limit", "⛔ Claude's usage limit is reached, so this handoff continues in Gemini.", desktop=True)
            return "agy"
    if st["tool"] == "agy" and st.get("product") != "ide" and not cfg["open_app_handoffs_in_ide"]:
        return None
    return st["tool"]


def finalize(st, cfg):
    """Handoff is on disk: queue the next session."""
    st["status"] = "done"
    st["done_at"] = time.time()
    run_id = os.environ.get("CONTEXT_RELAY_AUTOPILOT")
    target = next_target(st, cfg)
    if run_id:
        run = read_json(RUNS / f"{run_id}.json", {"handoffs": []})
        run["handoffs"].append(st["handoff_path"])
        run["last"] = st
        write_json(RUNS / f"{run_id}.json", run)
    elif target and cfg["auto_open_new_chat"]:
        rid = uuid.uuid4().hex[:8]
        req = {
            "id": rid,
            "kind": "handoff",
            "target": target,
            "cwd": st["cwd"],
            "from": st["tool"],
            "session_id": st["sid"],
            "handoff_path": st["handoff_path"],
            "prompt": next_session_prompt(st, rid),
            "auto_submit": cfg["auto_submit"],
            "auto_submit_delay_ms": cfg["auto_submit_delay_ms"],
            "created": time.time(),
        }
        write_json(OUTBOX / f"{rid}.json", req)
        try:  # the chain: this chat -> its handoff -> the chat that picks it up (linked when it is sent)
            with open(HOME / "chain.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "req": rid, "from_sid": st["sid"], "tool": st["tool"],
                                    "to_tool": target, "cwd": st["cwd"], "handoff": st["handoff_path"],
                                    "pct": round(st.get("pct", 0), 1), "why": st.get("stuck_note") or (
                                        "limit" if st.get("forced") else "context")}) + "\n")
        except OSError:
            pass
    log(f"{st['tool']} {st['sid'][:8]} handoff saved: {st['handoff_path']} (target={target}, autopilot={run_id})")
    return target


# ---------------------------------------------------------------- Claude Code

def claude_usage(transcript):
    """(context tokens, model) from the newest main-thread assistant message."""
    for line in reversed(tail_lines(transcript)):
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        msg = d.get("message") or {}
        u = msg.get("usage")
        if not u or msg.get("model") == "<synthetic>":
            continue
        return (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)), msg.get("model", "")
    return 0, ""


def first_prompt(transcript):
    """The first real user message of a Claude transcript (for chats that predate titles)."""
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 400:
                    break
                if '"type":"user"' not in line.replace(" ", ""):
                    continue
                c = (json.loads(line).get("message") or {}).get("content")
                if isinstance(c, list):
                    c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
                if c and not c.lstrip().startswith("<"):
                    return c
    except (OSError, ValueError):
        pass
    return ""


def claude_window(st, model, tokens, cfg):
    if cfg["claude_window_tokens"]:
        return cfg["claude_window_tokens"]
    if st.get("sl_window") and time.time() - st.get("sl_ts", 0) < 6 * 3600:
        return st["sl_window"]
    if "[1m]" in model or tokens > 200_000:  # a 1M window announces itself or is visibly in use
        return 1_000_000
    return 200_000


def advance(st, cfg, at_stop, over, touching_handoff=False):
    """The relay state machine both tools share. Returns (action, payload).

    action: "order" (payload = text telling the model to write or fix the handoff),
            "remind" (payload = reminder text), "done" (payload = target), "failed", or None.
    """
    if st["status"] == "ok":
        forced = take_force(st["tool"], st["sid"])
        if over or forced:
            st["forced"] = forced and not over
            request_handoff(st, cfg)
            if at_stop:
                st["stop_blocks"] = 1
            return "order", handoff_instructions(st, cfg)
        return None, None
    if st["status"] != "requested":
        return None, None
    if not at_stop:
        if handoff_written(st) or touching_handoff:
            return None, None
        st["since_request"] = st.get("since_request", 0) + 1
        if st["since_request"] % cfg["remind_every"] == 0:
            return "remind", reminder(st)
        return None, None
    written = handoff_written(st)
    missing = missing_sections(st, cfg) if written else []
    if written and not missing:
        return "done", finalize(st, cfg)
    if st.get("stop_blocks", 0) < cfg["max_stop_blocks"]:
        st["stop_blocks"] = st.get("stop_blocks", 0) + 1
        return "order", missing_sections_message(st, missing) if written else handoff_instructions(st, cfg)
    if written:  # incomplete, but better than nothing
        return "done", finalize(st, cfg)
    st["status"] = "failed"
    log(f"{st['tool']} {st['sid'][:8]} never wrote its handoff; giving up")
    return "failed", None


def done_message(st, target):
    if os.environ.get("CONTEXT_RELAY_AUTOPILOT"):
        where = "the autopilot starts the next session"
    elif target:
        where = f"a new {'Claude' if target == 'claude' else 'Antigravity'} chat is opening with it"
    else:
        where = "start a new chat and say 'continue'"
    return f"Context Relay: handoff saved to {st['handoff_path']}; {where}."


def unattended():
    """Headless sessions started by the phone, the night shift or the autopilot."""
    return bool(os.environ.get("CONTEXT_RELAY_PHONE") or os.environ.get("CONTEXT_RELAY_NIGHT")
                or os.environ.get("CONTEXT_RELAY_AUTOPILOT"))


def record_submit(m, sid):
    """The next chat has received its handoff: the extension stops waiting, and the chain gets its link."""
    write_json(STATE / "submitted" / f"{m.group(1)}.json", {"sid": sid, "ts": time.time()})
    try:
        with open(HOME / "chain.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "req": m.group(1), "to_sid": sid}) + "\n")
    except OSError:
        pass


def claude_hook():
    from . import smart
    data = read_stdin_json()
    cfg = load_config()
    event = data.get("hook_event_name", "")
    sid = data.get("session_id") or "unknown"
    if data.get("agent_id"):  # subagent events: the main thread owns the context
        return
    cwd = data.get("cwd") or os.getcwd()
    sp = session_state_path("claude", sid)
    st = read_json(sp, {}) or {"tool": "claude", "sid": sid, "status": "ok", "cwd": cwd}
    st.setdefault("first", time.time())
    if data.get("transcript_path"):
        st["transcript"] = data["transcript_path"]
    phone = bool(os.environ.get("CONTEXT_RELAY_PHONE"))

    if event == "SessionStart":
        parts = []
        if data.get("source") in ("startup", "clear"):
            ptr = pending_pointer("claude", cwd, cfg)
            if ptr:
                parts.append(ptr)
        rules = smart.rules_text(cwd)
        if rules:
            parts.append(rules)
        if parts:
            emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(parts)}})
        write_json(sp, st)
        return

    if event == "PreToolUse":  # pause / stop, pinned rules, danger gate, data guard; may wait for your answer
        before = json.dumps(st.get("rule_ok", {}))
        why = smart.before_tool("claude", st, cfg, data.get("tool_name", ""), data.get("tool_input") or {}, cwd, sid)
        if json.dumps(st.get("rule_ok", {})) != before:
            write_json(sp, st)
        if why:
            emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                         "permissionDecisionReason": why}})
        return

    if event == "UserPromptSubmit":
        prompt = data.get("prompt") or ""
        m = re.search(r"\[CONTEXT RELAY #(\w+)\]", prompt)
        if m:  # lets the extension confirm its auto-submit landed
            record_submit(m, sid)
        smart.clear_flags("stop")
        st.update(busy=True, busy_since=time.time(), check_rounds=0, recent=[], stuck_seen={})
        chat_title(st, prompt)
        if cfg["snapshots"]["enabled"]:
            spawn_bg("snapshot", cwd, "turn: " + " ".join(prompt.split())[:80], "claude", sid)

    transcript = data.get("transcript_path")
    over = False
    if transcript and os.path.exists(transcript):
        if not st.get("title"):
            chat_title(st, first_prompt(transcript))
        tokens, model = claude_usage(transcript)
        st["model"] = model or st.get("model", "")
        over = measure(st, tokens, claude_window(st, model, tokens, cfg), cfg) and not phone

    note = None
    if event in ("PostToolUse", "PostToolUseFailure"):
        resp = data.get("tool_response")
        note = smart.after_tool("claude", st, cfg, data.get("tool_name", ""), data.get("tool_input") or {},
                                event == "PostToolUse", str(data.get("error") or ""),
                                json.dumps(resp, ensure_ascii=False)[:3000] if resp is not None else "", cwd, sid)

    touching = (event == "PostToolUse" and st.get("handoff_path")
                and norm(st["handoff_path"]) in norm(json.dumps(data.get("tool_input") or {})).replace("//", "/"))
    action, payload = advance(st, cfg, event == "Stop", over, bool(touching))
    out = None
    if action == "order":
        if event == "UserPromptSubmit":
            out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": payload}}
        else:
            out = {"decision": "block", "reason": payload}
    elif action == "remind" and event == "PostToolUse":
        note = "\n\n".join(x for x in (note, payload) if x)
    elif action == "done":
        out = {"systemMessage": done_message(st, payload)}
    elif action == "failed":
        out = {"systemMessage": "Context Relay: the model did not write a handoff; relay skipped."}
    if note and out is None and event in ("PostToolUse", "PostToolUseFailure"):
        out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": note}}
    if event == "Stop":
        extra = smart.on_stop("claude", st, cfg, cwd, sid) if action is None else None
        if extra:  # failing checks, or ideas from your phone: back to work
            out = {"decision": "block", "reason": extra}
        elif action != "order":
            st["busy"] = False
            if not unattended():
                turn_done_ping(st, data.get("last_assistant_message"), action == "done")
    write_json(sp, st)
    emit(out)


def chat_title(st, prompt):
    """A readable name for the chat: its first message, or the handoff title for relay chats."""
    if st.get("title") or not prompt.strip():
        return
    m = re.search(r"^#\s*Handoff:\s*(.+)$", prompt, re.M) if "[CONTEXT RELAY" in prompt else None
    plain = " ".join(prompt.replace(MARK, "").split())
    if "[CONTEXT RELAY BATCH" in prompt or "[CONTEXT RELAY TASK" in prompt:
        st["title"] = "Gemini job"
    elif os.environ.get("CONTEXT_RELAY_NIGHT"):
        st["title"] = "🌙 night shift"
    elif os.environ.get("CONTEXT_RELAY_PHONE"):
        st["title"] = "📱 " + plain[:55]
    elif m:
        st["title"] = "↪ " + m.group(1).strip()[:60]
    elif "[CONTEXT RELAY" not in prompt:
        st["title"] = plain[:60]


def turn_done_ping(st, text, handed_off):
    who = "Claude" if st["tool"] == "claude" else "Gemini"
    proj = html_escape(Path(st["cwd"]).name)
    if handed_off:
        notify("handoff", f"🔁 <b>{who}</b> in <b>{proj}</b> reached {st['pct']:.0f}% and handed off; "
                          f"a fresh chat is taking over.", sid=st["sid"])
        return
    body = html_escape((text or "").strip()[:900])
    name = html_escape(st.get("title") or f"{who} chat")
    notify("turn_done", f"✅ <b>{name}</b> finished in <b>{proj}</b> ({st.get('pct', 0):.0f}% context)"
                        + (f"\n\n{body}" if body else ""), sid=st["sid"],
           actions=["tests", "commit", "summary", "handoff"])
    try:
        from . import smart
        smart.event("turn_done", f"{st.get('title') or who} finished in {Path(st['cwd']).name}", sid=st["sid"],
                    project=Path(st["cwd"]).name)
    except Exception:  # noqa: BLE001
        pass


def claude_statusline():
    data = read_stdin_json()
    cfg = load_config()
    cw = data.get("context_window") or {}
    sid = data.get("session_id")
    pct = cw.get("used_percentage")
    if sid and cw.get("context_window_size"):
        sp = session_state_path("claude", sid)
        st = read_json(sp, {}) or {"tool": "claude", "sid": sid, "status": "ok",
                                   "cwd": (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or os.getcwd()}
        st["sl_window"] = cw["context_window_size"]
        st["sl_ts"] = time.time()
        write_json(sp, st)
    if data.get("rate_limits"):  # shown in the editor panel: when Claude runs low, delegate more
        write_json(HOME / "claude_limits.json", {**data["rate_limits"], "ts": time.time()})
    thr = cfg["threshold_pct"]
    if pct is None:
        print(f"relay: waiting for first reply (hands off at {thr:.0f}%)")
        return
    filled = int(min(pct, 100) / 100 * 20)
    bar = "#" * filled + "-" * (20 - filled)
    mark = int(thr / 100 * 20)
    bar = bar[:mark] + "|" + bar[mark:]
    model = (data.get("model") or {}).get("display_name", "")
    print(f"{model}  ctx [{bar}] {pct:.0f}%  relay at {thr:.0f}%")


# ---------------------------------------------------------------- Antigravity

def agy_usage(transcript):
    for line in reversed(tail_lines(transcript)):
        if '"input_tokens"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "PLANNER_RESPONSE":
            return d.get("input_tokens", 0) + d.get("cache_read_tokens", 0)
    return 0


def agy_hook(event):
    from . import smart
    data = read_stdin_json()
    cfg = load_config()
    sid = data.get("conversationId") or "unknown"
    ws = data.get("workspacePaths") or []
    cwd = ws[0] if ws else os.getcwd()
    transcript = data.get("transcriptPath") or ""
    sp = session_state_path("agy", sid)
    st = read_json(sp, {}) or {"tool": "agy", "sid": sid, "status": "ok", "cwd": cwd}
    st.setdefault("first", time.time())
    st["product"] = "ide" if "antigravity-ide" in norm(transcript) else "app"
    st["model"] = data.get("modelName") or st.get("model", "")
    if transcript:
        st["transcript"] = transcript
    first_input = data.get("lastUserInput") or ""
    phone = bool(os.environ.get("CONTEXT_RELAY_PHONE"))
    out = {}

    if event == "PreToolUse":  # pause / stop, pinned rules, danger gate and data guard
        call = data.get("toolCall") or {}
        args = call.get("args") or {}
        before = json.dumps(st.get("rule_ok", {}))
        why = smart.before_tool("agy", st, cfg, call.get("name", ""), args, args.get("Cwd") or cwd, sid)
        if json.dumps(st.get("rule_ok", {})) != before:
            write_json(sp, st)
        emit({"decision": "allow"} if not why else {"decision": "deny", "reason": why})
        return

    if event == "PostToolUse":  # stuck detection; a nudge is delivered at the next model call
        call = data.get("toolCall") or {}
        err = str(data.get("error") or "")
        note = smart.after_tool("agy", st, cfg, call.get("name", ""), call.get("args") or {}, not err, err,
                                str(data.get("result") or "")[:3000], cwd, sid)
        if note:
            st["pending_note"] = "\n\n".join(x for x in (st.get("pending_note"), note) if x)
        write_json(sp, st)
        return

    m = re.search(r"\[CONTEXT RELAY #(\w+)\]", first_input)
    if m:
        record_submit(m, sid)

    if event == "SessionStart":
        parts = []
        ptr = pending_pointer("agy", cwd, cfg)
        if ptr and MARK[:-1] not in first_input:
            parts.append(ptr)
            st["pointer_given"] = True
        rules = smart.rules_text(cwd)
        if rules:
            parts.append(rules)
            st["rules_given"] = True
        if parts:
            out = {"injectSteps": [{"systemMessage": {"systemMessage": "\n\n".join(parts)}}]}
        write_json(sp, st)
        emit(out)
        return

    over = False
    if transcript and os.path.exists(transcript):
        over = measure(st, agy_usage(transcript), cfg["agy_window_tokens"], cfg) and not phone

    inject = []
    if event == "PreInvocation" and data.get("invocationNum") == 0:  # a new turn starts
        smart.clear_flags("stop")
        st.update(busy=True, busy_since=time.time(), check_rounds=0, recent=[], stuck_seen={})
        chat_title(st, first_input)
        if cfg["snapshots"]["enabled"] and "[CONTEXT RELAY BATCH" not in first_input:
            spawn_bg("snapshot", cwd, "turn: " + " ".join(first_input.split())[:80], "agy", sid)
        parts = []
        if st["status"] == "ok" and not st.get("pointer_given"):
            st["pointer_given"] = True  # SessionStart does not fire in every product
            ptr = pending_pointer("agy", cwd, cfg)
            if ptr and MARK[:-1] not in first_input:
                parts.append(ptr)
        if not st.get("rules_given"):
            st["rules_given"] = True
            rules = smart.rules_text(cwd)
            if rules:
                parts.append(rules)
        if parts:
            inject.append({"systemMessage": {"systemMessage": "\n\n".join(parts)}})
    if event == "PreInvocation":
        if st.get("pending_note"):
            inject.append({"ephemeralMessage": st.pop("pending_note")})
        msgs = smart.inbox_take(cwd, {"message"}, sid)
        if msgs:
            inject.append({"userMessage": smart.messages_text(msgs)})

    action, payload = advance(st, cfg, event == "Stop", over)
    if event == "PreInvocation":
        if action == "order":
            inject.append({"systemMessage": {"systemMessage": payload}})
        elif action == "remind":
            inject.append({"ephemeralMessage": payload})
        if inject:
            out = {"injectSteps": inject}
    elif event == "Stop":
        extra = smart.on_stop("agy", st, cfg, cwd, sid) if action is None else None
        if action == "order":
            out = {"decision": "continue", "reason": payload}
        elif extra:
            out = {"decision": "continue", "reason": extra}
        elif data.get("fullyIdle", True):
            st["busy"] = False
            if "[CONTEXT RELAY BATCH" not in first_input and "[CONTEXT RELAY TASK" not in first_input \
                    and not unattended():
                turn_done_ping(st, data.get("finalModelOutput"), action == "done")
    write_json(sp, st)
    emit(out)


# ---------------------------------------------------------------- autopilot

def agy_exe():
    return shutil.which("agy") or str(Path.home() / ".gemini" / "bin" / "agy.exe")


def run_autopilot(tool, task, cwd, max_sessions=None, extra=()):
    cfg = load_config()
    max_sessions = max_sessions or cfg["autopilot"]["max_sessions"]
    run_id = uuid.uuid4().hex[:10]
    write_json(RUNS / f"{run_id}.json", {"tool": tool, "task": task, "cwd": cwd, "handoffs": []})
    env = dict(os.environ, CONTEXT_RELAY_AUTOPILOT=run_id)
    prompt, seen = task, 0
    for n in range(1, max_sessions + 1):
        print(f"\n=== relay {run_id}: session {n} ({tool}) ===", flush=True)
        if tool == "claude":
            cmd = [shutil.which("claude") or "claude", "-p", prompt, *cfg["autopilot"]["claude_args"], *extra]
        else:
            cmd = [agy_exe(), "-p", prompt, *cfg["autopilot"]["agy_args"], *extra]
        code = subprocess.run(cmd, cwd=cwd, env=env).returncode
        run = read_json(RUNS / f"{run_id}.json", {"handoffs": []})
        if len(run["handoffs"]) == seen:
            print(f"\n=== relay {run_id}: finished after {n} session(s), exit code {code} ===")
            return code
        seen = len(run["handoffs"])
        print(f"\n=== relay {run_id}: handed off -> {run['handoffs'][-1]} ===", flush=True)
        prompt = next_session_prompt(run["last"], max_chars=24000)  # Windows caps command lines at 32k
    print(f"=== relay {run_id}: stopped at max_sessions={max_sessions} ===")
    return 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    cmd = a[0] if a else "help"
    if cmd in ("claude-hook", "agy-hook") and os.environ.get("CONTEXT_RELAY_DISABLE"):
        return 0  # delegated batch runs: short, and their output is ours to parse
    if cmd in ("claude-hook", "claude-statusline", "agy-hook"):
        try:  # a hook must never break the chat it watches
            {"claude-hook": claude_hook, "claude-statusline": claude_statusline}.get(cmd, lambda: agy_hook(a[1]))()
        except BaseException as e:  # noqa: BLE001
            import traceback
            log(f"{cmd} {a[1:]} error: {e!r}\n{traceback.format_exc()}")
        return 0
    if cmd == "run" and len(a) >= 3 and a[1] in ("claude", "agy"):
        own, extra = (a[:a.index("--")], a[a.index("--") + 1:]) if "--" in a else (a, [])
        cwd = own[own.index("--cwd") + 1] if "--cwd" in own else os.getcwd()
        mx = int(own[own.index("--max") + 1]) if "--max" in own else None
        return run_autopilot(a[1], a[2], cwd, mx, extra)
    if cmd in ("setup", "install", "uninstall", "status", "doctor", "telegram", "build-vsix", "version", "--version", "-V"):
        from . import installer
        return installer.run(a)
    if cmd == "handoff-now" and len(a) >= 3:
        f = STATE / "force" / a[1] / a[2]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(time.time()))
        return 0
    if cmd == "mcp":
        from . import mcp_server
        return mcp_server.serve(only="ask" if "ask" in a[1:] else None)
    if cmd in ("inbox", "rule", "watch", "stats", "night", "ask-reply"):
        from . import cli_extra
        return cli_extra.run(a)
    if cmd == "daemon":
        from . import daemon
        return daemon.main()
    if cmd == "snapshot" and len(a) >= 3:
        from . import guard
        try:
            guard.snapshot(a[1], a[2], *(a[3:5]))
            guard.prune(load_config()["snapshots"]["keep_days"])
        except Exception as e:  # noqa: BLE001
            log(f"snapshot error: {e!r}")
        return 0
    if cmd == "snapshots":
        from . import guard
        print(json.dumps(guard.snapshots(a[1] if len(a) > 1 else os.getcwd()), indent=1))
        return 0
    if cmd == "changes" and len(a) >= 3:
        from . import guard
        print(json.dumps(guard.changes_since(a[1], a[2])))
        return 0
    if cmd == "undo" and len(a) >= 3:
        from . import guard
        changed, before = guard.undo(a[1], a[2])
        print(json.dumps({"changed": changed, "redo": before}))
        return 0
    if cmd in ("job", "annotate", "delegate-task"):
        from . import delegate
        return delegate.cli(a)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
