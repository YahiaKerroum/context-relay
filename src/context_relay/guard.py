"""Safety nets: the danger gate, the data guard, approvals, and undo snapshots.

Danger gate  destructive shell commands wait for your OK (desktop or Telegram) even when the agent runs
             with permission prompts off.
Data guard   in data mode (metered connection, a hotspot you listed, or switched by hand), big downloads
             wait for your OK, with a size estimate where one is cheap to get.
Snapshots    a hidden git repo per project records the tree at the start of every agent turn, so one
             command undoes exactly what the turn changed. The project's own git is never touched.
"""
import json
import os
import re
import shlex
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from . import relay
APPROVALS = relay.HOME / "approvals"
SNAPS = relay.HOME / "snapshots"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

DANGER = [
    (r"\brm\s+(-\w*[rR]\w*|--recursive)\b", "recursive delete"),
    (r"\bRemove-Item\b.*-Recurse", "recursive delete"),
    (r"\b(rd|rmdir)\s+/s\b", "recursive delete"),
    (r"\bdel\s+(/\w\s+)*/[sq]\b", "bulk delete"),
    (r"\bshutil\.rmtree\(", "recursive delete"),
    (r"\bgit\s+push\b.*(\s--force\b|\s-f\b|--force-with-lease)", "force push"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+clean\s+-\w*f", "git clean"),
    (r"\bgit\s+(checkout|restore)\s+(--\s+)?\.(\s|$)", "discard all changes"),
    (r"\bgit\s+branch\s+-D\b", "delete branch"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "drop stash"),
    (r"\bgit\s+filter-(branch|repo)\b", "history rewrite"),
    (r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", "drop data"),
    (r"\bdelete\s+from\s+\w+\s*(;|\"|'|$)", "delete all rows"),
    (r"\bformat(\.com)?\s+[a-z]:", "format disk"),
    (r"\b(mkfs|diskpart)\b", "disk tool"),
    (r"\bdd\s+if=", "raw disk write"),
    (r"\b(kaggle\s+(datasets|kernels|models)\s+delete|gh\s+repo\s+delete)\b", "delete remote"),
    (r"\b(npm|twine|cargo)\s+publish\b|\buv\s+publish\b", "publish package"),
]
# recursive deletes of these are routine
SAFE_DELETE = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
               ".next", ".cache", ".parcel-cache", "coverage", ".turbo", "tmp", "temp", ".ipynb_checkpoints"}

DOWNLOADS = [
    (r"\b(pip3?|python3?\s+-m\s+pip|uv\s+pip|py\s+-m\s+pip)\s+install\b", "pip"),
    (r"\b(uv\s+(sync|add)|poetry\s+(install|add)|pipenv\s+install)\b", "python deps"),
    (r"\b(conda|mamba|micromamba)\s+(install|create|env\s+create)\b", "conda"),
    (r"\b(npm|pnpm|yarn|bun)\s+(i|install|add|ci)\b|\bnpx\s", "node packages"),
    (r"\bgit\s+clone\b|\bgit\s+lfs\s+(pull|fetch)\b", "git clone"),
    (r"\b(wget|aria2c)\b|\bcurl\b.*(\s-O\b|\s-o\s|--output|--remote-name)|\bInvoke-WebRequest\b.*-OutFile", "file download"),
    (r"\b(huggingface-cli|hf)\s+download\b|snapshot_download|from_pretrained|hf_hub_download", "Hugging Face"),
    (r"\bkaggle\s+(datasets|competitions|kernels|models)\s+(download|output)\b", "Kaggle download"),
    (r"\b(docker|podman)\s+pull\b|\bollama\s+pull\b", "image / model pull"),
    (r"\b(winget|choco|scoop|apt(-get)?)\s+install\b", "system package"),
]
HEAVY_PIP = {"torch": 2500, "torchvision": 800, "torchaudio": 600, "tensorflow": 600, "tensorflow-gpu": 600,
             "jax": 100, "jaxlib": 300, "nvidia": 1500, "cupy": 600, "xformers": 200, "vllm": 400,
             "bitsandbytes": 120, "opencv-python": 90, "paddlepaddle": 600, "onnxruntime-gpu": 300,
             "spacy": 40, "transformers": 12, "datasets": 6, "scipy": 40, "pandas": 15, "numpy": 20}


# ---------------------------------------------------------------- detection

def danger_reason(cmd):
    for pat, why in DANGER:
        m = re.search(pat, cmd, re.I)
        if not m:
            continue
        if why == "recursive delete" and _only_safe_targets(cmd[m.end():]):
            continue
        return why
    cfg = relay.load_config().get("guard", {})
    for pat in cfg.get("extra_danger_patterns", []):
        if re.search(pat, cmd, re.I):
            return f"matches your pattern {pat}"
    return None


TEMP_MARKERS = ("/tmp/", "/temp/", "appdata/local/temp", "scratchpad", "relay-test-", "relay-edge-", "relay-live-")


def _only_safe_targets(rest):
    """True when every path being deleted is a routine build/cache folder or lives in a temp/scratch area."""
    rest = re.split(r"[;&|\n]", rest)[0]
    try:
        args = [a for a in shlex.split(rest, posix=True) if not a.startswith("-")]
    except ValueError:
        return False
    paths = [a.replace("\\", "/").rstrip("/").lower() for a in args]
    return bool(paths) and all(Path(p).name in SAFE_DELETE or any(m in p + "/" for m in TEMP_MARKERS) for p in paths)


def download_kind(cmd):
    for pat, kind in DOWNLOADS:
        if re.search(pat, cmd, re.I):
            return kind
    return None


def estimate(cmd, kind):
    """A cheap size guess: a heavy-package table for pip, the Hub API for Hugging Face."""
    try:
        if kind == "pip":
            pkgs = re.findall(r"(?:^|\s)([A-Za-z][\w.\-]*)(?=[<>=!~\[\s]|$)", cmd.split("install", 1)[1])
            hits = {p: HEAVY_PIP[p.lower()] for p in pkgs if p.lower() in HEAVY_PIP}
            if "-r" in cmd.split() or "requirements" in cmd:
                return "installs a requirements file: size unknown, can be several GB with torch/CUDA"
            if hits:
                return "about " + ", ".join(f"{p} ~{mb} MB" for p, mb in hits.items())
        if kind == "Hugging Face":
            m = re.search(r"download\s+([\w.\-]+/[\w.\-]+)|from_pretrained\(\s*['\"]([\w.\-]+/[\w.\-]+)", cmd)
            repo = m and (m.group(1) or m.group(2))
            if repo:
                with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}?blobs=true", timeout=8) as r:
                    size = sum(s.get("size") or 0 for s in json.load(r).get("siblings", []))
                return f"{repo}: about {size / 1e6:,.0f} MB (whole repo)"
    except Exception:  # noqa: BLE001
        pass
    return "size unknown"


def network():
    """Written by the daemon every minute; falls back to the configured mode."""
    cfg = relay.load_config().get("network", {})
    net = relay.read_json(relay.HOME / "network.json", {}) or {}
    mode = cfg.get("mode", "auto")
    if mode == "auto":
        mode = net.get("mode", "wifi") if time.time() - net.get("ts", 0) < 300 else "wifi"
    return mode, net


# ---------------------------------------------------------------- approvals

def approver_reachable():
    """An editor window that shows prompts, or a paired Telegram through a live daemon."""
    ed = relay.read_json(relay.HOME / "editor_heartbeat.json", {}) or {}
    if time.time() - ed.get("ts", 0) < 15:
        return True
    d = relay.read_json(relay.HOME / "daemon.json", {}) or {}
    return time.time() - d.get("ts", 0) < 20 and d.get("paired") and d.get("telegram")


def ask(kind, title, detail, tool, cwd, timeout=None):
    """Wait for the user's decision from the editor or Telegram. Returns (allowed, note)."""
    cfg = relay.load_config().get("guard", {})
    timeout = timeout or cfg.get("approval_timeout_min", 10) * 60
    if not approver_reachable():  # nobody can answer: refuse now instead of hanging the agent
        return False, "no way to reach the user (no editor prompt, Telegram not connected)"
    aid = uuid.uuid4().hex[:8]
    path = APPROVALS / f"{aid}.json"
    relay.write_json(path, {"id": aid, "kind": kind, "title": title, "detail": detail, "tool": tool,
                            "cwd": cwd, "created": time.time(), "status": "pending"})
    end = time.time() + timeout
    while time.time() < end:
        a = relay.read_json(path, {}) or {}
        if a.get("status") in ("approved", "denied"):
            return a["status"] == "approved", a.get("by", "")
        time.sleep(0.5)
    a = relay.read_json(path, {}) or {}
    a["status"] = "expired"
    relay.write_json(path, a)
    return False, "no answer"


def _event(what, why, ok):
    try:
        from . import smart
        smart.event("approval", f"{what}: {why}", verdict="approved" if ok else "denied")
    except Exception:  # noqa: BLE001
        pass


def check_command(cmd, tool, cwd):
    """(allowed, reason) for a shell command an agent is about to run."""
    cfg = relay.load_config().get("guard", {})
    if cfg.get("danger_gate", True):
        why = danger_reason(cmd)
        if why:
            ok, by = ask("danger", f"{why}", cmd, tool, cwd)
            relay.log(f"danger gate {why}: {'approved' if ok else 'denied'} by {by or 'timeout'}: {' '.join(cmd.split())[:200]}")
            _event("danger gate", why, ok)
            if not ok:
                return False, (f"The user did not approve this {why} command ({by or 'no answer'}). Do not retry it "
                               f"or work around it; explain what you wanted to do and ask the user.")
    if cfg.get("data_guard", True):
        mode, _ = network()
        kind = download_kind(cmd) if mode == "data" else None
        if kind:
            size = estimate(cmd, kind)
            ok, by = ask("download", f"{kind} on mobile data ({size})", cmd, tool, cwd)
            relay.log(f"data guard {kind}: {'approved' if ok else 'denied'} ({size}): {' '.join(cmd.split())[:200]}")
            _event("data guard", kind, ok)
            if not ok:
                return False, ("The user is on mobile data and did not approve this download. Do not retry it or "
                               "fetch the same thing another way; continue without it or note it for later on Wi-Fi.")
    return True, None


# ---------------------------------------------------------------- snapshots

def _key(cwd):  # short: Windows paths cap at 260 characters and git objects nest deep
    import hashlib
    p = Path(cwd).resolve()
    name = re.sub(r"[^A-Za-z0-9]+", "-", p.name.lower()).strip("-")[:24]
    return f"{name}-{hashlib.sha1(str(p).lower().encode()).hexdigest()[:8]}"


def _git(gd, wt, *args, check=True):
    env = dict(os.environ, GIT_DIR=str(gd), GIT_WORK_TREE=str(wt))
    r = subprocess.run(["git", *args], cwd=str(wt), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {r.stderr.strip()[:300]}")
    return r.stdout


def project_root(cwd):
    r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def _repo(root):
    gd = SNAPS / _key(root)
    if not (gd / "HEAD").exists():
        gd.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-q", str(gd)], check=True, creationflags=NO_WINDOW)
        for k, v in (("core.autocrlf", "false"), ("user.name", "context-relay"), ("user.email", "relay@local"),
                     ("gc.auto", "0")):
            subprocess.run(["git", "--git-dir", str(gd), "config", k, v], creationflags=NO_WINDOW)
        objs = root / ".git" / "objects"
        if objs.is_dir():  # reuse the project's objects instead of copying every tracked file
            (gd / "objects" / "info").mkdir(parents=True, exist_ok=True)
            (gd / "objects" / "info" / "alternates").write_bytes((objs.resolve().as_posix() + "\n").encode())
    return gd


def snapshot(cwd, label, tool="", sid=""):
    root = project_root(cwd)
    if not root:
        return None
    max_mb = relay.load_config().get("snapshots", {}).get("max_file_mb", 20)
    gd = _repo(root)
    lock = gd / "relay.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 120:
            return None
        lock.unlink()
        return snapshot(cwd, label, tool, sid)
    try:
        excl = [".git/", ".context-relay/"]
        for f in _git(gd, root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if f and (root / f).is_file() and (root / f).stat().st_size > max_mb * 1e6:
                excl.append("/" + f)
        (gd / "info").mkdir(exist_ok=True)
        (gd / "info" / "exclude").write_bytes(("\n".join(excl) + "\n").encode("utf-8"))
        _git(gd, root, "add", "-A")
        head = _git(gd, root, "rev-parse", "-q", "--verify", "HEAD", check=False).strip()
        if head and not _git(gd, root, "diff", "--cached", "--name-only", "HEAD"):
            sha = head
        else:
            _git(gd, root, "commit", "-q", "--no-verify", "--allow-empty", "-m", label)
            sha = _git(gd, root, "rev-parse", "HEAD").strip()
        with open(gd / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"sha": sha, "ts": time.time(), "label": label, "tool": tool, "sid": sid,
                                "root": str(root)}) + "\n")
        return sha
    finally:
        lock.unlink(missing_ok=True)


def snapshots(cwd, limit=15):
    root = project_root(cwd)
    if not root:
        return []
    p = SNAPS / _key(root) / "log.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[::-1][:limit]


def changes_since(cwd, sha):
    root = project_root(cwd)
    gd = _repo(root)
    _git(gd, root, "add", "-A")
    out = _git(gd, root, "diff", "--cached", "--name-status", "--no-renames", sha)
    return [line.split("\t", 1) for line in out.splitlines() if "\t" in line]


def diff_stat(cwd, a, b=None):
    """[(added, deleted, path)] between snapshot a and snapshot b (or the working tree)."""
    root = project_root(cwd)
    gd = _repo(root)
    if b is None:
        _git(gd, root, "add", "-A")
        out = _git(gd, root, "diff", "--cached", "--numstat", "--no-renames", a)
    else:
        out = _git(gd, root, "diff", "--numstat", "--no-renames", a, b)
    rows = []
    for line in out.splitlines():
        parts = line.split("	", 2)
        if len(parts) == 3:
            rows.append((int(parts[0]) if parts[0].isdigit() else 0, int(parts[1]) if parts[1].isdigit() else 0, parts[2]))
    return rows


def undo(cwd, sha):
    """Put every file back the way it was at snapshot `sha`. The current state is snapshotted first."""
    root = project_root(cwd)
    gd = _repo(root)
    before = snapshot(cwd, f"before undo to {sha[:8]}", "relay")
    changed = changes_since(cwd, sha)
    for status, f in changed:
        if status == "A":  # created after the snapshot
            (root / f).unlink(missing_ok=True)
        else:
            _git(gd, root, "checkout", sha, "--", f)
    _git(gd, root, "add", "-A")
    relay.log(f"undo in {root} to {sha[:8]}: {len(changed)} file(s); redo point {before and before[:8]}")
    try:
        from . import smart
        smart.event("undo", f"undid {len(changed)} file(s) in {Path(root).name}")
    except Exception:  # noqa: BLE001
        pass
    return changed, before


def prune(keep_days=7, keep_min=30):
    for gd in SNAPS.glob("*"):
        p = gd / "log.jsonl"
        if not p.exists():
            continue
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        cut = time.time() - keep_days * 86400
        kept = [r for i, r in enumerate(rows) if r["ts"] >= cut or i >= len(rows) - keep_min]
        if len(kept) < len(rows):
            p.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
