"""Claude's usage limit: detect that it was hit, and keep the work moving in Gemini.

Claude Code only shows the 5-hour usage percentage in its terminal status line, not in the VS Code panel, so
this does not try to read it. It notices the moment Claude says the limit is reached, remembers it (`/limit`
sets it by hand), routes the next handoffs to Gemini, and, when the chat can no longer answer, builds an
emergency handoff from the transcript with Gemini so the work carries on while you rest.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import relay
from . import smart
FILE = relay.HOME / "claude_limits.json"
LIMIT_RE = re.compile(r"(usage limit|hit your (?:\w+\s)?limit|limit reached|reached your|exceeded your|"
                      r"out of (?:extra )?usage|resets?\s+(?:at\s+)?\d)", re.I)


def state():
    return relay.read_json(FILE, {}) or {}


def active():
    s = state()
    return bool(s.get("hit")) and time.time() < s.get("until", 0)


def mark_hit(text, hours=5, until=None):
    relay.write_json(FILE, {"hit": True, "text": text[:200], "ts": time.time(), "until": until or time.time() + hours * 3600})
    smart.event("limit", f"Claude limit: {text[:100]}")


def clear():
    relay.write_json(FILE, {"hit": False, "ts": time.time()})


def parse_reset(text):
    """'resets 3pm' / 'resets at 15:30' -> the next such local time, or None."""
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23:
        return None
    now = datetime.now()
    t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


def scan(chats, seen):
    """The newest 'limit reached' message in the tail of a recently active Claude transcript, or None."""
    for st in chats:
        path = st.get("transcript")
        if st["tool"] != "claude" or not path or time.time() - st.get("updated", 0) > 3 * 3600:
            continue
        try:
            lines = relay.tail_lines(path, 60_000)[-25:]
        except OSError:
            continue
        for line in reversed(lines):
            if "limit" not in line.lower() and "resets" not in line.lower():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            if not (d.get("isApiErrorMessage") or msg.get("model") == "<synthetic>"):
                continue
            content = msg.get("content")
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict)) if isinstance(content, list) \
                else str(content or "")
            key = f"{st['sid']}:{d.get('timestamp') or len(line)}"
            try:
                age = time.time() - datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                age = 0
            if age > 15 * 60:
                continue
            if LIMIT_RE.search(text) and key not in seen:
                return st, text, key
    return None


def digest(path, max_chars=26000):
    """A readable text rendering of a Claude transcript: the first request plus the most recent work."""
    items, first = [], None
    for line in relay.tail_lines(path, 900_000):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if d.get("type") == "user" and not d.get("isSidechain"):
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
            if text.strip() and not text.lstrip().startswith("<"):
                items.append("USER: " + " ".join(text.split())[:700])
                first = first or items[-1]
        elif d.get("type") == "assistant" and not d.get("isSidechain") and isinstance(content, list):
            for b in content:
                if b.get("type") == "text" and b.get("text", "").strip():
                    items.append("CLAUDE: " + " ".join(b["text"].split())[:600])
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                    items.append(f"CLAUDE used {b.get('name')}: {' '.join(str(arg).split())[:200]}")
    out, size = [], 0
    for it in reversed(items):
        size += len(it) + 1
        if size > max_chars:
            break
        out.append(it)
    out.reverse()
    if first and first not in out:
        out.insert(0, first + "\n[... earlier conversation omitted ...]")
    return "\n".join(out)


def emergency_handoff(st, cfg):
    """Claude can't answer any more: have Gemini write the handoff from the transcript digest. Returns True if queued."""
    cwd = st["cwd"]
    hdir = Path(relay.handoff_path_for(cwd, st["sid"], cfg)).parent
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dig = hdir / f"{stamp}-{st['sid'][:8]}.transcript.txt"
    out = hdir / f"{stamp}-{st['sid'][:8]}.md"
    dig.write_text(digest(st["transcript"]), encoding="utf-8")
    prompt = (f"{relay.MARK} Claude's session in this project ended abruptly because it hit its usage limit, and you "
              f"are taking over. Read the conversation digest at {dig} (the user's first request and the most recent "
              f"work), look at the project's current state (git status / git diff --stat if you can), and write a "
              f"handoff for the next session to the exact path {out}.\nUse these sections: # Handoff: <title>, "
              f"## Goal, ## Status (what is done, half done, verified), ## Next steps (numbered, step 1 immediately "
              f"actionable), ## Key decisions, ## Gotchas, ## Files and commands, ## Open questions. Be specific: "
              f"paths, names, numbers. When the file is written reply with one line and stop.")
    try:
        r = subprocess.run([relay.agy_exe(), "-p", prompt, "--output-format", "json"], cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=900,
                           env={**os.environ, "CONTEXT_RELAY_DISABLE": "1"},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (subprocess.TimeoutExpired, OSError) as e:
        relay.log(f"emergency handoff: {e!r}")
        return False
    if not out.exists() or out.stat().st_size < 300:
        relay.log(f"emergency handoff not written: {(r.stdout or r.stderr)[-200:]}")
        return False
    st = dict(st, handoff_path=str(out), requested_at=time.time() - 60, status="requested", forced=True)
    relay.finalize(st, cfg)
    sp = relay.session_state_path("claude", st["sid"])
    stored = relay.read_json(sp, {}) or {}
    stored.update(status="done", handoff_path=str(out), done_at=time.time(), busy=False)
    relay.write_json(sp, stored)
    return True
