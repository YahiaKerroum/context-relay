---
name: coordinate
description: Split a task between Claude (planning, hard implementation, review) and Gemini running in the user's Antigravity (bulk, tedious, parallel work such as annotating or labeling datasets, translations, docstrings, boilerplate). Use when the user says /coordinate, asks to "use Gemini/Antigravity for the boring part", or when a task contains a large repetitive chunk next to harder engineering work.
---

# Coordinate Claude and Gemini

You are the lead. Gemini, running in the user's Antigravity on their plan (not the Gemini API), is a fast, cheap
helper. It is good at volume and weaker at judgment. Split the work so each does what it is good at.

## 1. Plan and split

Write a short plan that sorts every piece of work into one of two lists:

**Keep (you):** architecture and design, tricky logic, anything that needs reading a lot of code at once,
debugging, deciding the annotation guidelines, building the calibration set, reviewing Gemini's output,
integration.

**Delegate (Gemini):** per-record work over many items (labeling, classification, extraction, rewriting,
translating, judging), mechanical edits over many files (docstrings, type hints, renames, fixtures,
boilerplate from a pattern you wrote), anything you could describe in a page of rules plus examples.

Show the user the split in two or three lines. Then start, unless they object.

## 2. Datasets: `gemini_annotate_dataset`

1. Write the guidelines as if for a careful contractor: the label set, the definitions, edge cases, and one or two
   worked examples. Ambiguity in the guidelines turns straight into noise in the labels.
2. **Calibration set (do not skip):** label 10–30 representative items yourself, including hard ones. Save them as
   JSONL with the same input fields as the data plus `"expected": {field: answer}`. The tool mixes a few into every
   batch, blind, and redoes batches that miss them. This is what catches Gemini drifting or going lenient.
3. **Pilot:** run with `limit: 40`. Read about 10 outputs and the calibration score. Tighten the guidelines and
   run the pilot again if needed.
4. **Full run.** Keep `batch_size` at 40 or below: every batch is a fresh conversation, which is the point. Start a
   background watcher (`python ".../relay.py" job wait <id>` with run_in_background) and carry on with your own list
   while it runs.
5. **Review** before using the output: the calibration accuracy, a random sample of 20, and the
   `.failed.jsonl` file if there is one. Redo failed batches with `gemini_job_resume`. Report the agreement numbers
   to the user.

The `runner` option: `cli` (default) is headless and parallel. `ide` opens each batch as a new conversation
in the Antigravity IDE so the user can watch. Use `ide` when the user wants to see it work.

## 3. Mechanical code work: `gemini_run_task`

Give exact scope (files or globs), the conventions to follow, an example of the finished pattern (do one file
yourself first), and how to verify. Afterwards, review the reported changed files with `git diff`, run the tests,
and fix or revert what is off. Never delegate security-sensitive code, schema migrations, or anything you could
not check quickly.

## 3b. When you are unsure: ask, don't guess

If you are genuinely torn between options that matter (which approach, what to delete, an ambiguous requirement),
call `ask_user` with 2-5 short options and the one you'd pick. It reaches the user's editor and phone, and you wait
for the answer. If they can't be reached you are told so: take the safest reversible option and write the
assumption in your final message. Use `notify_user` for a milestone worth a ping.

The user may also have pinned rules (shown at session start) and protected paths: follow them, and when one blocks
you, ask rather than work around it.

## 4. While Gemini works

Do your own list. Check progress with `gemini_job_status` only when you need the result. When Claude's own usage
limits run low (the Context Relay panel shows them), move more of the remaining volume to Gemini.

## 5. Report

At the end, report: what you did, what Gemini did (job ids, record counts, calibration accuracy, files changed),
what you checked, and anything left open.
