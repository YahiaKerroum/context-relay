"""The installer, run against a fake home directory (it never touches real settings)."""
import io
import json
import os
import shutil
import stat
import sys
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from _env import HOME, TMP  # noqa: F401

from context_relay import installer, relay  # noqa: E402


def run(*args, stdin=None):
    buf = io.StringIO()
    old_in = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(buf):
            code = installer.run(list(args))
    finally:
        sys.stdin = old_in
        installer.DRY = False
    return code, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = TMP / "fakehome"
        shutil.rmtree(self.fake, ignore_errors=True)
        self.fake.mkdir()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "PATH")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.fake)
        git_dir = os.path.dirname(shutil.which("git") or sys.executable)
        # no claude, agy or editors on this PATH, but keep git
        os.environ["PATH"] = str(self.fake / "nothing") + os.pathsep + git_dir
        shutil.rmtree(HOME, ignore_errors=True)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestClaudeHooks(Base):
    def make_settings(self):
        p = self.fake / ".claude" / "settings.json"
        p.parent.mkdir(parents=True)
        own = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-own-linter"}]}]},
               "statusLine": {"type": "command", "command": "my-statusline"}, "model": "opus"}
        p.write_text(json.dumps(own))
        return p

    def test_install_keeps_your_hooks_and_statusline_and_is_reversible(self):
        p = self.make_settings()
        code, out = run("install", "--no-extension", "--no-daemon", "--no-antigravity", "--no-telegram", "--claude-window", "1m")
        self.assertEqual(code, 0, out)
        s = json.loads(p.read_text())
        cmds = [h["command"] for e in s["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertIn("my-own-linter", cmds)  # yours is still there
        self.assertTrue(any("context_relay" in c for c in cmds))
        self.assertEqual(s["statusLine"]["command"], "my-statusline")  # not hijacked
        self.assertEqual(s["model"], "opus")
        self.assertEqual(sorted(s["hooks"]), ["PostToolUse", "PostToolUseFailure", "PreToolUse", "SessionStart",
                                              "Stop", "UserPromptSubmit"])
        self.assertTrue(list(p.parent.glob("settings.backup-*.json")), "a backup is made before editing")
        self.assertEqual(json.loads((HOME / "config.json").read_text())["claude_window_tokens"], 1_000_000)
        self.assertEqual(json.loads((HOME / "install.json").read_text())["args"], ["-m", "context_relay"])
        # installing twice does not duplicate anything
        run("install", "--no-extension", "--no-daemon", "--no-antigravity", "--no-telegram")
        s2 = json.loads(p.read_text())
        self.assertEqual(len(s2["hooks"]["Stop"]), 1)
        # uninstall removes only ours
        code, _ = run("uninstall")
        s3 = json.loads(p.read_text())
        self.assertEqual([h["command"] for e in s3["hooks"]["PreToolUse"] for h in e["hooks"]], ["my-own-linter"])
        self.assertNotIn("Stop", s3.get("hooks", {}))
        self.assertEqual(s3["statusLine"]["command"], "my-statusline")
        self.assertTrue(HOME.exists(), "your data is kept unless you pass --purge")

    def test_dry_run_changes_nothing(self):
        p = self.make_settings()
        before = p.read_text()
        code, out = run("setup", "--yes", "--dry-run", "--no-extension", "--no-daemon")
        self.assertEqual(code, 0)
        self.assertIn("would:", out)
        self.assertEqual(p.read_text(), before)
        self.assertFalse(list(p.parent.glob("settings.backup-*")))
        self.assertFalse((HOME / "install.json").exists())

    def test_missing_claude_cli_is_a_note_not_a_crash(self):
        self.make_settings()
        code, out = run("install", "--no-extension", "--no-daemon", "--no-antigravity", "--no-telegram")
        self.assertEqual(code, 0)
        self.assertIn("MCP tools for Claude", out)  # attempted, reported as not possible, setup carried on
        self.assertIn("Claude Code CLI not found", out)
        self.assertIn("Claude Code hooks", out)

    def test_nothing_installed_is_handled(self):
        code, out = run("install", "--no-extension", "--no-daemon", "--no-telegram")
        self.assertEqual(code, 0)
        self.assertIn("Done", out)
        self.assertFalse((self.fake / ".claude").exists())  # we do not invent tools you do not have


class TestAntigravity(Base):
    def test_hooks_are_unquoted_and_removable(self):
        (self.fake / ".gemini" / "config").mkdir(parents=True)
        hooks = self.fake / ".gemini" / "config" / "hooks.json"
        hooks.write_text(json.dumps({"other-tool": {"enabled": True}}))
        run("install", "--no-extension", "--no-daemon", "--no-claude", "--no-telegram")
        h = json.loads(hooks.read_text())
        self.assertIn("other-tool", h)
        mine = h["context-relay"]
        cmd = mine["Stop"][0]["command"]
        self.assertNotIn('"', cmd)  # Antigravity mangles quotes
        self.assertIn("-m context_relay agy-hook Stop", cmd)
        self.assertEqual(mine["Stop"][0]["timeout"], 900)
        run("uninstall")
        h = json.loads(hooks.read_text())
        self.assertEqual(list(h), ["other-tool"])


class TestHelpers(Base):
    def test_parse_window(self):
        self.assertEqual(installer.parse_window("1m"), 1_000_000)
        self.assertEqual(installer.parse_window("200k"), 200_000)
        self.assertEqual(installer.parse_window("123456"), 123456)
        self.assertEqual(installer.parse_window("auto"), "auto")

    def test_window_detection_from_transcripts(self):
        proj = self.fake / ".claude" / "projects" / "p"
        proj.mkdir(parents=True)
        line = lambda n: json.dumps({"message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": n}}})  # noqa: E731
        (proj / "a.jsonl").write_text(line(90_000) + "\n" + line(310_000) + "\n")
        self.assertGreater(installer.detect_claude_window(), 300_000)
        code, out = run("install", "--no-extension", "--no-daemon", "--no-antigravity", "--no-telegram")
        self.assertEqual(json.loads((HOME / "config.json").read_text())["claude_window_tokens"], 1_000_000)

    def test_small_transcripts_mean_a_200k_window(self):
        proj = self.fake / ".claude" / "projects" / "p"
        proj.mkdir(parents=True)
        (proj / "a.jsonl").write_text(json.dumps({"message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 90_000}}}))
        run("install", "--no-extension", "--no-daemon", "--no-antigravity", "--no-telegram")
        self.assertEqual(json.loads((HOME / "config.json").read_text())["claude_window_tokens"], 200_000)

    def test_vsix_is_a_valid_package(self):
        out = installer.build_vsix(TMP / "vsix")
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            self.assertIn("extension.vsixmanifest", names)
            self.assertIn("extension/package.json", names)
            self.assertIn("extension/extension.js", names)
            pkg = json.loads(z.read("extension/package.json"))
        self.assertEqual(pkg["name"], "context-relay")
        from context_relay import __version__
        self.assertEqual(pkg["version"], __version__, "extension and package versions move together")

    def test_doctor_runs(self):
        code, out = run("doctor")
        self.assertIn("Python", out)
        self.assertIn("git", out)
        self.assertEqual(code, 0, out)

    def test_status_and_version(self):
        self.assertIn("Context Relay", run("status")[1])
        self.assertIn("context-relay", run("version")[1])

    def test_skill_and_extension_ship_with_the_package(self):
        self.assertTrue((relay.ROOT / "skills" / "coordinate" / "SKILL.md").exists())
        self.assertTrue((relay.ROOT / "extension" / "package.json").exists())


class TestTelegramSetup(Base):
    def setUp(self):
        super().setUp()
        self.real = installer.tg_api
        installer.tg_api = lambda token, method: {"username": "my_test_bot"}
        self.restart = installer.restart_daemon
        installer.restart_daemon = lambda: None

    def tearDown(self):
        installer.tg_api = self.real
        installer.restart_daemon = self.restart
        super().tearDown()

    def test_token_from_stdin_is_stored_and_a_pairing_code_issued(self):
        code, out = run("telegram", "setup", "--token-stdin", stdin="123:ABC\n")
        self.assertEqual(code, 0, out)
        self.assertIn("my_test_bot", out)
        s = json.loads((HOME / "secrets.json").read_text())
        self.assertEqual(s["telegram_token"], "123:ABC")
        pair = json.loads((HOME / "pair_code.json").read_text())["code"]
        self.assertRegex(pair, r"^\d{6}$")
        self.assertIn(pair, out)
        self.assertNotIn("123:ABC", out, "the token is never printed")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(HOME / "secrets.json").st_mode), 0o600)

    def test_bad_token_is_rejected(self):
        def boom(token, method):
            raise RuntimeError("401 Unauthorized")
        installer.tg_api = boom
        code, out = run("telegram", "setup", "--token", "nope")
        self.assertEqual(code, 1)
        self.assertIn("rejected", out)
        self.assertFalse((HOME / "secrets.json").exists())

    def test_pair_status_unpair(self):
        run("telegram", "setup", "--token", "123:ABC")
        self.assertIn("Send this code", run("telegram", "pair")[1])
        relay.write_json(HOME / "secrets.json", {"telegram_token": "123:ABC", "telegram_chat_id": 42})
        self.assertIn("Already paired", run("telegram", "pair")[1])
        self.assertIn("paired", run("telegram", "status")[1])
        run("telegram", "unpair")
        self.assertNotIn("telegram_chat_id", json.loads((HOME / "secrets.json").read_text()))
        self.assertIn("Send this code", run("telegram", "pair")[1])

    def test_new_bot_means_pairing_again(self):
        relay.write_json(HOME / "secrets.json", {"telegram_token": "old:TOKEN", "telegram_chat_id": 42})
        run("telegram", "setup", "--token", "new:TOKEN")
        s = json.loads((HOME / "secrets.json").read_text())
        self.assertEqual(s["telegram_token"], "new:TOKEN")
        self.assertNotIn("telegram_chat_id", s)


if __name__ == "__main__":
    unittest.main()
