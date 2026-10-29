"""End-to-end tests of the hook logic: run the real hooks as subprocesses against a throwaway home folder.

    python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _env import HOME, ROOT, TMP  # noqa: E402  (sets CONTEXT_RELAY_HOME before relay is imported)

from context_relay import guard, relay, smart  # noqa: E402



def hook(kind, event, extra_env=None, **fields):
    """Run one hook the way the agent would; returns (parsed stdout or None)."""
    env = dict(os.environ, **(extra_env or {}))
    for k in ("CONTEXT_RELAY_PHONE", "CONTEXT_RELAY_NIGHT", "CONTEXT_RELAY_AUTOPILOT"):
        if not (extra_env and k in extra_env):
            env.pop(k, None)
    base = [sys.executable, "-m", "context_relay"]
    args = base + ["claude-hook"] if kind == "claude" else base + ["agy-hook", event]
    payload = {"hook_event_name": event, **fields}
    r = subprocess.run(args, input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=120)
    return json.loads(r.stdout) if r.stdout.strip() else None


def beat():
    relay.write_json(HOME / "editor_heartbeat.json", {"ts": time.time()})


def answer_first(verdict, wait=15):
    """Play the user: answer the first pending approval."""
    end = time.time() + wait
    while time.time() < end:
        beat()
        for f in (HOME / "approvals").glob("*.json") if (HOME / "approvals").exists() else []:
            a = relay.read_json(f, {})
            if a.get("status") == "pending":
                a.update(status=verdict, by="test")
                relay.write_json(f, a)
                return a
        time.sleep(0.2)
    return None


def project(name="proj"):
    p = TMP / name
    shutil.rmtree(p, ignore_errors=True)
    (p / "src").mkdir(parents=True)
    (p / "data").mkdir()
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    (p / "src" / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(p), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(p), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                   capture_output=True)
    return p


class Base(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        for d in ("flags", "approvals", "questions", "notify", "state"):
            shutil.rmtree(HOME / d, ignore_errors=True)
        for f in ("inbox.json", "rules.json", "checks_baseline.json", "events.jsonl", "watchers.json"):
            (HOME / f).unlink(missing_ok=True)
        beat()

    def sid(self):
        return "sess-" + self.id().split(".")[-1][-12:]


class TestPauseStop(Base):
    def pre(self, cwd, tool="Bash", **inp):
        return hook("claude", "PreToolUse", session_id=self.sid(), cwd=str(cwd), tool_name=tool,
                    tool_input=inp or {"command": "ls"})

    def test_stop_denies_and_new_prompt_clears_it(self):
        p = project()
        smart.set_flag("stop", self.sid())
        out = self.pre(p)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("STOP", out["hookSpecificOutput"]["permissionDecisionReason"])
        hook("claude", "UserPromptSubmit", session_id=self.sid(), cwd=str(p), prompt="carry on")
        self.assertIsNone(self.pre(p))

    def test_pause_holds_until_resumed(self):
        p = project()
        smart.set_flag("pause", self.sid())
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("out", self.pre(p)))
        t.start()
        t.join(2.5)
        self.assertTrue(t.is_alive(), "the tool call should be held while paused")
        smart.set_flag("pause", self.sid(), on=False)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertIsNone(result["out"])


class TestRules(Base):
    def test_rule_extracts_protected_path_and_blocks_edits(self):
        p = project()
        rule = smart.add_rule("never touch the data/ folder", p)
        self.assertEqual(rule["protect"], ["data/"])
        sid = self.sid()
        res = {}
        t = threading.Thread(target=lambda: res.setdefault("out", hook(
            "claude", "PreToolUse", session_id=sid, cwd=str(p), tool_name="Edit",
            tool_input={"file_path": str(p / "data" / "x.csv"), "old_string": "a", "new_string": "b"})))
        t.start()
        a = answer_first("denied")
        t.join(30)
        self.assertEqual(a["kind"], "rule")
        self.assertIn("pinned rule", res["out"]["hookSpecificOutput"]["permissionDecisionReason"])

    def test_other_files_and_reads_are_free(self):
        p = project()
        smart.add_rule("never touch the data/ folder", p)
        sid = self.sid()
        self.assertIsNone(hook("claude", "PreToolUse", session_id=sid, cwd=str(p), tool_name="Edit",
                               tool_input={"file_path": str(p / "src" / "app.py")}))
        self.assertIsNone(hook("claude", "PreToolUse", session_id=sid, cwd=str(p), tool_name="Bash",
                               tool_input={"command": "cat data/old.csv"}))

    def test_bash_mutation_of_protected_path_asks(self):
        p = project()
        smart.add_rule("do not delete anything in data/", p)
        res = {}
        t = threading.Thread(target=lambda: res.setdefault("out", hook(
            "claude", "PreToolUse", session_id=self.sid(), cwd=str(p), tool_name="Bash",
            tool_input={"command": "mv data/old.csv /tmp/x"})))
        t.start()
        answer_first("approved")
        t.join(30)
        self.assertIsNone(res["out"], "approved by the user: the call goes through")

    def test_gemini_file_tool_is_gated_too(self):
        p = project()
        smart.add_rule("never edit config.yaml", p)
        res = {}
        t = threading.Thread(target=lambda: res.setdefault("out", hook(
            "agy", "PreToolUse", conversationId="g1", workspacePaths=[str(p)],
            toolCall={"name": "write_to_file", "args": {"TargetFile": str(p / "config.yaml")}})))
        t.start()
        answer_first("denied")
        t.join(30)
        self.assertEqual(res["out"]["decision"], "deny")

    def test_session_start_carries_the_rules(self):
        p = project()
        smart.add_rule("always write docstrings", p)
        out = hook("claude", "SessionStart", session_id=self.sid(), cwd=str(p), source="compact")
        self.assertIn("always write docstrings", out["hookSpecificOutput"]["additionalContext"])
        out = hook("agy", "SessionStart", conversationId="g2", workspacePaths=[str(p)], transcriptPath="x")
        self.assertIn("always write docstrings", out["injectSteps"][0]["systemMessage"]["systemMessage"])


class TestStuck(Base):
    def fail(self, p, n, err="Exit code 1\nModuleNotFoundError: No module named 'foo'"):
        out = None
        for _ in range(n):
            out = hook("claude", "PostToolUseFailure", session_id=self.sid(), cwd=str(p), tool_name="Bash",
                       tool_input={"command": "python run.py"}, error=err)
        return out

    def test_nudge_at_three_then_escalate_at_six(self):
        p = project()
        out = self.fail(p, 2)
        self.assertIsNone(out)
        out = self.fail(p, 1)
        self.assertIn("going in circles", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.fail(p, 1))  # no repeat nudge
        self.fail(p, 1)
        out = self.fail(p, 1)
        self.assertEqual(out["decision"], "block")
        self.assertIn("going in circles", out["reason"])  # a handoff to a fresh session is ordered
        notes = [relay.read_json(f) for f in (HOME / "notify").glob("*.json")]
        self.assertTrue(any(n["kind"] == "stuck" for n in notes))

    def test_numbers_do_not_make_errors_look_different(self):
        p = project()
        out = None
        for i in range(3):
            out = hook("claude", "PostToolUseFailure", session_id=self.sid(), cwd=str(p), tool_name="Bash",
                       tool_input={"command": "x"}, error=f"Exit code 1\nError at line {i * 7 + 3}: boom 0x{i}ff")
        self.assertIn("going in circles", out["hookSpecificOutput"]["additionalContext"])

    def test_edit_loop_on_one_file(self):
        p = project()
        out = None
        for _ in range(6):
            out = hook("claude", "PostToolUse", session_id=self.sid(), cwd=str(p), tool_name="Edit",
                       tool_input={"file_path": str(p / "src" / "app.py")}, tool_response={})
        self.assertIn("src", out["hookSpecificOutput"]["additionalContext"].lower() + "src")
        self.assertIn("going in circles", out["hookSpecificOutput"]["additionalContext"])

    def test_gemini_nudge_arrives_at_next_model_call(self):
        p = project()
        for _ in range(3):
            hook("agy", "PostToolUse", conversationId="g3", workspacePaths=[str(p)], transcriptPath="x",
                 toolCall={"name": "run_command", "args": {"CommandLine": "python run.py"}}, error="exit status 1: boom")
        out = hook("agy", "PreInvocation", conversationId="g3", workspacePaths=[str(p)], transcriptPath="x",
                   invocationNum=1, initialNumSteps=5)
        self.assertIn("going in circles", json.dumps(out))


class TestInbox(Base):
    def test_message_is_injected_mid_turn_idea_waits_for_the_stop(self):
        p = project()
        sid = self.sid()
        smart.inbox_add("use a smaller batch", p, kind="message", target=sid[:8])
        smart.inbox_add("also fix the README", p, kind="idea")
        out = hook("claude", "PostToolUse", session_id=sid, cwd=str(p), tool_name="Bash", tool_input={"command": "ls"},
                   tool_response={"stdout": ""})
        self.assertIn("use a smaller batch", out["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("README", out["hookSpecificOutput"]["additionalContext"])
        out = hook("claude", "Stop", session_id=sid, cwd=str(p), last_assistant_message="done")
        self.assertEqual(out["decision"], "block")
        self.assertIn("also fix the README", out["reason"])
        out = hook("claude", "Stop", session_id=sid, cwd=str(p), last_assistant_message="done")
        self.assertIsNone(out)  # delivered once

    def test_idea_for_another_project_is_not_delivered_here(self):
        a, b = project("pa"), project("pb")
        smart.inbox_add("only for pb", b, kind="idea")
        out = hook("claude", "Stop", session_id=self.sid(), cwd=str(a), last_assistant_message="done")
        self.assertIsNone(out)

    def test_turn_done_pings_with_action_buttons_but_not_when_unattended(self):
        p = project()
        hook("claude", "Stop", session_id=self.sid(), cwd=str(p), last_assistant_message="All good")
        notes = [relay.read_json(f) for f in (HOME / "notify").glob("*.json")]
        self.assertTrue(any(n["kind"] == "turn_done" and "commit" in (n["actions"] or []) for n in notes))
        shutil.rmtree(HOME / "notify")
        hook("claude", "Stop", extra_env={"CONTEXT_RELAY_PHONE": "1"}, session_id=self.sid() + "b", cwd=str(p),
             last_assistant_message="x")
        self.assertFalse(list((HOME / "notify").glob("*.json")) if (HOME / "notify").exists() else [])


class TestChecks(Base):
    def make(self, passing):
        p = project("chk")
        (p / "tests").mkdir(exist_ok=True)
        (p / "tests" / "test_x.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(%s, 2)\n" %
            ("1 + 1" if passing else "1 + 2"))
        subprocess.run(["git", "-C", str(p), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(p), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "t"],
                       capture_output=True)
        return p

    def turn(self, p, sid):
        guard.snapshot(str(p), "turn: work", "claude", sid)
        (p / "src" / "app.py").write_text(f"x = {time.time()}\n")  # the agent edits code

    def stop(self, p, sid):
        return hook("claude", "Stop", session_id=sid, cwd=str(p), last_assistant_message="done")

    def test_failures_go_back_twice_then_are_accepted_and_not_repeated(self):
        p = self.make(passing=False)
        sid = self.sid()
        self.turn(p, sid)
        out = self.stop(p, sid)
        self.assertEqual(out["decision"], "block")
        self.assertIn("checks fail", out["reason"])
        self.assertIn("test_a", out["reason"])
        out = self.stop(p, sid)
        self.assertEqual(out["decision"], "block")  # round 2: the agent had a second chance
        out = self.stop(p, sid)
        self.assertIsNone(out)  # out of tries: accepted
        notes = [relay.read_json(f) for f in (HOME / "notify").glob("*.json")]
        self.assertTrue(any(n["kind"] == "checks" for n in notes))
        # a later turn that does not add new failures is not blocked by the old red test
        sid2 = sid + "2"
        self.turn(p, sid2)
        self.assertIsNone(self.stop(p, sid2))

    def test_green_run_passes_through(self):
        p = self.make(passing=True)
        sid = self.sid()
        self.turn(p, sid)
        self.assertIsNone(self.stop(p, sid))
        kinds = [json.loads(l)["text"] for l in (HOME / "events.jsonl").read_text().splitlines()
                 if json.loads(l)["kind"] == "checks"]
        self.assertIn("passed", kinds)

    def test_no_code_changes_means_no_run(self):
        p = self.make(passing=False)
        sid = self.sid()
        guard.snapshot(str(p), "turn: chat only", "claude", sid)
        (p / "notes.md").write_text("hello\n")
        self.assertIsNone(self.stop(p, sid))


class TestHandoffStillWorks(Base):
    def test_threshold_triggers_handoff_and_phone_runs_skip_it(self):
        p = project()
        tr = TMP / "tr.jsonl"
        tr.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
            "input_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 700000}}}) + "\n")
        out = hook("claude", "Stop", session_id=self.sid(), cwd=str(p), transcript_path=str(tr))
        self.assertEqual(out["decision"], "block")
        self.assertIn("handoff", out["reason"].lower())
        out = hook("claude", "Stop", extra_env={"CONTEXT_RELAY_PHONE": "1"}, session_id=self.sid() + "p", cwd=str(p),
                   transcript_path=str(tr))
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
