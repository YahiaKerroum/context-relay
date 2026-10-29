"""Install, inspect and remove Context Relay: `setup`, `doctor`, `status`, `uninstall`, `telegram`, `build-vsix`.

Everything is optional and detected: Claude Code hooks need Claude Code, Gemini features need the Antigravity CLI,
the panel needs VS Code (or Cursor / Windsurf / the Antigravity IDE), the phone needs a Telegram bot. Whatever the
machine lacks is skipped with a note, never an error. `--dry-run` shows what would change without touching anything.
"""
import argparse
import getpass
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__, relay
from .relay import HOME, ROOT, load_config, read_json, write_json

DRY = False
WIN = os.name == "nt"
OK, NO, WARN = "✓", "✗", "!"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AGY_TOOLS = "run_command|write_to_file|replace_file_content|multi_replace_file_content|edit_file|write_file"


# ---------------------------------------------------------------- small helpers

def say(msg=""):
    print(msg, flush=True)


def act(desc, fn):
    """Run one install step; in dry-run mode only describe it. A failing step never stops the others."""
    if DRY:
        say(f"  would: {desc}")
        return True
    try:
        res = fn()
        say(f"  {OK} {desc}" + (f" ({res})" if isinstance(res, str) and res else ""))
        return True
    except Exception as e:  # noqa: BLE001
        say(f"  {NO} {desc}: {str(e).strip()[:200]}")
        return False


def confirm(question, default=True, assume_yes=False):
    if assume_yes or not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"{question} {suffix} ").strip().lower()
    except EOFError:
        print()
        return default
    return default if not ans else ans.startswith("y")


def run_quiet(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def update_config(changes):
    """Merge into config.json (only the keys you set; everything else keeps following the defaults)."""
    cfg = read_json(HOME / "config.json", {}) or {}
    for k, v in changes.items():
        cur, parts = cfg, k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    write_json(HOME / "config.json", cfg)


# ---------------------------------------------------------------- detection

def find_claude():
    return shutil.which("claude")


def find_agy():
    p = shutil.which("agy")
    if p:
        return p
    for name in ("agy.exe", "agy"):
        q = Path.home() / ".gemini" / "bin" / name
        if q.exists():
            return str(q)
    return None


def editor_clis():
    """[(label, cli path)] for every supported editor found on this machine."""
    found = []
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        ("VS Code", "code", [r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
                             os.path.join(local, "Programs", "Microsoft VS Code", "bin", "code.cmd"),
                             "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"]),
        ("Cursor", "cursor", [os.path.join(local, "Programs", "cursor", "resources", "app", "bin", "cursor.cmd"),
                              "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"]),
        ("Windsurf", "windsurf", [os.path.join(local, "Programs", "Windsurf", "bin", "windsurf.cmd")]),
        ("Antigravity IDE", "antigravity-ide", [os.path.join(local, "Programs", "Antigravity IDE", "bin", "antigravity-ide.cmd")]),
    ]
    for label, name, extra in candidates:
        p = shutil.which(name) or next((e for e in extra if e and os.path.exists(e)), None)
        if p:
            found.append((label, p))
    return found


def detect_claude_window():
    """The biggest context seen in your recent Claude transcripts: above 200k means you have the 1M window."""
    best = 0
    files = sorted(glob.glob(str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl")),
                   key=lambda f: os.path.getmtime(f), reverse=True)[:25]
    for f in files:
        try:
            for line in relay.tail_lines(f, 1_500_000):
                if '"usage"' not in line:
                    continue
                u = (json.loads(line).get("message") or {}).get("usage") or {}
                best = max(best, u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                           + u.get("cache_read_input_tokens", 0))
        except (OSError, ValueError):
            continue
    return best


def have_module(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def doctor():
    say(f"Context Relay {__version__} · doctor\n")
    problems = 0
    py_ok = sys.version_info >= (3, 10)
    say(f"  {OK if py_ok else NO} Python {sys.version.split()[0]}" + ("" if py_ok else "  (3.10 or newer is required)"))
    problems += not py_ok
    git = shutil.which("git")
    say(f"  {OK if git else NO} git" + ("" if git else "  (required: undo snapshots and checks use it)"))
    problems += not git
    claude, agy = find_claude(), find_agy()
    say(f"  {OK if claude else WARN} Claude Code CLI" + (f"  {claude}" if claude else "  (not found: Claude hooks, MCP tools and phone sessions need it)"))
    say(f"  {OK if agy else WARN} Antigravity CLI (agy)" + (f"  {agy}" if agy else "  (optional: enables Gemini delegation and Gemini chats)"))
    eds = editor_clis()
    if eds:
        for label, p in eds:
            say(f"  {OK} {label}  {p}")
    else:
        say(f"  {WARN} no supported editor found (VS Code, Cursor, Windsurf, Antigravity IDE): the panel and auto-open need one")
    say(f"  {OK if have_module('PIL') else WARN} Pillow" + ("" if have_module("PIL") else "  (optional: pip install pillow, for /screenshot)"))
    from . import remote
    say(f"  {OK if remote.edge_exe() else WARN} Edge/Chrome" + ("" if remote.edge_exe() else "  (optional: for /preview of your running app)"))
    say(f"  {OK if WIN else WARN} platform: {sys.platform}" + ("" if WIN else "  (Windows is fully supported; on macOS/Linux screenshots, lock detection and Enter-to-send are unavailable)"))
    window = detect_claude_window()
    if window:
        say(f"  {OK} largest Claude context seen: {window:,} tokens -> window {'1M' if window > 200_000 else '200k'}")
    say("")
    say("Ready." if not problems else f"{problems} blocking problem(s) above.")
    return 1 if problems else 0


# ---------------------------------------------------------------- hook commands

def short_path(p):
    """Antigravity hands the command to cmd.exe with quotes escaped, so it must have no spaces."""
    p = str(p)
    if " " in p and WIN:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024):
            p = buf.value
    return Path(p).as_posix()


def hook_cmd(*args, quoted=True):
    py = Path(sys.executable)
    if quoted:
        return " ".join([f'"{py.as_posix()}"', "-m", "context_relay", *args])
    return " ".join([short_path(py), "-m", "context_relay", *args])


def is_ours(entry):
    s = json.dumps(entry)
    return "context_relay" in s or ("context-relay" in s and "relay.py" in s)  # the second form: older installs


def backup(p):
    if p.exists():
        shutil.copy2(p, p.with_name(f"{p.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{p.suffix}"))


# ---------------------------------------------------------------- Claude Code

def install_claude():
    p = Path.home() / ".claude" / "settings.json"
    s = read_json(p, {}) or {}
    backup(p)
    hooks = s.setdefault("hooks", {})
    for ev, matcher, timeout in (("PostToolUse", "*", 15), ("PostToolUseFailure", "*", 15),
                                 ("Stop", None, 900),  # long: runs your tests before letting the agent finish
                                 ("SessionStart", None, 15), ("UserPromptSubmit", None, 15),
                                 ("PreToolUse", "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit", 900)):  # may wait for your OK
        lst = [e for e in hooks.get(ev, []) if not is_ours(e)]
        entry = {"hooks": [{"type": "command", "command": hook_cmd("claude-hook"), "timeout": timeout}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        lst.append(entry)
        hooks[ev] = lst
    note = ""
    if "statusLine" not in s or is_ours(s["statusLine"]):
        s["statusLine"] = {"type": "command", "command": hook_cmd("claude-statusline")}
    else:
        note = "your own statusLine was left alone"
    write_json(p, s)
    return note or str(p)


def uninstall_claude():
    p = Path.home() / ".claude" / "settings.json"
    s = read_json(p, None)
    if not s:
        return "nothing to remove"
    backup(p)
    for ev in list(s.get("hooks", {})):
        s["hooks"][ev] = [e for e in s["hooks"][ev] if not is_ours(e)]
        if not s["hooks"][ev]:
            del s["hooks"][ev]
    if not s.get("hooks"):
        s.pop("hooks", None)
    if is_ours(s.get("statusLine", {})):
        del s["statusLine"]
    write_json(p, s)


def install_mcp():
    cli = find_claude()
    if not cli:
        raise RuntimeError("Claude Code CLI not found")
    run_quiet([cli, "mcp", "remove", "--scope", "user", "context-relay"])
    r = run_quiet([cli, "mcp", "add", "--scope", "user", "context-relay", "--", sys.executable, "-m", "context_relay", "mcp"])
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])


def uninstall_mcp():
    for cli, args in ((find_claude(), ["mcp", "remove", "--scope", "user", "context-relay"]),
                      (find_agy(), ["mcp", "remove", "context-relay"])):
        if cli:
            run_quiet([cli, *args])


def install_skill():
    dst = Path.home() / ".claude" / "skills" / "coordinate"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "skills" / "coordinate" / "SKILL.md", dst / "SKILL.md")
    return str(dst)


def uninstall_skill():
    shutil.rmtree(Path.home() / ".claude" / "skills" / "coordinate", ignore_errors=True)


# ---------------------------------------------------------------- Antigravity (Gemini)

def install_agy():
    p = Path.home() / ".gemini" / "config" / "hooks.json"
    s = read_json(p, {}) or {}
    backup(p)
    s["context-relay"] = {
        "enabled": True,
        "SessionStart": [{"type": "command", "command": hook_cmd("agy-hook", "SessionStart", quoted=False), "timeout": 15}],
        "PreInvocation": [{"type": "command", "command": hook_cmd("agy-hook", "PreInvocation", quoted=False), "timeout": 15}],
        "Stop": [{"type": "command", "command": hook_cmd("agy-hook", "Stop", quoted=False), "timeout": 900}],
        "PreToolUse": [{"matcher": AGY_TOOLS, "hooks": [
            {"type": "command", "command": hook_cmd("agy-hook", "PreToolUse", quoted=False), "timeout": 900}]}],
        "PostToolUse": [{"matcher": AGY_TOOLS, "hooks": [
            {"type": "command", "command": hook_cmd("agy-hook", "PostToolUse", quoted=False), "timeout": 15}]}],
    }
    write_json(p, s)
    return str(p)


def uninstall_agy():
    a = Path.home() / ".gemini" / "config" / "hooks.json"
    h = read_json(a, None)
    if h and "context-relay" in h:
        backup(a)
        h.pop("context-relay")
        write_json(a, h)


def install_agy_mcp():
    """Gemini gets ask_user / notify_user (not the delegation tools: it is the one being delegated to)."""
    agy = find_agy()
    if not agy:
        raise RuntimeError("Antigravity CLI not found")
    run_quiet([agy, "mcp", "remove", "context-relay"])
    r = run_quiet([agy, "mcp", "add", "context-relay", Path(sys.executable).as_posix(), "-m", "context_relay", "mcp", "ask"])
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])


# ---------------------------------------------------------------- editor extension

def build_vsix(out_dir=None):
    ext = ROOT / "extension"
    pkg = read_json(ext / "package.json")
    out = Path(out_dir or HOME / "dist")
    out.mkdir(parents=True, exist_ok=True)
    out = out / f"{pkg['name']}-{pkg['version']}.vsix"
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{pkg['name']}" Version="{pkg['version']}" Publisher="{pkg['publisher']}"/>
    <DisplayName>{pkg['displayName']}</DisplayName>
    <Description xml:space="preserve">{pkg['description']}</Description>
    <Categories>Other</Categories>
    <Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="{pkg['engines']['vscode']}"/></Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>"""
    types = ('<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension=".json" ContentType="application/json"/><Default Extension=".js" ContentType="application/javascript"/>'
             '<Default Extension=".svg" ContentType="image/svg+xml"/><Default Extension=".md" ContentType="text/markdown"/>'
             '<Default Extension=".vsixmanifest" ContentType="text/xml"/></Types>')
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("extension.vsixmanifest", manifest)
        for f in sorted(ext.rglob("*")):
            if f.is_file():
                z.write(f, f"extension/{f.relative_to(ext).as_posix()}")
    return out


def install_extension(editors):
    vsix = build_vsix()
    failed = []
    for label, cli in editors:
        r = run_quiet([cli, "--install-extension", str(vsix), "--force"], shell=cli.lower().endswith(".cmd"))
        if r.returncode:
            failed.append(f"{label}: {(r.stderr or r.stdout).strip()[:120]}")
    if failed:
        raise RuntimeError("; ".join(failed))
    return f"{', '.join(l for l, _ in editors)}; reload the window"


def uninstall_extension():
    for label, cli in editor_clis():
        run_quiet([cli, "--uninstall-extension", "local.context-relay"], shell=cli.lower().endswith(".cmd"))


# ---------------------------------------------------------------- the background daemon

def pythonw():
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return str(pyw) if pyw.exists() else sys.executable


def daemon_pid():
    d = read_json(HOME / "daemon.json", {}) or {}
    return d.get("pid") if time.time() - d.get("ts", 0) < 30 else None


def stop_daemon():
    pid = daemon_pid()
    if pid:
        relay.kill_tree(pid)
        time.sleep(1)


def start_daemon():
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if WIN else 0
    kw = {"creationflags": flags} if WIN else {"start_new_session": True}
    subprocess.Popen([pythonw(), "-m", "context_relay", "daemon"], cwd=str(HOME), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kw)


def restart_daemon():
    stop_daemon()
    start_daemon()


def install_daemon():
    HOME.mkdir(parents=True, exist_ok=True)
    note = "running now; start it yourself after a reboot: context-relay daemon"
    if WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "ContextRelay", 0, winreg.REG_SZ, f'"{pythonw()}" -m context_relay daemon')
        note = "starts at login, running now"
    restart_daemon()
    return note


def uninstall_daemon():
    stop_daemon()
    if WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, "ContextRelay")
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- Telegram

BOTFATHER = """  To connect your phone you need a Telegram bot (free, about a minute):
    1. In Telegram, open @BotFather and send:  /newbot
    2. Pick any name and a username ending in "bot".
    3. BotFather replies with a token like 123456789:AAF... — paste it below.
  The bot only ever answers the one chat you pair with a one-time code."""


def tg_api(token, method):
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", timeout=20) as r:
        return json.load(r)["result"]


def secrets_path():
    return HOME / "secrets.json"


def save_secret(**kw):
    s = read_json(secrets_path(), {}) or {}
    s.update(kw)
    write_json(secrets_path(), s)
    try:
        os.chmod(secrets_path(), 0o600)  # the token is a credential
    except OSError:
        pass


def telegram_setup(token=None, from_stdin=False, assume_yes=False):
    if from_stdin:
        token = sys.stdin.readline().strip()
    if not token:
        if not sys.stdin.isatty():
            say("No token given. Pass --token, or --token-stdin.")
            return 1
        say(BOTFATHER)
        token = getpass.getpass("  Bot token (hidden): ").strip()
    try:
        me = tg_api(token, "getMe")
    except Exception as e:  # noqa: BLE001
        say(f"  {NO} Telegram rejected that token ({str(e)[:100]}). Check it and try again.")
        return 1
    if DRY:
        say(f"  would: save the token for @{me['username']} and show a pairing code")
        return 0
    old = read_json(secrets_path(), {}) or {}
    if old.get("telegram_token") != token:
        old.pop("telegram_chat_id", None)  # a different bot: pair again
        write_json(secrets_path(), old)
    save_secret(telegram_token=token)
    paired = bool((read_json(secrets_path(), {}) or {}).get("telegram_chat_id"))
    if not paired:
        import random
        write_json(HOME / "pair_code.json", {"code": f"{random.randint(0, 999999):06d}"})
    if daemon_pid() or assume_yes:
        restart_daemon()
    say(f"  {OK} bot @{me['username']} saved")
    if paired:
        say("  already paired with a chat")
    else:
        code = read_json(HOME / "pair_code.json", {})["code"]
        say(f"  Now open https://t.me/{me['username']} in Telegram, press Start and send this code:  {code}")
        say("  (the daemon must be running: `context-relay setup` or `context-relay daemon`)")
    return 0


def telegram_cli(args):
    p = argparse.ArgumentParser(prog="context-relay telegram")
    p.add_argument("sub", choices=["setup", "pair", "status", "unpair"])
    p.add_argument("--token")
    p.add_argument("--token-stdin", action="store_true")
    ns = p.parse_args(args)
    s = read_json(secrets_path(), {}) or {}
    if ns.sub == "setup":
        return telegram_setup(ns.token, ns.token_stdin)
    if ns.sub == "pair":
        if s.get("telegram_chat_id"):
            say("Already paired.")
        else:
            code = (read_json(HOME / "pair_code.json", {}) or {}).get("code")
            say(f"Send this code to your bot: {code}" if code else "No bot set up yet: context-relay telegram setup")
        return 0
    if ns.sub == "status":
        if not s.get("telegram_token"):
            say("No bot configured. Run: context-relay telegram setup")
            return 1
        try:
            me = tg_api(s["telegram_token"], "getMe")
            say(f"Bot @{me['username']} · {'paired' if s.get('telegram_chat_id') else 'not paired yet'}"
                f" · daemon {'running' if daemon_pid() else 'NOT running'}")
        except Exception as e:  # noqa: BLE001
            say(f"Bot unreachable: {str(e)[:100]}")
            return 1
        return 0
    s.pop("telegram_chat_id", None)  # unpair
    write_json(secrets_path(), s)
    import random
    write_json(HOME / "pair_code.json", {"code": f"{random.randint(0, 999999):06d}"})
    if daemon_pid():
        restart_daemon()
    say("Unpaired. Send the new code from `context-relay telegram pair` to pair again.")
    return 0


# ---------------------------------------------------------------- setup / install

def setup(ns):
    global DRY
    DRY = ns.dry_run
    assume_yes = ns.yes
    say(f"Context Relay {__version__} · setup{' (dry run: nothing is changed)' if DRY else ''}\n")
    if sys.version_info < (3, 10) or not shutil.which("git"):
        say("Python 3.10+ and git are required. Run `context-relay doctor`.")
        return 1
    claude, agy, editors = find_claude(), find_agy(), editor_clis()
    claude_home = (Path.home() / ".claude").exists() or claude
    agy_home = (Path.home() / ".gemini").exists() or agy
    say("Found: " + ", ".join(x for x in [
        "Claude Code" if claude_home else "", "Antigravity/Gemini" if agy_home else "",
        *[l for l, _ in editors]] if x) or "Found: nothing to hook into yet (install Claude Code first)")
    say("")

    want = {
        "claude": not ns.no_claude and bool(claude_home) and confirm("Add the Claude Code hooks (relay, safety nets, MCP tools)?", True, assume_yes),
        "agy": not ns.no_antigravity and bool(agy_home) and confirm("Add the Antigravity (Gemini) hooks?", True, assume_yes),
        "ext": not ns.no_extension and bool(editors) and confirm(f"Install the editor extension ({', '.join(l for l, _ in editors)})?", True, assume_yes),
        "daemon": not ns.no_daemon and confirm("Start the background helper (Telegram, network mode, presence) and run it at login?", True, assume_yes),
    }
    changes = {}
    if want["claude"]:
        window = ns.claude_window
        if window == "auto":
            seen = detect_claude_window()
            window = 1_000_000 if seen > 200_000 else 200_000
            say(f"  Claude context window: {'1M' if window == 1_000_000 else '200k'} (detected from your recent chats; "
                f"change it with `--claude-window` or in Settings)")
        changes["claude_window_tokens"] = int(window)
    if ns.threshold:
        changes["threshold_pct"] = ns.threshold
    say("")
    say("Installing:")
    HOME.mkdir(parents=True, exist_ok=True)
    if changes:
        act("write your settings (" + ", ".join(f"{k}={v}" for k, v in changes.items()) + ")", lambda: update_config(changes))
    if not DRY:
        write_json(HOME / "install.json", {"python": sys.executable, "args": ["-m", "context_relay"], "version": __version__})
    if want["claude"]:
        act("Claude Code hooks", install_claude)
        act("MCP tools for Claude (Gemini delegation, ask_user)", install_mcp)
        act("/coordinate skill", install_skill)
    if want["agy"]:
        act("Antigravity hooks", install_agy)
        if agy:
            act("ask_user tool for Gemini", install_agy_mcp)
    if want["ext"]:
        act("editor extension", lambda: install_extension(editors))
    if want["daemon"]:
        act("background helper", install_daemon)
    if not ns.no_telegram and not DRY:
        s = read_json(secrets_path(), {}) or {}
        if not s.get("telegram_token") and sys.stdin.isatty() and not assume_yes and \
                confirm("\nConnect your phone via Telegram now?", False):
            telegram_setup()
    say("")
    if DRY:
        say("Dry run finished. Run again without --dry-run to apply.")
        return 0
    say("Done. Next steps:")
    if want["ext"]:
        say("  1. Reload your editor windows (Command Palette → Developer: Reload Window).")
    say("  2. Open the Relay panel in the activity bar, or run `context-relay status`.")
    if not (read_json(secrets_path(), {}) or {}).get("telegram_token"):
        say("  3. Optional: connect your phone with `context-relay telegram setup`.")
    say("  Something wrong? `context-relay doctor` · remove everything with `context-relay uninstall`.")
    return 0


def uninstall(ns):
    global DRY
    DRY = ns.dry_run
    say(f"Removing Context Relay{' (dry run)' if DRY else ''}:")
    act("Claude Code hooks", uninstall_claude)
    act("Antigravity hooks", uninstall_agy)
    act("MCP tools", uninstall_mcp)
    act("/coordinate skill", uninstall_skill)
    act("editor extension", uninstall_extension)
    act("background helper", uninstall_daemon)
    if ns.purge and not DRY:
        shutil.rmtree(HOME, ignore_errors=True)
        say(f"  {OK} deleted {HOME}")
    else:
        say(f"  Your settings, handoffs and history were kept in {HOME} (use --purge to delete them).")
    return 0


def status():
    cfg = load_config()
    say(f"Context Relay {__version__}")
    claude = read_json(Path.home() / ".claude" / "settings.json", {}) or {}
    agy = read_json(Path.home() / ".gemini" / "config" / "hooks.json", {}) or {}
    say(f"  Claude Code hooks   {OK if any(is_ours(e) for ev in claude.get('hooks', {}).values() for e in ev) else NO}")
    say(f"  Antigravity hooks   {OK if 'context-relay' in agy else NO}")
    say(f"  background helper   {OK + ' running' if daemon_pid() else NO + ' not running'}")
    s = read_json(secrets_path(), {}) or {}
    say(f"  Telegram            {'paired' if s.get('telegram_chat_id') else ('bot set, not paired' if s.get('telegram_token') else 'not set up')}")
    w = cfg["claude_window_tokens"]
    say(f"  threshold {cfg['threshold_pct']}% · Claude window {f'{w:,} tokens' if w else 'auto (200k unless more is seen)'}"
        f" · config {HOME / 'config.json'}")
    rows = []
    for tool in ("claude", "agy"):
        for p in (relay.STATE / tool).glob("*.json"):
            st = read_json(p, {}) or {}
            if st.get("updated"):
                rows.append((st["updated"], tool, st))
    for ts, tool, st in sorted(rows, key=lambda r: r[0], reverse=True)[:10]:
        say(f"  {datetime.fromtimestamp(ts):%m-%d %H:%M}  {tool:6} {st['sid'][:8]}  {st.get('pct', 0):5.1f}%  "
            f"{str(st.get('status')):9}  {st.get('cwd')}")
    return 0


def parse_window(v):
    v = str(v).strip().lower()
    if v == "auto":
        return v
    mult = 1_000_000 if v.endswith("m") else 1_000 if v.endswith("k") else 1
    return int(float(v.rstrip("mk")) * mult)


def run(a):
    global OK, NO, WARN
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            OK, NO, WARN = "+", "x", "!"
    cmd = a[0]
    if cmd in ("version", "--version", "-V"):
        say(f"context-relay {__version__}")
        return 0
    if cmd == "doctor":
        return doctor()
    if cmd == "status":
        return status()
    if cmd == "telegram":
        return telegram_cli(a[1:])
    if cmd == "build-vsix":
        if len(a) > 1 and a[1].startswith("-"):
            say("usage: context-relay build-vsix [OUT_DIR]   (default: the current folder)")
            return 0 if a[1] in ("-h", "--help") else 2
        out = build_vsix(a[1] if len(a) > 1 else ".")
        say(f"Built {out}")
        return 0
    p = argparse.ArgumentParser(prog=f"context-relay {cmd}")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask questions; take the defaults")
    p.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    if cmd == "uninstall":
        p.add_argument("--purge", action="store_true", help="also delete ~/.context-relay (settings, handoffs, history)")
        return uninstall(p.parse_args(a[1:]))
    p.add_argument("--claude-window", default="auto", type=parse_window, help="Claude context window in tokens: auto, 200000, 1000000 (default: auto)")
    p.add_argument("--threshold", type=float, help="hand off at this percentage of the window (default 65)")
    for flag in ("claude", "antigravity", "extension", "daemon", "telegram"):
        p.add_argument(f"--no-{flag}", action="store_true", help=f"skip the {flag} part")
    ns = p.parse_args(a[1:])
    if cmd == "install":
        ns.yes = True
    return setup(ns)
