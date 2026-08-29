# configuration

how user and agent config is stored, loaded and resolved.

## storage layout

yaml, editable by hand, one file per entity, nested per user. one file per agent.

```
config/
  defaults.yaml              # global defaults
  <user_id>/
    user.yaml                # user: channels, resources, provider key, agents
    agents/
      <agent_id>.yaml        # one file per agent
```

creating an agent = adding a file. isolation is natural per user folder. everything except secrets is versionable in git.

## secrets

api keys never live in yaml. each **user** brings their own provider key, referenced by env var name and resolved from the environment / `.env`:

```yaml
# user.yaml
provider:
  api_key_env: OPENROUTER_API_KEY_LUCAS
```

agents inherit their owner's provider key; they only pick model names, never keys.

## config files (sketch)

```yaml
# defaults.yaml
models:
  chat: openai/gpt-4o-mini
  embeddings: openai/text-embedding-3-small
overlay: safe
modes:
  dream: { every_hours: 6 }
  sleep: { every_hours: 3 }
```

```yaml
# <user_id>/user.yaml
id: user-a
name: User A
channels:
  telegram: [123456789]
provider:
  api_key_env: OPENROUTER_API_KEY_LUCAS
resources:
  vaults:
    main: /home/user/vault
  folders:
    refs: /home/user/refs
agents: [aria, scout]
```

```yaml
# <user_id>/agents/aria.yaml
id: aria
name: Aria
personality_prompt: >
  you are aria, a curious and tidy assistant...
models:                       # optional, overrides defaults
  chat: openai/gpt-4o-mini
sources:
  mind_palace:
    root: ./data/user-a/aria/mind_palace
  conversations:
    root: ./data/user-a/aria/conversations
  obsidian:
    resource: vaults.main      # from the owner's resources
    overlay: safe
  user_folder:
    resource: folders.refs
tools:
  allow: [list, read, search, query_memory, list_tags, find_notes_by_tag,
          write, edit, move, delete, recall_mode]
  deny:  [web_search, web_retrieve]
```

## loading pipeline

pydantic models mirror the docs: `Defaults`, `UserConfig`, `AgentConfig`, and a resolved `ResolvedAgent`.

```
raw yaml ─▶ pydantic validate ─▶ merge with defaults ─▶ resolve refs ─▶ ResolvedAgent
```

- **validate**: types and shapes per model.
- **merge**: precedence `defaults.yaml < user.yaml < agent.yaml` — define `models`, `overlay`, mode cadences once, override only what differs.
- **resolve refs**: `sources.obsidian.resource: vaults.main` → real path from `user.resources.vaults.main`; `provider.api_key_env` → value from the environment.
- **guard**: resolved paths must stay inside the user's allowed roots (anti-traversal, consistent with sources.md).

## validation at startup (fail-fast)

the whole config is validated when the platform boots; any error aborts startup instead of failing later mid-conversation. checks:

- unique `user_id` and, per user, unique `agent_id`.
- every listed agent has a file.
- every `resource:` reference exists in the owner's `resources`.
- every tool in `allow`/`deny` is a known tool (tools.md).
- `overlay ∈ {safe, auto}`.
- each user's `provider.api_key_env` is set in the environment.
- resolved source roots exist (or can be created) and are inside allowed roots.

## runtime: ConfigStore

at boot the `ConfigStore` loads everything into memory and builds lookup indexes:

```
ConfigStore
  ├─ users:   dict[user_id, UserConfig]
  ├─ agents:  dict[(user_id, agent_id), ResolvedAgent]
  └─ channels: dict[channel_id, user_id]     # reverse index for incoming messages
```

- `get_agent(user_id, agent_id) -> ResolvedAgent`; the per-agent `SourceRegistry` is built from it and cached.
- the `channels` index resolves an incoming message's identity to a `user_id` (see users.md).

## mutability

- **first approach**: static — config is read at boot; to change something, edit the yaml and restart (or call `store.reload()`).
- **later**: runtime creation (a user spins up a new agent) writes a new `<agent_id>.yaml` and calls `reload()`. the file-based layout supports this without migration.
