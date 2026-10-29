# Telegram remote control

Optional, free, and about two minutes. You need a Telegram account and the Context Relay background helper (`context-relay setup` starts it).

## Set up

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`. Choose a name and a username ending in `bot`. BotFather replies with a **token** that looks like `123456789:AA...`.
2. Run:

   ```bash
   context-relay telegram setup
   ```

   Paste the token when asked. It is read without echo and saved to `~/.context-relay/secrets.json` with owner-only permissions. In the editor you can use *Context Relay: Set Up Telegram (Bot Token)* instead. In scripts: `echo "$TOKEN" | context-relay telegram setup --token-stdin`.
3. Open your bot in Telegram, press **Start**, and send the six-digit pairing code that setup printed. The bot answers "Paired".

`context-relay telegram status` shows the bot, whether it is paired and whether the helper runs. `context-relay telegram pair` shows the code again. `context-relay telegram unpair` forgets the chat.

## Safety

* Only the paired chat is answered. Everyone else gets nothing.
* The token controls the bot. If it leaks, send `/revoke` to BotFather and run `context-relay telegram setup` with the new one.
* Use a **private chat** with the bot, not a group.
* Turn on two-step verification in Telegram: whoever controls your account controls your agents.
* Approvals wait for your answer and are **denied** after `guard.approval_timeout_min` (10 by default). No answer never means yes.

## When messages arrive

`telegram.notify` and `telegram.approvals` take `away` (default: only while you are away), `always` or `off`. You count as away after `/away`, when the PC is locked (Windows), or after `away_after_min` idle minutes. `/back` or using the PC ends it, and you get a "while you were away" summary.

## Commands

| | |
|---|---|
| Talk | just type · `/chats` · `/new task` · `/drop idea` · `/inbox` |
| See | `/status` · `/live` · `/screenshot` (Windows) · `/preview [port]` · `/missed` |
| Steer | `/pause` · `/resume` · `/stop` · `/handoff` · `/tests` · `/commit` · `/summary` · `/undo` |
| Rules | `/rule text` · `/rules` · `/unrule id` |
| Watch | `/watch path` · `/kaggle user/slug` · `/watchers` |
| Modes | `/away` · `/back` · `/mode wifi\|data\|auto` · `/limit [off]` · `/night` |

Tips: reply to a message from an agent to answer that chat specifically. `/rule never touch data/` pins the rule and protects `data/`. On a phone hotspot, `/mode data` gives smaller images and slower live updates.
