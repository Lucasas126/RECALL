# Telegram

The Telegram bot is a second channel over the same service as the web UI. Start it with `uv run python run_telegram.py`
(or the `recall-telegram` container, see [Docker](../deployment/docker.md#telegram-on-the-server)). The bot token is read
from `TELEGRAM_BOT_TOKEN`.

**Who may talk to the bot.** The administrator lists the approved Telegram ids in the environment variable named by
`telegram.allowed_users_env` (default `TELEGRAM_ALLOWED_USERS`) as comma-separated `<telegram_id>=<user_id>` entries, where
`<user_id>` is the RECALL user the Telegram account belongs to. Several people can share one bot, each mapped to their own user:

```dotenv
TELEGRAM_ALLOWED_USERS=111111111=alice,222222222=bob
```

Entries that do not parse are reported at start-up. To find a Telegram id, message a bot such as `@userinfobot`, or ask the
administrator to read it from the bot's log. Only one process can poll a bot token.

## Telegram access

Messages from IDs that are not listed in `TELEGRAM_ALLOWED_USERS` are
ignored completely.

Telegram provides `/help`,
`/settings`, `/newchat`, `/selectmodel`, `/selectagent`, `/agent`, `/model`,
`/stt` and `/dream` (status of the active agent's [dream mode](dream.md#commands);
`/dream start` starts a session and `/dream cancel` stops it). Telegram's native command menu is populated at startup, and
`/help` prints the same command list in the chat.
Operational notices and errors use the user's saved English/Spanish preference;
the native command descriptions and `/help` text remain English.
Telegram creates a chat automatically on the first message after the bot
restarts, and after one hour without a user message. The first assistant
message in each automatically created chat begins with bold-italic
`New chat started`.
`/selectmodel` and `/selectagent` open the same kind of inline
panel as `/settings`: buttons send callbacks instead of chat messages, and the
bot edits the panel in place for pagination. Choosing an option saves it and
replaces the panel with a short confirmation; **Close** only removes the
buttons. Model choices persist for the chat that was current when the
selector opened; if the active chat changes first, the selector expires.
The chat model selector starts with **Use user default**. Both model selectors
offer **only the user's favorite models**: there is no search (a term after
`/selectmodel` is ignored) and favorites are managed in the web UI. Every line
names the provider that serves the model (`OpenRouter · ...`, `Ollama · ...`,
`Claude · ...`), so the same model name from two providers can be told apart;
the confirmation after choosing repeats it. Favorites that are retired or whose
provider is unavailable are not shown; with no favorites the bot explains how to
add them. Favorites are ordered by input price, then output price. There
are ten models per page and `Previous`/`Next` navigation (localized in
Spanish). Only the most recent panel keeps working buttons; older panels
show an expiry alert. Failures such as a busy agent appear as callback alerts
and leave the panel open.
Telegram text, image, audio, voice, video, and supported media-document
messages are sent through `ChatService` with real provider streaming. Sending
another message to the active chat steers the generation through the service's
normal cancellation and continuation behavior. `/selectagent` changes the
active agent and persists the selection. The **Active agent** option inside
`/settings` offers the same choice within the settings panel, so `/selectagent`
need not be typed. Both paths leave the previous chat on its original agent and clear
the channel's current chat; `/newchat` or the next message creates a chat for
the selected agent. An already-running reply is not rerouted or interrupted.
When enabled in `/settings`, thinking tokens stream into expandable blockquotes,
while normal text is rendered with Telegram entities converted from Markdown.
Completed tool calls are shown in italics as literal
`>query_memory(query="example", top_k=5)` lines in event order, interleaved with
thinking and normal text rather than grouped separately. Tool events do not
carry a reasoning-phase flag: a call following thinking stays inside that
collapsible block; a call following normal text, or with no preceding tokens,
stays outside. Arguments are displayed without interpreting their Markdown;
tool results are not included. Final and interrupted replies preserve the
sequence without duplicating received calls. Long replies are split across
messages without discarding earlier thinking or actions.
In private chats, replies stream through Telegram's native `sendMessageDraft`
(an animated ephemeral draft that starts with Telegram's "Thinking..."
placeholder) and are then persisted with a normal message. If drafts are
unavailable, the bot falls back to sending one message and editing it.

## running the bot

Run the polling process locally with `uv run python run_telegram.py`. Polling
requests all update types, because Telegram otherwise keeps the bot's last
`allowed_updates` list and may never deliver inline-button callbacks.
