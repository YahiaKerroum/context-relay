"""MCP server (stdio, JSON-RPC) that gives Claude Code tools to delegate work to Gemini in Antigravity."""
import json
import os
import sys
import traceback

from . import delegate
from . import relay
RUNNER = {"type": "string", "enum": ["cli", "ide"],
          "description": "cli: Antigravity CLI, headless and parallel (default). ide: each batch opens as a new "
                         "conversation in the Antigravity IDE so the user can watch; one at a time."}
MODEL = {"type": "string", "description": "Antigravity model id, e.g. gemini-3.8-flash-low / -medium / -high. "
                                          "Defaults to the user's setting."}
WAIT_HINT = ("To be told when it finishes, run `{cmd}` with run_in_background; "
             "it exits with the final summary.")

ASK_TOOLS = [
    {
        "name": "ask_user",
        "description": (
            "Ask the user a question instead of guessing. Use it when you are genuinely torn between options that "
            "matter and would be costly to undo, or when a decision is the user's to make (which approach, which "
            "file to keep, whether to delete something, an ambiguous requirement). Do NOT use it for trivia or "
            "things you can find out yourself. Give 2-5 short options when the choices are clear; leave options "
            "empty for an open question. The question reaches the user in their editor and on their phone "
            "(Telegram), and you wait for the answer. If the user can't be reached you are told so, and should take "
            "the safest reversible option and state the assumption in your final message."),
        "inputSchema": {"type": "object", "required": ["question"], "properties": {
            "question": {"type": "string", "description": "One clear question."},
            "options": {"type": "array", "items": {"type": "string"}, "description": "2-5 short choices (optional)."},
            "default": {"type": "string", "description": "What you'd pick if forced to guess."},
            "context": {"type": "string", "description": "One or two lines of why you're asking."}}},
    },
    {
        "name": "notify_user",
        "description": "Send the user a short message on their phone and desktop, e.g. a milestone or something they "
                       "should look at. Does not wait for a reply. Keep it to a line or two.",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {
            "text": {"type": "string"},
            "urgent": {"type": "boolean", "description": "Send even when the user is at their desk."}}},
    },
]

TOOLS = [
    {
        "name": "gemini_annotate_dataset",
        "description": (
            "Delegate bulk annotation / labeling / extraction / classification of a dataset file to Gemini running in "
            "the user's Antigravity (their plan, not the Gemini API). Use this for tedious, repetitive per-record work "
            "(hundreds or thousands of items) so you can spend your effort on planning, hard code and review.\n"
            "How it works: records go out in small blind batches (keys r1..rN, ids hidden), each batch in a FRESH "
            "Antigravity conversation (long conversations drift and get lenient), with structured output. Every batch "
            "is checked: each record answered once, fields valid, and if a calibration file is given, gold items mixed "
            "into each batch must reach the accuracy bar, otherwise the batch is redone in a new conversation. Quota "
            "errors back off, then pause the job (gemini_job_resume continues it).\n"
            "Good practice: (1) write precise instructions with edge cases and one or two examples; (2) build a "
            "calibration JSONL of 10-30 items you label yourself: input fields plus \"expected\": {field: answer}; "
            "(3) run a pilot with limit=40, read some outputs and the calibration score, tighten the instructions; "
            "(4) run the full set; (5) spot-check the output before using it. Runs in the background and returns a "
            "job id at once."),
        "inputSchema": {"type": "object", "required": ["input_path", "instructions", "fields"], "properties": {
            "input_path": {"type": "string", "description": "JSONL, JSON list, CSV/TSV, or plain text (one record per line). Relative paths resolve against cwd."},
            "instructions": {"type": "string", "description": "Full annotation guidelines for Gemini."},
            "fields": {"description": "Output fields per record. Either a compact spec like "
                                      "'label:positive|negative|neutral, confidence:number, rationale:string' "
                                      "(types: string number integer boolean, a|b|c enums, type[] arrays) or a JSON "
                                      "schema object with properties/required.",
                       "anyOf": [{"type": "string"}, {"type": "object"}]},
            "cwd": {"type": "string", "description": "Project folder (defaults to the current one)."},
            "output_path": {"type": "string", "description": "Default: <input>.annotated.jsonl. .csv/.tsv also work."},
            "id_field": {"type": "string", "description": "Record id field (default 'id'; row number if absent)."},
            "calibration_path": {"type": "string", "description": "JSONL gold items: input fields + \"expected\": {...}. Strongly recommended."},
            "input_fields": {"type": "array", "items": {"type": "string"}, "description": "Only show these fields to Gemini."},
            "hide_fields": {"type": "array", "items": {"type": "string"}, "description": "Never show these fields (e.g. existing labels)."},
            "limit": {"type": "integer", "description": "Only the first N records (pilot runs)."},
            "batch_size": {"type": "integer", "description": "Records per conversation (default from settings, 20). Keep it at 40 or below."},
            "concurrency": {"type": "integer", "description": "Parallel conversations for the cli runner (default 3)."},
            "runner": RUNNER, "model": MODEL,
            "keep_input": {"type": "boolean", "description": "Output rows keep the input fields (default true)."},
            "inter_rater": {"type": "boolean", "description": "After the run, Claude blind re-labels a ~5% sample and "
                                                              "reports agreement (kappa). Default: the user's setting."}}},
    },
    {
        "name": "gemini_run_task",
        "description": (
            "Delegate a well-defined, tedious task to the Antigravity agent (Gemini), which edits files in the project: "
            "boilerplate, docstrings or type hints across many files, translations, renaming, test fixtures, data "
            "cleaning scripts you have specified, repetitive refactors. Keep the hard design and tricky logic for "
            "yourself. Spell out the exact scope, files, conventions and acceptance criteria. Runs in the background "
            "and returns a job id; when it ends, review the changed files it reports (git diff) before relying on them."),
        "inputSchema": {"type": "object", "required": ["task"], "properties": {
            "task": {"type": "string"}, "cwd": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Files in scope."},
            "runner": RUNNER, "model": MODEL}},
    },
    {
        "name": "gemini_job_status",
        "description": "Progress of a delegation job (batches done, retries, calibration accuracy, time left, output). "
                       "Without job_id, lists recent jobs.",
        "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}},
    },
    {
        "name": "gemini_job_cancel",
        "description": "Cancel a running delegation job.",
        "inputSchema": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}},
    },
    {
        "name": "gemini_job_resume",
        "description": "Resume a paused (quota) or finished job: failed batches are redone, finished ones kept.",
        "inputSchema": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}},
    },
]


def call(name, args, who="claude"):
    cwd = args.get("cwd") or os.getcwd()
    if name == "ask_user":
        from . import ask
        return ask.ask_user(args["question"], args.get("options"), args.get("default"), args.get("context", ""),
                            who, cwd)
    if name == "notify_user":
        relay.notify("agent", "🔔 " + relay.html_escape(args["text"]), always=bool(args.get("urgent")), desktop=True)
        return "sent"
    hint = lambda jid: WAIT_HINT.format(cmd=" ".join(relay.cli_cmd("job", "wait", jid)), jid=jid)  # noqa: E731
    if name == "gemini_annotate_dataset":
        jid = delegate.new_dataset_job(
            args["input_path"], args["instructions"], args["fields"], cwd, args.get("output_path"),
            args.get("id_field", "id"), args.get("calibration_path"), args.get("runner"), args.get("model"),
            args.get("batch_size"), args.get("concurrency"), args.get("limit"), args.get("input_fields"),
            args.get("hide_fields"), args.get("keep_input", True),
            None, args.get("inter_rater"))
        job = delegate.load_job(jid)
        n_batches = -(-job["records"] // job["batch_size"])
        return (f"Started job {jid}: {job['records']} records in {n_batches} batches of {job['batch_size']} on "
                f"{job['model']} via the Antigravity {job['runner']} runner; output -> {job['output']}.\n" + hint(jid))
    if name == "gemini_run_task":
        jid = delegate.new_task_job(args["task"], cwd, args.get("files"), args.get("runner"), args.get("model"))
        return f"Started job {jid} in {cwd}.\n" + hint(jid)
    if name == "gemini_job_status":
        if args.get("job_id"):
            return delegate.summary(args["job_id"])
        jobs = delegate.all_jobs(15)
        return "\n\n".join(delegate.summary(j) for j in jobs) if jobs else "No delegation jobs yet."
    if name == "gemini_job_cancel":
        return delegate.cancel(args["job_id"])
    if name == "gemini_job_resume":
        return delegate.resume(args["job_id"])
    raise ValueError(f"unknown tool {name}")


def send(obj):
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def serve(only=None):
    tools = ASK_TOOLS if only == "ask" else TOOLS + ASK_TOOLS
    who = "agy" if only == "ask" else "claude"
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:  # notification
            continue
        try:
            if method == "initialize":
                result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "context-relay", "version": "0.2.0"},
                          "instructions": (
                              "ask_user: when you are genuinely torn between options that matter, or a decision is "
                              "the user's to make, ask instead of guessing (it reaches their phone). "
                              + ("" if only == "ask" else
                                 "Delegate tedious bulk work to Gemini in Antigravity; keep planning, hard "
                                 "implementation and review for yourself."))}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                p = msg.get("params") or {}
                try:
                    result = {"content": [{"type": "text", "text": call(p.get("name"), p.get("arguments") or {}, who)}]}
                except Exception as e:  # noqa: BLE001
                    relay.log(f"mcp {p.get('name')} error: {traceback.format_exc()}")
                    result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
            elif method == "ping":
                result = {}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:  # noqa: BLE001
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})
    return 0
