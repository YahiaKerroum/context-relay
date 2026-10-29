"""Numbers for the dashboard: tokens per chat, Gemini work per day, time saved, the handoff chain with what each
session changed, and a count of what the safety nets did."""
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import guard
from . import relay
from . import smart
CACHE = relay.HOME / "stats_cache.json"
DAY = 86400


def transcript_usage(st, cache):
    path = st.get("transcript")
    if not path or not os.path.exists(path):
        return {}
    stt = os.stat(path)
    key = f"{path}"
    c = cache.get(key)
    if c and c["mtime"] == stt.st_mtime and c["size"] == stt.st_size:
        return c["u"]
    calls = out = peak = 0
    seen = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if st["tool"] == "claude":
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                if not u or d.get("isSidechain") or m.get("model") == "<synthetic>":
                    continue
                seen[m.get("id") or d.get("uuid")] = u
            elif '"PLANNER_RESPONSE"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                calls += 1
                out += d.get("output_tokens", 0)
                peak = max(peak, d.get("input_tokens", 0) + d.get("cache_read_tokens", 0))
    for u in seen.values():
        calls += 1
        out += u.get("output_tokens", 0)
        peak = max(peak, u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                   + u.get("cache_read_input_tokens", 0))
    res = {"calls": calls, "out_tokens": out, "peak_tokens": peak}
    cache[key] = {"mtime": stt.st_mtime, "size": stt.st_size, "u": res}
    return res


def chats(days=7):
    cache = relay.read_json(CACHE, {}) or {}
    out = []
    for tool in ("claude", "agy"):
        for p in (relay.STATE / tool).glob("*.json"):
            st = relay.read_json(p, {}) or {}
            if st.get("pct") is None or time.time() - st.get("updated", 0) > days * DAY:
                continue
            u = transcript_usage(st, cache) if time.time() - st.get("updated", 0) < days * DAY else {}
            out.append({"sid": st["sid"], "tool": tool, "title": st.get("title") or f"{tool} chat {st['sid'][:6]}",
                        "project": Path(st["cwd"]).name, "cwd": st["cwd"], "pct": round(st["pct"], 1),
                        "tokens": st.get("tokens", 0), "window": st.get("window", 0), "status": st.get("status"),
                        "first": st.get("first", st.get("updated")), "updated": st.get("updated"),
                        "model": st.get("model", ""), **u})
    try:
        relay.write_json(CACHE, cache)
    except OSError:
        pass
    return sorted(out, key=lambda c: c["updated"], reverse=True)


def gemini_days(cfg, n=14):
    days = defaultdict(lambda: {"jobs": 0, "batches": 0, "records": 0, "saved_s": 0})
    total_saved = 0
    manual = cfg["delegation"]["inter_rater"]["manual_seconds_per_record"]
    jobs = relay.HOME / "jobs"
    if jobs.exists():
        for jd in jobs.iterdir():
            st = relay.read_json(jd / "status.json", {}) or {}
            job = relay.read_json(jd / "job.json", {}) or {}
            end = st.get("finished") or st.get("updated") or 0
            if not end or time.time() - end > n * DAY:
                continue
            day = datetime.fromtimestamp(end).strftime("%Y-%m-%d")
            d = days[day]
            d["jobs"] += 1
            if job.get("kind") == "dataset":
                rep = relay.read_json(jd / "report.json", {}) or {}
                d["batches"] += st.get("done", 0)
                recs = rep.get("annotated", 0)
                d["records"] += recs
                wall = max(0, (st.get("finished", end) or end) - (st.get("started") or end))
                saved = max(0, recs * manual - wall)
                d["saved_s"] += saved
                total_saved += saved
    series = []
    for i in range(n - 1, -1, -1):
        day = datetime.fromtimestamp(time.time() - i * DAY).strftime("%Y-%m-%d")
        series.append({"day": day, **days.get(day, {"jobs": 0, "batches": 0, "records": 0, "saved_s": 0})})
    return series, total_saved


def chain_timeline(cs):
    links = {}
    p = relay.HOME / "chain.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            links.setdefault(d["req"], {}).update(d)
    nxt, prev = {}, set()
    for r in links.values():
        if r.get("from_sid") and r.get("to_sid"):
            nxt[r["from_sid"]] = (r["to_sid"], r)
            prev.add(r["to_sid"])
    by_sid = {c["sid"]: c for c in cs}
    chains = []
    for head in [s for s in nxt if s not in prev]:
        seq = [head]
        while seq[-1] in nxt and len(seq) < 30:
            seq.append(nxt[seq[-1]][0])
        nodes = []
        snaps_cache = {}
        firsts = []
        for sid in seq:
            c = by_sid.get(sid)
            cwd = (c or {}).get("cwd") or next((r.get("cwd") for r in links.values() if r.get("from_sid") == sid), None)
            if cwd and cwd not in snaps_cache:
                try:
                    snaps_cache[cwd] = guard.snapshots(cwd, 400)
                except Exception:  # noqa: BLE001
                    snaps_cache[cwd] = []
            mine = [s for s in snaps_cache.get(cwd, []) if s.get("sid") == sid]
            firsts.append(mine[-1]["sha"] if mine else None)
            nodes.append({"sid": sid, "cwd": cwd, "c": c})
        out_nodes = []
        for i, n in enumerate(nodes):
            a = firsts[i]
            b = next((x for x in firsts[i + 1:] if x), None)
            files = []
            if a and n["cwd"]:
                try:
                    files = guard.diff_stat(n["cwd"], a, b)
                except Exception:  # noqa: BLE001
                    files = []
            c = n["c"] or {}
            link = nxt.get(n["sid"], (None, {}))[1]
            out_nodes.append({
                "sid": n["sid"], "tool": c.get("tool") or link.get("tool", "?"),
                "title": c.get("title") or f"chat {n['sid'][:6]}", "pct": c.get("pct"),
                "first": c.get("first"), "updated": c.get("updated"),
                "files": len(files), "added": sum(f[0] for f in files), "deleted": sum(f[1] for f in files),
                "top": [f[2] for f in sorted(files, key=lambda f: -(f[0] + f[1]))[:5]],
                "handoff": link.get("handoff"), "why": link.get("why"), "to_tool": link.get("to_tool")})
        chains.append({"project": Path(nodes[0]["cwd"] or "?").name, "nodes": out_nodes})
    return sorted(chains, key=lambda c: -(c["nodes"][-1].get("updated") or 0))[:6]


def counts(hours=24):
    since = time.time() - hours * 3600
    c = defaultdict(int)
    for e in smart.events_since(since):
        c[e["kind"]] += 1
        if e["kind"] == "approval":
            c["approval_" + e.get("verdict", "?")] += 1
    return dict(c)


def build():
    cfg = relay.load_config()
    cs = chats()
    series, saved = gemini_days(cfg)
    return {"now": time.time(), "chats": cs, "gemini_days": series, "saved_s": saved,
            "chains": chain_timeline(cs), "counts": counts(24), "counts_week": counts(24 * 7),
            "totals": {"out_tokens": sum(c.get("out_tokens", 0) for c in cs if c["updated"] > time.time() - DAY),
                       "chats_today": sum(1 for c in cs if c["updated"] > time.time() - DAY),
                       "handoffs_week": len({r["req"] for r in _chain_rows() if r.get("from_sid")
                                             and r["ts"] > time.time() - 7 * DAY})}}


def _chain_rows():
    p = relay.HOME / "chain.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows
