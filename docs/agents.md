# agents: multi-agent & multi-user separation

the platform hosts **many agents**, each belonging to a user. agents differ in personality, task, data and available tools. nothing is shared implicitly between them.

## what an agent instance is

an agent instance is defined by:

- **identity**: id, name, personality / system prompt.
- **data scope**: its own mind palace, its bound obsidian vault(s), its conversations, its user folders.
- **tool policy**: which tools it is allowed to use.
- **config**: chat behavior, overlay mode per vault, mode cadences (dream/sleep).

Model IDs belong to the user, not the agent. The user's LLM/STT defaults apply
unless a chat has an explicit override; embeddings also use the user
configuration. The agent's runtime receives those dependencies without owning
their configuration. See [model defaults](config.md#user-model-defaults-and-chat-overrides).

everything is resolved *within* the agent — its `SourceRegistry` is built from its own config, so `mind_palace:` always means **this** agent's palace.

## data separation

- each agent has its **own mind palace root** — fully isolated.
- **conversations** are scoped per agent (and per user).
- **obsidian** vaults are bound per agent/user; two agents do not share a vault unless explicitly configured, and even then the safe-mode shadow (`ai-agent/`) should be per agent to avoid collisions.
- the **vector index is namespaced per agent**: either a separate collection per agent, or a mandatory `agent_id` metadata filter on every query. retrieval must never cross agents.

isolation is enforced in one place: the per-agent `SourceRegistry` (path resolution + traversal guard) and the per-agent index namespace.

## tool separation

- each agent has a **tool-group policy**: a selection from the shared tool-group catalog.
- the available groups are memory read-only, memory read and edit, and web search.
- legacy `allow`/`deny` tool lists are still accepted as an additional per-tool filter.
- **modes** (recall/dream) further restrict the active set on top of the policy.

the effective toolset at any moment is:

```
effective = agent_policy ∩ mode_toolset
```

example: an agent whose policy excludes the web group will not have `web_search` or
`web_retrieve`.

## managing agents

users create, edit and delete agents from the web **Agents** tab; the routes and rules are in [config.md](./config.md#managing-agents). A new agent's id comes from its name and stays fixed in the form. Changing an id through the API moves its data folder, and deleting an agent keeps its data on disk.

## agent config (sketch)

```yaml
id: aria
name: Aria
personality_prompt: >
  you are aria, a curious and tidy assistant...
sources:
  mind_palace:
    root: ./data/user-a/aria/mind_palace
  conversations:
    root: ./data/user-a/aria/conversations
  obsidian:
    resource: vaults.main      # from the owner's resources (users.md)
    overlay: safe              # safe | auto
  user_folder:
    resource: folders.refs
chat_config:
  tools: true
  tool_groups: [memory_read, memory_edit]
modes:
  dream: { every_hours: 6 }
  sleep: { every_hours: 3 }
```

## resolution order for a tool call

1. is the tool included by one of the agent's selected tool groups?
2. is it exposed by the current mode?
3. does the target `source:path` resolve inside one of this agent's sources?

if any check fails, the call is rejected before touching data.
