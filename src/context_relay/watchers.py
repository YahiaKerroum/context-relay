"""Watchers: training logs, Kaggle notebook runs, and the internet connection.

Log watcher     follows a file and alerts on NaN/inf loss, crashes, out-of-memory, and a stall (no new output).
Kaggle watcher  polls `kaggle kernels status`; alerts when a run starts, finishes or fails, and on finishing
                (Wi-Fi mode) fetches only the log and results/ to pull out the key numbers.
Connectivity    a cheap probe every few seconds; PreToolUse hooks hold network tools while it is down.
"""
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

from . import relay
from . import smart
WATCH = relay.HOME / "watchers.json"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

NAN_RE = re.compile(r"(loss\W{0,14}(nan|inf)\b|\b(nan|inf)\b\W{0,14}loss|grad_norm\W{0,8}(nan|inf)|'loss':\s*(nan|inf))", re.I)
CRASH_RE = re.compile(r"Traceback \(most recent call last\)|CUDA out of memory|OutOfMemoryError|Segmentation fault|"
                      r"\bKilled\b|CUDA error|NCCL error|Fatal Python error|\bFATAL\b|RuntimeError:|AssertionError", re.I)
DONE_RE = re.compile(r"(training|run|job|evaluation|export)\W+(complete|completed|finished|done)\b|\bAll done\b", re.I)
METRIC_RE = re.compile(r"\b(acc(uracy)?|f1|loss|auc|kappa|bleu|rouge\w*|wer|cer|perplexity|ppl|mae|rmse|exact_match|em)\b"
                       r"[^\n]{0,40}?[-+]?\d+\.\d+", re.I)


def load():
    return relay.read_json(WATCH, None) or {"items": []}


def _save_item(wid, **kw):
    def fn(d):
        for it in d["items"]:
            if it["id"] == wid:
                it.update(kw)
    smart.update_json(WATCH, fn, {"items": []})


def add_log(path, name=None, cwd=None, stall_min=10):
    item = {"id": uuid.uuid4().hex[:5], "kind": "log", "path": str(path), "name": name or Path(path).name,
            "cwd": str(cwd or ""), "stall_min": stall_min, "created": time.time(), "offset": None,
            "tail": [], "alerts": {}, "last_growth": time.time(), "active": True}
    smart.update_json(WATCH, lambda d: d["items"].append(item), {"items": []})
    smart.event("watch", f"watching {item['name']}")
    return item


def find_kaggle(cwd=None):
    for base in [Path(cwd)] if cwd else []:
        for p in [base, *base.parents]:
            for rel in (".venv/Scripts/kaggle.exe", "venv/Scripts/kaggle.exe", ".venv/bin/kaggle"):
                if (p / rel).exists():
                    return str(p / rel)
    return shutil.which("kaggle") or (relay.load_config().get("kaggle", {}).get("exe") or None)


def add_kaggle(slug, cwd=None, auto=False):
    exe = find_kaggle(cwd)
    if not exe:
        relay.log(f"kaggle watcher: no kaggle CLI found for {cwd}")
        return None
    if any(i["kind"] == "kaggle" and i["slug"] == slug and i.get("active") for i in load()["items"]):
        return None
    item = {"id": uuid.uuid4().hex[:5], "kind": "kaggle", "slug": slug, "name": slug.split("/")[-1], "exe": exe,
            "cwd": str(cwd or ""), "created": time.time(), "status": None, "next_check": 0, "active": True,
            "alerts": {}, "auto": auto}
    smart.update_json(WATCH, lambda d: d["items"].append(item), {"items": []})
    smart.event("watch", f"watching Kaggle run {slug}")
    relay.notify("kaggle", f"👀 Watching Kaggle run <b>{relay.html_escape(slug)}</b>; I'll tell you when it starts, "
                           f"finishes or fails.", always=True)
    return item


def remove(wid):
    def fn(d):
        d["items"] = [i for i in d["items"] if i["id"] != wid]
    smart.update_json(WATCH, fn, {"items": []})


def alert(item, kind, text, cooldown=600, **kw):
    last = item.get("alerts", {}).get(kind, 0)
    if time.time() - last < cooldown:
        return False
    item.setdefault("alerts", {})[kind] = time.time()
    relay.notify("watch", text, always=True, desktop=True, **kw)
    smart.event("watch", text.replace("\n", " ")[:160], watcher=item["id"], what=kind)
    return True


# ---------------------------------------------------------------- log files

def _tail_text(lines, n=8):
    return relay.html_escape("\n".join(l[:200] for l in lines[-n:]))


def tick_log(item):
    p = Path(item["path"])
    if not p.exists():
        return
    size = p.stat().st_size
    off = item.get("offset")
    if off is None or size < off:  # new watcher or the file was truncated / restarted
        off = max(0, size - 4000)
        item["tail"] = []
    if size > off:
        with open(p, "rb") as f:
            f.seek(off)
            chunk = f.read(256_000).decode("utf-8", "replace")
        item["offset"] = off + len(chunk.encode("utf-8", "replace"))
        new = [l.rstrip() for l in chunk.splitlines() if l.strip()]
        item["tail"] = (item.get("tail", []) + new)[-14:]
        item["last_growth"] = time.time()
        if item["alerts"].get("stall"):
            item["alerts"].pop("stall", None)
            relay.notify("watch", f"▶️ <b>{relay.html_escape(item['name'])}</b> is producing output again.", always=True)
        text = "\n".join(new)
        head = f"<b>{relay.html_escape(item['name'])}</b>"
        if NAN_RE.search(text):
            alert(item, "nan", f"⚠️ {head}: loss is NaN/inf.\n<pre>{_tail_text(item['tail'])}</pre>")
        if CRASH_RE.search(text):
            alert(item, "crash", f"💥 {head}: looks like a crash or error.\n<pre>{_tail_text(item['tail'], 12)}</pre>")
        elif DONE_RE.search(text):
            alert(item, "done", f"✅ {head}: looks finished.\n<pre>{_tail_text(item['tail'], 5)}</pre>", cooldown=3600)
    else:
        item["offset"] = off
        idle = time.time() - item.get("last_growth", time.time())
        if idle > item.get("stall_min", 10) * 60 and not item["alerts"].get("done"):
            alert(item, "stall", f"⏸️ <b>{relay.html_escape(item['name'])}</b>: no new output for {idle / 60:.0f} min.\n"
                                 f"<pre>{_tail_text(item['tail'], 4)}</pre>", cooldown=10 ** 9)


# ---------------------------------------------------------------- Kaggle

def kaggle_status(item):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([item["exe"], "kernels", "status", item["slug"]], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60, env=env, creationflags=NO_WINDOW,
                       cwd=item.get("cwd") or None)
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r'status\s+"?(?:KernelWorkerStatus\.)?(\w+)"?', out)
    fail = re.search(r"[Ff]ailure message:\s*(.+)", out)
    return (m.group(1).upper() if m else None), (fail.group(1).strip() if fail else ""), out.strip()[-300:]


def fetch_numbers(item):
    """Download only logs and results/ for a finished run and pull out the key numbers."""
    dest = relay.HOME / "kaggle" / item["slug"].replace("/", "_")
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    pattern = relay.load_config().get("kaggle", {}).get("file_pattern", r"(.*\.log|results/.*\.(json|csv|txt))")
    r = subprocess.run([item["exe"], "kernels", "output", item["slug"], "-p", str(dest), "--file-pattern", pattern],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, env=env,
                       creationflags=NO_WINDOW, cwd=item.get("cwd") or None)
    lines, files = [], []
    for f in sorted(dest.rglob("*")):
        if not f.is_file():
            continue
        files.append(f.name)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f.suffix == ".json":
            try:
                import json
                d = json.loads(text)
                flat = {k: v for k, v in d.items() if isinstance(v, (int, float)) and not isinstance(v, bool)} \
                    if isinstance(d, dict) else {}
                lines += [f"{f.name}: " + ", ".join(f"{k}={v:.4g}" for k, v in list(flat.items())[:8])] if flat else []
            except ValueError:
                pass
        else:
            hits = [m.group(0).strip() for m in METRIC_RE.finditer("\n".join(text.splitlines()[-600:]))]
            lines += [f"{f.name}: {h[:110]}" for h in hits[-5:]]
    if not files:
        return None, (r.stderr or r.stdout or "nothing matched")[-200:]
    return (lines[-10:] or [f"fetched {', '.join(files[:4])} but found no obvious metrics"]), str(dest)


def tick_kaggle(item, net_mode):
    if time.time() < item.get("next_check", 0):
        return
    item["next_check"] = time.time() + 60
    try:
        status, fail, raw = kaggle_status(item)
    except Exception as e:  # noqa: BLE001
        relay.log(f"kaggle status {item['slug']}: {e}")
        item["next_check"] = time.time() + 180
        return
    if not status or status == item.get("status"):
        return
    prev, item["status"] = item.get("status"), status
    name = f"<b>{relay.html_escape(item['name'])}</b>"
    sid = None
    if status == "RUNNING":
        relay.notify("kaggle", f"🏃 Kaggle {name} started running.", always=True)
    elif status == "QUEUED" and prev is None:
        relay.notify("kaggle", f"🕒 Kaggle {name} is queued.", always=True)
    elif status == "COMPLETE":
        text = f"✅ Kaggle {name} finished."
        mode = relay.load_config().get("kaggle", {}).get("auto_fetch", "wifi")
        buttons = None
        if mode == "always" or (mode == "wifi" and net_mode != "data"):
            try:
                nums, where = fetch_numbers(item)
                text += ("\n<pre>" + relay.html_escape("\n".join(nums)) + "</pre>") if nums else f"\n(no numbers: {where})"
            except Exception as e:  # noqa: BLE001
                text += f"\n(couldn't fetch results: {relay.html_escape(str(e)[:100])})"
        else:
            buttons = [[("📥 Get the numbers", f"kg:{item['id']}")]]
        relay.notify("kaggle", text, always=True, desktop=True, buttons=buttons)
        item["active"] = False
    elif status in ("ERROR", "CANCEL_ACKNOWLEDGED", "CANCELED", "CANCELLED"):
        relay.notify("kaggle", f"❌ Kaggle {name} {'failed' if status == 'ERROR' else 'was cancelled'}."
                               + (f"\n{relay.html_escape(fail[:300])}" if fail else ""), always=True, desktop=True)
        item["active"] = False
    smart.event("kaggle", f"{item['slug']}: {status}")


# ---------------------------------------------------------------- driver and connectivity

def tick(net_mode="wifi"):
    d = load()
    for item in d["items"]:
        if not item.get("active", True):
            continue
        try:
            if item["kind"] == "log":
                tick_log(item)
            elif item["kind"] == "kaggle":
                tick_kaggle(item, net_mode)
        except Exception as e:  # noqa: BLE001
            relay.log(f"watcher {item['id']}: {e!r}")
        keep = {k: item[k] for k in ("offset", "tail", "alerts", "last_growth", "status", "next_check", "active")
                if k in item}
        _save_item(item["id"], **keep)
    # forget finished Kaggle watchers after a day
    now = time.time()
    if any(not i.get("active", True) and now - i.get("created", now) > 86400 for i in d["items"]):
        smart.update_json(WATCH, lambda dd: dd.__setitem__(
            "items", [i for i in dd["items"] if i.get("active", True) or now - i.get("created", now) < 86400]),
            {"items": []})


def probe_online():
    for host in ("api.anthropic.com", "1.1.1.1", "generativelanguage.googleapis.com"):
        try:
            with socket.create_connection((host, 443), timeout=3):
                return True
        except OSError:
            continue
    return False
