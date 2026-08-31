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
