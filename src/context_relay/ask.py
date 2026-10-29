"""ask_user: an agent that is torn between options asks you instead of guessing.

The question appears in the editor and, when you're away (or after a couple of minutes), on Telegram with one
button per option. The agent waits for the answer; if nobody can be reached it is told to take the safest
option and write the assumption down.
"""
import os
import time
import uuid

from . import guard
from . import relay
from . import smart
QUESTIONS = relay.HOME / "questions"


def fallback(default, why):
    pick = f" Prefer: {default}." if default else ""
    return (f"{why} Do not block on this: take the safest, most reversible option with your own best judgement.{pick} "
            f"State the assumption you made clearly in your final message so the user can correct it.")


def ask_user(question, options=None, default=None, context="", tool="claude", cwd=None):
    cfg = relay.load_config()["ask"]
    if os.environ.get("CONTEXT_RELAY_NIGHT"):
        return fallback(default, "The user is asleep (night shift), so no one can answer right now.")
    if not guard.approver_reachable():
        return fallback(default, "The user can't be reached right now (no editor window, Telegram not connected).")
    options = [str(o) for o in (options or [])][:8]
    qid = uuid.uuid4().hex[:6]
    path = QUESTIONS / f"{qid}.json"
    relay.write_json(path, {"id": qid, "question": question, "options": options, "default": default,
                            "context": (context or "")[:500], "tool": tool, "cwd": cwd or os.getcwd(),
                            "created": time.time(), "status": "pending", "telegram_sent": False})
    smart.event("ask", f"asked: {question[:90]}", id=qid)
    end = time.time() + cfg["timeout_min"] * 60
    while time.time() < end:
        q = relay.read_json(path, {}) or {}
        if q.get("status") == "answered":
            smart.event("ask", f"answered: {q.get('answer', '')[:60]}", id=qid)
            return f"The user answered: {q['answer']}"
        time.sleep(1)
    q = relay.read_json(path, {}) or {}
    q["status"] = "expired"
    relay.write_json(path, q)
    return fallback(default, f"The user didn't answer within {cfg['timeout_min']} minutes.")


def answer(qid, text, by="editor"):
    path = QUESTIONS / f"{qid}.json"
    q = relay.read_json(path, None)
    if not q or q.get("status") != "pending":
        return False
    q.update(status="answered", answer=text, by=by)
    relay.write_json(path, q)
    return True
