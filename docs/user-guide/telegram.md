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
`/stt`, `/dream` (status of the active agent's [dream mode](dream.md#commands): last
session with its summary, schedule, pending chats), `/dreamstart` (starts a session) and
`/dreamcancel` (stops it). Every command is one word so Telegram renders it as tappable and lists it in the
command menu; `/dream start` and `/dream cancel` typed as text still work but are not advertised.
Telegram's native command menu is populated at startup, and
`/help` prints the same command list in the chat.
Operational notices and errors use the user's saved English/Spanish preference;
the native command descriptions and `/help` text remain English.
The agent's name opens the first message of each reply inline and in bold (`**Aria:** answer`); when the answer starts with a
list, code block, heading, table or quote, the name takes its own line instead. Long replies carry it only in the first message.

Telegram resumes your most recently active **Telegram chat** (of any agent) when you write
within `telegram.chat_idle_minutes` (default 60; the local environment variable
`RECALL_TELEGRAM_CHAT_IDLE_MINUTES` overrides it) of its last activity, even
right after the bot restarts. It creates a new chat when the last activity is
older than that or there is no Telegram chat yet. Only chats created from Telegram
(origin `telegram`) count, both for resuming and for measuring idle time; chats you
use in the web UI never do, however recent. Activity in the chat itself counts, so
continuing a Telegram-created chat in the web keeps it fresh for Telegram too.
Chats that existed before origins were introduced have no marker and count as web
chats, so the first Telegram message after that update starts a new chat once. Every assistant reply starts with the
agent's name in **bold** on its own line (only the final message, placed right
before the answer and after any thinking or tool lines; never in streamed drafts,
and only on the first message when a long reply is split).
`/newchat` (also `/new`) opens a picker with your agents (the current one is
marked ✅) and **✖ Cancel (keep current chat)**. Choosing an agent saves it as
the active agent, creates a chat with it and closes the picker; Cancel changes
nothing. With only one agent the chat is created directly. The confirmation
(`New chat created with **Agent**.` and `Model: _Provider · model_ (Agent default)`)
is formatted: bold agent name, italic model.
A new chat started by the idle time (or a single-agent `/newchat`) uses your **default agent** (Settings > General; the
first agent in your list when none is set), not the last agent you used. The `/settings` panel shows it as
`Default agent`.
When a message starts a new chat because of the idle time (not after
`/newchat`, and not for the very first chat), the bot does not answer at once:
it creates the new chat and sends a short message with two buttons,
**↩ Continue previous chat** and **🔀 Change agent**, saying it will answer in
8 seconds. A tap acts immediately: the first switches to the previous chat (and
its agent) and replays your message there; the second opens the agent picker
(giving you 60 seconds) and replays the message in a fresh chat with the chosen
agent. Either way the auto-created chat, still empty, is deleted and the
buttons are removed, so you get exactly one answer. If you do not tap, after
the 8 seconds the bot answers in the new chat and the message becomes
"Answering in a new chat with ..." without buttons. If you send another message
during the wait, the pending one is answered first in the new chat and the new
one follows. A tap after that answers "This option expired.". The pending
message, including downloaded attachments, is kept in memory only during the
wait.

### Telegram orchestrator (automatic agent choice)

When the orchestrator is on, a message that starts a **new chat** (the first ever, or after the idle time) is sent
to a fast *decision model* that reads the agents' descriptions and picks the one whose topic matches best. It never
runs in the middle of a conversation, nor for `/newchat` or `/selectagent`, which stay manual.

- **Confident pick** (confidence at least `orchestrator.min_confidence`, default 0.6): the bot creates the chat
  with that agent and sends first `It has been a while. I chose **Agent** for this message.` with the buttons
  **↩ Continue previous chat** (only when a previous chat exists) and **🔀 Other agent** (the full picker, as
  before). If you do nothing, the chosen agent answers after `grace_seconds` (default 8).
- **Unsure pick** (greetings, vague or ambiguous messages): the default agent is your **core agent** (Settings >
  *Core agent (used when unsure)*), or your **default agent** (Settings > General) when none is set. The bot shows buttons for the three most
  likely agents (the default marked ⭐), then **↩ Continue previous chat** and **🔀 More agents**. Tap an agent to replay
  the message in a new chat with it; tap the ⭐ one to answer at once; do nothing and the default answers after
  `uncertain_grace_seconds` (default 20).
- **First message ever**: routed and with buttons too, but without *Continue previous chat*.
- A photo, audio or file without text (or caption) is not routed: it goes to the core agent, or to your default
  one.
- It falls back silently to the old behaviour (your default agent, the usual buttons) when the orchestrator is
  off, you have fewer than two agents, the API key is missing, or the router fails, times out
  (`timeout_seconds`, default 4) or answers with an agent you do not have. While it thinks the bot shows *typing*.

**Setup.** The administrator turns it on globally with `orchestrator.enabled: true` in `settings.yaml` (see
[settings.yaml](../reference/settings-yaml.md#telegram-orchestrator)); each user can switch it on or off with the
checkbox *Choose the agent automatically in Telegram* in the web Settings (or `orchestrator` in
`PATCH /api/preferences`). It only works well if every agent has a clear description: see
[agents](agents.md#writing-agent-descriptions). To tune descriptions, run
`python -m src.admin route-test "message text" --user <id>` to print the ranking, confidence and latency.

**Privacy.** Only the message text plus the agents' ids, names and descriptions are sent to the decision model's
provider (OpenRouter), never the chat history or attachments. With `require_zdr` (or `orchestrator.zdr`) the request
asks for zero data retention. The text is not written to the info-level log. Agent descriptions and the first message
of a new chat leave your machine in this request.

`/selectmodel` opens the same kind of inline
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
normal cancellation and continuation behavior. `/selectagent` ("Choose the agent
for a new chat") is the same picker as `/newchat`: choosing an agent saves it as
the active agent and immediately creates a fresh chat with it, so you never keep
talking to the previous agent. The **Active agent** option inside `/settings`
does the same and closes the panel with the new-chat confirmation. The previous
chat stays on its original agent. An already-running reply is not rerouted or
interrupted.
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
