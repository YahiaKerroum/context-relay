"""Context Relay daemon: one background process that ties it all together.

Telegram     long polling; only the paired chat is answered. Notifications, approvals, questions, commands.
Network      metered / hotspot detection (data mode) and a connectivity probe (pause and resume on drops).
Presence     away when you say /away, the PC is locked, or you've been idle; a "while you were away" card on return.
Awake        keeps Windows from sleeping while an agent, a Gemini job or the night shift is working.
Watchers     training logs and Kaggle runs. Limits: notices when Claude's usage limit is hit.
"""
import ctypes
import html
import json
import mimetypes
import os
import random
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import relay
from . import smart
SECRETS = relay.HOME / "secrets.json"
NOTIFY = relay.HOME / "notify"
APPROVALS = relay.HOME / "approvals"
QUESTIONS = relay.HOME / "questions"
STATUS = relay.HOME / "daemon.json"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
LOCK_PORT = 47613

esc = html.escape


def secrets():
    return relay.read_json(SECRETS, {}) or {}


def save_secret(**kw):
    s = secrets()
    s.update(kw)
    relay.write_json(SECRETS, s)


def plain(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s))


# ---------------------------------------------------------------- Telegram

class Bot:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.offset = 0

    def call(self, method, timeout=30, **params):
        data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(self.base + method, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"telegram {method}: {e.code} {e.read()[:200]!r}")
        if not res.get("ok"):
            raise RuntimeError(f"telegram {method}: {res}")
        return res["result"]

    def send(self, text, buttons=None, chat=None):
        chat = chat or secrets().get("telegram_chat_id")
        if not chat:
            return None
        return self.call("sendMessage", chat_id=chat, text=text[:4000], parse_mode="HTML",
                         disable_web_page_preview=True,
                         reply_markup={"inline_keyboard": buttons} if buttons else None)

    def edit(self, msg_id, text, buttons=None):
        try:
            self.call("editMessageText", chat_id=secrets().get("telegram_chat_id"), message_id=msg_id,
                      text=text[:4000], parse_mode="HTML",
                      reply_markup={"inline_keyboard": buttons or []})
        except Exception as e:  # noqa: BLE001
            if "not modified" not in str(e):
                relay.log(f"telegram edit: {e}")

    def send_photo(self, path, caption="", buttons=None, chat=None):
        chat = chat or secrets().get("telegram_chat_id")
        boundary = uuid.uuid4().hex
        fields = {"chat_id": str(chat), "caption": caption[:1000], "parse_mode": "HTML"}
        if buttons:
            fields["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        body = b""
        for k, v in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        name = Path(path).name
        ctype = mimetypes.guess_type(name)[0] or "image/jpeg"
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{name}"\r\n'
                 f'Content-Type: {ctype}\r\n\r\n').encode() + Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.base + "sendPhoto", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["result"]

    def updates(self):
        url = self.base + "getUpdates?" + urllib.parse.urlencode(
            {"timeout": 50, "offset": self.offset, "allowed_updates": json.dumps(["message", "callback_query"])})
        with urllib.request.urlopen(url, timeout=70) as r:
            res = json.load(r)["result"]
        if res:
            self.offset = res[-1]["update_id"] + 1
        return res


# ---------------------------------------------------------------- presence, network, awake

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    if os.name != "nt":
        return 0
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000


def is_locked():
    """The lock screen runs on a secure desktop that a normal process cannot switch to."""
    if os.name != "nt":
        return False
    try:
        u = ctypes.windll.user32
        h = u.OpenInputDesktop(0, False, 0x0100)
        if not h:
            return True
        ok = u.SwitchDesktop(h)
        u.CloseDesktop(h)
        return not ok
    except Exception:  # noqa: BLE001
        return False


PS_COST = ("$null=[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,"
           "ContentType=WindowsRuntime]; $p=[Windows.Networking.Connectivity.NetworkInformation]::"
           "GetInternetConnectionProfile(); if($p){$p.GetConnectionCost().NetworkCostType}else{'Offline'}")


def check_network(cfg):
    net = {"ts": time.time()}
    try:
        cost = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS_COST],
                              capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout.strip()
        wl = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=10,
                            creationflags=NO_WINDOW).stdout
        m = re.search(r"^\s+SSID\s+:\s(.+)$", wl, re.M)
        net.update(cost=cost, ssid=m.group(1).strip() if m else None, online=cost != "Offline")
    except Exception as e:  # noqa: BLE001
        net.update(cost="unknown", ssid=None, online=True, error=str(e)[:200])
    ncfg = cfg.get("network", {})
    listed = {s.lower() for s in ncfg.get("data_ssids", [])}
    auto = "data" if (net["cost"] in ("Fixed", "Variable") or (net["ssid"] or "").lower() in listed) else "wifi"
    net["detected"] = auto
    net["mode"] = ncfg.get("mode", "auto") if ncfg.get("mode", "auto") != "auto" else auto
    return net


def set_awake(on):
    if os.name == "nt":  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))


def bar(pct, width=10):
    n = int(round(min(pct, 100) / 100 * width))
    return "▓" * n + "░" * (width - n)


# ---------------------------------------------------------------- the daemon

class Daemon:
    def __init__(self):
        self.net = relay.read_json(relay.HOME / "network.json", {}) or {}
        self.away = False
        self.away_since = None
        self.manual_away_at = None
        self.locked = False
        self.awake = False
        self.bot = None
        self.tg_ok = False
        self.remote = None
        self.approval_msgs = {}
        self.question_msgs = {}
        self.pending_undo = {}
        self.seen_limits = set()
        self.off_fail = 0
        self.offline_since = None
        self.jobs = {}
        self.bg_running = set()

    # -- background helper: run fn every `every` seconds in its own thread, never overlapping
    def bg(self, name, every, fn):
        last = self.jobs.get(name, 0)
        if time.time() - last < every or name in self.bg_running:
            return
        self.jobs[name] = time.time()
        self.bg_running.add(name)

        def go():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                relay.log(f"{name}: {e!r}")
            finally:
                self.bg_running.discard(name)
        threading.Thread(target=go, daemon=True).start()

    def run(self):
        from . import nightshift
        from . import remote
        from . import watchers
        from . import limits
        token = secrets().get("telegram_token")
        if token:
            self.bot = Bot(token)
            self.remote = remote.Remote(self)
            threading.Thread(target=self.poll_telegram, daemon=True).start()
        while True:
            cfg = relay.load_config()
            self.bg("network", 60, self.refresh_network)
            self.probe_connection(cfg)
            self.update_presence(cfg)
            busy = self.agents_busy()
            if busy != self.awake and cfg.get("keep_awake", True):
                set_awake(busy)
                self.awake = busy
            self.dispatch_notifications(cfg)
            self.dispatch_approvals(cfg)
            self.dispatch_questions(cfg)
            self.bg("watchers", 15, lambda: watchers.tick(self.net.get("mode", "wifi")))
            self.bg("limits", 30, lambda: self.scan_limits(cfg))
            self.bg("trim", 3600, smart.trim_events)
            nightshift.tick()
            if self.remote:
                self.remote.tick()
            relay.write_json(STATUS, {
                "pid": os.getpid(), "ts": time.time(), "away": self.away, "locked": self.locked,
                "telegram": self.tg_ok, "paired": bool(secrets().get("telegram_chat_id")),
                "awake": self.awake, "mode": self.net.get("mode"), "ssid": self.net.get("ssid"),
                "offline": self.offline_since is not None, "night": nightshift.load()["state"],
                "limit": limits.active()})
            time.sleep(2)

    def agents_busy(self):
        from . import nightshift
        now = time.time()
        for tool in ("claude", "agy"):
            for p in (relay.STATE / tool).glob("*.json"):
                st = relay.read_json(p, {}) or {}
                if st.get("busy") and now - st.get("updated", 0) < 6 * 3600 and now - st.get("busy_since", now) < 12 * 3600:
                    return True
        jobs = relay.HOME / "jobs"
        if jobs.exists():
            for d in jobs.iterdir():
                if (relay.read_json(d / "status.json", {}) or {}).get("state") == "running":
                    return True
        if self.remote and self.remote.phone.active:
            return True
        return nightshift.load()["state"] == "running"

    def refresh_network(self):
        cfg = relay.load_config()
        net = check_network(cfg)
        if self.net.get("mode") and net["mode"] != self.net.get("mode"):
            relay.notify("network", f"{'📶 Data mode' if net['mode'] == 'data' else '🛜 Wi-Fi mode'}: "
                                    f"{'big downloads now ask first' if net['mode'] == 'data' else 'downloads unrestricted'}"
                                    f" ({net.get('ssid') or net.get('cost')})")
        self.net = net
        relay.write_json(relay.HOME / "network.json", net)

    # -- connection drops: hold network tools, resume interrupted chats
    def probe_connection(self, cfg):
        from . import watchers
        if time.time() - self.jobs.get("probe", 0) < 8:
            return
        self.jobs["probe"] = time.time()
        ok = watchers.probe_online()
        self.off_fail = 0 if ok else self.off_fail + 1
        if self.off_fail >= 2 and self.offline_since is None:
            self.offline_since = time.time()
            smart.event("offline", "connection lost")
        relay.write_json(relay.HOME / "offline.json", {"offline": self.offline_since is not None,
                                                       "since": self.offline_since, "ts": time.time()})
        if ok and self.offline_since is not None:
            since, self.offline_since = self.offline_since, None
            relay.write_json(relay.HOME / "offline.json", {"offline": False, "since": None, "ts": time.time()})
            mins = (time.time() - since) / 60
            smart.event("offline", f"back online after {mins:.0f} min")
            if cfg["recovery"]["enabled"] and mins >= 0.3:
                threading.Thread(target=self.recover, args=(since, cfg), daemon=True).start()

    def recover(self, since, cfg):
        """Back online: chats that were mid-turn and went quiet get a nudge to carry on."""
        from . import remote
        time.sleep(cfg["recovery"]["grace_s"])  # give the agents' own retries a chance first
        stuck = []
        for st in remote.all_chats(2):
            if st.get("busy") and st.get("updated", 0) < since + 60 and not remote.is_busy_recent(st, since):
                stuck.append(st)
        names = []
        for st in stuck:
            if self.remote and not self.remote.phone.running(st["cwd"]):
                self.remote.phone.start(st["cwd"], "The connection dropped for a while and is back. Carry on "
                                                   "exactly where you left off.", st=st, label="resume after drop")
                names.append(remote.chat_name(st))
        mins = (time.time() - since) / 60
        relay.notify("offline", f"🔌 Connection is back after {mins:.0f} min."
                                + (f" Resumed: {esc(', '.join(names))}." if names else " Nothing needed resuming."),
                     always=True, desktop=True)

    def scan_limits(self, cfg):
        from . import limits
        from . import remote
        hit = limits.scan(remote.all_chats(3), self.seen_limits)
        if not hit:
            return
        st, text, key = hit
        self.seen_limits.add(key)
        limits.mark_hit(text, until=limits.parse_reset(text))
        until = limits.state().get("until", 0)
        when = time.strftime("%H:%M", time.localtime(until)) if until else "later"
        switching = cfg["auto_switch"]["enabled"] and st.get("status") == "ok" and st.get("transcript")
        relay.notify("limit", f"⛔ <b>Claude hit its usage limit</b> (back around {when}).\n"
                              + ("I'm having Gemini write the handoff from the transcript and continue the work."
                                 if switching else "Handoffs go to Gemini until then."), always=True, desktop=True)
        if switching:
            if limits.emergency_handoff(st, cfg):
                relay.notify("limit", "✅ Handoff written; a Gemini chat is picking the work up.", always=True)
            else:
                relay.notify("limit", "⚠️ I couldn't build the handoff automatically. Your transcript digest is in "
                                      "the project's .context-relay/handoffs folder.", always=True)

    # -- presence
    def update_presence(self, cfg):
        self.locked = is_locked() if cfg.get("locked_is_away", True) else False
        idle = idle_seconds()
        if self.manual_away_at and idle < time.time() - self.manual_away_at - 5:
            self.manual_away_at = None  # you touched the PC after saying /away
        away_after = cfg.get("away_after_min", 10) * 60
        was = self.away
        self.away = bool(self.manual_away_at) or self.locked or (away_after > 0 and idle > away_after)
        if self.away and not was:
            self.away_since = time.time()
            relay.write_json(relay.HOME / "presence.json", {"away": True, "since": self.away_since})
        elif was and not self.away:
            since = self.away_since or time.time()
            relay.write_json(relay.HOME / "presence.json", {"away": False, "back_at": time.time(), "since": since})
            if time.time() - since >= 180:
                card = self.build_card(since)
                if card["total"]:
                    relay.write_json(relay.HOME / "away_card.json", {"ts": time.time(), "since": since, **card})

    def build_card(self, since):
        ev = smart.events_since(since)
        lines = []

        def add(icon, text):
            lines.append((icon, text))
        turns = [e for e in ev if e["kind"] == "turn_done"]
        if turns:
            per = {}
            for e in turns:
                per[e.get("project", "?")] = per.get(e.get("project", "?"), 0) + 1
            add("✅", f"{len(turns)} turn{'s' if len(turns) > 1 else ''} finished (" +
                ", ".join(f"{k} ×{v}" for k, v in per.items()) + ")")
        for kind, icon, word in (("handoff", "🔁", "handoff"), ("stuck", "🧱", "stuck moment"),
                                 ("phone", "📱", "phone prompt"), ("undo", "↩️", "undo")):
            n = sum(1 for e in ev if e["kind"] == kind)
            if n:
                add(icon, f"{n} {word}{'s' if n > 1 else ''}")
        ap = [e for e in ev if e["kind"] == "approval"]
        if ap:
            add("🛡", f"{len(ap)} approval{'s' if len(ap) > 1 else ''} handled "
                      f"({sum(1 for e in ap if e.get('verdict') == 'approved')} allowed, "
                      f"{sum(1 for e in ap if e.get('verdict') == 'denied')} denied)")
        ch = [e for e in ev if e["kind"] == "checks"]
        if ch:
            add("🧪", f"checks: {sum(1 for e in ch if e['text'] == 'passed')} passed, "
                      f"{sum(1 for e in ch if e['text'].startswith('failed'))} sent back to the agent")
        for e in ev:
            if e["kind"] in ("watch", "kaggle", "limit", "night", "offline"):
                add({"watch": "👀", "kaggle": "🏃", "limit": "⛔", "night": "🌙", "offline": "🔌"}[e["kind"]], e["text"][:110])
        asks = sum(1 for e in ev if e["kind"] == "ask" and e["text"].startswith("answered"))
        if asks:
            add("❓", f"{asks} question{'s' if asks > 1 else ''} answered")
        try:
            from . import remote as _remote
            chats = _remote.all_chats(12)
            changed = 0
            from . import guard
            for st in chats:
                snaps = [s for s in guard.snapshots(st["cwd"], 100) if s["ts"] >= since]
                if snaps:
                    changed += len(guard.changes_since(st["cwd"], snaps[-1]["sha"]))
            if changed:
                add("📝", f"{changed} file change{'s' if changed > 1 else ''} while you were away")
        except Exception:  # noqa: BLE001
            pass
        waiting = []
        for f in APPROVALS.glob("*.json") if APPROVALS.exists() else []:
            if (relay.read_json(f, {}) or {}).get("status") == "pending":
                waiting.append("an approval")
        for f in QUESTIONS.glob("*.json") if QUESTIONS.exists() else []:
            if (relay.read_json(f, {}) or {}).get("status") == "pending":
                waiting.append("a question")
        queued = len([i for i in smart.inbox_list("queued")])
        if queued:
            waiting.append(f"{queued} queued idea{'s' if queued > 1 else ''}")
        mins = (time.time() - since) / 60
        head = f"While you were away ({mins:.0f} min)"
        body = [f"{i} {t}" for i, t in lines] or ["Nothing happened."]
        if waiting:
            body.append("⏳ Waiting for you: " + ", ".join(waiting))
        return {"total": len(lines) + len(waiting), "title": head,
                "text": f"<b>{head}</b>\n" + "\n".join(esc(b) for b in body), "plain": "\n".join(body)}

    # -- sending
    def wants_telegram(self, cfg, n):
        mode = cfg.get("telegram", {}).get("notify", "away")
        if not self.bot or not secrets().get("telegram_chat_id") or mode == "off":
            return False
        if n.get("kind") == "network":
            return self.away
        return bool(n.get("always")) or mode == "always" or self.away

    def dispatch_notifications(self, cfg):
        if not NOTIFY.exists():
            return
        from .remote import action_rows
        for f in sorted(NOTIFY.glob("*.json")):
            n = relay.read_json(f, {}) or {}
            try:
                if self.wants_telegram(cfg, n):
                    rows = []
                    if n.get("actions") and n.get("sid"):
                        rows += action_rows(n["sid"], n["actions"], 2)
                    for row in n.get("buttons") or []:
                        rows.append([{"text": l, "callback_data": cb} for l, cb in row])
                    msg = self.bot.send(n.get("text", ""), rows or None)
                    if msg and n.get("sid"):
                        from .remote import tset, tstate
                        mm = tstate().get("msg_map") or {}
                        mm[str(msg["message_id"])] = n["sid"]
                        tset(msg_map=dict(list(mm.items())[-200:]))
                    time.sleep(0.4)
                f.unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001  (offline: keep it for the next round)
                if time.time() - n.get("ts", 0) > 3600:
                    f.unlink(missing_ok=True)
                relay.log(f"telegram notify: {e}")
                return

    def dispatch_approvals(self, cfg):
        if not APPROVALS.exists() or not self.bot or not secrets().get("telegram_chat_id"):
            return
        always = cfg.get("telegram", {}).get("approvals", "away") == "always"
        for f in APPROVALS.glob("*.json"):
            a = relay.read_json(f, {}) or {}
            aid = a.get("id")
            if not aid:
                continue
            stale = time.time() - a.get("created", 0) > 90  # a longer wait: you've probably stepped away
            if a.get("status") == "pending" and aid not in self.approval_msgs and (self.away or always or stale):
                who = "Claude" if a.get("tool") == "claude" else "Gemini"
                icon = {"danger": "⚠️", "rule": "📌"}.get(a.get("kind"), "📶")
                text = (f"{icon} <b>{who}</b> wants to run a <b>{esc(a['title'])}</b> in "
                        f"<b>{esc(Path(a.get('cwd') or '').name)}</b>:\n<pre>{esc(a['detail'][:1500])}</pre>")
                try:
                    msg = self.bot.send(text, [[{"text": "✅ Allow", "callback_data": f"ap:{aid}:y"},
                                                {"text": "❌ Deny", "callback_data": f"ap:{aid}:n"}]])
                    self.approval_msgs[aid] = (msg["message_id"], text)
                except Exception as e:  # noqa: BLE001
                    relay.log(f"telegram approval: {e}")
            elif a.get("status") != "pending" and aid in self.approval_msgs:
                mid, text = self.approval_msgs.pop(aid)
                verdict = {"approved": "✅ Allowed", "denied": "❌ Denied", "expired": "⌛ Expired"}.get(a["status"])
                self.bot.edit(mid, f"{text}\n{verdict}{' from ' + a['by'] if a.get('by') else ''}")
            if a.get("status") != "pending" and time.time() - a.get("created", 0) > 86400:
                f.unlink(missing_ok=True)

    def dispatch_questions(self, cfg):
        if not QUESTIONS.exists() or not self.bot or not secrets().get("telegram_chat_id"):
            return
        for f in QUESTIONS.glob("*.json"):
            q = relay.read_json(f, {}) or {}
            qid = q.get("id")
            if not qid:
                continue
            age = time.time() - q.get("created", 0)
            if q.get("status") == "pending" and qid not in self.question_msgs and (
                    self.away or age > cfg["ask"]["telegram_after_s"]):
                who = "Claude" if q.get("tool") == "claude" else "Gemini"
                opts = q.get("options") or []
                rows = [[{"text": o[:40], "callback_data": f"q:{qid}:{i}"} for i, o in enumerate(opts)][j:j + 2]
                        for j in range(0, len(opts), 2)]
                rows.append([{"text": "💬 Other…", "callback_data": f"qo:{qid}"},
                             {"text": "🤷 You decide", "callback_data": f"qs:{qid}"}])
                text = (f"❓ <b>{who}</b> asks ({esc(Path(q.get('cwd') or '').name)}):\n\n{esc(q['question'])}"
                        + (f"\n\n<i>{esc(q['context'])}</i>" if q.get("context") else "")
                        + (f"\n\nIt would pick: <b>{esc(q['default'])}</b>" if q.get("default") else ""))
                try:
                    msg = self.bot.send(text, rows)
                    self.question_msgs[qid] = (msg["message_id"], text)
                except Exception as e:  # noqa: BLE001
                    relay.log(f"telegram question: {e}")
            elif q.get("status") != "pending" and qid in self.question_msgs:
                mid, text = self.question_msgs.pop(qid)
                self.bot.edit(mid, f"{text}\n\n✅ {esc(str(q.get('answer', 'answered'))[:200])}"
                              if q.get("status") == "answered" else f"{text}\n\n⌛ expired")
            if q.get("status") != "pending" and time.time() - q.get("created", 0) > 86400:
                f.unlink(missing_ok=True)

    def answer_question(self, qid, text, by):
        from . import ask
        if ask.answer(qid, text, by):
            if by == "Telegram":
                self.bot.send("👍 Sent.")

    # -- Telegram updates
    def poll_telegram(self):
        try:
            me = self.bot.call("getMe")
            from .remote import HELP
            self.bot.call("setMyCommands", commands=[
                {"command": c, "description": d} for c, d in (
                    ("status", "Chats, jobs, mode"), ("live", "Watch the agent work"),
                    ("screenshot", "Screenshot of the PC"), ("preview", "Screenshot of your running app"),
                    ("drop", "Queue an idea"), ("chats", "Pick which chat I talk to"),
                    ("rule", "Pin a rule"), ("pause", "Pause the agent"), ("stop", "Stop the agent"),
                    ("tests", "Run the tests"), ("night", "Overnight queue"), ("missed", "What did I miss?"),
                    ("away", "Send everything here"), ("help", "All commands"))])
            self.tg_ok = True
            relay.log(f"telegram: connected as @{me['username']}")
        except Exception as e:  # noqa: BLE001
            relay.log(f"telegram start: {e}")
        delay = 5
        while True:
            try:
                for u in self.bot.updates():
                    self.tg_ok = True
                    try:
                        self.handle(u)
                    except Exception as e:  # noqa: BLE001
                        import traceback
                        relay.log(f"telegram handle: {e!r}\n{traceback.format_exc()}")
                delay = 5
            except Exception as e:  # noqa: BLE001  (offline or Telegram hiccup)
                self.tg_ok = False
                relay.log(f"telegram poll: {e}")
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def handle(self, u):
        paired = secrets().get("telegram_chat_id")
        if "callback_query" in u:
            q = u["callback_query"]
            if str(q["message"]["chat"]["id"]) != str(paired):
                return
            try:
                self.bot.call("answerCallbackQuery", callback_query_id=q["id"])
            except Exception:  # noqa: BLE001
                pass
            return self.on_button(q["data"], q["message"]["message_id"])
        msg = u.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not paired:
            code = (relay.read_json(relay.HOME / "pair_code.json", {}) or {}).get("code")
            if code and code in text:
                save_secret(telegram_chat_id=chat)
                (relay.HOME / "pair_code.json").unlink(missing_ok=True)
                from .remote import HELP
                self.bot.send("✅ Paired. This chat now controls Context Relay on your PC.\n\n" + HELP, chat=chat)
                relay.log(f"telegram: paired with chat {chat}")
            else:
                self.bot.send("Send the pairing code shown in VS Code (Context Relay) to connect.", chat=chat)
            return
        if str(chat) != str(paired):
            return  # strangers get nothing
        if self.remote:
            self.remote.handle(msg)

    def on_button(self, data, msg_id):
        kind, _, rest = data.partition(":")
        if kind == "ap":
            aid, _, yn = rest.partition(":")
            p = APPROVALS / f"{aid}.json"
            a = relay.read_json(p, {}) or {}
            if a.get("status") == "pending":
                a.update(status="approved" if yn == "y" else "denied", by="Telegram")
                relay.write_json(p, a)
        elif kind == "q":
            qid, _, idx = rest.partition(":")
            q = relay.read_json(QUESTIONS / f"{qid}.json", {}) or {}
            opts = q.get("options") or []
            if idx.isdigit() and int(idx) < len(opts):
                self.answer_question(qid, opts[int(idx)], "Telegram")
        elif kind == "qo":
            from .remote import tset
            tset(awaiting_text=rest)
            self.bot.send("✍️ Type your answer as a normal message.")
        elif kind == "qs":
            self.answer_question(rest, "You decide: use your best judgement and state the assumption in your final "
                                       "message.", "Telegram")
        elif kind == "undo":
            if rest == "no" or not self.pending_undo or not self.pending_undo["sha"].startswith(rest):
                return self.bot.edit(msg_id, "Kept as is.")
            from . import guard
            changed, before = guard.undo(self.pending_undo["cwd"], self.pending_undo["sha"])
            self.pending_undo = {}
            self.bot.edit(msg_id, f"↩️ Undone: {len(changed)} file(s) restored. "
                                  f"(Changed your mind? The previous state is snapshot {before[:8] if before else '?'}.)")
        elif self.remote:
            self.remote.on_button(data, msg_id)

    # -- status and undo (also used by the remote)
    def send_status(self):
        from . import limits
        from . import nightshift
        from . import remote
        cfg = relay.load_config()
        thr = cfg["threshold_pct"]
        target = remote.default_target()
        lines = [f"<b>Context Relay</b> · {'📶 data mode' if self.net.get('mode') == 'data' else '🛜 Wi-Fi mode'}"
                 f" · {'🚶 away' if self.away else '🧑‍💻 at desk'}"
                 + (" · 🔒 locked" if self.locked else "") + (" · 🔌 offline" if self.offline_since else "")]
        chats = remote.all_chats(12)[:6]
        rows = []
        if chats:
            lines.append("")
        for st in chats:
            pin = "📌 " if target and st["sid"] == target["sid"] else ""
            state = "⏸ paused" if smart.has_flag("pause", st["sid"]) else ("working" if remote.is_busy(st) else st.get("status"))
            lines.append(f"{pin}{remote.icon(st)} <b>{esc(remote.chat_name(st)[:44])}</b> · "
                         f"{esc(Path(st['cwd']).name)} · {state}\n   {bar(st['pct'])} {st['pct']:.0f}%  (relay at {thr:.0f}%)")
            if remote.is_busy(st):
                short = remote.clip(remote.chat_name(st), 12)
                rows.append([{"text": f"📺 {short}", "callback_data": f"live:{st['sid'][:8]}"},
                             {"text": "⏸", "callback_data": f"b:pause:{st['sid'][:8]}"},
                             {"text": "⏹", "callback_data": f"b:stop:{st['sid'][:8]}"}])
        jobs = relay.HOME / "jobs"
        if jobs.exists():
            for jd in sorted(jobs.iterdir(), reverse=True)[:4]:
                s = relay.read_json(jd / "status.json", {}) or {}
                if s.get("state") in ("running", "queued", "paused") or time.time() - s.get("finished", 0) < 3 * 3600:
                    cal = f" · calibration {100 * s['cal_ok'] / s['cal_n']:.0f}%" if s.get("cal_n") else ""
                    lines.append(f"⚙️ {esc(s.get('title', jd.name))}: {s.get('state')} {s.get('done', 0)}/{s.get('total', 0)}{cal}")
        night = nightshift.load()
        if night["state"] != "idle":
            lines.append(f"🌙 Night shift: {night['state']} ({sum(1 for t in night['tasks'] if t['status'] == 'done')}/{len(night['tasks'])} tasks)")
        if limits.active():
            lines.append("⛔ Claude's usage limit is reached; handoffs go to Gemini.")
        queued = len(smart.inbox_list("queued"))
        if queued:
            lines.append(f"📥 {queued} idea{'s' if queued > 1 else ''} in the inbox")
        self.bot.send("\n".join(lines), rows or None)

    def offer_undo(self):
        from . import guard
        from . import remote
        cwd = remote.active_cwd()
        snaps = [s for s in guard.snapshots(cwd) if not s["label"].startswith("before undo")] if cwd else []
        if not snaps:
            return self.bot.send("No snapshots yet for the active project.")
        s = snaps[0]
        changed = guard.changes_since(cwd, s["sha"])
        if not changed:
            return self.bot.send("Nothing changed since the last turn started.")
        files = "\n".join(f"{st} {esc(f)}" for st, f in changed[:25]) + (f"\n… {len(changed) - 25} more" if len(changed) > 25 else "")
        self.pending_undo = {"cwd": cwd, "sha": s["sha"]}
        ago = int((time.time() - s["ts"]) / 60)
        self.bot.send(f"↩️ Undo the last turn in <b>{esc(Path(cwd).name)}</b> (started {ago} min ago: "
                      f"{esc(s['label'][:80])})?\n<pre>{files}</pre>",
                      [[{"text": "↩️ Undo", "callback_data": f"undo:{s['sha'][:12]}"},
                        {"text": "Keep", "callback_data": "undo:no"}]])


def main():
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))  # one daemon per machine
    except OSError:
        print("daemon already running")
        return 0
    if not secrets().get("telegram_chat_id") and not (relay.HOME / "pair_code.json").exists():
        relay.write_json(relay.HOME / "pair_code.json", {"code": f"{random.randint(0, 999999):06d}"})
    relay.log(f"daemon started (pid {os.getpid()})")
    Daemon().run()
    return 0
