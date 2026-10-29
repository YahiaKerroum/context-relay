"""Telegram commands (with a fake bot), watchers, limits, questions, night shift, stats."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from _env import HOME, TMP  # noqa: F401  (sets CONTEXT_RELAY_HOME before relay is imported)

from context_relay import (ask, guard, limits, nightshift, relay, remote, smart, stats,  # noqa: E402
                           watchers)


class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.photos = [], [], []

    def send(self, text, buttons=None, chat=None):
        self.sent.append((text, buttons))
        return {"message_id": len(self.sent)}

    def edit(self, msg_id, text, buttons=None):
        self.edits.append((msg_id, text))

    def send_photo(self, path, caption="", buttons=None, chat=None):
        size = Path(path).stat().st_size
        self.photos.append((str(path), caption, size))
        keep = TMP / f"photo{len(self.photos)}{Path(path).suffix}"
        shutil.copy(path, keep)
        return {"message_id": 99}

    def text(self):
        return "\n".join(t for t, _ in self.sent)


class FakeDaemon:
    def __init__(self):
        self.bot = FakeBot()
        self.net = {"mode": "wifi", "detected": "wifi"}
        self.away = False
        self.locked = False
        self.manual_away_at = None
        self.answered = []

    def send_status(self):
        self.bot.send("STATUS")

    def offer_undo(self):
        self.bot.send("UNDO")

    def refresh_network(self):
        pass

    def build_card(self, since):
        return {"total": 0, "text": ""}

    def answer_question(self, qid, text, by):
        self.answered.append((qid, text, by))


def make_chat(tool="claude", sid="aaaa1111bbbb", cwd=None, busy=False, pct=30, title="Fix the eval"):
    cwd = str(cwd or TMP / "proj")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    st = {"tool": tool, "sid": sid, "status": "ok", "cwd": cwd, "pct": pct, "tokens": pct * 10000, "window": 1000000,
          "updated": time.time(), "title": title, "busy": busy, "first": time.time() - 600}
    relay.write_json(relay.session_state_path(tool, sid), st)
    return st


class Base(unittest.TestCase):
    def setUp(self):
        for d in ("state", "flags", "notify", "desktop", "questions", "approvals", "jobs", "night"):
            shutil.rmtree(HOME / d, ignore_errors=True)
        for f in ("inbox.json", "rules.json", "events.jsonl", "watchers.json", "telegram_state.json", "night.json",
                  "claude_limits.json", "offline.json"):
            (HOME / f).unlink(missing_ok=True)
        self.d = FakeDaemon()
        self.r = remote.Remote(self.d)
        self.started = []
        self.r.phone.start = lambda *a, **k: self.started.append((a, k)) or True  # no real sessions
        relay.write_json(HOME / "editor_heartbeat.json", {"ts": time.time()})

    def say(self, text, reply_to=None):
        msg = {"text": text}
        if reply_to:
            msg["reply_to_message"] = {"message_id": reply_to}
        self.r.handle(msg)
        return self.d.bot.text()


class TestMessagingAChat(Base):
    def test_working_chat_gets_the_message_injected(self):
        st = make_chat(busy=True)
        self.say("use a smaller batch size")
        items = smart.inbox_list("queued")
        self.assertEqual(items[0]["kind"], "message")
        self.assertEqual(items[0]["target"], st["sid"][:8])
        self.assertFalse(self.started)
        self.assertIn("next step", self.d.bot.text())

    def test_idle_chat_runs_a_phone_session_that_continues_it(self):
        st = make_chat(busy=False)
        self.say("summarise what you changed")
        (args, kw), = self.started
        self.assertEqual(kw["st"]["sid"], st["sid"])
        self.assertIn("summarise", args[1])

    def test_full_chat_starts_fresh_from_its_handoff(self):
        st = make_chat(pct=70)
        st.update(status="done", handoff_path=str(TMP / "h.md"))
        (TMP / "h.md").write_text("# Handoff: x\n## Goal\nship it\n## Status\n-\n## Next steps\n1. go\n" + "y" * 300)
        relay.write_json(relay.session_state_path("claude", st["sid"]), st)
        self.say("continue please")
        (args, kw), = self.started
        self.assertTrue(kw["fresh"])
        self.assertIn("ship it", args[1])

    def test_chats_picker_and_reply_targeting(self):
        make_chat(sid="aaaa1111bbbb", title="First")
        make_chat(sid="cccc2222dddd", title="Second", busy=True)
        self.say("/chats")
        self.assertIn("First", self.d.bot.text())
        self.r.on_button("t:aaaa1111", 5)
        self.assertEqual(remote.tstate()["target"], "aaaa1111bbbb")
        # replying to a notification that belongs to the other chat targets that chat
        remote.tset(msg_map={"77": "cccc2222dddd"})
        self.say("hello second", reply_to=77)
        self.assertEqual(smart.inbox_list("queued")[0]["target"], "cccc2222")


class TestIdeasAndRules(Base):
    def test_drop_queues_for_the_active_project(self):
        make_chat()
        self.say("/drop fix the README typo")
        it = smart.inbox_list("queued")[0]
        self.assertEqual((it["kind"], it["text"]), ("idea", "fix the README typo"))
        self.assertIn("Queued", self.d.bot.text())

    def test_idle_ideas_are_started_after_the_wait(self):
        make_chat(busy=False)
        smart.inbox_add("add a docstring", TMP / "proj", kind="idea")
        data = relay.read_json(smart.INBOX)
        data["items"][0]["ts"] -= 120
        relay.write_json(smart.INBOX, data)
        self.r.last_tick = 0
        self.r.tick()
        (args, kw), = self.started
        self.assertTrue(kw["fresh"])
        self.assertIn("add a docstring", args[1])
        self.assertEqual(smart.inbox_list("running")[0]["text"], "add a docstring")

    def test_busy_project_keeps_ideas_queued(self):
        make_chat(busy=True)
        smart.inbox_add("later", TMP / "proj", kind="idea")
        data = relay.read_json(smart.INBOX)
        data["items"][0]["ts"] -= 120
        relay.write_json(smart.INBOX, data)
        self.r.last_tick = 0
        self.r.tick()
        self.assertFalse(self.started)

    def test_rule_pin_list_remove(self):
        make_chat()
        subprocess.run(["git", "init", "-q", str(TMP / "proj")])
        out = self.say("/rule never touch the data/ folder")
        self.assertIn("pinned", out)
        self.assertIn("data/", out)
        self.say("/rules")
        rid = smart.load_rules()["projects"][next(iter(smart.load_rules()["projects"]))][0]["id"]
        self.say(f"/unrule {rid}")
        self.assertEqual(smart.rules_for(TMP / "proj"), [])


class TestControls(Base):
    def test_pause_resume_stop_flags(self):
        st = make_chat(busy=True)
        self.say("/pause")
        self.assertTrue(smart.has_flag("pause", st["sid"]))
        self.say("/resume")
        self.assertFalse(smart.has_flag("pause", st["sid"]))
        self.say("/stop")
        self.assertTrue(smart.has_flag("stop", st["sid"]))

    def test_buttons_summary_commit_handoff(self):
        st = make_chat(busy=False)
        self.r.on_button(f"b:summary:{st['sid'][:8]}", 1)
        self.assertIn("6 short lines", self.started[-1][0][1])
        self.r.on_button(f"b:commit:{st['sid'][:8]}", 1)
        self.assertIn("Commit", self.started[-1][0][1])
        self.r.on_button(f"b:handoff:{st['sid'][:8]}", 1)
        self.assertTrue((relay.STATE / "force" / "claude" / st["sid"]).exists())

    def test_tests_button_runs_the_project_tests(self):
        proj = TMP / "tproj"
        shutil.rmtree(proj, ignore_errors=True)
        (proj / "tests").mkdir(parents=True)
        (proj / "tests" / "test_a.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n")
        subprocess.run(["git", "init", "-q", str(proj)])
        st = make_chat(cwd=proj, sid="tttt1111")
        self.r.run_tests(st)
        self.assertIn("✅", self.d.bot.text())

    def test_limit_command_switches_handoffs(self):
        self.say("/limit")
        self.assertTrue(limits.active())
        st = make_chat(tool="claude")
        st.update(handoff_path="x", sid="zzzz")
        self.assertEqual(relay.next_target(st, relay.load_config()), "agy")
        self.say("/limit off")
        self.assertFalse(limits.active())
        self.assertEqual(relay.next_target(st, relay.load_config()), "claude")


class TestSeeing(Base):
    def test_live_text_reads_the_transcript(self):
        tr = TMP / "claude_tr.jsonl"
        lines = [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me look at the failing test."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "python -m unittest"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:/p/src/app.py"}}]}}]
        tr.write_text("\n".join(json.dumps(l) for l in lines))
        st = make_chat(busy=True)
        st["transcript"] = str(tr)
        text = remote.live_text(st)
        self.assertIn("failing test", text)
        self.assertIn("python -m unittest", text)
        self.assertIn("app.py", text)
        self.assertIn("working", text)

    def test_gemini_transcript_too(self):
        tr = TMP / "agy_tr.jsonl"
        tr.write_text(json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [
            {"name": "run_command", "args": {"CommandLine": "pytest -q"}}]}))
        st = make_chat(tool="agy", sid="gggg1111", busy=True)
        st["transcript"] = str(tr)
        self.assertIn("pytest -q", remote.live_text(st))

    @unittest.skipIf(os.environ.get("SKIP_SCREEN") or sys.platform.startswith("linux"), "needs a desktop session")
    def test_screenshot_is_a_real_image(self):
        self.say("/screenshot")
        self.assertTrue(self.d.bot.photos, self.d.bot.text())
        _, caption, size = self.d.bot.photos[0]
        self.assertGreater(size, 5000)
        from PIL import Image
        im = Image.open(TMP / "photo1.jpg")
        self.assertGreater(im.width, 500)
        self.d.net["mode"] = "data"
        self.say("/screenshot")
        self.assertLess(self.d.bot.photos[1][2], size * 1.2)  # data mode is not larger

    def test_locked_screen_falls_back_to_text(self):
        make_chat(busy=True)
        self.d.locked = True
        self.say("/screenshot")
        self.assertFalse(self.d.bot.photos)
        self.assertIn("locked", self.d.bot.text())

    @unittest.skipUnless(remote.edge_exe(), "needs Edge or Chrome")
    def test_preview_of_a_local_page(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html><body style='background:#123456;color:white'><h1>Hello from the app</h1></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 18765), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.say("/preview 18765")
            self.assertTrue(self.d.bot.photos, self.d.bot.text())
            self.say("/preview 18765 mobile")
            self.assertEqual(len(self.d.bot.photos), 2)
            from PIL import Image
            wide, narrow = Image.open(TMP / "photo1.png"), Image.open(TMP / "photo2.png")
            self.assertGreater(wide.width, narrow.width)
            self.assertEqual(wide.getpixel((5, 5))[:3], (0x12, 0x34, 0x56))
        finally:
            srv.shutdown()


class TestWatchers(Base):
    def notes(self):
        return [relay.read_json(f) for f in (HOME / "notify").glob("*.json")] if (HOME / "notify").exists() else []

    def test_log_nan_crash_and_stall(self):
        log = TMP / "train.log"
        log.write_text("epoch 1 loss 0.9\nepoch 2 loss 0.7\n")
        self.say(f"/watch {log}")
        watchers.tick()
        with open(log, "a") as f:
            f.write("epoch 3 loss nan\n")
        watchers.tick()
        self.assertTrue(any("NaN" in n["text"] for n in self.notes()))
        with open(log, "a") as f:
            f.write("Traceback (most recent call last):\n  File x\nRuntimeError: CUDA out of memory\n")
        watchers.tick()
        self.assertTrue(any("crash" in n["text"] for n in self.notes()))
        # stall: pretend nothing has been written for 20 minutes
        d = watchers.load()
        d["items"][0]["last_growth"] -= 1200
        d["items"][0]["alerts"].pop("done", None)
        relay.write_json(watchers.WATCH, d)
        watchers.tick()
        self.assertTrue(any("no new output" in n["text"] for n in self.notes()))

    def test_no_alert_for_a_healthy_log(self):
        log = TMP / "ok.log"
        log.write_text("step 1 loss 1.2\n")
        watchers.add_log(log)
        watchers.tick()
        with open(log, "a") as f:
            f.write("step 2 loss 1.1\nstep 3 loss 1.0\n")
        watchers.tick()
        self.assertEqual(self.notes(), [])

    @unittest.skipUnless(os.name == "nt", "the fake kaggle CLI is a .cmd file")
    def test_kaggle_transitions_and_data_mode_button(self):
        fake = TMP / "kaggle.cmd"
        state = TMP / "kstate.txt"
        state.write_text("RUNNING")
        fake.write_text(f'@echo off\r\nset /p S=<"{state}"\r\necho user/nb has status "KernelWorkerStatus.%S%"\r\n')
        item = watchers.add_kaggle("user/nb", str(TMP))  # finds no CLI on its own
        self.assertIsNone(item)
        d = {"id": "kg1", "kind": "kaggle", "slug": "user/nb", "name": "nb", "exe": str(fake), "cwd": str(TMP),
             "active": True, "alerts": {}, "status": None, "next_check": 0}
        watchers.tick_kaggle(d, "data")
        self.assertTrue(any("started running" in n["text"] for n in self.notes()))
        state.write_text("COMPLETE")
        d["next_check"] = 0
        watchers.tick_kaggle(d, "data")
        done = [n for n in self.notes() if "finished" in n["text"]][0]
        self.assertEqual(done["buttons"][0][0][1], "kg:kg1")  # data mode: asks before downloading
        self.assertFalse(d["active"])
        state.write_text("ERROR")
        d.update(active=True, next_check=0, status="RUNNING")
        watchers.tick_kaggle(d, "wifi")
        self.assertTrue(any("failed" in n["text"] for n in self.notes()))

    def test_kaggle_push_is_picked_up_automatically(self):
        for sub, name in ((".venv/Scripts", "kaggle.exe"), (".venv/bin", "kaggle")):
            exe = TMP / "fake" / sub
            exe.mkdir(parents=True, exist_ok=True)
            (exe / name).write_text("x")
        hit = smart.after_tool("claude", {"sid": "k"}, relay.load_config(), "Bash",
                               {"command": "kaggle kernels push -p ."}, True, "",
                               "Kernel version 3 successfully pushed. Please check progress at "
                               "https://www.kaggle.com/code/alice/my-model", str(TMP / "fake"), "k")
        self.assertIsNone(hit)
        items = watchers.load()["items"]
        self.assertEqual(items[0]["slug"], "alice/my-model")


class TestLimits(Base):
    def test_limit_message_detected_and_transcript_digested(self):
        tr = TMP / "lim.jsonl"
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        rows = [
            {"type": "user", "message": {"content": "Please refactor the eval pipeline"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Starting with the loader."},
                                                           {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}},
            {"type": "assistant", "isApiErrorMessage": True, "timestamp": ts,
             "message": {"model": "<synthetic>", "content": [{"type": "text",
                                                               "text": "5-hour limit reached ∙ resets 3pm"}]}}]
        tr.write_text("\n".join(json.dumps(r) for r in rows))
        st = make_chat(busy=True)
        st["transcript"] = str(tr)
        seen = set()
        hit = limits.scan([st], seen)
        self.assertIsNotNone(hit)
        self.assertIn("resets 3pm", hit[1])
        self.assertGreater(limits.parse_reset(hit[1]), time.time())
        seen.add(hit[2])
        self.assertIsNone(limits.scan([st], seen))
        dig = limits.digest(str(tr))
        self.assertIn("refactor the eval pipeline", dig)
        self.assertIn("Edit", dig)

    def test_old_errors_are_ignored(self):
        tr = TMP / "lim_old.jsonl"
        tr.write_text(json.dumps({"type": "assistant", "isApiErrorMessage": True, "timestamp": "2020-01-01T00:00:00.000Z",
                                  "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "usage limit reached"}]}}))
        st = make_chat()
        st["transcript"] = str(tr)
        self.assertIsNone(limits.scan([st], set()))


class TestAsk(Base):
    def test_answer_flows_back_to_the_agent(self):
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("a", ask.ask_user("Keep old or new API?", ["old", "new"], "old")))
        t.start()
        end = time.time() + 10
        qid = None
        while time.time() < end and not qid:
            fs = list((HOME / "questions").glob("*.json")) if (HOME / "questions").exists() else []
            qid = fs[0].stem if fs else None
            time.sleep(0.1)
        self.assertTrue(ask.answer(qid, "new", "test"))
        t.join(10)
        self.assertEqual(out["a"], "The user answered: new")

    def test_unreachable_user_gets_a_safe_fallback_at_once(self):
        (HOME / "editor_heartbeat.json").unlink(missing_ok=True)
        t0 = time.time()
        res = ask.ask_user("Delete the cache?", ["yes", "no"], "no")
        self.assertLess(time.time() - t0, 3)
        self.assertIn("safest", res)
        self.assertIn("no", res)

    def test_night_shift_never_blocks_on_a_question(self):
        os.environ["CONTEXT_RELAY_NIGHT"] = "1"
        try:
            self.assertIn("asleep", ask.ask_user("x?"))
        finally:
            del os.environ["CONTEXT_RELAY_NIGHT"]

    def test_telegram_buttons_and_free_text(self):
        remote.tset(awaiting_text="q1")
        self.say("use the second one")
        self.assertEqual(self.d.answered, [("q1", "use the second one", "Telegram")])


class TestNightShift(Base):
    def test_queue_schedule_report(self):
        make_chat()
        self.assertIn("empty", self.say("/night"))
        self.say("/night add refactor the loader")
        self.say("/night add gemini write docstrings")
        t = nightshift.load()["tasks"]
        self.assertEqual([x["kind"] for x in t], ["claude", "agy"])
        out = nightshift.telegram("start 23:30", str(TMP / "proj"))
        self.assertIn("23:30", out)
        d = nightshift.load()
        self.assertEqual(d["state"], "scheduled")
        self.assertGreater(d["start_at"], time.time() - 5)
        nightshift.telegram("clear", None)
        self.assertEqual(nightshift.load()["tasks"], [])

    def test_report_text(self):
        nightshift.add("claude", "refactor the loader", TMP / "proj")
        d = nightshift.load()
        d["tasks"][0].update(status="done", started=time.time() - 3600, finished=time.time(), files=4, note="2 session(s)")
        d["started"] = time.time() - 3700
        relay.write_json(nightshift.FILE, d)
        rep = nightshift.build_report()
        self.assertIn("refactor the loader", rep)
        self.assertIn("4 file(s) changed", rep)
        self.assertIn("1/1 tasks finished", rep)


class TestStatsAndCard(Base):
    def test_stats_build_and_chain(self):
        proj = TMP / "sproj"
        shutil.rmtree(proj, ignore_errors=True)
        proj.mkdir()
        subprocess.run(["git", "init", "-q", str(proj)])
        (proj / "a.py").write_text("x=1\n")
        a = make_chat(sid="chain-a-1111", cwd=proj, title="First session")
        b = make_chat(sid="chain-b-2222", cwd=proj, title="Second session")
        guard.snapshot(str(proj), "turn: one", "claude", "chain-a-1111")
        (proj / "a.py").write_text("x=1\ny=2\nz=3\n")
        guard.snapshot(str(proj), "turn: two", "claude", "chain-b-2222")
        (proj / "b.py").write_text("print(1)\n")
        with open(HOME / "chain.jsonl", "w") as f:
            f.write(json.dumps({"ts": time.time(), "req": "r1", "from_sid": "chain-a-1111", "tool": "claude",
                                "to_tool": "claude", "cwd": str(proj), "handoff": "h.md", "why": "context"}) + "\n")
            f.write(json.dumps({"ts": time.time(), "req": "r1", "to_sid": "chain-b-2222"}) + "\n")
        s = stats.build()
        self.assertEqual(len(s["chats"]), 2)
        chain = s["chains"][0]
        self.assertEqual([n["title"] for n in chain["nodes"]], ["First session", "Second session"])
        self.assertGreaterEqual(chain["nodes"][0]["added"], 2)  # session one added two lines
        self.assertEqual(chain["nodes"][1]["files"], 1)  # session two added b.py
        self.assertEqual(s["totals"]["handoffs_week"], 1)

    def test_gemini_days_and_time_saved(self):
        jd = HOME / "jobs" / "j1"
        relay.write_json(jd / "job.json", {"kind": "dataset", "title": "t"})
        now = time.time()
        relay.write_json(jd / "status.json", {"done": 10, "started": now - 600, "finished": now, "state": "done"})
        relay.write_json(jd / "report.json", {"annotated": 500})
        series, saved = stats.gemini_days(relay.load_config())
        self.assertEqual(series[-1]["records"], 500)
        self.assertAlmostEqual(saved, 500 * 20 - 600, delta=5)

    def test_away_card(self):
        from context_relay import daemon
        d = daemon.Daemon()
        since = time.time() - 600
        smart.event("turn_done", "x finished", project="my-app")
        smart.event("turn_done", "y finished", project="web")
        smart.event("approval", "danger gate: force push", verdict="denied")
        smart.inbox_add("later", TMP / "proj", kind="idea")
        card = d.build_card(since)
        self.assertIn("2 turns finished", card["plain"])
        self.assertIn("1 approval", card["plain"])
        self.assertIn("queued idea", card["plain"])


if __name__ == "__main__":
    unittest.main()
