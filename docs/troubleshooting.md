# Troubleshooting

Start with `context-relay doctor`: it checks Python, git, the CLIs and editors it can see, and your context window. Then look at the end of `~/.context-relay/relay.log`.

## Install

| Problem | Fix |
|---|---|
| `context-relay: command not found` | The Python scripts folder is not on `PATH`. Use `python -m context_relay ...`, or install with `pipx install git+https://github.com/YahiaKerroum/context-relay.git`. |
| "Python 3.10+ and git are required" | Install them, then rerun. Git is used for undo snapshots. |
| Setup skipped Claude, Antigravity or an editor | It only touches tools it finds. Install the tool (or put its CLI on `PATH`) and run `context-relay setup` again. |
| I want to see before it changes anything | `context-relay setup --dry-run` |
| `pip install` fails while building | `python -m pip install -U pip setuptools`, then retry. |

## Nothing happens

* **Reload the editor window** after setup and start a **new chat**: open chats keep the hooks they started with.
* `context-relay status` shows what is installed; rerun `context-relay setup` to repair it.
* Hooks fail open, so an error inside Context Relay is silent in the chat. It is in `~/.context-relay/relay.log`.
* Recreated your Python environment? The hooks call the Python that ran `setup`. Run `context-relay setup` again from the new one.

## The handoff comes too early or too late

The percentage is `tokens / window`, so a wrong window means a wrong percentage. Run `context-relay setup --claude-window 1m` (or `200k`, or a token count). For Antigravity set `agy_window_tokens` in `~/.context-relay/config.json`.

## The new chat opens but nothing is sent

Auto-submit works on Windows. On macOS and Linux press Enter in the new chat once. To always review the first message yourself, set `"auto_submit": false`.

## Approvals

* **A command was blocked and I never saw a prompt.** The request appears in the editor, and on Telegram if paired. If nobody answers it is denied after `guard.approval_timeout_min`.
* **A harmless command keeps asking.** Approve it from the prompt, or turn the gate off with `"guard": {"danger_gate": false}`. `guard.extra_danger_patterns` adds your own patterns.

## Telegram

* `context-relay telegram status` should say "paired" and "daemon running".
* Helper not running: start `context-relay daemon` in a terminal to see errors directly.
* "Telegram rejected that token": copy it again from BotFather, without spaces.
* Lost the pairing code: `context-relay telegram pair`.
* If `api.telegram.org` is blocked on your network the bot cannot work; everything else does.

## Uninstall

`context-relay uninstall --dry-run` shows what would be removed. Backups of your settings made during setup sit next to the originals (`settings.backup-<date>.json`). If the extension remains, remove `local.context-relay` in the editor's Extensions view.

## Still stuck

[Open an issue](https://github.com/YahiaKerroum/context-relay/issues) with the output of `context-relay doctor` and the last lines of `relay.log`. **Remove tokens and private paths first.**
