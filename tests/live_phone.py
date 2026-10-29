"""Manual check (uses a little Claude quota): a real headless session started the way Telegram does it,
then a forked continuation of it.   python tests/live_phone.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="relay-live-"))
os.environ["CONTEXT_RELAY_HOME"] = str(TMP / "home")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from context_relay import relay, remote  # noqa: E402


class FakeRemote:
    def __init__(self):
        self.done = []

    def phone_done(self, *a):
        self.done.append(a)


proj = TMP / "proj"
proj.mkdir()
subprocess.run(["git", "init", "-q", str(proj)])
fr = FakeRemote()
phone = remote.Phone(fr)

print("1) fresh session...")
phone.start(str(proj), "Create a file hello.txt containing exactly: hi from the phone. Then reply with the single word DONE.",
            fresh=True, tool="claude", label="fresh")
while phone.active:
    time.sleep(1)
cwd, tool, sid, result, err, label, st = fr.done[-1]
print("   result:", result[:80], "| err:", err[:200], "| session:", sid)
print("   hello.txt:", (proj / "hello.txt").read_text().strip() if (proj / "hello.txt").exists() else "MISSING")

print("2) continue it (forked)...")
chat = {"tool": "claude", "sid": sid, "cwd": str(proj)}
phone.start(str(proj), "What did the file hello.txt contain? Answer in six words or fewer.", st=chat, label="follow-up")
while phone.active:
    time.sleep(1)
cwd, tool, sid2, result, err, label, st = fr.done[-1]
print("   result:", result[:120], "| err:", err[:200])
print("   forked into a new session:", sid2 != sid, sid2)

state_dir = TMP / "home" / "state" / "claude"
for p in sorted(state_dir.glob("*.json")) if state_dir.exists() else []:
    s = json.loads(p.read_text())
    print("   hook state:", p.stem[:8], "| title:", s.get("title"), "| busy:", s.get("busy"), "| status:", s.get("status"))
notes = list((TMP / "home" / "notify").glob("*.json")) if (TMP / "home" / "notify").exists() else []
print("   Telegram pings from phone sessions (should be 0):", len(notes))
snaps = list((TMP / "home" / "snapshots").glob("*/log.jsonl"))
print("   snapshots taken for undo:", sum(len(p.read_text().splitlines()) for p in snaps))
