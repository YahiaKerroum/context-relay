# Changelog

## 0.5.0 — first public release

- Packaged as an installable Python project: `pip install`, then `context-relay setup` (with `--dry-run`, `--yes`, per-part opt-outs), `doctor`, `status`, `uninstall [--purge]`.
- Telegram setup from the command line (`context-relay telegram setup | pair | status | unpair`) and from the editor; the token is read without echo and stored owner-only.
- The Claude context window is detected from your recent chats (200k or 1M) instead of assumed.
- The installer writes only your overrides to `config.json`, so new defaults reach existing installs.
- Hooks and the extension run `python -m context_relay`; no dependence on a folder location.
- Editor extension 0.5.0, still `local.context-relay`, so older installs upgrade in place.
- CI on Windows and Linux (Python 3.10 and 3.13).

### Features carried over from the private 0.4 line

Context relay at a threshold; danger gate and data guard; undo snapshots; pinned rules; stuck detector; "done means green" checks; Telegram remote (live view, screenshots, previews, approvals, ideas inbox); `ask_user`; log and Kaggle watchers; night shift; away card; connection-drop recovery; delegation to Gemini with calibration and an optional inter-rater check; dashboard.
