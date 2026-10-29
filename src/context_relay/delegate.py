"""Delegation: Claude keeps the hard work, Gemini in Antigravity takes the bulk.

Job kinds
  dataset  annotate a JSONL / JSON / CSV file in small blind batches. Every batch is a fresh
           Antigravity conversation (long conversations drift), carries calibration items with known
           answers, and is checked: each record answered exactly once, fields valid, calibration
           accuracy above the bar. Failing batches are retried in a new conversation; quota errors
           back off and finally pause the job, which `job resume` picks up where it stopped.
  task     a well-defined tedious task (boilerplate, docstrings, translations, data cleaning)
           that the Antigravity agent carries out in the project folder.

Runners (both are Antigravity on your plan; neither uses the Gemini API)
  cli  the Antigravity CLI (agy -p), headless, several batches in parallel
  ide  the Antigravity IDE: the extension opens each batch as a new conversation in the agent
       panel so you can watch it work; one batch at a time
"""
import csv
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import relay
JOBS = relay.HOME / "jobs"
TERMINAL = ("done", "failed", "cancelled")
QUOTA_RE = re.compile(r"quota|rate.?limit|\b429\b|resource.?exhausted|too many requests", re.I)
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_lock = threading.Lock()


# ---------------------------------------------------------------- job files

def jdir(jid):
    return JOBS / jid


def load_job(jid):
    job = relay.read_json(jdir(jid) / "job.json")
    if not job:
        raise SystemExit(f"no job {jid}")
    return job


def status(jid):
    return relay.read_json(jdir(jid) / "status.json", {}) or {}


def update(jid, **kw):
    with _lock:
        st = status(jid)
        st.update(kw)
        st["updated"] = time.time()
        relay.write_json(jdir(jid) / "status.json", st)
        return st


def jlog(jid, msg):
    with _lock, open(jdir(jid) / "log.txt", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def create(kind, spec):
    jid = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    jdir(jid).mkdir(parents=True)
    if spec.get("runner") == "ide":  # the IDE agent works inside the project, so its files live there
        work = Path(spec["cwd"]) / ".context-relay" / "delegation" / jid
        relay.handoff_path_for(spec["cwd"], jid, relay.load_config())  # also git-excludes .context-relay/
    else:
        work = jdir(jid) / "work"
    work.mkdir(parents=True, exist_ok=True)
    relay.write_json(jdir(jid) / "job.json", {"id": jid, "kind": kind, "created": time.time(),
                                              "work_dir": str(work), **spec})
    update(jid, state="queued", kind=kind, title=spec.get("title", ""), runner=spec.get("runner"),
           model=spec.get("model"), cwd=spec.get("cwd"))
    return jid


def spawn(jid):
    """Run the job in its own process so it outlives the chat or MCP call that started it."""
    cmd = relay.cli_cmd("job", "run", jid)
    out = open(jdir(jid) / "runner.log", "ab")
    kw = dict(stdout=out, stderr=out, stdin=subprocess.DEVNULL, cwd=str(relay.ROOT), close_fds=True)
    if os.name == "nt":
        base = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW
        for flags in (base | 0x01000000, base):  # 0x01000000: break away from the parent's job object
            try:
                subprocess.Popen(cmd, creationflags=flags, **kw)
                return
            except OSError:
                continue
    subprocess.Popen(cmd, start_new_session=True, **kw)


def all_jobs(limit=30):
    if not JOBS.exists():
        return []
    ids = sorted((p.name for p in JOBS.iterdir() if (p / "job.json").exists()), reverse=True)
    return ids[:limit]


# ---------------------------------------------------------------- data

def load_records(path, id_field):
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".jsonl":
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif ext == ".json":
        rows = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(rows, dict):  # {"items": [...]}
            rows = next((v for v in rows.values() if isinstance(v, list)), None)
        if not isinstance(rows, list):
            raise ValueError("a .json input must hold a list of records")
    elif ext in (".csv", ".tsv"):
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f, delimiter="\t" if ext == ".tsv" else ","))
    else:
        rows = [{"text": line} for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    out = []
    for i, r in enumerate(rows):
        r = r if isinstance(r, dict) else {"text": r}
        out.append((str(r.get(id_field, i)), r))
    if len({k for k, _ in out}) != len(out):
        raise ValueError(f"the id field '{id_field}' has duplicate values")
    return out


def load_calibration(path):
    """JSONL of gold items: the input fields plus {"expected": {field: answer}}."""
    if not path:
        return []
    gold = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            exp = d.pop("expected", None)
            if not isinstance(exp, dict):
                raise ValueError("every calibration line needs an \"expected\" object")
            gold.append((d, exp))
    return gold


def _type(t):
    t = t.strip()
    if "|" in t:
        return {"type": "string", "enum": [x.strip() for x in t.split("|") if x.strip()]}
    if t.endswith("[]"):
        return {"type": "array", "items": _type(t[:-2])}
    alias = {"int": "integer", "float": "number", "num": "number", "bool": "boolean", "str": "string", "": "string"}
    t = alias.get(t, t)
    if t in ("string", "number", "integer", "boolean"):
        return {"type": t}
    return {"type": "string", "description": t}


def parse_fields(fields):
    """'label:pos|neg|neutral, score:number, note' or a JSON schema -> (properties, required)."""
    if isinstance(fields, str) and fields.strip().startswith("{"):
        fields = json.loads(fields)
    if isinstance(fields, dict):
        if "properties" in fields:
            return fields["properties"], fields.get("required", list(fields["properties"]))
        props = {k: v if isinstance(v, dict) else _type(str(v)) for k, v in fields.items()}
        return props, list(props)
    props = {}
    for part in str(fields).split(","):
        if part.strip():
            name, _, typ = part.partition(":")
            props[name.strip()] = _type(typ)
    if not props:
        raise ValueError("no output fields given")
    return props, list(props)


def valid(v, sch):
    if "enum" in sch and v not in sch["enum"]:
        return False
    t = sch.get("type")
    return {"string": isinstance(v, str),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": isinstance(v, int) and not isinstance(v, bool),
            "boolean": isinstance(v, bool),
            "array": isinstance(v, list)}.get(t, True)


def same(a, b):
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-9
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def describe_fields(props, required):
    lines = []
    for k, s in props.items():
        kind = " | ".join(map(str, s["enum"])) if "enum" in s else s.get("type", "string")
        note = f" ({s['description']})" if s.get("description") else ""
        lines.append(f"- {k}: {kind}{note}{'' if k in required else ' (optional)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- dataset jobs

def prepare_batches(jid, job):
    root = Path(job["work_dir"]) / "batches"
    if root.exists():  # resume: same batches, same blind keys
        return sorted(p for p in root.iterdir() if p.is_dir())
    recs = load_records(job["input"], job["id_field"])
    if job.get("limit"):
        recs = recs[:job["limit"]]
    gold = load_calibration(job.get("calibration"))
    hide = set(job.get("hide_fields") or []) | {job["id_field"]}
    show = job.get("input_fields")
    rng = random.Random(jid)
    bs = job["batch_size"]
    for b, start in enumerate(range(0, len(recs), bs), 1):
        chunk = [("rec", rid, r) for rid, r in recs[start:start + bs]]
        k = min(job["calibration_per_batch"], len(gold))
        for j in range(k):  # rotate through the gold set, at random positions
            ci = (b * k + j) % len(gold)
            chunk.insert(rng.randrange(len(chunk) + 1), ("cal", ci, gold[ci][0]))
        lines, keys = [], {}
        for n, (kind, ref, rec) in enumerate(chunk, 1):
            key = f"r{n}"
            fields = {f: v for f, v in rec.items() if f not in hide and (not show or f in show)}
            lines.append(json.dumps({"_k": key, **fields}, ensure_ascii=False))
            keys[key] = [kind, ref]
        bd = root / f"b{b:04d}"
        bd.mkdir(parents=True)
        (bd / "input.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        relay.write_json(bd / "keys.json", keys)
    return sorted(p for p in root.iterdir() if p.is_dir())


def batch_prompt(jid, job, bd, n, props, required):
    inp = (bd / "input.jsonl").resolve()
    if job["runner"] == "ide":
        deliver = (f"Write your answer to {(bd / 'output.json').resolve()} as a single JSON object "
                   f'{{"items": [{{"_k": "r1", ...}}, ...]}}, with nothing else in that file. When the file is '
                   f"written, reply BATCH DONE and stop.")
    else:
        deliver = "Return the items as your structured answer. Do not write any files."
    return f"""[CONTEXT RELAY BATCH {jid}/{bd.name}] You are annotating one batch of a dataset for a lead engineer. Accuracy and consistency matter more than speed.

INSTRUCTIONS
{job['instructions']}

INPUT
{inp} holds {n} records, one JSON object per line. Each has a key "_k" (r1, r2, ...). Read the whole file before answering.

OUTPUT
Exactly one item per record, in file order. Each item has the record's "_k" and these fields:
{describe_fields(props, required)}

Rules: do not skip, merge or invent records. Judge every record on its own, with the same care as the first. Do not modify the input file.
{deliver}"""


def items_schema(props, required):
    return {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"_k": {"type": "string"}, **props}, "required": ["_k", *required]}}}}


class Ctx:
    """Shared by a job's worker threads: running processes, cancel and pause."""

    def __init__(self, jid):
        self.jid = jid
        self.procs = set()
        self.cancelled = False
        self.paused = None
        self.consecutive_quota = 0
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        while not self.cancelled:
            if (jdir(self.jid) / "cancel").exists():
                self.cancelled = True
                for p in list(self.procs):
                    try:
                        p.kill()
                    except OSError:
                        pass
            time.sleep(1)

    def stopping(self):
        return self.cancelled or self.paused

    def sleep(self, s):
        end = time.time() + s
        while time.time() < end and not self.stopping():
            time.sleep(1)


def run_agy_cli(ctx, job, cwd, prompt, timeout, extra):
    cmd = [relay.agy_exe(), "-p", prompt, "--output-format", "json", "--model", job["model"], *extra]
    env = dict(os.environ, CONTEXT_RELAY_DISABLE="1")
    p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         creationflags=NO_WINDOW)
    ctx.procs.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return None, "timed out"
    finally:
        ctx.procs.discard(p)
    text, errt = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    try:
        res, _ = json.JSONDecoder().raw_decode(text, text.index("{"))
    except ValueError:
        return None, (errt.strip() or text.strip() or f"exit {p.returncode}")[-600:]
    if res.get("status") != "SUCCESS":
        return None, json.dumps(res)[:600]
    return res, None


def run_ide_request(ctx, job, kind, prompt, done_file, timeout):
    """Ask the Antigravity IDE extension to run this in a new conversation; wait for its file."""
    done_file = Path(done_file)
    if done_file.exists():
        done_file.unlink()
    rid = uuid.uuid4().hex[:8]
    relay.write_json(relay.OUTBOX / f"{rid}.json", {  # the #id tag lets the extension confirm it was sent
        "id": rid, "kind": kind, "target": "agy", "cwd": job["cwd"], "prompt": f"[CONTEXT RELAY #{rid}] {prompt}",
        "done_file": str(done_file), "created": time.time(), "timeout_s": timeout})
    end = time.time() + timeout
    last = None
    while time.time() < end and not ctx.stopping():
        if done_file.exists():
            try:
                size = done_file.stat().st_size
                if size and size == last:  # stopped growing
                    return done_file.read_text(encoding="utf-8"), None
                last = size
            except OSError:
                pass
        time.sleep(2)
    for f in relay.OUTBOX.glob(f"{rid}.json"):  # nobody picked it up
        f.unlink()
        return None, "no Antigravity IDE window took the batch (is the project open in Antigravity IDE?)"
    return None, "cancelled" if ctx.stopping() else "timed out"


def check(keys, items, props, required, gold):
    got, problems = {}, []
    for it in items if isinstance(items, list) else []:
        k = it.get("_k") if isinstance(it, dict) else None
        if k not in keys:
            problems.append(f"unknown key {k}")
        elif k in got:
            problems.append(f"duplicate {k}")
        else:
            bad = [f for f in required if f not in it or not valid(it[f], props.get(f, {}))]
            if bad:
                problems.append(f"{k}: bad {', '.join(bad)}")
            else:
                got[k] = {f: it[f] for f in props if f in it}
    missing = [k for k in keys if k not in got]
    cal_ok = cal_n = 0
    for k, (kind, ref) in keys.items():
        if kind == "cal":
            cal_n += 1
            if k in got and all(same(got[k].get(f), v) for f, v in gold[ref][1].items()):
                cal_ok += 1
    return got, missing, problems, cal_ok, cal_n


def run_batch(ctx, jid, job, bd, props, required, gold):
    res_path = bd / "result.json"
    if (relay.read_json(res_path, {}) or {}).get("ok"):
        return
    keys = relay.read_json(bd / "keys.json")
    n = len(keys)
    prompt = batch_prompt(jid, job, bd, n, props, required)
    best, attempt, waits = None, 0, 0
    while attempt <= job["max_retries"] and not ctx.stopping():
        t0 = time.time()
        if job["runner"] == "ide":
            raw, err = run_ide_request(ctx, job, "batch", prompt, bd / "output.json", job["batch_timeout_s"])
            items = None
            if raw is not None:
                try:
                    items = json.loads(raw).get("items")
                except (ValueError, AttributeError):
                    err = "output.json is not valid JSON"
        else:
            res, err = run_agy_cli(ctx, job, bd, prompt, job["batch_timeout_s"],
                                   ["--json-schema", str(Path(job["work_dir"]) / "schema.json")])
            items = ((res or {}).get("structured_output") or {}).get("items") if res else None
        if ctx.stopping():
            return
        if err:
            if QUOTA_RE.search(err):
                waits += 1
                ctx.consecutive_quota += 1
                if ctx.consecutive_quota >= 6:
                    ctx.paused = "Antigravity quota or rate limit reached; resume the job later"
                    jlog(jid, f"{bd.name}: quota again, pausing the job")
                    return
                wait = min(600, 60 * 2 ** (waits - 1))
                jlog(jid, f"{bd.name}: quota / rate limit, waiting {wait}s")
                ctx.sleep(wait)
                continue  # not counted as an attempt
            attempt += 1
            jlog(jid, f"{bd.name} attempt {attempt}: {err[:300]}")
            continue
        ctx.consecutive_quota = 0
        got, missing, problems, cal_ok, cal_n = check(keys, items, props, required, gold)
        acc = cal_ok / cal_n if cal_n else None
        cand = {"got": got, "missing": missing, "problems": problems[:20], "cal_ok": cal_ok, "cal_n": cal_n,
                "seconds": round(time.time() - t0, 1)}
        if not best or (len(got), acc or 0) > (len(best["got"]), (best["cal_ok"] / best["cal_n"]) if best["cal_n"] else 0):
            best = cand
        if not missing and (acc is None or acc >= job["calibration_min_accuracy"]):
            best["ok"] = True
            break
        attempt += 1
        jlog(jid, f"{bd.name} attempt {attempt}: {len(missing)} missing, {len(problems)} invalid, "
                  f"calibration {cal_ok}/{cal_n}; retrying in a fresh conversation")
    best = best or {"got": {}, "missing": list(keys), "problems": [], "cal_ok": 0, "cal_n": 0}
    annotations = {keys[k][1]: v for k, v in best["got"].items() if keys[k][0] == "rec"}
    ok = bool(best.get("ok"))
    relay.write_json(res_path, {  # a batch that failed its checks is kept apart as unverified
        "ok": ok, "attempts": attempt + (1 if ok else 0),
        "annotations": annotations if ok else {}, "unverified": {} if ok else annotations,
        "missing": [keys[k][1] for k in best["missing"] if keys[k][0] == "rec"],
        "problems": best["problems"], "cal_ok": best["cal_ok"], "cal_n": best["cal_n"],
        "seconds": best.get("seconds")})
    progress(jid, job)


def progress(jid, job):
    root = Path(job["work_dir"]) / "batches"
    res = [relay.read_json(p / "result.json") for p in root.iterdir() if (p / "result.json").exists()]
    cal_ok = sum(r["cal_ok"] for r in res)
    cal_n = sum(r["cal_n"] for r in res)
    secs = [r["seconds"] for r in res if r.get("seconds")]
    update(jid, done=sum(1 for r in res if r["ok"]), failed=sum(1 for r in res if not r["ok"]),
           retried=sum(1 for r in res if r["attempts"] > 1), cal_ok=cal_ok, cal_n=cal_n,
           avg_batch_s=round(sum(secs) / len(secs), 1) if secs else None)


def merge(jid, job):
    recs = load_records(job["input"], job["id_field"])
    if job.get("limit"):
        recs = recs[:job["limit"]]
    root = Path(job["work_dir"]) / "batches"
    ann, unverified, per_batch = {}, {}, []
    for bd in sorted(root.iterdir()):
        r = relay.read_json(bd / "result.json", {}) or {}
        ann.update(r.get("annotations", {}))
        unverified.update(r.get("unverified", {}))
        per_batch.append({"batch": bd.name, "ok": r.get("ok", False), "attempts": r.get("attempts", 0),
                          "calibration": f"{r.get('cal_ok', 0)}/{r.get('cal_n', 0)}", "missing": len(r.get("missing", []))})
    out = Path(job["output"])
    rows, failed = [], []
    for rid, r in recs:
        if rid in ann:
            rows.append({**r, **ann[rid]} if job.get("keep_input", True) else {job["id_field"]: rid, **ann[rid]})
        else:
            failed.append({**r, "_unverified": unverified[rid]} if rid in unverified else r)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in (".csv", ".tsv"):
        cols = list(dict.fromkeys(k for row in rows for k in row))
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, cols, delimiter="\t" if out.suffix.lower() == ".tsv" else ",")
            w.writeheader()
            w.writerows(rows)
    else:
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    failed_path = out.with_name(out.stem + ".failed.jsonl")
    if failed:
        failed_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in failed), encoding="utf-8")
    st = status(jid)
    report = {"job": jid, "records": len(recs), "annotated": len(rows), "failed": len(failed),
              "batches": len(per_batch), "batches_failed": sum(1 for b in per_batch if not b["ok"]),
              "calibration": f"{st.get('cal_ok', 0)}/{st.get('cal_n', 0)}", "output": str(out),
              "failed_records": str(failed_path) if failed else None, "model": job["model"],
              "runner": job["runner"], "per_batch": per_batch}
    relay.write_json(jdir(jid) / "report.json", report)
    return report


# ---------------------------------------------------------------- inter-rater check

def cohen_kappa(a, b):
    n = len(a)
    if not n:
        return None
    labels = sorted(set(a) | set(b), key=str)
    po = sum(1 for x, y in zip(a, b) if same(x, y)) / n
    pe = sum((sum(1 for x in a if same(x, l)) / n) * (sum(1 for y in b if same(y, l)) / n) for l in labels)
    return 1.0 if pe >= 1 else (po - pe) / (1 - pe)


def pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else None


def claude_exe():
    import shutil
    return shutil.which("claude") or "claude"


def inter_rater(jid, job, rep):
    """Claude blind-labels a random sample of the finished set; reports how well it agrees with Gemini."""
    ir = relay.load_config()["delegation"]["inter_rater"]
    if job.get("inter_rater") is False or not (job.get("inter_rater") or ir["enabled"]):
        return None
    props, required = parse_fields(job["fields"])
    out = Path(job["output"])
    if not out.exists():
        return None
    if out.suffix.lower() == ".jsonl":
        rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        rows = [dict(r) for _, r in load_records(out, job["id_field"])]
    rows = [r for r in rows if all(f in r for f in props)]
    if len(rows) < 5:
        return None
    n = min(len(rows), max(ir["min"], min(ir["max"], round(len(rows) * ir["fraction"]))))
    sample = random.Random(jid + "ir").sample(rows, n)
    hide = set(job.get("hide_fields") or []) | {job["id_field"]} | set(props)
    show = job.get("input_fields")
    lines, keys = [], {}
    for i, r in enumerate(sample, 1):
        k = f"r{i}"
        lines.append(json.dumps({"_k": k, **{f: v for f, v in r.items() if f not in hide and (not show or f in show)}},
                                ensure_ascii=False))
        keys[k] = r
    prompt = ("You are an independent second annotator. Annotate each record below following the guidelines "
              "exactly. Judge every record on its own.\n\nGUIDELINES\n" + job["instructions"] +
              "\n\nOUTPUT\nFor each record return an item with its \"_k\" and these fields:\n" +
              describe_fields(props, required) + "\n\nRECORDS (one JSON per line)\n" + "\n".join(lines))
    schema = items_schema(props, required)
    cmd = [claude_exe(), "-p", "--output-format", "json", "--json-schema", json.dumps(schema), "--model", ir["model"]]
    env = dict(os.environ, CONTEXT_RELAY_DISABLE="1", PYTHONIOENCODING="utf-8")
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=900, env=env, cwd=job["work_dir"], creationflags=NO_WINDOW)
    try:
        d, _ = json.JSONDecoder().raw_decode(r.stdout, r.stdout.index("{"))
        res = d.get("structured_output") or json.loads(d.get("result") or "{}")
        items = {it["_k"]: it for it in res.get("items", []) if isinstance(it, dict) and "_k" in it}
    except (ValueError, KeyError, AttributeError):
        jlog(jid, "inter-rater: could not read Claude's answer: " + (r.stderr or r.stdout)[-200:])
        return None
    per_field, disagreements = {}, []
    for f, sch in props.items():
        pairs = [(keys[k][f], it[f]) for k, it in items.items() if k in keys and f in it and valid(it[f], sch)]
        if not pairs:
            continue
        a, b = [p[0] for p in pairs], [p[1] for p in pairs]
        entry = {"n": len(pairs)}
        if sch.get("type") in ("number", "integer"):
            entry["mae"] = round(sum(abs(x - y) for x, y in pairs) / len(pairs), 3)
            c = pearson(a, b)
            entry["correlation"] = None if c is None else round(c, 3)
        else:
            entry["agreement"] = round(sum(1 for x, y in pairs if same(x, y)) / len(pairs), 3)
            kp = cohen_kappa(a, b)
            entry["kappa"] = None if kp is None else round(kp, 3)
        per_field[f] = entry
    for k, it in items.items():
        if k not in keys:
            continue
        diffs = {f: {"gemini": keys[k][f], "claude": it[f]} for f in props
                 if f in it and props[f].get("type") not in ("number", "integer") and not same(keys[k][f], it[f])}
        if diffs:
            disagreements.append({"record": {kk: vv for kk, vv in keys[k].items() if kk not in props},
                                  "differs": diffs})
    dpath = out.with_name(out.stem + ".disagreements.jsonl")
    if disagreements:
        dpath.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in disagreements), encoding="utf-8")
    result = {"sampled": n, "of": len(rows), "model": ir["model"], "fields": per_field,
              "disagreements": str(dpath) if disagreements else None}
    rep["inter_rater"] = result
    relay.write_json(jdir(jid) / "report.json", rep)
    line = ""
    cats = [(n, e) for n, e in per_field.items() if "agreement" in e]
    if cats or per_field:
        name, main = (cats or list(per_field.items()))[0]
        line = (f"'{name}': " + (f"{100 * main['agreement']:.0f}% agreement" if "agreement" in main else f"MAE {main['mae']}")
                + (f", kappa {main['kappa']:.2f}" if main.get("kappa") is not None else ""))
    relay.notify("job", f"🔍 Claude re-labelled {n} of {len(rows)} records of "
                        f"<b>{relay.html_escape(job.get('title', jid))}</b> blind: {line}", always=True, desktop=True)
    return result


def run_dataset(jid, job):
    props, required = parse_fields(job["fields"])
    relay.write_json(Path(job["work_dir"]) / "schema.json", items_schema(props, required))
    gold = load_calibration(job.get("calibration"))
    batches = prepare_batches(jid, job)
    update(jid, state="running", total=len(batches), pid=os.getpid(), started=status(jid).get("started") or time.time(),
           message=None)
    progress(jid, job)
    ctx = Ctx(jid)
    workers = 1 if job["runner"] == "ide" else max(1, int(job["concurrency"]))
    with ThreadPoolExecutor(workers) as ex:
        for f in [ex.submit(run_batch, ctx, jid, job, bd, props, required, gold) for bd in batches]:
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                jlog(jid, f"batch crashed: {e!r}")
    if ctx.cancelled:
        return update(jid, state="cancelled", finished=time.time())
    if ctx.paused:
        merge(jid, job)
        return update(jid, state="paused", message=ctx.paused)
    rep = merge(jid, job)
    msg = f"{rep['annotated']}/{rep['records']} records annotated"
    try:
        ir_on = job.get("inter_rater")
        if ir_on is None:
            ir_on = relay.load_config()["delegation"]["inter_rater"]["enabled"]
        if rep["annotated"] and ir_on:
            update(jid, message="checking agreement: Claude is re-labelling a sample...")
            ir = inter_rater(jid, job, rep)
            cats = [e for e in (ir or {}).get("fields", {}).values() if "agreement" in e]
            if cats:
                msg += f" - Claude agrees on {100 * cats[0]['agreement']:.0f}% of a {ir['sampled']}-record sample"
    except Exception as e:  # noqa: BLE001
        jlog(jid, f"inter-rater failed: {e!r}")
    update(jid, state="done", finished=time.time(), output=rep["output"], message=msg)


# ---------------------------------------------------------------- task jobs

TASK_PREFIX = """[CONTEXT RELAY TASK] A lead engineer (Claude) delegated this well-defined task to you. Do exactly this task, nothing beyond it: do not refactor or reformat unrelated code, do not change files the task does not need. If something is ambiguous, pick the most conservative reading and note it in your summary.

TASK
"""


def git_state(cwd):
    r = subprocess.run(["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    return set(r.stdout.splitlines()) if r.returncode == 0 else None


def run_task(jid, job):
    update(jid, state="running", pid=os.getpid(), started=time.time(), total=1, done=0)
    before = git_state(job["cwd"])
    ctx = Ctx(jid)
    summary_file = Path(job["work_dir"]) / "summary.md"
    root = Path(job["cwd"]).resolve()
    prompt = TASK_PREFIX + job["task"] + f"\n\nPROJECT FOLDER\n{root} (relative paths in the task are relative to it)"
    if job.get("files"):
        prompt += "\n\nFILES IN SCOPE\n" + "\n".join(f"- {root / f}" for f in job["files"])
    if job["runner"] == "ide":
        prompt += (f"\n\nWhen you are done, write a short summary of what you changed (file paths, anything "
                   f"skipped and why) to {summary_file}.")
        text, err = run_ide_request(ctx, job, "task", prompt, summary_file, job["task_timeout_s"])
        answer, conv = text, None
    else:
        prompt += "\n\nWhen you are done, end with a short summary: files changed, anything skipped and why."
        if job["task_permissions"] == "all":
            extra = ["--dangerously-skip-permissions"]
        else:
            extra = []
            prompt += ("\n\nYou can read and edit files but you cannot run shell commands here: do not try. "
                       "Use the absolute paths above.")
        res, err = run_agy_cli(ctx, job, job["cwd"], prompt, job["task_timeout_s"], extra)
        answer, conv = (res or {}).get("response"), (res or {}).get("conversation_id")
    after = git_state(job["cwd"])
    changed = sorted({line[3:] for line in (after or set()) - (before or set())}) if after is not None else None
    if ctx.cancelled:
        return update(jid, state="cancelled", finished=time.time())
    report = {"job": jid, "ok": err is None, "error": err, "summary": answer, "changed_files": changed,
              "conversation_id": conv, "model": job["model"], "runner": job["runner"]}
    relay.write_json(jdir(jid) / "report.json", report)
    update(jid, state="done" if err is None else "failed", finished=time.time(), done=1 if err is None else 0,
           failed=0 if err is None else 1, message=err or f"{len(changed or [])} file(s) changed")


# ---------------------------------------------------------------- entry points

def new_dataset_job(input, instructions, fields, cwd, output=None, id_field="id", calibration=None,
                    runner=None, model=None, batch_size=None, concurrency=None, limit=None,
                    input_fields=None, hide_fields=None, keep_input=True, title=None, inter_rater=None):
    d = relay.load_config()["delegation"]
    inp = Path(input) if Path(input).is_absolute() else Path(cwd) / input
    if not inp.exists():
        raise ValueError(f"input file not found: {inp}")
    parse_fields(fields)  # fail early on a bad field spec
    out = Path(output) if output else inp.with_name(inp.stem + ".annotated.jsonl")
    out = out if out.is_absolute() else Path(cwd) / out
    cal = None
    if calibration:
        cal = str(Path(calibration) if Path(calibration).is_absolute() else Path(cwd) / calibration)
        load_calibration(cal)
    n = len(load_records(inp, id_field))
    jid = create("dataset", {
        "title": title or f"annotate {inp.name}", "cwd": str(cwd), "input": str(inp), "output": str(out),
        "instructions": instructions, "fields": fields, "id_field": id_field, "calibration": cal,
        "runner": runner or d["runner"], "model": model or d["model"],
        "batch_size": int(batch_size or d["batch_size"]), "concurrency": int(concurrency or d["concurrency"]),
        "max_retries": d["max_retries"], "calibration_per_batch": d["calibration_per_batch"] if cal else 0,
        "calibration_min_accuracy": d["calibration_min_accuracy"], "batch_timeout_s": d["batch_timeout_s"],
        "limit": int(limit) if limit else None, "input_fields": input_fields, "hide_fields": hide_fields,
        "keep_input": keep_input, "inter_rater": inter_rater,
        "records": min(n, int(limit)) if limit else n})
    spawn(jid)
    return jid


def new_task_job(task, cwd, files=None, runner=None, model=None, title=None):
    d = relay.load_config()["delegation"]
    jid = create("task", {"title": title or task.strip().splitlines()[0][:80], "cwd": str(cwd), "task": task,
                          "files": files, "runner": runner or d["runner"], "model": model or d["model"],
                          "task_timeout_s": d["task_timeout_s"],
                          "task_permissions": d.get("task_permissions", "edits")})
    spawn(jid)
    return jid


def summary(jid):
    job, st = load_job(jid), status(jid)
    rep = relay.read_json(jdir(jid) / "report.json", {}) or {}
    head = f"Job {jid} ({job['kind']}, {job['runner']}, {job['model']}): {st.get('state')}"
    if st.get("message"):
        head += f" | {st['message']}"
    lines = [head, f"  {job.get('title', '')}"]
    if job["kind"] == "dataset":
        total = st.get("total") or 0
        lines.append(f"  batches {st.get('done', 0)}/{total} ok, {st.get('failed', 0)} failed, "
                     f"{st.get('retried', 0)} retried; {job.get('records')} records")
        if st.get("cal_n"):
            lines.append(f"  calibration {st['cal_ok']}/{st['cal_n']} ({100 * st['cal_ok'] / st['cal_n']:.0f}%)")
        if st.get("state") == "running" and st.get("avg_batch_s") and total:
            left = total - st.get("done", 0) - st.get("failed", 0)
            eta = left * st["avg_batch_s"] / (1 if job["runner"] == "ide" else job["concurrency"])
            lines.append(f"  about {eta / 60:.0f} min left")
        if rep:
            lines.append(f"  output: {rep['output']} ({rep['annotated']}/{rep['records']} records)")
            if rep.get("failed_records"):
                lines.append(f"  not annotated: {rep['failed_records']} (job resume {jid} retries them)")
    elif rep:
        lines.append(f"  changed files: {', '.join(rep.get('changed_files') or []) or 'none detected'}")
        if rep.get("error"):
            lines.append(f"  error: {rep['error']}")
        if rep.get("summary"):
            lines.append("  Gemini's summary:\n" + "\n".join("    " + s for s in rep["summary"].strip().splitlines()))
    lines.append(f"  files: {jdir(jid)} (log.txt, report.json)")
    return "\n".join(lines)


def cancel(jid):
    (jdir(jid) / "cancel").write_text("1")
    st = status(jid)
    if st.get("state") in ("queued", "paused"):
        update(jid, state="cancelled")
    return f"cancel requested for {jid}"


def resume(jid):
    job = load_job(jid)
    (jdir(jid) / "cancel").unlink(missing_ok=True)
    if job["kind"] == "dataset":  # failed batches get another round; finished ones are kept
        for bd in (Path(job["work_dir"]) / "batches").iterdir():
            r = relay.read_json(bd / "result.json", {}) or {}
            if r and not r.get("ok"):
                (bd / "result.json").unlink()
    update(jid, state="queued", message="resumed")
    spawn(jid)
    return f"resumed {jid}"


def wait(jid, poll=5):
    while status(jid).get("state") not in (*TERMINAL, "paused"):
        time.sleep(poll)
    print(summary(jid))


def cli(a):
    def opt(name, default=None):
        return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default

    cwd = opt("--cwd", os.getcwd())
    if a[0] == "annotate":
        jid = new_dataset_job(opt("--input"), opt("--instructions"), opt("--fields"), cwd, opt("--output"),
                              opt("--id-field", "id"), opt("--calibration"), opt("--runner"), opt("--model"),
                              opt("--batch-size"), opt("--concurrency"), opt("--limit"),
                              inter_rater=True if "--inter-rater" in a else None)
        print(jid)
    elif a[0] == "delegate-task":
        print(new_task_job(opt("--task"), cwd, runner=opt("--runner"), model=opt("--model")))
    elif a[1] == "run":
        job = load_job(a[2])
        try:
            (run_dataset if job["kind"] == "dataset" else run_task)(a[2], job)
        except Exception as e:  # noqa: BLE001
            import traceback
            jlog(a[2], traceback.format_exc())
            update(a[2], state="failed", message=f"{e!r}"[:300], finished=time.time())
        st = status(a[2])
        icon = {"done": "✅", "paused": "⏸️", "cancelled": "🛑"}.get(st.get("state"), "❌")
        cal = f" · calibration {100 * st['cal_ok'] / st['cal_n']:.0f}%" if st.get("cal_n") else ""
        from . import smart
        smart.event("job", f"{st.get('state')}: {job.get('title', a[2])}", job=a[2])
        relay.notify("job", f"{icon} Gemini job <b>{relay.html_escape(job.get('title', a[2]))}</b>: "
                            f"{st.get('state')}{cal}\n{relay.html_escape(st.get('message') or '')}")
    elif a[1] == "list":
        for jid in all_jobs():
            st = status(jid)
            print(f"{jid}  {st.get('kind', ''):7} {st.get('state', ''):9} {st.get('done', 0)}/{st.get('total', 0)}  "
                  f"{st.get('title', '')}")
    elif a[1] == "status":
        print(summary(a[2]))
    elif a[1] == "wait":
        wait(a[2])
    elif a[1] == "cancel":
        print(cancel(a[2]))
    elif a[1] == "resume":
        print(resume(a[2]))
    return 0
