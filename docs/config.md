# configuration

how user and agent config is stored, loaded and resolved.

## storage layout

yaml, editable by hand, one file per entity, nested per user. one file per agent. the folder is `.config/` (kept distinct from the `src/config` code package).

```
.config/
  <user_id>/
    user.yaml                # user: provider, agents (channels/resources planned)
    agents/
      <agent_id>.yaml        # one file per agent
```

agents can be created, edited, renamed and deleted from the web **Agents** tab or the `/api/agents` routes (see [managing agents](#managing-agents)); hand-written files work too. the `id` field inside the file identifies the agent, so the file name is only a convention. isolation is natural per user folder. everything except secrets is versionable in git. a global `defaults.yaml` with merge is planned but not yet used.

## provider & secrets

each **user** picks a provider by name and (for cloud providers) brings their own key, referenced by env var name and resolved from the environment / `.env`. local providers (ollama) need no key:

```yaml
# user.yaml
language: en                         # language for user-facing capability errors
provider:
  name: openrouter                    # selects the provider implementation
  api_key: OPENROUTER_LUCAS_API_KEY   # env var NAME holding the key; omit for local providers
```

API keys never live in YAML. Providers and model IDs belong to the user;
agents configure behavior, not inference or embedding models.

### Telegram access

The Telegram channel maps approved Telegram user IDs to existing RECALL users
through the environment variable named by `telegram.allowed_users_env` in
`settings.yaml`. The default is `TELEGRAM_ALLOWED_USERS`, with entries such as
`123456789=user-a,987654321=user-c`. Messages from IDs that are not listed are
ignored completely. The bot token remains in `TELEGRAM_BOT_TOKEN`.

Telegram provides `/help`,
`/settings`, `/newchat`, `/selectmodel`, `/selectagent`, `/agent`, `/model`, `/selectstt`,
and `/stt`. Telegram's native command menu is populated at startup, and
`/help` prints the same command list in the chat.
Operational notices and errors use the user's saved English/Spanish preference;
the native command descriptions and `/help` text remain English.
Telegram creates a chat automatically on the first message after the bot
restarts, and after one hour without a user message. The first assistant
message in each automatically created chat begins with bold-italic
`New chat started`.
`/selectmodel`, `/selectstt` and `/selectagent` open the same kind of inline
panel as `/settings`: buttons send callbacks instead of chat messages, and the
bot edits the panel in place for pagination. Choosing an option saves it and
replaces the panel with a short confirmation; **Close** only removes the
buttons. Model and STT choices persist for the chat that was current when the
selector opened; if the active chat changes first, the selector expires.
Both model selectors start with **Use user default**, and accept an optional
search term (`/selectmodel claude`). Available user favorites appear first
with a star; each group is ordered by input price, then output price. There
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

### video input

A video sent to a chat model that declares `image` but not `video` input is
decomposed before the model runs. RECALL uses `ffmpeg` and `ffprobe` from the
`PATH` to sample one frame every `video.frame_interval_seconds` (up to
`video.max_frames`, scaled to `video.max_width` pixels), and sends the frames as
image attachments in order. The audio track, when present, is transcribed with
the speech-to-text flow below. The message carries a note with the frame
timestamps, whether only part of the video was sampled, and whether the video
has an audio track, so the model knows the frames come from a decomposed video.
Models with native `video` input receive the file unchanged. Models without
`image` input still reject videos. Sampling stops after the last frame, and the
audio extraction skips video decoding, to keep CPU use low.

When this happens the web UI and Telegram show a warning above the reply, because
the model sees only the sampled frames and may miss motion or events between them.

The defaults live in `settings.yaml` under `video`. Install `ffmpeg` (which
includes `ffprobe`) on the host running RECALL; a missing binary produces an
explicit error for video messages only.

### message tags and timestamps

Each user turn is sent to the model prefixed with its local timestamp (for example
`[2026-10-31 14:01+01:00]`), taken from the `ts` stored with the turn, so reloaded
chats keep the same dates. Assistant replies are not prefixed. Text derived from
audio starts with `[AUDIO]`, and text describing a video starts with `[VIDEO]`.
Stored conversations keep the original message text; the prefixes are added only
to the context sent to the model.

### speech-to-text

Audio is always transcribed before the chat model is invoked. RECALL uses
OpenRouter's `/api/v1/audio/transcriptions` endpoint with the configured model
and routing policy, then stores the transcription as text in the conversation.
This keeps later model changes independent of whether the selected chat model
supports audio input.

The initial user STT default is `openai/whisper-large-v3-turbo`, with ZDR required.
The effective user/chat model must have an active ZDR transcription endpoint when
`speech_to_text.zdr` is `true`; otherwise audio requests fail explicitly.
OpenRouter's current transcription catalog can be queried through
`GET /api/speech-to-text/models?q=<query>&zdr=true`.

Run the polling process locally with `uv run python run_telegram.py`. Polling
requests all update types, because Telegram otherwise keeps the bot's last
`allowed_updates` list and may never deliver inline-button callbacks.

`language` is an optional user preference used for messages generated by the
channel and service when a selected model lacks an input modality. It accepts a
two-letter language code (`en`, `es`) or a language-region code (`en-US`,
`es-ES`) and defaults to `en`.

### shared user preferences

User-wide channel preferences are stored separately from YAML in the ignored
`.config/<user_id>/preferences.json`. Telegram `/settings` sends one message
with inline buttons. Choices do not post messages to the chat: each button
sends a callback, the bot saves the change, answers the callback and edits the
same message in place. It offers thinking visibility, English/Spanish alerts,
the active agent, default LLM/STT models and **Close**, which removes the
buttons. Agent and default-model choices open paginated lists in the same panel
(LLM favorites first) with **Back**; a choice saves immediately and returns to
the main view, without requiring a chat. Opening `/settings` again removes the
previous panel's buttons; pressing a button on an old panel shows an expiry
alert. Opening any inline panel briefly sends and deletes a cleanup message
with `ReplyKeyboardRemove`, clearing reply keyboards left by older bot
versions before the inline panel is displayed. The web
sidebar's **Settings** dialog edits thinking, language and default LLM/STT
models; agent switching remains in the existing sidebar selector.
Thinking visibility, preferred-agent selection and English/Spanish operational
notices/errors are implemented in both channels.

```json
{"show_thinking": false, "language": "es", "agent_id": "aria"}
```

- `show_thinking`: a display preference, defaulting to `true`. It does not
  disable model reasoning or delete saved reasoning.
- `language`: `en` or `es` for RECALL-generated alerts/errors, not model replies
  or third-party error text. Without an override, Spanish user YAML language
  codes resolve to `es`; other codes resolve to `en`.
- `agent_id`: must identify an agent configured for this user with an existing
  agent configuration. Without an override, or with `null`, it resolves to the
  user's first agent; users without agents receive `null`. This preference is
  used for new chats, not moving existing chats or interrupting replies.

The web refreshes preferences at login, when opening settings or a chat, before
sending or creating a chat, and when its window regains focus. Hiding thinking
applies to both existing history and live reply blocks; the separate local
expanded/collapsed preference is unchanged. The current conversation stays on
its own agent until a new chat is requested or the user explicitly switches
agents. Selecting an agent in the web sidebar also saves the shared preference.

Telegram reads the shared agent when creating a chat, including automatic
first-message/idle-timeout chats. Thinking visibility is read at the beginning
of each reply and applies to previews, final messages and interrupted replies.
Tool-call lines stay visible even when their preceding thinking is hidden.
Previously delivered Telegram messages are not edited retroactively.

Telegram reloads the alert language before each incoming update; choosing a
language in `/settings` translates that settings menu immediately. The web
localizes its own error/notification templates, settings controls and retention
warning after refreshing preferences. HTTP and SSE application errors use the
saved language; opaque provider errors and low-level technical diagnostics stay
unchanged. The public `GET /api/localization?language=en|es` catalog keeps Python
and browser templates consistent. Template values such as filenames, agent IDs
and provider error text are never translated.

Cached service agents receive the current user language before each generation,
so unsupported-modality errors also follow preference changes without restarting
or rebuilding the agent. If preferences cannot be read, the failure is reported
explicitly: Telegram uses its last successfully read language, while HTTP errors
use the browser's last-known `Accept-Language` header and log the lookup failure.

`GET /api/preferences` returns resolved fields for the current
cookie user. `PATCH /api/preferences` accepts any subset of the fields and
returns the resolved result. Omitted fields keep their saved overrides, or
continue following configuration defaults when never explicitly saved.
For example, `{"show_thinking": false}` leaves language and agent unchanged.
Unknown fields, unsupported languages, non-boolean thinking values and malformed
agent IDs are rejected; an agent belonging to another user is never accepted.

Preference reads are not cached, so separate web and Telegram processes can
observe each other's changes on their next read without restarting. Updates
merge under an operating-system file lock and use atomic replacement; the
adjacent `preferences.lock` file is persistent coordination state, not a stale
lock to delete between requests. Invalid files, failed writes and saved agents
that are no longer available produce explicit errors rather than silently
resetting preferences. A removed agent can be replaced with a valid selection
or reset with `{"agent_id": null}`. Neither user YAML nor model favorites are
rewritten.

### user model defaults and chat overrides

All model IDs belong to the user. User YAML supplies initial defaults:

```yaml
models:
  chat: openai/gpt-4o-mini
  stt: openai/whisper-large-v3-turbo
  embeddings: openai/text-embedding-3-small
```

Persistent user preferences can override `default_llm_model` and
`default_stt_model`; null restores the corresponding user YAML value.
Setting an LLM default validates the current ZDR chat catalog; setting an STT
default validates the transcription catalog under the framework routing policy.
Defaults are independent of agent selection. Embeddings remain a user YAML
setting, not an editable inference preference.

New chats have no LLM or STT selection: both overrides are null. Effective
models resolve as **explicit chat override > current user default**.
Changing a default affects subsequent requests in every inheriting chat,
including existing chats, without changing chats with explicit overrides or
any generation already running. Cached agents refresh inherited inference
models before the next generation, invalidate model-specific token estimates,
and retain their existing embedding index. CLI requests refresh the same user
defaults. Agent YAML has no `models` block and rejects one.

`GET /api/agents/<agent_id>/chats/<chat_id>` includes effective `model` and
`stt_model`, plus nullable `model_override` and `stt_model_override`.
Its `/models` GET endpoint returns only those four model fields, without
loading conversation history or initializing an agent.
The chat `/model` and `/stt-model` PUT endpoints accept `{"model_id": null}`
to clear an override without changing user defaults. `/stt-model` with a model
ID persists it for that chat only; Telegram `/selectstt` uses this operation
instead of channel-session state. Both override operations require an idle
agent. STT runs before chat inference and resolves its own independent selection.
Audio requests validate chat existence before resolving STT or contacting the
provider; missing/invalid chats return 404, including deletion during transcription.

Remove old agent `models` blocks and configure model IDs in user YAML instead.
There is no legacy configuration compatibility or migration utility.
Existing chat LLM sidecars remain explicit overrides; missing STT metadata
means inheritance.

Both channels expose user defaults: Telegram `/settings` saves model choices
immediately; web **Settings** stages choices until **Save settings**, including
when no chat is open. Their selectors require ZDR; web chat LLM overrides can
still select other routing policies.

Telegram `/selectmodel` and `/selectstt`, and the web header's chat/audio
selectors, change only the current chat. **Use user default** clears the
corresponding override. Selectors distinguish **User default** from **Chat
override** and show the effective model. Web settings changes from Telegram
refresh at the existing preference-refresh points without overwriting explicit
chat selections. With an open chat, the web also reads current model metadata
after refreshing or saving preferences, so overrides selected or cleared in
Telegram replace stale display state. It does not reload messages; responses
for replaced sessions/chats or superseded local model selections are ignored.
Default changes can be saved while a reply runs; changing chat
overrides still requires an idle agent.

Status: schemas, service/API precedence, user-default controls and chat reset
controls are implemented in Telegram and web.

### openrouter model catalog and zero data retention

OpenRouter chat requests default to `provider: {"zdr": true}`. This applies
to streaming, non-streaming, transport retries and auxiliary chat calls such as
associative-memory query generation. OpenRouter must reject requests with no
eligible ZDR route; RECALL never relaxes it automatically after a failure.
A chat can explicitly opt out by selecting a model with the selector's ZDR
checkbox unchecked, which persists `zdr: false` for that chat and sends
`provider: {"zdr": false}`. The flag is passed per request, including auxiliary
queries and retries; it is not mutable state shared by providers or chats.
New chats and old metadata without the flag still require ZDR. OpenRouter
account/guardrail policies can still require ZDR even when a chat opts out.
Ollama accepts but ignores this routing preference; embeddings are unchanged.

The web backend exposes `GET /api/models?q=<name-or-id>&zdr=true` for the current user.
It is available only when that user's chat provider is OpenRouter. It fetches
the public `/api/v1/models` and `/api/v1/endpoints/zdr` catalogs without sending
credentials, conversation content or the search term to OpenRouter. Results
contain `id`, `name`, `description`, sorted, deduplicated `zdr_providers`, and
`pricing: {"prompt": "0.1", "completion": "0.5"}`.
Prices are decimal strings in **USD per million input/output tokens**,
calculated independently as the minimum known nonnegative base price among
that model's active ZDR routes when `zdr=true`, not an unrestricted cheaper route.
With `zdr=false`, prices come from the public model catalog's base pricing.
Missing or negative (unknown/dynamic) upstream prices become `null`, displayed
as unavailable rather than free. Zero is a valid free price. The selector
shows both rates with a "from" label, including in the favorites view.
These are indicative base rates, not a quote: routing, context-length tiers,
caching, account discounts and additional charges can change the final cost.
Only text-output models are returned, sorted by name. By default, an active
(`status: 0`) ZDR endpoint is also required; `zdr=false` explicitly includes
models without one. `zdr_providers` always describes known active ZDR routes,
not the actual route a later request will use. Search matches model names or IDs, case-insensitively.
An empty result is valid; failed or invalid upstream responses produce an
explicit HTTP 502, never an unfiltered fallback. The catalog is fetched on
each request and describes public routes, not account-specific availability,
credits, access or a guarantee that a route will remain available.

ZDR here means [OpenRouter's policy](https://openrouter.ai/docs/guides/features/zdr):
it permits in-memory prompt caching and includes legal/CSAM retention exceptions.
It is not a blanket no-retention guarantee for RECALL, local history/logs,
embeddings or external tools such as Tavily. No OpenRouter plugins are enabled
by this integration.

### choosing a model per chat

For OpenRouter users, the web chat header opens a model selector with name/ID
search, ZDR provider names and a Refresh button. A star beside the search
field toggles Favorites only; its filled/highlighted state means the filter
is active. ZDR remains visible, and capability checkboxes live in the collapsed
**Filters** disclosure with an active-filter count. Closing it keeps filters
applied; each selector opening collapses it again. **Information** holds the
longer policy/capability notes; the non-ZDR retention warning remains visible
whenever ZDR is unchecked. The favorites toggle supports Enter/Space and exposes
its pressed state to assistive technology; both disclosures use native keyboard
controls.
The **ZDR only** checkbox is checked on every opening,
regardless of the current chat's policy or a previous search. Unchecking it
fetches the unrestricted text-model catalog and shows a retention warning.
Editing the search text does not reset the checkbox. Opening the selector or
changing its filter does not change the chat's policy: choosing a model does.
The header shows "ZDR required" or "ZDR not required" for the current chat.
Saved favorites appear first, and reopening the selector defaults to favorites when
any exist. Favorites missing from the current catalog are visible but cannot
be selected; they can still be removed. The browser keeps the catalog until
refresh, a filter change or logout, but additions and selections are revalidated
on the server. An unrestricted catalog is never reused as a ZDR-filtered one.

The selector's **Sort** menu defaults to Favorites first and also offers
Name A-Z and input/output prices in either direction. Price ordering uses the
unrounded numeric rates for the current catalog/filter, not their displayed
currency strings. Unknown prices always sort last, including when descending;
zero-priced models are valid. Price and name modes do not pin favorites first.
Ties sort by name, then ID. Sorting combines with search and Favorites only
without changing ZDR or chat settings. The choice lasts until logout/reload;
ZDR still resets to checked whenever the selector opens.

Capability checkboxes filter for **Tools**, **Image input** and **Reasoning**.
All selected capabilities must match. Tools requires `tools`; reasoning requires
`reasoning` or `reasoning_effort` in the supported parameters. With ZDR checked,
one active ZDR endpoint must advertise both when both are selected; capabilities
from separate routes are not combined. Without ZDR, the public model's supported
parameters are used. Image input uses the model's declared `input_modalities`,
not an endpoint-specific guarantee. Missing metadata does not satisfy a filter.
Filters combine with search, favorites and ordering and reset on logout/reload.
They do not change the chat routing policy, enable agent tools/reasoning, or
pin an upstream provider. The web composer accepts image, audio and video
attachments; each selected model must explicitly declare the corresponding
`input_modalities` before RECALL sends the file, except that videos are
decomposed for image-only models (see video input). Files are limited to 10 MB
each and 25 MB per message. Audio is passed directly to models that advertise
audio input; automatic speech-to-text is not configured. Prices remain catalog base minima under the ZDR filter, not quotes
for a capability-filtered route. The API includes `input_modalities` and
`parameter_routes` (one supported-parameter list per active ZDR endpoint, or a
single model-level list when ZDR is unchecked) for these client-side filters.

Selecting a model changes **only the current chat**, preserving its messages
and leaving user defaults, other chats and embeddings unchanged. New chats
inherit the user's current default without an explicit override. A selection survives browser/server restarts
and is also honored when the CLI opens that chat. A chat with an OpenRouter
override requires an OpenRouter chat provider; changing the user's provider
does not silently reinterpret its model ID.

`GET /api/agents/<agent_id>/chats/<chat_id>` includes
`"model": {"provider": "openrouter", "id": "vendor/model", "zdr": true}` (the override or
user default). `PUT` to that chat's `/model` endpoint with
`{"model_id": "vendor/model", "zdr": true}` validates the exact ID against the
live catalog with that filter and persists both fields through the conversations
source. Omission of `zdr` defaults to true; JSON opt-out requires the actual
boolean `false`, not a string or null. Changing models or routing policy is
disabled while the agent has active or queued replies; direct callers receive
HTTP 409. It never interrupts or changes the model of an in-flight reply.
Reopening/switching chats restores their own model and invalidates the cached
context-window size when the model changes.

### favorite models

OpenRouter favorites are stored in the ignored
`.config/<user_id>/openrouter-favorites.json`, separately from user YAML:

```json
{"model_ids": ["vendor/model"]}
```

They belong to the user, not an individual agent or browser, and survive
restarts. IDs retain insertion order; adding/removing an existing/missing
favorite is idempotent. Telegram reads this same list every time `/selectmodel`
opens, pins favorites present in the current ZDR/search results first and marks
them with a star. Retired or non-ZDR favorites are not added to Telegram's
selectable catalog. Favorite additions/removals remain web-only; the web
selector can also be used from a mobile browser. The backend provides:

- `GET /api/models/favorites`: saved model IDs.
- `POST /api/models/favorites` with `{"model_id": "vendor/model", "zdr": true}`: add a model
  only if the user's chat provider is OpenRouter and its exact ID is currently
  in the catalog under that filter. Explicit `zdr: false` permits favoriting a
  model without ZDR routes; it does not change any chat's routing policy.
- `DELETE /api/models/favorites/vendor/model`: remove an ID, even if it is
  no longer available or the user has switched providers.

Each operation resolves the current user before accessing preferences. Favorite
IDs are preferences, not proof of availability: listing them does not contact
OpenRouter, and retired or no-longer-ZDR models stay saved until removed.
Clients must match them against the current catalog before offering selection.
Favorite IDs do not store a routing-policy opt-out and never override the
selector's current filter. Invalid additions return HTTP 400;
catalog failures return 502. Invalid preference files and storage failures
return an explicit 500 rather than silently discarding existing favorites.
Writes use same-directory temporary files and atomic replacement, with updates
serialized within one service instance. Separate server processes do not
coordinate concurrent updates; run one process for preference mutations.

## shared web access (tavily)

Tavily uses a **framework-wide key**, shared by all users and agents. This is
separate from each user's chat/embedding provider credentials and shares one
Tavily account's quota and billing. Put the key in the ignored root `.env` file
(the normal entry points load it), or set it in the process environment:

```dotenv
TAVILY_API_KEY=your_tavily_api_key
```

The root `settings.yaml` references the variable name, never its value:

```yaml
tavily:
  api_key: TAVILY_API_KEY
  timeout_seconds: 30
  max_content_chars: 16000
```

`timeout_seconds` must be between 1 and 60; `max_content_chars` must be an
integer between 1,000 and 100,000. The client resolves the key when constructed,
not when settings are imported. An unset/blank key leaves web access unavailable;
it does not prevent normal chat. Restart after changing configuration or the key.

Implemented: the standalone client in `src/providers/tavily.py`, basic search
(1-10 results, at most 2,000 excerpt characters per result), search filters
(topic, recency or publication-date range, domain include/exclude lists and
country preference), and Markdown extraction with optional query-focused
passages and explicit truncation. Search always requests Tavily's
`published_date` estimate. Results whose date is unknown are kept by Tavily
even with a date filter; `web_search` then adds a note so the agent does not
present them as verified recent. Search depth is fixed to `basic`.
Extraction checks public HTTP(S) URLs and their locally resolved addresses,
rejects credentials and nonstandard ports, and delegates fetching to Tavily.
These checks do not control Tavily's own DNS resolution or page redirects.
Provider errors, empty extraction and per-URL failures are surfaced explicitly.
No automatic advanced extraction, retries, persistence or memory indexing is used.
The client sends only the supplied query/URL, not agent history or memory;
callers must not supply secrets or private data. Web content is untrusted data,
not instructions.

Agents with `chat_config.tools: true` (the default) register `web_search` and
`web_retrieve` when the shared key is set. Without the key they keep their existing
memory tools; with `tools: false` no tools are registered and the key is not
resolved. Web actions use the same chat events and history display as memory
actions. The complete result enters the active context, while the action preview
stored with the turn is truncated to 500 characters.
Per-user Tavily credentials and per-agent web policies are not implemented.

## config files

```yaml
# <user_id>/user.yaml
id: user-a
name: User A
provider:
  name: openrouter
  api_key: OPENROUTER_LUCAS_API_KEY   # omit for local providers (ollama)
agents: [aria]
models:
  chat: openai/gpt-4o-mini
  stt: openai/whisper-large-v3-turbo
  embeddings: openai/text-embedding-3-small
```

```yaml
# <user_id>/agents/<agent_id>.yaml
id: aria
name: Aria
personality_prompt: >
  you are aria, a helpful assistant...
chat_config:
  stream: true                       # stream tokens as they arrive
  has_memory: true                   # keep conversation history in context
  reasoning: true                    # request and separate thinking tokens
  reasoning_tokens: 20               # optional reasoning budget
  keep_reasoning_in_memory: false    # store the thinking in context/history
  conversation_messages_retained: 20 # last N messages kept in context (see sources.md)
```

## loading

`load_agent(user_id, agent_id?)` reads `user.yaml` and the agent file, resolves the provider key from the environment, and returns a `ResolvedAgent` (used to build the `Agent` and its `SourceRegistry`). if `agent_id` is omitted, the user's first agent is used.

checks today: the agent belongs to the user, and the provider key env var (if configured) is set.

planned: a `defaults.yaml < user.yaml < agent.yaml` merge, full fail-fast validation, resource/vault references, a per-tool policy, and a `ConfigStore` with a channel→user index for routing incoming messages.

## managing agents

the web **Agents** tab lists the user's agents and edits the name, `personality_prompt` and all `chat_config` options. a new agent's `id` is derived from its name (lowercase, accents removed, non-alphanumeric characters replaced by `-`) and is not editable from the form. the same operations are available as routes:

| method | route | result |
| --- | --- | --- |
| `GET` | `/api/agents` | full configuration of each agent the user owns |
| `POST` | `/api/agents` | creates `agents/<id>.yaml` and appends the id to `agents` in `user.yaml` (`201`) |
| `GET` | `/api/agents/<agent_id>` | one agent's configuration |
| `PUT` | `/api/agents/<agent_id>` | replaces the agent; the body `id` may differ, which renames it |
| `DELETE` | `/api/agents/<agent_id>` | removes the agent from `user.yaml` and deletes its file (`204`) |

rules:

- ids match `[A-Za-z0-9_-]{1,64}`; `name` has 1–100 characters and `personality_prompt` 1–20000.
- ids are unique per user. a duplicate id, or an existing file name, returns `409`.
- changing the `id` through `PUT` (the web form never does) moves `data/<user_id>/<old_id>` to `data/<user_id>/<new_id>` and updates the user's preferred agent. if the agent file was named after its old id, it is renamed after the new one; otherwise it keeps its name. if the data folder cannot be moved (for example, another process is using it), the change is rolled back.
- deleting keeps `data/<user_id>/<agent_id>` on disk, so creating an agent later with the same id reuses that memory. if the deleted agent was the preferred one, the preference falls back to the user's first agent.
- editing, renaming or deleting an agent with active or queued replies returns `409`; the cached runtime is rebuilt from the saved file on the next message.
- writes are atomic and serialized per user by `.config/<user_id>/agents.lock`. only the `agents:` entry of `user.yaml` is rewritten, so comments and other keys are kept.
- Telegram sessions that are already open keep the agent id they started with; use `/selectagent` (or restart the bot) after a rename or delete.

## mutability

YAML config is read at load time; edit it and restart to change framework/user/
agent defaults. Model favorites, per-chat selections and agent edits made through
the APIs above are persisted immediately, without restarting.
