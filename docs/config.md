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

creating an agent = adding a file. isolation is natural per user folder. everything except secrets is versionable in git. a global `defaults.yaml` with merge is planned but not yet used.

## provider & secrets

each **user** picks a provider by name and (for cloud providers) brings their own key, referenced by env var name and resolved from the environment / `.env`. local providers (ollama) need no key:

```yaml
# user.yaml
provider:
  name: openrouter                    # selects the provider implementation
  api_key: OPENROUTER_LUCAS_API_KEY   # env var NAME holding the key; omit for local providers
```

api keys never live in yaml. agents inherit their owner's provider; they only pick model names, never keys.

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
(1-10 results, at most 2,000 excerpt characters per result), and Markdown
extraction with optional query-focused passages and explicit truncation.
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
```

```yaml
# <user_id>/agents/<agent_id>.yaml
id: aria
name: Aria
personality_prompt: >
  you are aria, a helpful assistant...
models:
  chat: qwen3:8b
  embeddings: openai/text-embedding-3-small
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

## mutability

config is read at load time; to change something, edit the yaml and restart. the file-based layout supports runtime agent creation later (write a new `<agent_id>.yaml`) without migration.
