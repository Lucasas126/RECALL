# architecture

## the problem

recall has two very different kinds of functionality:

- **stateless i/o**: read/write the mind palace, read/write the user's obsidian vault, search the internet. these are clean request/response operations.
- **stateful orchestration**: associative memory, recall mode, dream mode. these are not tools you can hang off the side — they are part of the agent's structure, run over time, and also read/write the same data as the i/o operations.

if we bury the memory logic inside the agent graph, it can't be reused or exposed. if we treat everything as an isolated tool, the cognitive modes (recall/dream) don't fit, because they need context control and background execution.

## the solution: layered architecture

separate the system into layers where **data lives in its own plane** and everything else consumes it. the key rule

```
L4  clients / orchestration   recall agent (langgraph + telegram), external clients
L3  external adapter (future) exposes L1/L2 to external clients
L2  capabilities (internal)   associative memory, recall mode, dream mode
L1  data / sources plane      mind palace, conversations, obsidian (+overlay), vector index
```

- **L1 — data/sources plane (core)**: the actual data and how to access it. a `SourceRegistry` gives unified, typed access (`mind_palace()`, `obsidian()`, `vector()`). depends on nothing above.
- **L2 — capabilities**: the cognitive modes. each one receives the sources it needs by injection (dependency injection), never via a global. reads/writes L1 directly.
- **L3 — external adapter (future)**: a thin adapter that could expose L1 and selected L2 capabilities to external clients. It imports L1 and L2, never the reverse.
- **L4 — orchestration**: the recall agent itself, plus the background scheduler for dream mode ([dream.md](../user-guide/dream.md)). accesses L1 and L2 directly.

## resources vs tools

- **resources** = read-only views of sources: mind palace notes, conversations, the agent's own files, user folders.
- **tools** = what the model calls to read or change them: `query_memory`, `read`, `write`, `edit`, `folder_*`, `web_search`, `memory`, `soul_edit`, ... (see [tools](../reference/tools.md)). Recall mode (`deep_recall`) is planned, not implemented.
- dream mode is **not** a tool — it is a background session that writes to L1, triggered by its schedule, `/dream start` or the web API.

## channels and the chat service

channels (web, telegram, cli) are thin adapters over one channel-agnostic `ChatService` (`src/service/chat.py`), so adding a channel never touches the agent or the data plane.

```
web (src/web)  ─┐
telegram (src/telegram) ─┼─▶ ChatService ─▶ Agent ─▶ SourceRegistry
cli (cli.py, direct) ─┘
```

- a channel resolves its own identity to a `user_id` (web: Argon2 password login and signed session; telegram: a telegram id → user through `TELEGRAM_ALLOWED_USERS`) and then calls the service with that `user_id`. the service never trusts ids to be well formed: it validates user, agent ownership and chat ids.
- operations: `login`, `list_agents`, `list_chats`, `create_chat`, `get_chat`, `delete_chat`, `send`. CRUD returns plain dicts; `send` returns events.
- `send` yields `ChatEvent`s (`reasoning`, `text`, `tool`, `usage`, `interrupted`, `notice` when a video was replaced by frames for an image-only model, and `memory` while associative recall searches and when it ends), not channel formats. the `memory` event carries the search state and the number of matched fragments (see the messages endpoint below); adapters decide how to display it. the telegram adapter streams the events into messages (native drafts in private chats) and handles `interrupted` without claiming a completed answer. rendering and transport stay in the adapter.
- one `Agent` runtime is kept per (user, agent, chat), so chat context and mutable reply state cannot leak between conversations; different chats of the same configured agent can execute in parallel, while a new message can interrupt its active reply to the same chat.

status: implemented for the web and Telegram channels; the cli still uses `Agent` directly.

The web composer sends image, audio and video attachments as validated
OpenAI-compatible content parts. Providers expose declared input modalities;
unsupported attachments are rejected before model invocation with a message
localized using the user's `language` setting. Audio is transcribed through
the configured OpenRouter speech-to-text model before the chat model runs. Videos
for image-only models are decomposed into frames and audio first (see
[config.md](../user-guide/files-and-media.md#video-input)). The audio becomes text, so the resulting
conversation turn stays text-only apart from any frames and can be reopened with
any later chat model. PDFs follow the same rule: `ChatService.send` replaces each PDF with its
extracted text, with page markers, before validation, so no provider receives the file or
its page images (see [config.md](../user-guide/files-and-media.md#pdf-input)).

### web interface

The web UI (`src/web/static`, vanilla HTML/CSS/JS) is a thin adapter and holds no backend logic.
Status: implemented.

- theme: light, dark or follow the system (`prefers-color-scheme`). The choice is per browser,
  stored in `localStorage` (`theme`) and set in Settings > Theme, not shared with Telegram. All
  colors are CSS variables in `style.css`; a small inline script in `index.html` applies the saved
  theme before first paint.
- layout: a centered reading column on desktop; below 800px the sidebar becomes a drawer opened
  from the menu button, dialogs go full screen and touch devices get 44px hit areas.
- active agent: the chat header shows the agent's initial, name and a switcher (a transparent
  `<select>` over the banner, so the native picker is used on every device). Assistant messages
  carry the agent's name and the composer placeholder addresses it.
- sidebar and composer: the sidebar has the brand, New chat and the Chats/Agents tabs on top, and
  the account row (user, theme toggle, Settings, Log out) at the bottom. The composer is a single
  card; the chat model and reasoning effort are compact chips inside it, next to the attach and
  send icons. Buttons use inline SVG line icons with an `aria-label`.
- settings and agent editor: Settings > Connections is a collapsed disclosure showing how many
  services are connected (it only polls for sign-in while open). The agent editor has four form
  tabs: Basic (name, description and, for a new agent, its personality), Advanced (streaming,
  memory, reasoning and associative-memory options, retained messages), Tools (Use tools and the
  tool groups) and MCP. Save and Delete stay visible under those tabs, and a rejected field opens
  the tab that holds it. A saved agent also has the [dream mode](../user-guide/dream.md#web-ui) tabs Soul &
  memory, Dream and Dream sessions, each with its own save actions.
- reasoning blocks: trailing newlines from the model are trimmed so no empty line shows under an
  expanded block.

### model catalog and chat selection

The provider layer fetches OpenRouter's public model and ZDR endpoint catalogs;
`ChatService` scopes catalog access and persistent favorites to the current
user. The web channel exposes these operations as HTTP and a searchable model
selector. ZDR is an optional user policy (`require_zdr` in `user.yaml`, off by
default; one helper in `src/config/zdr.py` resolves it for every feature). When
it applies, every OpenRouter chat inference request independently receives the
chat's ZDR routing preference, including auxiliary memory queries and transport
retries; when it does not, no `provider.zdr` field is sent and catalogs are not
filtered. A chat follows the user's setting unless it stored its own flag during
model selection; the selector resets its checkbox to the user's setting each time
it opens.
Telegram's `/selectmodel` reads the same user favorites, pins available favorites
before the price-sorted remainder, and marks them with stars without expanding
the catalog or exposing favorite mutations in the bot.

Model selection is a nullable chat override stored through `ConversationsSource`,
not a mutation of user defaults or a direct filesystem operation in the channel.
The service validates model availability and rejects changes while its agent
has active/queued work. Its cached agent is rebuilt lazily when the current
chat's selection changes. `Agent.open_chat` restores each chat's override (or
the user default), so other chats and CLI reopening behave consistently.
See [model selection and favorites](../user-guide/models-and-providers.md#choosing-a-model-per-chat).

Reasoning effort follows the same pattern: a nullable per-chat override in
`ConversationsSource`, validated by the service against the model's advertised
reasoning support, and applied by `Agent.open_chat` over the agent default.
Telegram's `/settings` panel and the web composer both use it. See
[reasoning effort per chat](../user-guide/models-and-providers.md#reasoning-effort-per-chat).

### shared channel preferences

`ChatService.get_preferences` and `update_preferences` expose user-scoped
thinking visibility, notification language, a preferred agent and current
user-owned LLM/STT defaults.
Overrides live in a validated configuration-side store, separate from user YAML
and conversation sources. Reads bypass caches; updates merge under an
interprocess file lock before atomic replacement, so web and Telegram processes
do not overwrite unrelated fields. Agent selections are validated against the
requesting user's configured agents before saving. Preference operations
neither move chats between agents nor alter in-flight execution; inherited
inference models refresh before the next request.

Status: persistence, `GET`/`PATCH /api/preferences`, Telegram's inline `/settings` panel,
web settings controls, default-model selection, nullable chat override resets,
thinking visibility and agent selection are
implemented. Visibility is enforced by channel renderers without changing
reasoning generation, stored history or service events. Telegram captures each
reply's user/agent/chat before asynchronous delivery, so changing a session
cannot reroute an accepted message. The settings agent option changes the
active Telegram agent and starts a fresh chat with it (like `/selectagent`), rather than just saving a future
preference. Web agent switching is the chat header's agent banner, which shows the active agent's name and switches agent when clicked.
English/Spanish operational notices and
application errors use a shared translation catalog served to the browser.
Errors carry their RECALL-owned template separately from opaque interpolation
values; provider text is not translated. Cached agents receive the latest
language at generation start for capability errors, without changing models
or rebuilding context. Static navigation/help text and technical diagnostics
are not a complete localized interface.
See [shared user preferences](../reference/user-yaml.md#shared-user-preferences).

### channel-neutral steering

`ChatService.send` validates and schedules immediately. A transient worker per
(user, agent) generates replies independently of event consumption, so an idle,
slow or disconnected channel does not block a later steering message. Workers
exit when their queue is empty. Scheduling is in-process, not a durable queue;
multiple server processes do not coordinate active generations.

A new message to the active chat cancels its unfinished generation, preserves
the partial reply and appends the new user input before generating again. A burst
keeps every user message in arrival order; superseded queued generations record
an empty interrupted turn without invoking the model. The same-chat continuation
stays together ahead of unrelated queued chats. Other chats on that agent run independently without interrupting it; each chat
has its own worker and runtime. Different users and agents have independent
slots as before.
Active and queued chats are excluded from empty-chat pruning.

Every `ChatEvent` carries a `generation_id`. An interrupted request ends with
`kind="interrupted"`, the full partial answer in `text`, and
`data={"reasoning": ..., "actions": [...]}`. This snapshot includes completed
actions, even if cancellation prevented their individual events from being
delivered. Adapters should reconcile it rather than append duplicate fragments
or actions. Interrupted requests do not emit `usage` or a successful final
`text` event when `stream=False`. Worker failures are raised to the iterator's
consumer; a later request can still run.

A steering continuation retains the active turn even with `has_memory: false`
or a small `conversation_messages_retained`; ordinary later turns still follow
those settings. The agent's `continuation=True` option is for such uninterrupted
same-chat scheduling, not for resuming arbitrary older chats. Committing a final
answer and accepting cancellation are mutually exclusive: a message arriving
after commit starts a normal turn, not an interruption of an already saved reply.

Status: service steering, web integration, Telegram integration and interruption
metadata in `get_chat` are implemented. Telegram uses the same service events,
including streaming text, steering cancellation, and validated image, audio
and video attachments.

### web steering

The composer remains enabled while a reply is generating; its button reads
`Steer`. Sending another message stops the previous generation on the server,
not just the browser fetch. Each stream has its own reply view. The partial
answer remains visible with an interruption label, also when reopening history;
the interruption snapshot replaces text/reasoning and adds missing actions
without duplicating already displayed ones.

Rapid sends wait only for the previous HTTP response headers (acceptance), not
its generated answer, to preserve input order. Changing agents, opening/creating
chats and switching between chats are allowed while replies are generating: each
chat with a reply in flight keeps its own message view in the browser, which
keeps receiving the stream while another chat is open and is restored as-is when
the chat is reopened. The chat list marks generating chats, and a generating chat
cannot be deleted. `Steer`, the model picker and the usage panel only apply to
the open chat. Chats of the same agent generate in parallel too; only messages
targeting the same chat are ordered and steered together.
Logging out detaches the browser streams; callbacks from an old session cannot
modify the new session, and only the latest request of a chat can update its usage panel.
Detaching a transport does not cancel a service generation on its own.

The messages endpoint returns SSE with the generation id in each event's `id:`
field. Existing `text`, `reasoning`, `tool`, `usage` and `done` payloads are unchanged.
An `error` event carries the (localized) message as a plain string, or
`{"message": "...", "code": "..."}` when the error has a stable code (see
[error codes](../reference/api.md#error-codes)). When a turn runs an associative search, `memory` events report its
state: `{"state": "searching"}` when the stream starts, then once
`{"state": "done", "matches": N}` (`N` is 0 when nothing relevant was found) or
`{"state": "skipped"}` when the search failed, finished too late to be used or was
interrupted. The final event can arrive after `text` events, see
[associative recall](#associative-recall-in-parallel). Replies that are not streamed
get only the final event. The chat history stores the outcome on the assistant
message (`associative_matches`, including 0, or `associative_skipped: true`) so the
web UI can show the indicator after reopening a chat; turns without a search, and
older ones, have neither.
Assistant history also stores its formatted `usage_report`, allowing the web
context panel to be restored when reopening a chat. Older turns that predate
`usage_report` show their saved input/output token totals, when available. When
the provider reports prompt-cache usage, the report has a `cache:` line with the
tokens read from and written to the cache, and the saved totals include
`cache_read` and `cache_write` (see [prompt caching](../reference/settings-yaml.md#prompt-caching)).
The current response's `usage` event also appends ephemeral latency measurements:
total agent processing time, associative-memory lookup, summed main-model calls,
and remaining tools/overhead time, each in milliseconds and as a share of total.
When the lookup runs in parallel with the model its line is labelled
`associative memory (parallel)` and the shares can add up to more than 100%;
tools/overhead is the time covered by neither the lookup nor a model call.
Latency is not stored in conversation history; model-call time includes waiting
for streamed tokens and excludes the separate associative-memory query call.
`interrupted` carries
`{"content": "...", "reasoning": "...", "actions": [...]}` and is followed by
`done`. Streams disable proxy buffering; a connection that ends without `done`
is shown as an error rather than a completed answer.

### associative recall in parallel

For streamed replies the associative search (query model plus vector lookup) runs in
a worker thread while the main model already generates, instead of delaying its first
token. `Agent._take_assoc_result` uses its outcome once per turn:

- finished before a model call starts: the `[associative memory]` block is appended to
  that call's context, as before
- finishes while a model call is running: that call is stopped at once if it has not
  written any text yet, otherwise at the next sentence end (`.`, `!`, `?` or a line
  break), so a sentence is never cut. The text written so far is kept as an assistant
  message, the block (plus a note asking the model to continue and to correct anything
  the fragments contradict) is added after it, and the model continues in a new call.
  The final `memory` event is emitted, followed by a blank-line content chunk when the text ended
  a sentence, so channels show one continuous reply and the stored turn is the partial
  text, that gap and the continuation. Reasoning written before the pause is kept with
  the turn but not resent
- nothing found, or the search fails: the reply is never touched (a warning is logged
  for failures)
- not finished when the reply completes, or the turn is interrupted or steered: the
  search is cancelled and its result discarded

The system prompt tells the model this can happen. Agents with
`associative_execution: sequential` never run this way: the stream emits `searching`,
waits for the search (still cancellable), emits its final `memory` event and only then
calls the model, so nothing is paused or skipped for arriving late, and the prompt
omits the notice. Calls without a cancellation token and `stream=False` (the
synchronous path) cannot be paused either, so they wait for the search first. The search only sees the history from before the current message and never
returns chunks of the chat being answered (`recall(..., chat_id=...)` passes it to
`VectorIndex.query(exclude_chat=...)`). The lookup itself never updates the index: it
queries what is already indexed, and the index is kept current in background workers
(the chat's new exchange and the files the turn wrote, queued after each reply; a
stat-only reconcile at chat and dream start; user folders on an interval), see
[memory index](../user-guide/memory.md#memory-index). `CancellationToken._link` lets the search stop one
model call without cancelling the turn. Status: implemented; the web and Telegram
channels receive the `memory` events from the service, the CLI prints the final one as
a note. The web chat shows them as a small pill above the reply: a pulsing dot while
searching (static with `prefers-reduced-motion`), then the number of matches, "no
matches" or "skipped" (dimmed), restored from the stored outcome when a chat is
reopened. Telegram ignores the events.

### generation cancellation

`Agent.invoke(..., cancellation=token)` accepts a one-shot, thread-safe
`CancellationToken` from `src/providers/cancellation.py`. Another thread can call
`token.cancel()`; the invocation raises `GenerationCancelled`, not a successful
empty reply. Create a fresh token for each generation. `cancel()` returns false
if the final answer has already committed.

OpenRouter and Ollama chat requests use a shared HTTPX transport with a local
async event loop behind the synchronous provider API. Cancellation interrupts
waiting for HTTP headers or body data and closes the connection; it does not
wait for the next token. Closing the connection does not guarantee that a remote
provider stops computing or billing immediately. Embeddings and metadata requests
are unchanged. These synchronous chat APIs must run outside an existing async
event loop (the chat service runs them in its own worker).

The agent preserves emitted answer/reasoning fragments and completed actions in
an assistant turn with `interrupted: true`. Its active and reloaded context marks
the answer as interrupted; reasoning still follows `keep_reasoning_in_memory`.
No final usage report is produced for an interrupted generation. Tools already
running are allowed to finish, not rolled back; pending tools are skipped and
receive placeholder results in the active context to keep tool-call pairs valid.
Associative query generation runs on its own token, which a turn cancellation also cancels. Cancellable
agent calls use internal streaming even with `stream=False`, so channels without
token-by-token delivery can still preserve partial replies. OpenRouter's
non-streaming fallback emits the same reply events and is used only before any
output or tool calls arrive; a failed partial stream is surfaced, not silently
retried or duplicated.
An input accepted with an already cancelled token is still logged, together with
an empty interrupted reply, without calling the provider.

Focused regression checks (no configured providers or private data required):

```shell
uv run python -m unittest discover -s tests -p "test_*.py" -v
node --check src/web/static/app.js
node --test tests/test_model_sorting.cjs
```

The Node checks cover model ordering, capability matching and selector state
(favorites toggle, active-filter counts, collapsed filters and session reset).
Keyboard navigation and responsive layout still require a browser check.

## first approach: internal capabilities only

the L3 adapter is purely an exposure surface for external clients. **for the first approach it will not be used.** the agent uses `core/` + `capabilities/` directly, so recall, dream, associative memory and the obsidian overlay all work without it.

adding an external adapter later is straightforward (write an adapter, zero core refactor) **as long as we keep these habits from the start**:

1. capabilities and sources expose a clean, serializable api (return dicts/pydantic, not internal objects).
2. no global state — everything goes through the injected `SourceRegistry`.
3. keep logic separate from langgraph orchestration — a capability is a plain function/class that could be called from a graph node or an external adapter.
