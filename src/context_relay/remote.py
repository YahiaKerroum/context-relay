"""The Telegram side of Context Relay: commands, buttons, screenshots, live view, app previews,
prompts to a chat, the idea inbox, pinned rules, and headless "phone runs" for when a chat is idle.

Prompting a chat from the phone
  working chat   the message is queued and injected at the agent's next step (it sees it mid-turn)
  idle chat      a headless session continues the chat (forked by default, so your editor chat is untouched)
  ideas (/drop)  picked up when the agent finishes its task, or started on their own after a short wait
"""
import ctypes
import html
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import guard
from . import relay
from . import smart
from . import watchers
esc = html.escape
TG = relay.HOME / "telegram_state.json"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
DEV_PORTS = (5173, 3000, 3001, 4173, 4200, 5000, 5500, 8000, 8080, 8501, 8888, 7860, 3030, 9000, 4321, 1313)

ACTION_LABELS = {"tests": "🧪 Tests", "commit": "💾 Commit", "summary": "📝 Summary", "handoff": "🔁 Hand off",
                 "pause": "⏸ Pause", "resume": "▶️ Resume", "stop": "⏹ Stop", "live": "📺 Live", "shot": "📸 Screen"}


def action_rows(sid, names, per_row=3):
    row, rows = [], []
    for n in names:
        row.append({"text": ACTION_LABELS[n], "callback_data": f"b:{n}:{sid[:8]}"})
        if len(row) == per_row:
            rows.append(row)
            row = []
    return rows + ([row] if row else [])


def tstate():
    return relay.read_json(TG, {}) or {}


def tset(**kw):
    d = tstate()
    d.update(kw)
    relay.write_json(TG, d)


def clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


# ---------------------------------------------------------------- chats

def all_chats(hours=12):
    out = []
    for tool in ("claude", "agy"):
        for p in (relay.STATE / tool).glob("*.json"):
            st = relay.read_json(p, {}) or {}
            if st.get("pct") is not None and st.get("title") != "Gemini job" \
                    and time.time() - st.get("updated", 0) < hours * 3600:
                out.append(st)
    return sorted(out, key=lambda s: s["updated"], reverse=True)


def find_chat(prefix):
    for st in all_chats(72):
        if st["sid"].startswith(prefix):
            return st
    return None


def chat_name(st):
    return st.get("title") or f"{'Claude' if st['tool'] == 'claude' else 'Gemini'} chat {st['sid'][:6]}"


def icon(st):
    return "🟣" if st["tool"] == "claude" else "🔵"


def is_busy(st):
    return bool(st.get("busy")) and time.time() - st.get("updated", 0) < 240


def is_busy_recent(st, since):
    """Did this chat do anything after `since`? (its transcript moved on)"""
    try:
        return os.path.getmtime(st["transcript"]) > since + 30
    except (KeyError, OSError):
        return False


def project_busy(cwd):
    c = relay.norm(cwd)
    for st in all_chats(1):
        if is_busy(st) and (relay.norm(st["cwd"]) == c or relay.norm(st["cwd"]).startswith(c + "/")
                            or c.startswith(relay.norm(st["cwd"]) + "/")):
            return True
    return False


def default_target():
    chats = [c for c in all_chats() if c.get("status") == "ok"]
    t = tstate().get("target")
    if t:
        for c in chats:
            if c["sid"] == t:
                return c
    return chats[0] if chats else (all_chats()[0] if all_chats() else None)


def active_cwd():
    t = default_target()
    return t["cwd"] if t else None


def bar(pct, width=10):
    n = int(round(min(pct, 100) / 100 * width))
    return "▓" * n + "░" * (width - n)


# ---------------------------------------------------------------- what is the agent doing

def tool_line(name, args):
    a = args or {}
    if name in ("Bash", "PowerShell", "run_command"):
        return "🔧 " + clip(a.get("command") or a.get("CommandLine") or "", 130)
    path = a.get("file_path") or a.get("TargetFile") or a.get("path") or a.get("AbsolutePath") or a.get("notebook_path")
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "write_to_file", "replace_file_content",
                "multi_replace_file_content", "edit_file", "write_file"):
        return "✏️ " + (Path(str(path)).name if path else name)
    if name in ("Read", "view_file", "read_file"):
        return "📖 " + (Path(str(path)).name if path else name)
    if name in ("Grep", "Glob", "grep_search", "list_dir"):
        return "🔎 " + clip(a.get("pattern") or a.get("Query") or a.get("DirectoryPath") or name, 80)
    return "🔧 " + name + (" " + clip(a.get("toolSummary") or a.get("description") or "", 80) if a else "")


def activity(st, n=8):
    path = st.get("transcript")
    if not path or not os.path.exists(path):
        return []
    out = []
    for line in relay.tail_lines(path, 400_000):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if st["tool"] == "claude":
            if d.get("type") != "assistant" or d.get("isSidechain"):
                continue
            for b in (d.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append("💬 " + clip(b["text"], 240))
                elif b.get("type") == "tool_use":
                    out.append(tool_line(b.get("name"), b.get("input")))
        elif d.get("type") == "PLANNER_RESPONSE":
            txt = d.get("response") or d.get("content") or d.get("text")
            if isinstance(txt, str) and txt.strip():
                out.append("💬 " + clip(txt, 240))
            for tc in d.get("tool_calls") or []:
                out.append(tool_line(tc.get("name"), tc.get("args")))
    return out[-n:]


def live_text(st):
    thr = relay.load_config()["threshold_pct"]
    state = "⏸ paused" if smart.has_flag("pause", st["sid"]) else ("working" if is_busy(st) else "idle")
    head = (f"{icon(st)} <b>{esc(chat_name(st))}</b> · {state}\n{bar(st['pct'])} {st['pct']:.0f}% "
            f"(hands off at {thr:.0f}%)\n")
    acts = activity(st, 8)
    body = "\n".join(esc(a) for a in acts) or "(nothing yet)"
    return f"{head}\n{body}\n\n<i>updated {time.strftime('%H:%M:%S')}</i>"


# ---------------------------------------------------------------- screenshots and previews

def foreground_title():
    try:
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(h, buf, 256)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def screenshot(data_mode):
    from PIL import Image, ImageGrab
    im = ImageGrab.grab(all_screens=True).convert("RGB")
    max_w, q = (1000, 55) if data_mode else (1700, 82)
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
    p = relay.HOME / "tmp" / f"shot-{int(time.time())}.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    im.save(p, "JPEG", quality=q, optimize=True)
    return p


def edge_exe():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return shutil.which("msedge") or shutil.which("chrome")


def page_shot(url, mobile=False, data_mode=False):
    exe = edge_exe()
    if not exe:
        raise RuntimeError("no Edge or Chrome found")
    out = relay.HOME / "tmp" / f"page-{int(time.time())}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    prof = tempfile.mkdtemp(prefix="relay-edge-")
    size = "390,844" if mobile else ("1100,720" if data_mode else "1366,860")
    try:
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                        f"--window-size={size}", "--virtual-time-budget=6000", f"--user-data-dir={prof}",
                        f"--screenshot={out}", url], capture_output=True, timeout=60, creationflags=NO_WINDOW)
    finally:
        shutil.rmtree(prof, ignore_errors=True)
    if not out.exists():
        raise RuntimeError("the browser produced no image (is the page reachable?)")
    return out


def open_ports():
    found = []
    for port in DEV_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                found.append(port)
        except OSError:
            pass
    return found


# ---------------------------------------------------------------- headless runs for the phone

class Phone:
    """Headless sessions started from Telegram. One per project at a time."""

    def __init__(self, remote):
        self.remote = remote
        self.active = {}

    def running(self, cwd):
        return relay.norm(cwd) in self.active

    def kill(self, cwd):
        info = self.active.get(relay.norm(cwd))
        if info:
            try:
                relay.kill_tree(info["proc"].pid)
            except OSError:
                pass
            return True
        return False

    def start(self, cwd, prompt, st=None, fresh=False, tool=None, item_ids=(), label=""):
        key = relay.norm(cwd)
        if key in self.active:
            return False
        self.active[key] = {"proc": None, "started": time.time()}
        threading.Thread(target=self._run, args=(key, cwd, prompt, st, fresh, tool, list(item_ids), label),
                         daemon=True).start()
        return True

    def _run(self, key, cwd, prompt, st, fresh, tool, item_ids, label):
        cfg = relay.load_config()["phone"]
        tool = tool or (st["tool"] if st else cfg["default_tool"])
        env = dict(os.environ, CONTEXT_RELAY_PHONE="1", PYTHONIOENCODING="utf-8")
        if tool == "claude":
            cmd = [shutil.which("claude") or "claude", "-p", prompt, "--output-format", "json",
                   "--permission-mode", cfg["permission_mode"]]
            if cfg["allowed_tools"]:
                cmd += ["--allowedTools", ",".join(cfg["allowed_tools"])]
            if st and not fresh:
                cmd += ["--resume", st["sid"]] + (["--fork-session"] if cfg["resume_mode"] == "fork" else [])
        else:
            cmd = [relay.agy_exe(), "-p", prompt, "--output-format", "json"]
            if st and not fresh:
                cmd += ["--conversation", st["sid"]]
            if cfg["agy_permissions"] == "all":
                cmd += ["--dangerously-skip-permissions"]
        result, err, new_sid = "", "", None
        try:
            for attempt in (1, 2):
                proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=NO_WINDOW)
                self.active[key]["proc"] = proc
                out, e = proc.communicate(timeout=cfg["timeout_min"] * 60)
                text = out.decode("utf-8", "replace")
                err = e.decode("utf-8", "replace")
                try:
                    d, _ = json.JSONDecoder().raw_decode(text, text.index("{"))
                    result = d.get("result") or d.get("response") or ""
                    new_sid = d.get("session_id") or d.get("conversation_id")
                    if d.get("is_error") or d.get("status") not in (None, "SUCCESS"):
                        err = err or result or json.dumps(d)[:300]
                    break
                except ValueError:
                    if attempt == 2 or proc.returncode == 0:
                        err = err or text[-300:] or f"exit {proc.returncode}"
        except subprocess.TimeoutExpired:
            self.kill(cwd)
            err = f"timed out after {cfg['timeout_min']} min"
        except Exception as e:  # noqa: BLE001
            err = repr(e)
        finally:
            self.active.pop(key, None)
        for iid in item_ids:
            smart.inbox_set(iid, status="done" if not err or result else "failed")
        self.remote.phone_done(cwd, tool, new_sid, result, err, label, st)


# ---------------------------------------------------------------- the handler

HELP = """<b>Context Relay</b>
<b>Talk to your agents</b>
just type: message the current chat (/chats to switch)
/drop <i>idea</i> – queue an idea; done when the agent finishes, or started if idle
/inbox – queued ideas · /new <i>task</i> – start a fresh session
<b>See</b>
/status · /live – what the agent is doing, updating · /screenshot · /preview [port]
/missed – what happened since you last looked
<b>Steer</b>
/pause · /resume · /stop · /handoff · /tests · /commit · /summary · /undo
/rule <i>text</i> – pin a rule (also protects paths you name) · /rules · /unrule <i>id</i>
<b>Watch</b>
/watch <i>path</i> – follow a training log · /kaggle <i>user/slug</i> · /watchers
<b>Modes</b>
/away · /back · /mode wifi|data|auto · /limit [off] – Claude's limit reached
/night – overnight queue (/night add, /night start 23:00, /night report)"""


class Remote:
    def __init__(self, daemon):
        self.d = daemon
        self.phone = Phone(self)
        self.live = None  # {"msg": id, "sid": ..., "until": ts, "last": text}
        self.last_tick = 0

    @property
    def bot(self):
        return self.d.bot

    # -- plumbing
    def send(self, text, buttons=None):
        return self.bot.send(text, buttons)

    def photo(self, path, caption, buttons=None):
        return self.bot.send_photo(path, caption, buttons)

    def data_mode(self):
        return self.d.net.get("mode") == "data"

    def touch(self):
        tset(last_seen=time.time())

    # -- text entry point
    def handle(self, msg):
        text = (msg.get("text") or "").strip()
        if not text:
            return
        prev = tstate().get("last_seen", time.time() - 3600)
        self.touch()
        tset(prev_seen=prev)
        reply_sid = None
        rt = msg.get("reply_to_message")
        if rt:
            reply_sid = (tstate().get("msg_map") or {}).get(str(rt["message_id"]))
        q = tstate().get("awaiting_text")
        if q and not text.startswith("/"):
            return self.answer_text(q, text)
        if not text.startswith("/"):
            return self.prompt_chat(text, find_chat(reply_sid) if reply_sid else None)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        fn = getattr(self, "cmd_" + cmd[1:], None)
        if fn:
            return fn(arg)
        self.send("Unknown command. /help lists them.")

    def cmd_start(self, arg):
        self.send(HELP)

    cmd_help = cmd_start

    # -- seeing
    def cmd_status(self, arg):
        self.d.send_status()

    cmd_s = cmd_status

    def cmd_chats(self, arg):
        chats = all_chats()
        if not chats:
            return self.send("No chats in the last 12 hours.")
        cur = default_target()
        rows = []
        lines = []
        for c in chats[:8]:
            mark = "📌 " if cur and c["sid"] == cur["sid"] else ""
            lines.append(f"{mark}{icon(c)} <b>{esc(clip(chat_name(c), 50))}</b> · {esc(Path(c['cwd']).name)} · "
                         f"{c['pct']:.0f}% · {'working' if is_busy(c) else c['status']}")
            rows.append([{"text": f"📌 {clip(chat_name(c), 28)}", "callback_data": f"t:{c['sid'][:8]}"}])
        self.send("\n".join(lines) + "\n\nTap to send your messages to a chat.", rows)

    def cmd_live(self, arg):
        busy = [c for c in all_chats() if is_busy(c)]
        st = default_target() if len(busy) <= 1 else None
        if len(busy) == 1:
            st = busy[0]
        if not st:
            chats = busy or all_chats()
            if not chats:
                return self.send("No chats to show.")
            return self.send("Which chat?", [[{"text": f"{icon(c)} {clip(chat_name(c), 30)}",
                                               "callback_data": f"live:{c['sid'][:8]}"}] for c in chats[:6]])
        self.start_live(st)

    def start_live(self, st):
        cfg = relay.load_config()["live"]
        mins = cfg["max_min_data"] if self.data_mode() else cfg["max_min"]
        m = self.send(live_text(st), [[{"text": "⏹ Stop live", "callback_data": "livestop"}] +
                                      action_rows(st["sid"], ["pause", "stop"])[0]])
        self.live = {"msg": m["message_id"], "sid": st["sid"], "until": time.time() + mins * 60, "last": "",
                     "next": 0}

    def tick_live(self):
        lv = self.live
        if not lv or time.time() < lv["next"]:
            return
        cfg = relay.load_config()["live"]
        lv["next"] = time.time() + (cfg["every_s_data"] if self.data_mode() else cfg["every_s"])
        st = find_chat(lv["sid"])
        if not st or time.time() > lv["until"]:
            if st:
                self.bot.edit(lv["msg"], live_text(st) + "\n⏹ live view ended")
            self.live = None
            return
        text = live_text(st)
        if text.split("<i>")[0] != lv["last"]:
            lv["last"] = text.split("<i>")[0]
            self.bot.edit(lv["msg"], text, [[{"text": "⏹ Stop live", "callback_data": "livestop"}] +
                                           action_rows(st["sid"], ["pause", "stop"])[0]])

    def cmd_screenshot(self, arg):
        if self.d.locked:
            st = default_target()
            note = "🔒 The PC is locked, so there's no screen to capture. Here's what the agent is doing instead:\n\n"
            return self.send(note + (live_text(st) if st else "(no chats)"))
        try:
            p = screenshot(self.data_mode())
        except Exception as e:  # noqa: BLE001
            return self.send(f"Couldn't take a screenshot: {esc(str(e)[:150])}")
        title = foreground_title()
        self.photo(p, f"🖥 {time.strftime('%H:%M:%S')}" + (f" · {esc(title[:70])}" if title else ""))
        p.unlink(missing_ok=True)

    cmd_shot = cmd_screenshot

    def cmd_preview(self, arg):
        mobile = "mobile" in arg.lower()
        arg = re.sub(r"\bmobile\b", "", arg, flags=re.I).strip()
        if arg.startswith("http"):
            return self.preview(arg, mobile)
        if arg.isdigit():
            return self.preview(f"http://localhost:{arg}", mobile)
        ports = open_ports()
        if not ports:
            return self.send("No dev server found on the usual ports. Start it, or give me a port or URL: "
                             "<code>/preview 5173</code>")
        if len(ports) == 1:
            return self.preview(f"http://localhost:{ports[0]}", mobile)
        self.send("Which one?", [[{"text": f":{p}", "callback_data": f"pv:{p}{'m' if mobile else ''}"}
                                  for p in ports[:6]]])

    def preview(self, url, mobile=False):
        try:
            p = page_shot(url, mobile, self.data_mode())
        except Exception as e:  # noqa: BLE001
            return self.send(f"Couldn't capture {esc(url)}: {esc(str(e)[:150])}")
        self.photo(p, f"🌐 {esc(url)}{' (phone size)' if mobile else ''} · {time.strftime('%H:%M:%S')}")
        p.unlink(missing_ok=True)

    def cmd_missed(self, arg):
        since = tstate().get("prev_seen") or time.time() - 6 * 3600
        card = self.d.build_card(since)
        self.send(card["text"] if card["total"] else "Nothing happened since you last looked. 🌿")

    cmd_card = cmd_missed

    # -- modes
    def cmd_away(self, arg):
        self.d.manual_away_at = time.time()
        self.d.away = True
        self.send("🚶 Away mode: finished turns, questions and approvals come here until you're back at the PC.")

    def cmd_back(self, arg):
        self.d.manual_away_at = None
        self.send("🧑‍💻 Back at the desk: notifications stay on the PC.")

    def cmd_mode(self, arg):
        mode = arg.lower()
        if mode not in ("wifi", "data", "auto"):
            return self.send(f"Now: {self.d.net.get('mode')} (detected {self.d.net.get('detected')}). "
                             f"Use /mode wifi, /mode data or /mode auto.")
        c = relay.load_config()
        c.setdefault("network", {})["mode"] = mode
        relay.write_json(relay.HOME / "config.json", c)
        self.d.refresh_network()
        self.send(f"Network mode: <b>{mode}</b> (now {self.d.net['mode']}).")

    def cmd_limit(self, arg):
        from . import limits
        if arg.lower() in ("off", "clear", "no"):
            limits.clear()
            return self.send("Claude's limit flag cleared; handoffs go back to the same tool.")
        limits.mark_hit("set from Telegram", hours=5)
        self.send("⛔ Noted: Claude's limit is reached. For the next ~5 hours, handoffs continue in Gemini. "
                  "<code>/limit off</code> clears it.")

    # -- steering
    def target_or_say(self):
        st = default_target()
        if not st:
            self.send("No chat to act on yet.")
        return st

    def cmd_pause(self, arg):
        if arg.lower() == "all":
            smart.set_flag("pause")
            return self.send("⏸ All agents will pause at their next step. /resume continues them.")
        st = self.target_or_say()
        if st:
            self.act("pause", st)

    def cmd_resume(self, arg):
        smart.clear_flags("pause")
        self.send("▶️ Resumed.")

    def cmd_stop(self, arg):
        st = self.target_or_say()
        if st:
            self.act("stop", st)

    def cmd_handoff(self, arg):
        st = self.target_or_say()
        if st:
            self.act("handoff", st)

    def cmd_tests(self, arg):
        st = self.target_or_say()
        if st:
            self.act("tests", st)

    def cmd_commit(self, arg):
        st = self.target_or_say()
        if st:
            self.act("commit", st)

    def cmd_summary(self, arg):
        st = self.target_or_say()
        if st:
            self.act("summary", st)

    def cmd_undo(self, arg):
        self.d.offer_undo()

    def act(self, action, st):
        name = esc(chat_name(st))
        if action == "pause":
            smart.set_flag("pause", st["sid"])
            self.send(f"⏸ <b>{name}</b> pauses at its next step.", [[{"text": "▶️ Resume", "callback_data":
                                                                       f"b:resume:{st['sid'][:8]}"}]])
        elif action == "resume":
            smart.set_flag("pause", st["sid"], on=False)
            smart.set_flag("pause", None, on=False)
            self.send(f"▶️ <b>{name}</b> continues.")
        elif action == "stop":
            smart.set_flag("stop", st["sid"])
            killed = self.phone.kill(st["cwd"])
            self.send(f"⏹ <b>{name}</b> {'was stopped' if killed else 'is told to stop at its next step'}. "
                      f"Send it a message when you want it to continue.")
            smart.event("control", f"stop {chat_name(st)}", sid=st["sid"])
        elif action == "handoff":
            smart.relay_force(st["tool"], st["sid"])
            self.send(f"🔁 <b>{name}</b> will write its handoff at its next step"
                      + ("" if is_busy(st) else " (send it any message if it's idle)") + ".")
        elif action == "tests":
            threading.Thread(target=self.run_tests, args=(st,), daemon=True).start()
        elif action == "commit":
            self.send_to(st, "Commit the current changes in a clean, well-described commit that follows this "
                             "repository's commit conventions. Do not push.", "commit")
        elif action == "summary":
            self.send_to(st, "In at most 6 short lines: what you did in this chat, what state things are in, and "
                             "what you'd do next.", "summary")
        elif action == "live":
            self.start_live(st)
        elif action == "shot":
            self.cmd_screenshot("")

    def run_tests(self, st):
        cfg = relay.load_config()
        root = guard.project_root(st["cwd"])
        cmds = smart.detect_checks(root, cfg) if root else []
        if not cmds:
            return self.send("I couldn't find a test command for this project. Put one in "
                             "<code>.context-relay/checks.json</code>: <code>{\"commands\": [\"...\"]}</code>")
        self.send(f"🧪 Running tests in <b>{esc(Path(st['cwd']).name)}</b>…")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", CONTEXT_RELAY_DISABLE="1")
        for cmd in cmds:
            t0 = time.time()
            try:
                r = subprocess.run(cmd, cwd=str(root), shell=True, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=cfg["checks"]["timeout_s"] * 2, env=env,
                                   creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired:
                self.send(f"⏱ <code>{esc(clip(cmd, 60))}</code> timed out.")
                continue
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            tail = "\n".join(out.splitlines()[-12:])[-900:]
            self.send(f"{'✅' if r.returncode == 0 else '❌'} <code>{esc(clip(cmd, 60))}</code> "
                      f"({time.time() - t0:.0f}s)\n<pre>{esc(tail)}</pre>")

    # -- prompting a chat
    def send_to(self, st, text, label=""):
        """Deliver a message to a chat: injected if it is working, run headless if it is idle."""
        if is_busy(st):
            smart.inbox_add(text, st["cwd"], kind="message", source="telegram", target=st["sid"][:8])
            return self.send(f"📨 {icon(st)} <b>{esc(clip(chat_name(st), 40))}</b> is working; it will see this at its "
                             f"next step.")
        return self.run_idle(st, text, label)

    def run_idle(self, st, text, label=""):
        cfg = relay.load_config()
        stale = st.get("status") != "ok" or st["pct"] >= cfg["threshold_pct"]
        prompt = text
        if stale and st.get("handoff_path") and os.path.exists(st["handoff_path"]):
            prompt = relay.next_session_prompt(st, max_chars=20000) + "\n\nThe user's new message:\n" + text
        if not self.phone.start(st["cwd"], prompt, st=st, fresh=stale, label=label or clip(text, 40)):
            smart.inbox_add(text, st["cwd"], kind="message", source="telegram", target=st["sid"][:8])
            return self.send("⏳ A phone session is already running in this project; your message is queued behind it.")
        how = "a fresh session (that chat is full)" if stale else (
            "a copy of the chat" if cfg["phone"]["resume_mode"] == "fork" and st["tool"] == "claude" else "the chat")
        self.send(f"🚀 {icon(st)} Running it in {how}… I'll send the answer.")
        smart.event("phone", f"prompt to {chat_name(st)}: {clip(text, 80)}", sid=st["sid"])

    def prompt_chat(self, text, chat=None):
        st = chat or default_target()
        if not st:
            return self.send("I don't know of any chat yet. Start one in your editor, or use /new.")
        tset(target=st["sid"])
        self.send_to(st, text)

    def cmd_new(self, arg):
        if not arg:
            return self.send("Usage: <code>/new fix the failing test in src/io</code>")
        cwd = active_cwd()
        if not cwd:
            return self.send("I don't know which project yet. Use the editor once, then try again.")
        if not self.phone.start(cwd, arg, fresh=True, label=clip(arg, 40)):
            return self.send("A phone session is already running in this project.")
        self.send(f"🚀 New session in <b>{esc(Path(cwd).name)}</b>… I'll send the answer.")

    def phone_done(self, cwd, tool, new_sid, result, err, label, st):
        name = esc(Path(cwd).name)
        if err and not result:
            return self.send(f"❌ Phone session in <b>{name}</b> failed:\n<pre>{esc(err[-500:])}</pre>")
        buttons = []
        if new_sid:
            tset(target=new_sid)
            smart.event("phone", f"phone session finished in {Path(cwd).name}", sid=new_sid)
            buttons = action_rows(new_sid, ["tests", "commit", "summary", "handoff"], 2)
        self.send(f"📱 <b>{esc(label or name)}</b> ✅ ({name})\n\n{esc(result.strip()[:3300])}", buttons)

    # -- ideas and rules
    def cmd_drop(self, arg):
        if not arg:
            return self.send("Usage: <code>/drop fix the README typo</code>")
        cwd = active_cwd()
        if not cwd:
            return self.send("I don't know which project yet; use the editor once, then drop again.")
        smart.inbox_add(arg, cwd, kind="idea", source="telegram")
        n = len([i for i in smart.inbox_list("queued") if i["kind"] == "idea"])
        wait = relay.load_config()["inbox"]["idle_wait_s"]
        self.send(f"📥 Queued for <b>{esc(Path(cwd).name)}</b> ({n} in the inbox). It's picked up when the agent "
                  f"finishes its current task"
                  + (f", or started on its own after {wait}s if nothing is running." if relay.load_config()["inbox"]["auto_run"]
                     else "."))

    def cmd_inbox(self, arg):
        items = [i for i in smart.inbox_list() if i["status"] in ("queued", "running")][-10:]
        if not items:
            return self.send("Inbox is empty. 🌿")
        self.send("\n".join(f"{'⏳' if i['status'] == 'queued' else '🏃'} {esc(clip(i['text'], 90))} "
                            f"<i>({esc(Path(i['cwd']).name) if i['cwd'] else 'any'})</i>" for i in items),
                  [[{"text": f"✖ {clip(i['text'], 24)}", "callback_data": f"ix:{i['id']}"}] for i in items[:5]])

    def cmd_rule(self, arg):
        if not arg:
            return self.send("Usage: <code>/rule never touch the data/ folder</code> (add <code>global</code> first "
                             "for every project)")
        scope = "project"
        if arg.lower().startswith("global "):
            scope, arg = "global", arg[7:].strip()
        cwd = active_cwd()
        root = guard.project_root(cwd) if cwd else None
        rule = smart.add_rule(arg, root or cwd, scope)
        extra = (f"\n🛡 Also protecting: <code>{esc(', '.join(rule['protect']))}</code> (edits wait for your OK)"
                 if rule["protect"] else "")
        where = "every project" if scope == "global" or not cwd else esc(Path(root or cwd).name)
        self.send(f"📌 Rule #{rule['id']} pinned for {where}: “{esc(rule['text'])}”{extra}\n"
                  f"New chats get it automatically; running ones on their next start. /unrule {rule['id']} removes it.")

    def cmd_rules(self, arg):
        r = smart.load_rules()
        items = [*r["global"]] + [x for lst in r["projects"].values() for x in lst]
        if not items:
            return self.send("No pinned rules. <code>/rule never touch the data/ folder</code>")
        self.send("\n".join(f"#{x['id']} {'🌐' if x['scope'] == 'global' else '📁'} {esc(x['text'])}"
                            + (f" 🛡 {esc(', '.join(x['protect']))}" if x["protect"] else "") for x in items),
                  [[{"text": f"✖ #{x['id']}", "callback_data": f"ru:{x['id']}"} for x in items[:6]]])

    def cmd_unrule(self, arg):
        if not arg.strip().isdigit():
            return self.send("Usage: <code>/unrule 3</code>")
        r = smart.remove_rule(int(arg))
        self.send(f"Removed rule #{arg}." if r else f"No rule #{arg}.")

    # -- watchers
    def cmd_watch(self, arg):
        if not arg:
            return self.send("Usage: <code>/watch C:\\path\\to\\train.log</code>")
        p = Path(arg.strip('"'))
        if not p.is_absolute():
            p = Path(active_cwd() or ".") / p
        if not p.exists():
            return self.send(f"Not found: <code>{esc(str(p))}</code>")
        w = watchers.add_log(p, cwd=active_cwd())
        self.send(f"👀 Watching <b>{esc(w['name'])}</b>. I'll warn about NaN loss, crashes and stalls.")

    def cmd_kaggle(self, arg):
        if "/" not in arg:
            return self.send("Usage: <code>/kaggle yourname/notebook-slug</code>")
        w = watchers.add_kaggle(arg.strip(), active_cwd())
        if not w:
            self.send("Already watching it, or no Kaggle CLI found for this project.")

    def cmd_watchers(self, arg):
        items = [i for i in watchers.load()["items"] if i.get("active", True)]
        if not items:
            return self.send("Nothing watched. <code>/watch path</code> or <code>/kaggle user/slug</code>")
        lines = [f"{'📄' if i['kind'] == 'log' else '🏃'} <b>{esc(i['name'])}</b>" + (f" · {i.get('status')}" if i["kind"] == "kaggle" else "")
                 for i in items]
        self.send("\n".join(lines), [[{"text": f"✖ {clip(i['name'], 22)}", "callback_data": f"wx:{i['id']}"}]
                                     for i in items[:6]])

    def cmd_night(self, arg):
        from . import nightshift
        self.send(nightshift.telegram(arg, active_cwd()))

    # -- questions from agents (ask_user)
    def answer_text(self, qid, text):
        tset(awaiting_text=None)
        self.d.answer_question(qid, text, "Telegram")

    # -- buttons
    def on_button(self, data, msg_id):
        kind, _, rest = data.partition(":")
        if kind == "b":
            action, _, sid = rest.partition(":")
            st = find_chat(sid)
            if not st:
                return self.send("That chat is gone.")
            return self.act(action, st)
        if kind == "t":
            st = find_chat(rest)
            if st:
                tset(target=st["sid"])
                self.bot.edit(msg_id, f"📌 Messages now go to {icon(st)} <b>{esc(chat_name(st))}</b>.")
        elif kind == "live":
            st = find_chat(rest)
            if st:
                self.start_live(st)
        elif data == "livestop":
            self.live = None
            self.bot.edit(msg_id, "⏹ Live view stopped.")
        elif kind == "pv":
            self.preview(f"http://localhost:{rest.rstrip('m')}", rest.endswith("m"))
        elif kind == "ix":
            smart.inbox_remove(rest)
            self.bot.edit(msg_id, "Removed from the inbox.")
        elif kind == "ru":
            r = smart.remove_rule(int(rest))
            self.bot.edit(msg_id, f"Removed rule #{rest}." if r else "Already gone.")
        elif kind == "wx":
            watchers.remove(rest)
            self.bot.edit(msg_id, "Stopped watching.")
        elif kind == "kg":
            item = next((i for i in watchers.load()["items"] if i["id"] == rest), None)
            if not item:
                return
            self.send("📥 Fetching the log and results…")

            def go():
                try:
                    nums, where = watchers.fetch_numbers(item)
                    self.send(("<pre>" + esc("\n".join(nums)) + "</pre>") if nums else f"No numbers: {esc(where)}")
                except Exception as e:  # noqa: BLE001
                    self.send(f"Couldn't fetch: {esc(str(e)[:150])}")
            threading.Thread(target=go, daemon=True).start()

    # -- periodic work
    def tick(self):
        self.tick_live()
        now = time.time()
        if now - self.last_tick < 5:
            return
        self.last_tick = now
        cfg = relay.load_config()
        ib = cfg["inbox"]
        if ib["auto_run"]:
            seen = set()
            for it in smart.inbox_list("queued"):
                if it["kind"] != "idea" or now - it["ts"] < ib["idle_wait_s"] or not it["cwd"] or it["cwd"] in seen:
                    continue
                seen.add(it["cwd"])
                if project_busy(it["cwd"]) or self.phone.running(it["cwd"]):
                    continue
                batch = smart.inbox_take(it["cwd"], {"idea"}, "phone", mark="running")
                if batch:
                    self.start_ideas(it["cwd"], batch)
        for it in smart.inbox_list("queued"):  # messages nobody picked up
            if it["kind"] == "message" and now - it["ts"] > 150 and it.get("target"):
                st = find_chat(it["target"])
                if st and not is_busy(st) and not self.phone.running(st["cwd"]):
                    smart.inbox_take(it["cwd"], {"message"}, "phone", mark="running")
                    self.run_idle(st, it["text"])

    def start_ideas(self, cwd, batch):
        body = "\n".join(f"{n}. {i['text']}" for n, i in enumerate(batch, 1))
        last = None
        for st in all_chats(72):
            if relay.norm(st["cwd"]) == relay.norm(cwd) and st.get("handoff_path"):
                last = st["handoff_path"]
                break
        prompt = (f"{relay.MARK} The user dropped these ideas from their phone for the project \"{Path(cwd).name}\". "
                  f"Do them in order as a careful engineer: read the code before changing it, keep changes small, "
                  f"run the project's tests if it has any, and do not commit unless an idea says so. Reply with a "
                  f"short summary: what you did for each idea, the files changed, and anything you skipped and why."
                  f"\n\nIdeas:\n{body}" + (f"\n\nLatest handoff for background: {last}" if last else ""))
        self.phone.start(cwd, prompt, fresh=True, item_ids=[i["id"] for i in batch],
                         label=f"{len(batch)} idea{'s' if len(batch) > 1 else ''}")
        self.send(f"🚀 Nothing was running, so I started {len(batch)} dropped idea{'s' if len(batch) > 1 else ''} in "
                  f"<b>{esc(Path(cwd).name)}</b>.")
