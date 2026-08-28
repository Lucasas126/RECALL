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

## first approach: no mcp

the mcp (L3) is purely an exposure surface for external clients. **for the first approach it will not be used.** the agent uses `core/` + `capabilities/` directly, so recall, dream, associative memory and the obsidian overlay all work without it.

adding the mcp later is trivial (write an adapter, zero core refactor) **as long as we keep these habits from the start**:

1. capabilities and sources expose a clean, serializable api (return dicts/pydantic, not internal objects).
2. no global state — everything goes through the injected `SourceRegistry`.
3. keep logic separate from langgraph orchestration — a capability is a plain function/class that could be called from a graph node *or* an mcp tool.
