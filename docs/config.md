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
require_zdr: false                   # optional, off by default: only use zero-data-retention routes (see below)
provider:
  name: openrouter                    # selects the provider implementation
  api_key: OPENROUTER_LUCAS_API_KEY   # env var NAME holding the key; omit for local providers
```

API keys never live in YAML. Providers and model IDs belong to the user;
agents configure behavior, not inference or embedding models.

`provider` is the user's **main provider**: it serves bare model ids and
embeddings. It is set in `user.yaml` only, not in the web UI. The other providers
need no setup in the user's file: the model picker offers every provider the
server can use (see [providers](#providers)).

### model references

A model reference is a bare model id, served by the user's main provider, or
`<provider>:<model>`, served by another provider: `openrouter:vendor/model`,
`ollama:llama3:8b`, `claude_subscription:sonnet`. Only the provider names
`openrouter`, `ollama` and `claude_subscription` count as a prefix, so ids with
their own colon (`llama3:8b`, `vendor/model:free`) stay bare ids. An explicit
prefix for the main provider is accepted and saved in its bare form. The same
reference works everywhere a model is chosen: `models.*` in `user.yaml`, the
default and per-chat pickers, favorites, an agent's `dream.model` and Telegram.

A bare id always means the **current** main provider; changing the main provider
in `user.yaml` does not rewrite stored references (there is no migration step).
The web model picker chooses the provider on the picker itself and saves the
reference in this form: bare for the main provider's models, prefixed for the
others.

Capabilities: OpenRouter serves chat, speech-to-text and embeddings; Ollama chat
and embeddings; the experimental Claude subscription chat only (see
[claude subscription](claude-subscription.md) for setup, privacy flags and the
terms caveat). `models.stt` and `models.embeddings` reject a prefix whose provider
lacks that capability, and the main provider must be `openrouter` or `ollama`.

### providers

There is no per-user "enable a provider" setting. The **model picker** (chat,
Settings → default LLM/STT, Dream tab) has a provider switcher at its top, one tab
per provider able to serve that kind of model: OpenRouter, Ollama and the
experimental Claude subscription for chat and dream models; OpenRouter only for
speech-to-text. For chat and dream models a first **Any** tab (usable when at
least one provider is) lists the models of every available provider together,
each row tagged with its provider; unavailable providers are left out, and if
one provider's catalog fails while others answer, the others still show. Choosing
a row in Any is the same as choosing it in its own tab (the stored reference is
`<provider>:<model>`), favorites show all providers' favorites, and the ZDR
checkbox filters the OpenRouter rows only. A provider tab lists only that
provider's models. The picker opens on **Any** if that was the last tab the user
chose (kept in the browser, per user); otherwise on the provider of the currently
selected model, else the last provider used, else the main provider. Switching
tab never changes the *favorites only* filter. Embeddings always use the main
provider (no picker).

A provider is **available** when the server can use it, checked on the server:

| provider | available when | otherwise the tab is disabled with |
| --- | --- | --- |
| OpenRouter | its API key environment variable is set (never read back) | `API key variable not set` |
| Ollama | the server answers at its host | `Ollama not reachable` |
| Claude subscription | the `claude` CLI is found and `claude auth status` says it is logged in | `Claude Code not installed` / ``Run `claude auth login` `` |

Slow checks (Ollama, Claude Code) are cached for 60 seconds per server, so opening
the picker does not start the CLI each time; the key check is a plain lookup. The
Claude tab is labelled *experimental* (and *experimental, not ZDR* when the user
requires ZDR). The ZDR checkbox appears for OpenRouter only; Ollama and Claude
subscription models do not use it. Telegram has
no provider switcher: its list is the user's favorites from all available
providers, each line tagged with its provider.

Optional provider settings stay in `user.yaml`, with no web UI. All blocks are
optional:

```yaml
provider:                 # main provider: serves bare model ids and embeddings
  name: openrouter
  api_key: OPENROUTER_API_KEY     # env var NAME of the main provider's key
openrouter:               # key variable of OpenRouter when it is not the main provider
  api_key: OPENROUTER_API_KEY     # env var NAME
ollama:
  host: null                      # null = OLLAMA_HOST, else http://localhost:11434 (also for a main Ollama)
claude_subscription:      # advanced options only, see claude-subscription.md
  claude_config_dir: null
  command: null
  claude_code_telemetry: false
  timeout_seconds: 300
```

The blocks have no `enabled` key (the strict schema rejects it). A model whose
provider is not available fails with `provider not available: <provider>: <what to
fix>`: choosing it in a picker or the Dream tab is refused with the same message in
the user's language, and a reply, dream or chat title that needs it later reports
it (a missing OpenRouter key variable, or Claude Code's own "not installed" /
"run `claude auth login`" error). RECALL never logs in to Claude from the web and
never reads credential files; the status check only learns whether the CLI is
logged in (the account e-mail is never passed on).

API (authenticated, scoped to the session user):

- `GET /api/providers/status?capability=chat|stt`: `{"main", "providers": [{"id",
  "label", "main", "available", "reason", "detail", "experimental",
  "zdr_applies", "capabilities"}]}`, the providers able to serve the capability.
  `reason` is `no_api_key`, `unreachable`, `cli_missing`, `logged_out` or
  `cli_unknown` when not available; `detail` is the variable name, host or hint,
  never a secret value.
- `GET /api/models/catalog?capability=chat|stt&provider=<id>&zdr=true&q=`:
  `{"models": [...]}`, the models of that one provider. An unavailable provider
  or one that cannot serve the capability is refused with 400 and the reason.
  Without `provider`, speech-to-text means OpenRouter and chat merges the
  available providers (Telegram). Each row has `provider`, `provider_label`,
  `main_provider` and `zdr_applies`.

### web authentication

Web users are preconfigured; there is no registration flow. Add an Argon2 password
hash to each user's `user.yaml`:

```yaml
id: johndoe
name: John Doe
password_hash: "$argon2id$..."
```

Generate a hash without putting the password in a file or command history:

```powershell
uv run python -m src.web.auth
```

The web UI uses a signed, HTTP-only `recall_session` cookie. Set the session
signing secret through the environment variable named by `web.session_secret_env`
(default: `RECALL_SESSION_SECRET`) before starting `web.py`. Use a long random
value and keep it outside YAML and source control. Logging out clears the session.

The web UI binds to `web.host`/`web.port` in `settings.yaml` (default
`127.0.0.1:8000`). The `RECALL_WEB_HOST` environment variable, when set and not
empty, overrides `web.host`; it is read from the process environment when the
settings load, not from `.env`. The Docker image sets it to `0.0.0.0` (see
[deployment](deployment.md)).

`web.public_url` (default empty) is the address users open the web UI at, for
example `http://<server-ip>:8000`. When set, Telegram [dream
notifications](dream.md#telegram-notification) include a link to the session
(`<public_url>/#agents/<agent_id>/dreams/<session_id>`); when empty they stay
text-only. The `RECALL_PUBLIC_URL` environment variable, when set and not empty,
overrides it; like `RECALL_WEB_HOST` it is read from the process environment, not
from `.env` (Docker Compose passes `.env` to the containers' environment, so
there it works). A trailing `/` is dropped; a value that does not start with
`http://` or `https://` stops startup with an error. The link is sent as a
Telegram `text_link` so it is clickable; Telegram accepts IP addresses and
domains but rejects `localhost`, in which case the message falls back to plain
text (copy the address by hand). Use the server's LAN IP, not `localhost`.

### Telegram access

The Telegram channel maps approved Telegram user IDs to existing RECALL users
through the environment variable named by `telegram.allowed_users_env` in
`settings.yaml`. The default is `TELEGRAM_ALLOWED_USERS`, with entries such as
`123456789=user-a,987654321=user-c`. Messages from IDs that are not listed are
ignored completely. The bot token remains in `TELEGRAM_BOT_TOKEN`.

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

### pdf input

The web composer and Telegram accept PDFs. RECALL reads them locally before the model runs: PyMuPDF
extracts the selectable text of each page, and a page with fewer than `pdf.ocr_min_chars` non-whitespace
characters (default 25) is rendered at `pdf.ocr_dpi` and read with Tesseract as a whole. A page with enough
text is still read with OCR for each large embedded image (at least 5% of the page area, up to 10 images per
page), because scanned or photographed details such as a card's name and address are often stored only as
images. Small images such as logos are skipped, and pages without large images skip OCR. The OCR text of an image
is added after the page's digital text, so a heading that appears in both may show up twice. The model receives
only text: no PDF file and no page images. Vector graphics and charts are not interpreted, except text Tesseract
recognizes inside images.

The text is added to the user's message under `Attached document: <name>`, with a `[Page N]` marker before each
page (`[Page N, OCR]` for OCR pages) so answers can cite pages. The stored turn keeps this text, not the file,
so later chats can recall it like any other message.

Limits: `pdf.max_pages` (default 30) per PDF; longer documents are rejected. Files are subject to the channel
size limits (10 MB per file and 25 MB per web message, 22 MB per Telegram file). Password-protected and invalid
files are rejected with an explicit error. Tesseract must be installed as a system program (see
[Tesseract OCR](#tesseract-ocr)); without it, PDFs with selectable text still work, and a scanned
page fails with an error that names the fix. `pdf.ocr_languages` must match the installed language packs.

The defaults live in `settings.yaml` under `pdf`.

### tesseract ocr

Scanned PDF pages are read with the Tesseract OCR program. The Python package `pytesseract` only wraps
the program, and `uv sync` does not install it, so install it on the machine that runs RECALL (the web UI
and the Telegram bot). PDFs with selectable text do not need it.

Windows (PowerShell or Command Prompt):

```
winget install --id UB-Mannheim.TesseractOCR -e
```

The installer is also available from the [UB-Mannheim Tesseract page](https://github.com/UB-Mannheim/tesseract/wiki).
In its component list, select the Spanish language data if you want Spanish OCR.

macOS (Homebrew):

```
brew install tesseract tesseract-lang
```

Debian or Ubuntu:

```
sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa
```

Fedora:

```
sudo dnf install tesseract tesseract-langpack-eng tesseract-langpack-spa
```

Check the installation in a new terminal:

```
tesseract --version
tesseract --list-langs
```

The language list must include every code in `pdf.ocr_languages` (default `eng+spa`). To use other
languages, install their packs and change `pdf.ocr_languages` in `settings.yaml`.

RECALL finds the executable in this order: `pdf.tesseract_cmd` in `settings.yaml` (full path), then the
`PATH`, then `C:\Program Files\Tesseract-OCR\tesseract.exe` on Windows. Restart the web UI and the Telegram
bot after installing, so they see the new `PATH`. If Tesseract is missing, a scanned page fails with an
error that names this section; PDFs with selectable text still work.

#### windows notes

- `winget` may install Tesseract per user, in `%LOCALAPPDATA%\Programs\Tesseract-OCR`, instead of
  `C:\Program Files\Tesseract-OCR`. RECALL does not search that folder, so add it to the user `PATH`
  (PowerShell; open a new terminal afterwards):

  ```
  [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';' + "$env:LOCALAPPDATA\Programs\Tesseract-OCR", 'User')
  ```

  Check whether it is already there first: `[Environment]::GetEnvironmentVariable('Path','User') -split ';'`.
- A terminal or process started before the `PATH` change does not see it. Open a new terminal, or reload
  the variable in the current one with `$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')`.
  Then restart the web UI and the Telegram bot from that terminal.
- The installer's Spanish data may be missing. Download it into the `tessdata` folder of the installation,
  then check with `tesseract --list-langs`:

  ```
  Invoke-WebRequest -Uri 'https://github.com/tesseract-ocr/tessdata/raw/main/spa.traineddata' -OutFile "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tessdata\spa.traineddata"
  ```

  Use the same folder for other languages, for example `eng.traineddata`. Pack files from other sources must
  match the Tesseract version; use the `tessdata` repository above.

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

The transcription model is a single user setting, the **Default STT model** in
web **Settings** or Telegram `/settings`; it is shared by all chats and agents.
The initial default is `openai/whisper-large-v3-turbo`. When ZDR applies
(`speech_to_text.zdr`, else the user's `require_zdr`) the model must have an
active ZDR transcription endpoint and requests carry `provider: {"zdr": true}`;
otherwise audio requests fail explicitly. Without ZDR the plain public
transcription catalog is used and no `provider` field is sent.
OpenRouter's current transcription catalog can be queried through
`GET /api/speech-to-text/models?q=<query>&zdr=true`; without `zdr` the endpoint follows
that policy. Telegram `/stt` shows the
current default.

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
models (the audio model is set only here); agent switching is the **Agent**
selector in the chat header. The web sidebar lists the chats of all of the
user's agents (`GET /api/chats`), each labelled in italics with its agent.
Opening a chat from another agent makes that agent active; choosing an agent in
the selector starts a new chat with it. The per-chat reasoning effort is also available in Telegram's
`/settings` panel (**Chat reasoning effort**).
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
agents. Selecting an agent in the web chat header also saves the shared preference.

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
Setting an LLM default validates the chat catalog under the user's ZDR policy
(`require_zdr`); setting an STT default validates the transcription catalog under
the speech-to-text policy.
Defaults are independent of agent selection. Embeddings remain a user YAML
setting, not an editable inference preference.

New chats have no LLM selection: `model_override` is null. Effective LLM
models resolve as **explicit chat override > agent default model > current user
default**. An agent sets its default with the optional `chat_config.model` key of
its YAML (a full reference such as `claude_subscription:sonnet` or
`anthropic/claude-haiku-5.5`; unset follows the user default, which is what every
existing agent does). It applies to new chats and to old chats without a
selection; a chat's own selection keeps winning. The picker entry that clears a
chat selection reads **Use agent default** when the agent has one, else **Use
user default** (web and Telegram). The agent default is edited in the web UI
(Agents > Advanced > Default model, same picker with provider tabs; **Clear**
follows the user default), not in Telegram. Saving validates it like a chat model
(provider usable, model offered, ZDR catalog filter) and rewrites the agent file
through the agent store, which keeps all other keys but not YAML comments.
With `require_zdr` a `claude_subscription:` agent default is refused (it has no
ZDR and an agent default has no `zdr` override): saving fails, and a file edited
by hand fails the replies of chats that inherit it with a clear message. A retired
model behaves as a retired user default. Dream with no `dream.model` follows the
agent default; chat naming and associative memory keep their own models. The STT
model has no per-chat override; it is always the current user default.
Changing a default affects subsequent requests in every inheriting chat,
including existing chats, without changing chats with explicit overrides or
any generation already running. Cached agents refresh inherited inference
models before the next generation, invalidate model-specific token estimates,
and retain their existing embedding index. CLI requests refresh the same user
defaults. Agent YAML has no `models` block and rejects one.

`GET /api/agents/<agent_id>/chats/<chat_id>` includes the effective `model`,
the user's `stt_model` default (read-only here), and a nullable `model_override`.
Its `/models` GET endpoint returns only those model fields (plus
`model_source`: `chat` | `agent` | `user`, the level the effective model comes
from, `agent_default_model` and `user_default_model`) and the reasoning
effort state, without loading conversation history or initializing an agent.
The chat `/model` PUT endpoint accepts `{"model_id": null}` to clear an override
without changing user defaults. Changing it requires an idle agent. Audio requests
validate chat existence before resolving STT or contacting the provider;
missing/invalid chats return 404, including deletion during transcription.

Remove old agent `models` blocks and configure model IDs in user YAML instead.
There is no legacy configuration compatibility or migration utility.
Existing chat LLM sidecars remain explicit overrides. Legacy `<chat_id>.stt.json`
files are no longer read and are removed with their chat.

Both channels expose user defaults: Telegram `/settings` saves model choices
immediately; web **Settings** stages choices until **Save settings**, including
when no chat is open. Their selectors follow the user's ZDR policy; web chat LLM
overrides can still select the other policy.

Telegram `/selectmodel` and the web composer's chat model selector change only
the current chat. The STT default is changed only in **Settings** (web or Telegram). **Use user default** clears the
corresponding override. Selectors distinguish **User default**, **Agent default** and **Chat
override** and show the effective model. Web settings changes from Telegram
refresh at the existing preference-refresh points without overwriting explicit
chat selections. With an open chat, the web also reads current model metadata
after refreshing or saving preferences, so overrides selected or cleared in
Telegram replace stale display state. It does not reload messages; responses
for replaced sessions/chats or superseded local model selections are ignored.
Default changes can be saved while a reply runs; changing chat
overrides still requires an idle agent.

Status: schemas, service/API precedence, user-default controls and chat reset
controls are implemented in Telegram and web. The default speech-to-text model
must be an OpenRouter model; default chat models can come from any available
provider.

### openrouter model catalog and zero data retention

ZDR is optional and off by default (`require_zdr: false`, see
[above](#zero-data-retention-is-a-user-policy)). While it is off, RECALL sends no
`provider.zdr` field at all (neither `true` nor `false`), so the OpenRouter account's
own policy decides, and it uses the plain public catalog and base prices.

When ZDR applies (`require_zdr: true`, a feature's own `zdr: true`, or a chat that
opted in) OpenRouter chat requests carry `provider: {"zdr": true}`. This applies to
streaming, non-streaming, transport retries and auxiliary chat calls such as
associative-memory query generation. OpenRouter must reject requests with no
eligible ZDR route; RECALL never relaxes it automatically after a failure.
A chat can deviate from the user's setting by selecting a model with the selector's
ZDR checkbox changed, which persists `zdr: true` or `zdr: false` for that chat. The
flag is passed per request, including auxiliary queries and retries; it is not
mutable state shared by providers or chats. New chats and old metadata without the
flag follow the user's `require_zdr`; metadata with a flag keeps it. Only a choice
that differs from the user's setting is stored. OpenRouter account/guardrail
policies can still require ZDR even when ZDR is off in RECALL.
Ollama accepts but ignores this routing preference; embeddings are unchanged.

The web backend exposes `GET /api/models?q=<name-or-id>&zdr=true` for the current user:
the chat rows of `GET /api/models/catalog` (see [providers](#providers)), merged
over the available providers; `zdr` filters only the OpenRouter rows and defaults to
the user's `require_zdr` when omitted. It fetches
the public `/api/v1/models` and `/api/v1/endpoints/zdr` catalogs without sending
credentials, conversation content or the search term to OpenRouter. Results
contain `id`, `name`, `description`, sorted, deduplicated `zdr_providers`, and
`pricing: {"prompt": "0.1", "completion": "0.5"}`.
Prices are decimal strings in **USD per million input/output tokens**,
calculated independently as the minimum known nonnegative base price among
that model's active ZDR routes when ZDR applies, not an unrestricted cheaper route.
Without ZDR, prices come from the public model catalog's base pricing.
Missing or negative (unknown/dynamic) upstream prices become `null`, displayed
as unavailable rather than free. Zero is a valid free price. The selector
shows both rates with a "from" label, including in the favorites view.
These are indicative base rates, not a quote: routing, context-length tiers,
caching, account discounts and additional charges can change the final cost.
Only text-output models are returned, sorted by name. When ZDR applies, an active
(`status: 0`) ZDR endpoint is also required; otherwise models without one are included. `zdr_providers` always describes known active ZDR routes,
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

The **Chat model** control in the web composer (above the message box) opens a
model selector, grouped by provider (main provider first, each group labelled), with name/ID
search, ZDR provider names (while ZDR applies) and a Refresh button. A star beside the search
field toggles Favorites only; its filled/highlighted state means the filter
is active. The ZDR checkbox remains visible, and capability checkboxes live in the collapsed
**Filters** disclosure with an active-filter count. Closing it keeps filters
applied; each selector opening collapses it again. **Information** holds the
longer policy/capability notes; the non-ZDR retention warning appears only when
the user requires ZDR and unchecks the box. The favorites toggle supports Enter/Space and exposes
its pressed state to assistive technology; both disclosures use native keyboard
controls.
The **ZDR only** checkbox opens in the user's policy (unchecked unless
`require_zdr` is true), regardless of the current chat's policy or a previous
search. Checking it fetches the ZDR-filtered catalog; when the user requires ZDR,
unchecking it fetches the unrestricted catalog and shows a retention warning.
Editing the search text does not reset the checkbox. Opening the selector or
changing its filter does not change the chat's policy: choosing a model does.
The composer shows no ZDR text while the user does not require it and the chat has not
opted in; otherwise it shows "ZDR required" or "ZDR not required" for the current chat.
Saved favorites appear first. The Favorites only filter is always off when the selector
opens. Favorites missing from the current catalog are visible but cannot
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
ZDR still resets to the user's policy whenever the selector opens.

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
attachments and PDFs; PDFs are converted to text on the server (see [pdf input](#pdf-input))
and need no model support. Each selected model must explicitly declare the corresponding
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
and is also honored when the CLI opens that chat. The override stores the model
reference; a bare id is served by the user's main provider when the chat runs (see
[model references](#model-references)), and a chat whose provider was disabled
fails with an error naming the setting.

The **ZDR only** filter applies to OpenRouter rows. Ollama rows have no per-token
price; they are local models except Ollama cloud models (tags ending in `cloud`),
which run on Ollama's servers and are labelled cloud (and not ZDR when the user requires ZDR). With the experimental [Claude subscription](claude-subscription.md)
available, `claude_subscription:haiku`, `:sonnet` and `:opus` are listed in their own
group whatever the ZDR filter, labelled experimental (and not ZDR when the user requires ZDR), without per-token
prices. Choosing one stores the chat override without a `zdr` flag, or as
`{"provider": "claude_subscription", "id": "claude_subscription:sonnet", "zdr": false}`
when the user requires ZDR. The composer shows "Claude subscription" (plus ", not ZDR" when the user requires ZDR; no warning style, and the *experimental* note lives on the picker tab only). The default
LLM picker and the Dream tab's **Browse** use this dialog; Telegram `/selectmodel`
and the default model in `/settings` list the user's favorites from all available
providers instead (no search). The default STT picker lists OpenRouter only. Telegram says nothing about ZDR unless the user requires it (or the chat opted in);
then it never says "ZDR: enabled" for a Claude subscription model but "Not ZDR (Claude
subscription, experimental)" (or the Spanish equivalent). Its lists tag Claude rows
with 🧪 and "plan" ("plan, no ZDR" when the user requires ZDR).

`GET /api/agents/<agent_id>/chats/<chat_id>` includes
`"model": {"provider": "openrouter", "id": "vendor/model", "zdr": true, "require_zdr": false}`
(the override or user default; `zdr` is the effective flag and `require_zdr` the user's
setting). `PUT` to that chat's `/model` endpoint with
`{"model_id": "vendor/model", "zdr": true}` validates the exact ID against the
live catalog with that filter and persists the model, and the flag when it differs
from the user's setting, through the conversations source. Omission of `zdr` (or
null) follows the user's setting; an explicit choice requires the actual boolean,
not a string. Changing models or routing policy is
disabled while the agent has active or queued replies; direct callers receive
HTTP 409. It never interrupts or changes the model of an in-flight reply.
Reopening/switching chats restores their own model and invalidates the cached
context-window size when the model changes.

### reasoning effort per chat

The composer's **Reasoning effort** selector overrides the agent's
`chat_config.reasoning_effort` for the current chat only. Its first option,
**Default**, shows the agent's value and removes the override. The override is
stored with the chat (`<chat_id>.effort.json`), survives restarts, is honored
when the CLI opens that chat, and is deleted with the chat.

The selector is disabled when the agent has `reasoning: false`, or when the
selected model does not advertise `reasoning` or `reasoning_effort` in its
OpenRouter supported parameters (the same check as the Reasoning filter). The
UI then says why. Support is cached by the service for ten minutes; if the
catalog cannot be reached, the state is shown as unverified and the value cannot
be changed. Changes are rejected with HTTP 409 while the agent has active or
queued replies.

`GET /api/agents/<agent_id>/chats/<chat_id>/models` includes
`"reasoning_effort": {"override", "default", "effective", "reasoning", "supported"}`
(`supported` is `true`, `false`, or `null` when unverified). `PUT` to
`/api/agents/<agent_id>/chats/<chat_id>/reasoning-effort` with
`{"effort": "high"}` sets the override, and `{"effort": null}` clears it. Accepted
values are `minimal`, `low`, `medium`, `high`, `xhigh` and `max`.

### favorite models

OpenRouter favorites are stored in the ignored
`.config/<user_id>/openrouter-favorites.json`, separately from user YAML:

```json
{"model_ids": ["vendor/model"]}
```

They belong to the user, not an individual agent or browser, and survive
restarts. IDs retain insertion order; adding/removing an existing/missing
favorite is idempotent. Telegram reads this same list every time `/selectmodel`
opens and offers only the favorites present in the current catalog (starred, with
their provider named). Retired or (when ZDR is required) non-ZDR favorites, and those of an unavailable
provider, are not selectable in Telegram. Favorite additions/removals remain web-only; the web
selector can also be used from a mobile browser. The backend provides:

- `GET /api/models/favorites`: saved model IDs.
- `POST /api/models/favorites` with `{"model_id": "vendor/model", "zdr": true}`: add a model
  only if its exact ID is currently in the catalog under that filter (`zdr` defaults
  to the user's `require_zdr`). `zdr: false` permits favoriting a model without ZDR
  routes; it does not change any chat's routing policy.
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

## associative memory

Each agent chooses in `chat_config` how it searches its memory before replying:

```yaml
chat_config:
  associative_memory: llm          # off | llm | question_only
  associative_execution: parallel  # parallel | sequential
```

- `off`: no search, no indicator and no associative-memory note in the system prompt
- `llm` (default): a small model reads the recent conversation and proposes a short
  search query, or decides nothing needs recalling
- `question_only`: the user's message (at most 1,000 characters) is searched as is, with
  no model call. It is faster, but it searches every message and cannot resolve references
  to earlier turns; the similarity threshold filters irrelevant hits

`associative_execution` (default `parallel`) chooses when the search runs. `parallel`
starts it beside the model call and, if it finds something while the reply is being
written, pauses the reply to add it (see
[associative recall in parallel](architecture.md#associative-recall-in-parallel)).
`sequential` waits for the search to finish before the model starts, so its result is
always in the first call and a reply is never paused, at the cost of the search time
added to the first token. The system prompt only mentions mid-reply hints in
`parallel`. It has no effect when `associative_memory` is `off`.

`true` and `false` from older agent files are still accepted and mean `llm` and `off`.
Other values are rejected when the agent is loaded. A chat without memory
(`has_memory: false`) never searches.

The root `settings.yaml` holds the framework-wide tuning shared by all agents. The
`llm` mode query step is a small, fast model call that runs before every reply, so it
should not use the chat's own model:

```yaml
associative_memory:
  context_messages: 6
  top_n: 3
  score_threshold: 0.25
  query_model: openai/gpt-4o-mini
  api_key: OPENROUTER_API_KEY
  # zdr: true   # optional override of the user's require_zdr
```

`query_model` is an OpenRouter model ID (or a `claude_subscription:<model>`
reference, which runs with the requesting user's own Claude login; see
[claude subscription](claude-subscription.md)) and the optional `zdr` overrides the
user's `require_zdr` for these requests (true restricts routing to zero data
retention providers; unset follows the user). `api_key` names the env var holding the shared OpenRouter key.
Set `query_model: null` to propose the query with each chat's own provider, model and
ZDR flag instead (the only option for Ollama-only setups); `question_only` uses
neither. If the env var is unset or the query request fails, the error is logged, the
turn continues without associative memory and the indicator shows "skipped".
`context_messages` (0-100) is how many recent messages the query model sees,
`top_n` (1-20) the fragments injected, and `score_threshold` (0-1) the minimum similarity.
Each fragment is shown with its address and heading path (or chat title and date) and
up to 400 characters of its text, cut at a sentence end when possible. Chunks of the
chat being answered are never suggested: that chat is already in the context.

### memory index

The search runs over the agent's vector index (`data/<user_id>/<agent_id>/.index`),
which covers the mind palace, the agent's skills (`agent:skills/**`), its conversations
and every user folder with `indexed: true` (see [user folders](#user-folders)).
`SOUL.md`, `MEMORY.md` and `USER.md` are not indexed: they are in every system prompt.
Hidden entries (names starting with `.`) are skipped. The search never updates the
index; it queries what is already indexed, which is kept current in the background:

- after each reply: the chat's new exchanges and the files the turn's tools wrote;
- when a chat opens and when a dream starts: a stat-only reconcile of the agent's own
  sources (unchanged files are never read; deleted files and chats are dropped);
- user folders: when a chat opens or the index worker is idle, once
  `folders_reconcile_minutes` have passed since the last scan, and on demand with
  **Reindex** in the agent's Folders tab.

The framework-wide settings live in the `index` block of the root `settings.yaml`:

```yaml
index:
  dir_name: .index          # index folder inside each agent's data folder
  collection: memory        # Chroma collection name
  file_types: [txt, md, jsonl]  # files the text search tools read; md and txt are indexed
  chunk_tokens: 400         # target chunk size, in estimated tokens (characters / 4)
  max_chunk_tokens: 600     # larger paragraphs and code blocks are split
  overlap_tokens: 60        # repeated at the start of the next chunk of the same section
  embed_batch: 64           # texts per embeddings request
  folders_reconcile_minutes: 10
```

Markdown is split by headings, then packed by paragraphs; fenced code blocks are kept
whole unless they exceed `max_chunk_tokens`. Each chunk starts with its context
(`<source>:<path> > <title> > <heading path>`). A conversation chunk is one exchange
(the user message and the reply), prefixed with the chat title and date. Skills are
one chunk per section, prefixed with the skill name and description.

The collection stores the embeddings model and the chunking scheme (a version plus
the three sizes above). When either differs from the current configuration (for
example after changing `models.embeddings` or a chunk size), the index is dropped and
rebuilt in the background on the next chat or dream start; searches return only what
the new index already holds until it is complete. The web UI shows the index status
and can force a rebuild (see [managing agents](#managing-agents)).

The web and Telegram processes may share a data folder. Chroma's persistent client is
not safe across processes, so every index access holds a short cross-process lock
(`.index/store.lock`) and reloads Chroma when another process wrote since; embedding
work holds a second lock (`.index/write.lock`) per file or chat. See the module notes
in `src/core/index.py`.

## prompt caching

Prompt caching lets the provider reuse the part of a request it has already
processed (the system prompt and earlier messages), billed at a fraction of the
input price. It is framework-wide, in the root `settings.yaml`:

```yaml
prompt_cache:
  enabled: true
  anthropic_providers: [Amazon Bedrock]
  trim_slack_percent: 25
```

How each model family is handled:

- **Claude on OpenRouter** (`anthropic/...` and `~anthropic/...` aliases) does not
  cache on its own. When `enabled` is true, RECALL puts a `cache_control` breakpoint
  on the system prompt and another on the last message that has text, on every chat
  request (streaming or not). The second breakpoint moves forward each turn, and the
  provider reuses the longest prefix it already has, so a conversation keeps reading
  its history from the cache. The cache lives about five minutes since its last use
  (OpenRouter's default). A system prompt shorter than the model's minimum
  (roughly 1,000-4,000 tokens, depending on the model) is not cached at all, which
  costs nothing. Cache writes are billed above the normal input price and reads
  well below it; a chat that sends fewer than two messages within the cache
  lifetime pays the write without ever reading it.
- **Other models** (DeepSeek, GPT, Gemini, Grok, ...) cache the repeated prefix on
  their own, so RECALL sends their requests unchanged. Which ones do is up to
  OpenRouter and the provider.
- The associative-memory hint, the `[CUTOFF CONVERSATION]` marker and other
  `system` messages that follow the conversation do not break the cache: the
  prefix before them is unchanged.

`anthropic_providers` is the list of OpenRouter provider names tried first for
Claude requests (`provider.order`). It exists because routing matters for caching:
in tests, requests routed through Google Vertex lost the cache whenever any text
after the breakpoint changed, and sometimes without a visible reason, while Amazon
Bedrock behaved as documented. Other providers stay available as a fallback, and
the ZDR policy still applies on top, so the list is a preference, not a restriction.
An unknown provider name is ignored and the request still works, so a renamed or
dropped provider degrades to OpenRouter's default routing. Use an empty list (`[]`)
to let OpenRouter choose freely. Requiring the `Anthropic` provider alone (no
fallback) returned "no endpoints found" when tested with ZDR on, so it is not a
useful value here.

`trim_slack_percent` (0-90) controls how the context window is cut. A cut moves
the start of the history, which invalidates the cached history. Without slack the
agent drops one message per turn once `conversation_messages_retained` is reached,
so the cache would be lost on every turn. With slack, the agent cuts down to
`retained - slack%` and then lets the window grow again until it passes
`retained`, so the cache survives between cuts. The window never exceeds
`conversation_messages_retained`; it only holds fewer messages right after a cut
(20 retained with 25% slack goes from 21 to 15 messages). Use `0` for the exact
sliding window. It applies to every provider, not only OpenRouter. Small windows
(for example 3 or less) are not reduced.

What still invalidates the cache, by design:

- Changing the agent's tools, personality (`SOUL.md`, read when a chat starts) or model. Tools come before the
  system prompt in the cached prefix, so a change in the tool list (an MCP server
  connecting, going down or being disabled) also invalidates the system prompt. That
  is why MCP notes live in the system prompt and are not split into a separate block.
- A different chat or model: each has its own prefix.
- A pause longer than the cache lifetime.
- Editing earlier messages or the first message in the window being cut off.

The web debug panel and the saved `usage_report` show the result on a second line
of the usage report:

```
cache: read=4958 (91% of input) write=13
```

`read` are input tokens served from the cache, `write` tokens stored in it for
later, and the percentage is the share of input that was read. The line is absent
when the provider returns no breakdown (it is unknown, not zero). The report covers
the last model call of the turn, so a turn that used tools shows the final call
only. The chat history also stores `cache_read` and `cache_write` next to the saved
input and output tokens. Prefer this line to the cost estimate when checking that
caching works: after the first message of a chat, `read` should be close to the
whole previous context.

## chat naming

The root `settings.yaml` holds a framework-wide small model used to generate chat
titles. Like Tavily, it uses one shared key referenced by env var name:

```yaml
chat_naming:
  model: openai/gpt-4o-mini
  api_key: OPENROUTER_API_KEY
  # zdr: true   # optional override of the user's require_zdr
  min_messages: 5
```

`model` is an OpenRouter model ID (or a `claude_subscription:<model>` reference,
which runs with the requesting user's own Claude login; see
[claude subscription](claude-subscription.md)), the optional `zdr` overrides the user's
`require_zdr` (true restricts routing to zero data retention providers; unset follows
the user), and `min_messages` (1-1000) is the number of user and assistant messages
required before a title is generated. Behavior:

- the title is generated once, after the reply that reaches `min_messages`, and stored
  in `<chat_id>.title.json` next to the chat's other metadata; it is not regenerated
- shorter chats keep the first user message as their title
- if the env var is unset or the naming request fails, a warning is logged and the
  chat keeps its previous title; the reply has already been delivered by then. An
  unreadable title file is an explicit error, not a fallback
- the title is shown in the chat list and removed with the chat
- users can rename a chat with messages from the web chat list (✎ or double-click the title),
  or with `PUT /api/agents/<agent_id>/chats/<chat_id>/title` and `{"title": "..."}`. the
  title is trimmed to one line of 1-60 characters (otherwise `400`) and replaces the
  stored one; automatic naming never overwrites an existing title

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

Agents with `chat_config.tools: true` (the default) use the selected tool
groups. The web group registers `web_search` and `web_retrieve` when the shared
key is set; the memory groups expose the corresponding read or edit tools. With `tools: false`
no tools are registered and the key is not resolved. Web actions use the same
chat events and history display as memory actions. The complete result enters
the active context, while the action preview stored with the turn is truncated
to 500 characters.

The shared tool-group catalog is returned by `GET /api/tool-groups` (each group
with `id`, `name`, `description` and `default`) and is selectable in the web
**Agents** tab. Agent YAML stores the selection as `chat_config.tool_groups`;
omitting it selects the groups marked `default`: every group except the folder
ones, which are always an explicit choice.
Existing `allow` and `deny` lists remain supported as an additional per-tool
filter.

## MCP servers

External services such as Google Calendar are connected through MCP servers. The
catalog lives under `mcp` in the root `settings.yaml`, each user connects their own
account in Settings > Connections (credentials are stored in
`.config/<user_id>/mcp/<server_id>/`), and each agent enables servers in
`chat_config.mcp`. Shared values such as the Google OAuth client path come from
environment variables (`GOOGLE_OAUTH_CREDENTIALS`). See [MCP servers](mcp.md).

### zero data retention is a user policy

`require_zdr` (boolean, default `false`) in `user.yaml` is the one switch for
[zero data retention](#openrouter-model-catalog-and-zero-data-retention). It is the
default of every ZDR-aware feature: new chats, the model picker's checkbox when it
opens, default-model validation, Telegram's lists, chat naming, the associative-memory
query model, dream and speech-to-text. RECALL does not force or label ZDR: the
privacy of an account is managed in the provider (for example OpenRouter's
account-wide ZDR setting, or Anthropic's training opt-out), and a user who does not
care simply uses any model. A user who wants the guarantee sets `require_zdr: true`.

The per-feature `zdr` keys (`chat_naming.zdr`, `associative_memory.zdr`,
`speech_to_text.zdr` in `settings.yaml`) remain valid as overrides; when unset they
inherit `require_zdr`. A chat's stored `zdr` flag overrides it too (see
[choosing a model per chat](#choosing-a-model-per-chat)). The decision lives in
`src/config/zdr.py`.

## config files

```yaml
# <user_id>/user.yaml
id: user-a
name: User A
require_zdr: false                   # optional; true requires zero-data-retention routes everywhere
provider:
  name: openrouter
  api_key: OPENROUTER_LUCAS_API_KEY   # omit for local providers (ollama)
agents: [aria]
ollama:                              # optional additional providers (see providers)
  enabled: false
claude_subscription:                 # optional, experimental, off by default (see claude-subscription.md)
  enabled: false
models:
  chat: openai/gpt-4o-mini           # or ollama:llama3:8b / claude_subscription:sonnet once enabled
  stt: openai/whisper-large-v3-turbo
  embeddings: openai/text-embedding-3-small
```

```yaml
# <user_id>/agents/<agent_id>.yaml
id: aria
name: Aria
description: Helps with notes and planning  # optional; shown in the web agent list, not sent to the model
chat_config:
  stream: true                        # stream tokens as they arrive
  has_memory: true                   # keep conversation history in context
  reasoning: true                    # request and separate thinking tokens
  reasoning_tokens: 20               # optional reasoning budget
  keep_reasoning_in_memory: false    # store the thinking in context/history
  conversation_messages_retained: 20 # at most N messages kept in context; cuts go below N (see prompt caching)
  tools: true                       # enable the selected tool groups
  tool_groups: [memory_read, memory_edit, self]  # memory_read, memory_edit, web, self, folders_read, folders_edit
  mcp:                               # MCP servers enabled for this agent (see mcp.md)
    google_calendar: {tools: null, read_only: false, instructions: ""}
dream:                               # background self-review sessions (see dream.md)
  enabled: false                     # runs the schedule; /dream start works either way
  every_hours: null                  # run every N hours, or
  daily_at: "04:00"                  # once a day at this local time; setting both is invalid
  model: null                        # null = the user's chat model
  reasoning_effort: medium
  max_steps: 60                      # model calls per session
  max_tokens: 500000                 # input + output tokens per session
  max_minutes: 5                     # wall-clock limit (1-120); wrap-up warning 1 minute before
  tools: [web, folders]              # optional read groups for dreams
  prompt: ""                         # empty = built-in default
  notify_telegram: false
folders:                             # user folders, each exposed as the source `<name>:` (see user folders)
  - name: notes                      # ^[a-z0-9_-]{1,32}$, unique, not a built-in source name
    path: /path/to/obsidian-vault    # must exist; never created
    indexed: true                    # feeds associative memory and query_memory (default true)
    read_only: false                 # never writable, whatever the tool groups (default false)
memory_limits:                       # chars of MEMORY.md / USER.md, injected in every prompt
  memory_chars: 2200
  user_chars: 1400
```

the agent's personality lives only in `data/<user_id>/<agent_id>/SOUL.md` (a
missing file is an empty personality; the agent YAML has no personality field),
with its memories and skills next to it; see [dream mode](dream.md#per-agent-files) for the
layout and the prompt assembly. when `tool_groups` is omitted, the default is
`[memory_read, memory_edit, web, self]`; folder groups are always explicit.

### user folders

`folders` lists folders on the machine running RECALL (an Obsidian vault, a project
notes folder) that one agent may use. each folder becomes a source named after it, so
`notes:Projects/plan.md` addresses a file inside the `notes` folder, with the usual path
guard; hidden entries (`.obsidian/`, `.git/`, `.trash/`) are left out of listings,
searches, reads and the index.

- names match `^[a-z0-9_-]{1,32}$`, are unique within the agent and cannot be a built-in
  source (`mind_palace`, `conversations`, `agent`, `dream`); invalid names are a `422`.
- the path must be an existing folder: saving an agent with a missing one is a `400`
  naming the folder. When an agent loads and a folder is missing (an unmounted drive, a
  moved vault), that folder is skipped with a warning in the log and the agent works
  without it; its indexed chunks are kept.
- `indexed: true` (default) adds the folder to the [memory index](#memory-index).
- in chats, `folders_read` gives `folder_list`, `folder_read` and `folder_search` over every
  folder, and `folders_edit` adds `folder_write`, `folder_append`, `folder_edit`,
  `folder_move` and `folder_delete`; with either group the generic memory tools can also
  read the folders, and with `folders_edit` they can write them. `read_only: true` refuses
  every write to that folder, whatever the groups. without a folder group the folders are
  still indexed but no tool can read them.
- dreams only read folders, and only with the `folders` dream tool group.

the former single `vault: <path>` key was removed; an agent file that still has it is
rejected with a message pointing to `folders`. move the path to
`folders: [{name: vault, path: <path>}]` and rename the `vault_read` / `vault_edit` tool
groups to `folders_read` / `folders_edit` and the `vault` dream tool group to `folders`.

## loading

`load_agent(user_id, agent_id?)` reads `user.yaml` and the agent file, resolves the provider key from the environment, and returns a `ResolvedAgent` (used to build the `Agent` and its `SourceRegistry`). if `agent_id` is omitted, the user's first agent is used.

checks today: the agent belongs to the user, and the provider key env var (if configured) is set.

planned: a `defaults.yaml < user.yaml < agent.yaml` merge, resource/folder
references, and a `ConfigStore` with a channel→user index for routing incoming
messages.

## managing agents

the web **Agents** tab lists the user's agents in the sidebar (name and description) and edits the name, `description`, all `chat_config` options (associative memory is a mode selector plus a timing selector, the latter disabled while the mode is off) and, in the **Folders** tab, the folder list (name, path, indexed, read-only) with the agent's memory index status (sources, files, chats and chunks indexed, last reconciles, rebuilding or indexing) and **Reindex** / **Rebuild from scratch** buttons. a new agent's `id` is derived from its name (lowercase, accents removed, non-alphanumeric characters replaced by `-`) and is not editable from the form. the same operations are available as routes:

| method | route | result |
| --- | --- | --- |
| `GET` | `/api/tool-groups` | shared tool-group catalog available to the user's agents |
| `GET` | `/api/mcp` | shared MCP server catalog with the user's connection status |
| `GET` | `/api/mcp/<server_id>/tools` | tools of a server the user has connected (`409` when not connected) |
| `POST` | `/api/mcp/<server_id>/connect` | starts the server's sign-in for the user; returns the status and sign-in URL |
| `DELETE` | `/api/mcp/<server_id>/connection` | stops the user's server and deletes their stored credentials |
| `GET` | `/api/agents` | full configuration of each agent the user owns, including `chat_config.tool_groups` |
| `POST` | `/api/agents` | creates `agents/<id>.yaml` and appends the id to `agents` in `user.yaml` (`201`) |
| `GET` | `/api/agents/<agent_id>` | one agent's configuration |
| `PUT` | `/api/agents/<agent_id>` | replaces the agent; the body `id` may differ, which renames it |
| `DELETE` | `/api/agents/<agent_id>` | removes the agent from `user.yaml` and deletes its file (`204`) |
| `GET` | `/api/agents/<agent_id>/index` | memory index status: `sources`, `files`, `chats`, `chunks`, `last_reconcile`, `folders_reconciled_at`, `rebuilding`, `busy`, `model`, `error` |
| `POST` | `/api/agents/<agent_id>/index/reindex` | queues a reconcile of every source, folders included (`202`); body `{"full": true}` drops the index and rebuilds it |

dream settings, SOUL (with history), memories and dream sessions have their own
routes, listed in [dream mode](dream.md#web-api).

rules:

- ids match `[A-Za-z0-9_-]{1,64}`; `name` has 1–100 characters and `description` 0–500 (optional, empty by default). the agent body has no personality field (an unknown field such as `personality_prompt` is a `422`): the personality is `SOUL.md`, read and written through `/api/agents/<agent_id>/soul`. the form's personality field for a new agent is sent there right after the agent is created.
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
