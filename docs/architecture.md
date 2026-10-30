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
- `send` yields `ChatEvent`s (`reasoning`, `text`, `tool`, `usage`, `interrupted`, and `memory` when associative recall finds matches), not channel formats. the `memory` event carries the number of matched fragments; adapters decide how to display it. a telegram adapter would call `send(..., stream=False)`, deliver the `text` event as a message and handle `interrupted` without claiming a completed answer. rendering and transport stay in the adapter.
- one `Agent` is kept per (user, agent) and switches between that agent's chats with `open_chat`; execution on an agent is serialized, but a new message can interrupt its active reply to the same chat.

status: implemented for the web channel; the cli still uses `Agent` directly.

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

Status: service steering, web integration and interruption metadata in `get_chat`
are implemented. Telegram remains planned; it can reuse this service without
changing the agent.

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
```

## first approach: no mcp

the mcp (L3) is purely an exposure surface for external clients. **for the first approach it will not be used.** the agent uses `core/` + `capabilities/` directly, so recall, dream, associative memory and the obsidian overlay all work without it.

adding the mcp later is trivial (write an adapter, zero core refactor) **as long as we keep these habits from the start**:

1. capabilities and sources expose a clean, serializable api (return dicts/pydantic, not internal objects).
2. no global state — everything goes through the injected `SourceRegistry`.
3. keep logic separate from langgraph orchestration — a capability is a plain function/class that could be called from a graph node *or* an mcp tool.
