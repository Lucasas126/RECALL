# architecture

## the problem

recall has two very different kinds of functionality:

- **stateless i/o**: read/write the mind palace, read/write the user's obsidian vault, search the internet. these are clean request/response operations.
- **stateful orchestration**: associative memory, recall mode, dream/sleep mode. these are not tools you can hang off the side — they are part of the agent's structure, run over time, and also read/write the same data as the i/o operations.

if we bury the memory logic inside the agent graph, it can't be reused or exposed. if we treat everything as an isolated tool, the cognitive modes (recall/dream) don't fit, because they need context control and background execution.

## the solution: layered architecture

separate the system into layers where **data lives in its own plane** and everything else consumes it. the key rule

```
L4  clients / orchestration   recall agent (langgraph + telegram), external clients
L3  mcp (optional adapter)    exposes L1/L2 to external agents as tools/resources
L2  capabilities (internal)   associative memory, recall mode, dream/sleep mode
L1  data / sources plane      mind palace, conversations, obsidian (+overlay), vector index
```

- **L1 — data/sources plane (core)**: the actual data and how to access it. a `SourceRegistry` gives unified, typed access (`mind_palace()`, `obsidian()`, `vector()`). depends on nothing above.
- **L2 — capabilities**: the cognitive modes. each one receives the sources it needs by injection (dependency injection), never via a global. reads/writes L1 directly.
- **L3 — mcp**: a thin adapter that exposes L1 as read-only *resources* and selected L2 capabilities + crud/search as *tools*. this is what makes the memory portable to other agents (copilot, claude desktop, etc). imports L1 and L2, never the reverse.
- **L4 — orchestration**: the recall agent itself, plus background schedulers for dream/sleep. accesses L1 and L2 directly (not through the mcp).

## resources vs tools (mcp)

- **resources** = read-only views: mind palace snapshot, recent conversations, obsidian notes.
- **tools** = mutations or reasoning: `remember`, `obsidian_write`, `associative_search`, `deep_recall`, `consolidate`.
- dream/sleep are **not** tools — they are background daemons in L2 that write to L1. at most, expose a manual `consolidate()` trigger.

## channels and the chat service

channels (web, telegram, cli) are thin adapters over one channel-agnostic `ChatService` (`src/service/chat.py`), so adding a channel never touches the agent or the data plane.

```
web (src/web)  ─┐
telegram (planned) ─┼─▶ ChatService ─▶ Agent ─▶ SourceRegistry
cli (cli.py, direct) ─┘
```

- a channel resolves its own identity to a `user_id` (web: login cookie; telegram: a telegram id → user, planned) and then calls the service with that `user_id`. the service never trusts ids to be well formed: it validates user, agent ownership and chat ids.
- operations: `login`, `list_agents`, `list_chats`, `create_chat`, `get_chat`, `delete_chat`, `send`. CRUD returns plain dicts; `send` returns events.
- `send` yields `ChatEvent`s (`reasoning`, `text`, `tool`, `usage`, `interrupted`, `notice` when a video was replaced by frames for an image-only model, and `memory` when associative recall finds matches), not channel formats. the `memory` event carries the number of matched fragments; adapters decide how to display it. a telegram adapter would call `send(..., stream=False)`, deliver the `text` event as a message and handle `interrupted` without claiming a completed answer. rendering and transport stay in the adapter.
- one `Agent` is kept per (user, agent) and switches between that agent's chats with `open_chat`; execution on an agent is serialized, but a new message can interrupt its active reply to the same chat.

status: implemented for the web and Telegram channels; the cli still uses `Agent` directly.

The web composer sends image, audio and video attachments as validated
OpenAI-compatible content parts. Providers expose declared input modalities;
unsupported attachments are rejected before model invocation with a message
localized using the user's `language` setting. Audio is transcribed through
the configured OpenRouter speech-to-text model before the chat model runs. Videos
for image-only models are decomposed into frames and audio first (see
[config.md](./config.md#video-input)). The audio becomes text, so the resulting
conversation turn stays text-only apart from any frames and can be reopened with
any later chat model.

### model catalog and chat selection

The provider layer fetches OpenRouter's public model and ZDR endpoint catalogs;
`ChatService` scopes catalog access and persistent favorites to the current
user. The web channel exposes these operations as HTTP and a searchable model
selector. Every OpenRouter chat inference request independently receives the
chat's ZDR routing preference, including auxiliary memory queries and transport
retries. ZDR is required unless that chat explicitly opted out during model
selection; the selector resets its filter to checked each time it opens.
Telegram's `/selectmodel` reads the same user favorites, pins available favorites
before the price-sorted remainder, and marks them with stars without expanding
the ZDR catalog or exposing favorite mutations in the bot.

Model selection is a nullable chat override stored through `ConversationsSource`,
not a mutation of user defaults or a direct filesystem operation in the channel.
The service validates model availability and rejects changes while its agent
has active/queued work. Its cached agent is rebuilt lazily when the current
chat's selection changes. `Agent.open_chat` restores each chat's override (or
the user default), so other chats and CLI reopening behave consistently.
See [model selection and favorites](config.md#choosing-a-model-per-chat).

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
active Telegram agent like `/selectagent`, rather than just saving a future
preference. Web agent switching stays in the sidebar.
English/Spanish operational notices and
application errors use a shared translation catalog served to the browser.
Errors carry their RECALL-owned template separately from opaque interpolation
values; provider text is not translated. Cached agents receive the latest
language at generation start for capability errors, without changing models
or rebuilding context. Static navigation/help text and technical diagnostics
are not a complete localized interface.
See [shared user preferences](config.md#shared-user-preferences).

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
stays together ahead of unrelated queued chats. Other chats on that agent wait
without interrupting it; different users and agents have independent workers.
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
chats and deleting chats remain blocked until the current streams finish.
Logging out detaches the browser streams; callbacks from an old session cannot
modify the new session, and only the latest request can update the usage panel.
Detaching a transport does not cancel a service generation on its own.

The messages endpoint returns SSE with the generation id in each event's `id:`
field. Existing `text`, `reasoning`, `tool`, `usage`, `error` and `done` payloads
are unchanged. When associative recall finds fragments, a `memory` event carries
`{"matches": N}` and the chat history includes `associative_matches` on the
assistant message so the web UI can show the indicator after reopening a chat.
Assistant history also stores its formatted `usage_report`, allowing the web
context panel to be restored when reopening a chat. Older turns that predate
`usage_report` show their saved input/output token totals, when available.
The current response's `usage` event also appends ephemeral latency measurements:
total agent processing time, associative-memory lookup, summed main-model calls,
and remaining tools/overhead time, each in milliseconds and as a share of total.
Latency is not stored in conversation history; model-call time includes waiting
for streamed tokens and excludes the separate associative-memory query call.
`interrupted` carries
`{"content": "...", "reasoning": "...", "actions": [...]}` and is followed by
`done`. Streams disable proxy buffering; a connection that ends without `done`
is shown as an error rather than a completed answer.

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
Associative query generation also receives the cancellation signal. Cancellable
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

## first approach: no mcp

the mcp (L3) is purely an exposure surface for external clients. **for the first approach it will not be used.** the agent uses `core/` + `capabilities/` directly, so recall, dream, associative memory and the obsidian overlay all work without it.

adding the mcp later is trivial (write an adapter, zero core refactor) **as long as we keep these habits from the start**:

1. capabilities and sources expose a clean, serializable api (return dicts/pydantic, not internal objects).
2. no global state — everything goes through the injected `SourceRegistry`.
3. keep logic separate from langgraph orchestration — a capability is a plain function/class that could be called from a graph node *or* an mcp tool.
