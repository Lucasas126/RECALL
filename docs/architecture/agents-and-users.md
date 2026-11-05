# agents and users

The platform is multi-user. Each **user** (a human, identified by `user_id`) owns one or more **agents**; each agent belongs to
exactly one user. Users never see each other's agents, data or memories, and nothing is shared implicitly between agents.
How to manage them day to day is in the [agents guide](../user-guide/agents.md); the files are described in the
[user.yaml](../reference/user-yaml.md) and [agent.yaml](../reference/agent-yaml.md) references.

```
user --owns--> agent --scoped--> sources (mind_palace, conversations, agent files, dream state, user folders)
```

## identity resolution

Channels map an external identity to a `user_id`; unknown identities are rejected. There is no registration flow.

- **web**: the Argon2 `password_hash` in `.config/<user_id>/user.yaml` is checked at login and the id is stored in a signed,
  HTTP-only session cookie (see [web authentication](../reference/settings-yaml.md#web-authentication)).
- **Telegram**: `TELEGRAM_ALLOWED_USERS` maps `<telegram_id>=<user_id>` entries, several users per bot ([Telegram](../user-guide/telegram.md)).
- **CLI** (`cli.py`): the `USER_ID` / `AGENT_ID` values of `.env`.

After that the target agent is chosen (explicit selection, else the user's last used agent (`agent_id`), else the first one in `agents`; new chats nobody picked an agent for use the
user's default agent, `default_agent_id`, falling back to the first one),
its `SourceRegistry` is built scoped to user and agent, and the message is handled.

## data layout

```
data/<user_id>/<agent_id>/
  SOUL.md  soul_history/  memories/  skills/     the agent's own files (source `agent`)
  mind_palace/  conversations/                    notes and chats
  dream/                                          dream state (not readable by tools)
  .index/                                         vector index (Chroma + manifest)
```

Everything an agent produces lives under its user's folder. The vector index belongs to the agent, so retrieval never
crosses users or agents. See [dream mode](../user-guide/dream.md#per-agent-files) for the file roles.

## what an agent instance is

- **identity**: id, name, description and its own files: `SOUL.md`, memories, skills.
- **data scope**: its mind palace, conversations and user folders.
- **tool policy**: the tool groups and MCP servers it may use.
- **chat behavior**: streaming, reasoning, memory, associative-memory mode, optional default model, dream settings.

Model ids and providers belong to the **user**, not the agent: the user's defaults apply unless the agent sets
`chat_config.model` or a chat has its own selection (precedence: chat > agent > user; see
[models and providers](../user-guide/models-and-providers.md)). Embeddings use the user configuration.

## isolation

Isolation is enforced in one place: the per-agent `SourceRegistry` (path resolution and traversal guard) and the per-agent
index namespace. A user folder belongs to the agent that lists it in `folders:`; paths are validated when the agent is saved.
Binding an Obsidian vault through the safe/auto overlay (a per-agent shadow tree) is a planned design, see
[overlay](obsidian-overlay.md); today a user folder pointing at the vault is the way to use one.

## tool separation

- each agent has a **tool-group policy**: a selection from the shared catalog (`memory_read`, `memory_edit`, `web`, `self`,
  `folders_read`, `folders_edit`, see [tools](../reference/tools.md#tool-group-catalog));
- each agent also chooses which shared **MCP servers** it uses, optionally read-only, with a tool selection and extra
  instructions; they act on the agent's own user's connected account ([MCP servers](../user-guide/mcp.md));
- legacy `allow`/`deny` lists are still accepted as an additional per-tool filter;
- dream sessions use their own toolset ([dream mode](../user-guide/dream.md#dream-toolset)); recall mode (planned) would restrict it further.

The effective toolset is `agent_policy ∩ mode_toolset`. For a tool call: (1) is the tool in one of the agent's selected
groups, (2) does the current mode expose it, (3) does the target `source:path` resolve inside one of this agent's sources?
If any check fails the call is rejected before touching data.
