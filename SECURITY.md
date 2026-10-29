# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting: **Security → Report a vulnerability** on this repository. Include what you found, how to reproduce it, and what an attacker could do. You will get a first answer within a few days.

## What Context Relay touches

* It edits your Claude Code and Antigravity settings (with a backup) to register hooks.
* It stores a Telegram bot token in `~/.context-relay/secrets.json` (owner-only permissions where supported).
* It can run commands in your projects when you message the bot (limited by the `phone.allowed_tools` list and permission mode).
* It can take screenshots of your screen on request (Windows).

## Things to know

* The Telegram bot answers **only the paired chat**. Anyone who controls that Telegram account controls your agents, so protect it with two-step verification.
* The danger gate and data guard are safety nets based on pattern matching. They are not a sandbox and will not catch everything.
* Hooks fail open on purpose. A bug in Context Relay will not block your agent, but it also means the gate is not guaranteed to run.
* If your bot token leaks, revoke it with `/revoke` in @BotFather and run `context-relay telegram setup` again.

## Supported versions

Only the latest release receives fixes.
