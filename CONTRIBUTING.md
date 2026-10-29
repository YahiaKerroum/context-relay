# Contributing

Thanks for helping. This is a small project; the bar is "clear, tested, and does not slow the hooks down".

## Setup

```bash
git clone https://github.com/YahiaKerroum/context-relay.git
cd context-relay
pip install -e ".[dev]"
cd tests && python -m unittest discover -s .
ruff check .
```

The suite uses a throwaway home directory (`tests/_env.py`) and a fake home for the installer, so it never touches your real Claude, Antigravity or Telegram setup. Set `SKIP_SCREEN=1` to skip the tests that need Pillow and a screen.

## Ground rules

* **No runtime dependencies.** Hooks run on every agent event and must start instantly. Optional extras (such as Pillow) must be imported lazily and degrade gracefully.
* **Hooks fail open.** An exception in a hook must log and let the agent continue, never freeze or break the chat.
* **No secrets or personal paths** in code, tests, docs or screenshots. Use neutral sample data (`my-app`).
* **Cross-platform by default.** Windows-only code goes behind `os.name == "nt"` and gets a fallback or a clear message elsewhere.
* **Match the surrounding code**: naming, comment density, idiom. Python 3.10 syntax at most.
* **Changes to behaviour come with a test**, and a line in `CHANGELOG.md`.

## Layout

```text
src/context_relay/      the package (relay.py = hooks and CLI, guard.py = safety nets, daemon.py + remote.py = Telegram,
                        delegate.py = Gemini, installer.py = setup, extension/ = the editor extension, skills/)
tests/                  unittest suites; live_phone.py is a manual end-to-end script
docs/                   user documentation
```

## Pull requests

1. Open an issue first for anything big.
2. Keep the PR focused; describe what changed and how you tested it, including the OS.
3. CI must be green (tests on Windows and Linux, Python 3.10 and 3.13, ruff, extension syntax check).

Testing on macOS or Linux machines is the most valuable contribution right now. If you try Context Relay there, please tell us what worked and what did not.
