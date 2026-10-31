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

- each agent has a **tool policy**: an allow/deny list over the full tool catalog (tools.md).
- some agents may lack `web_search`, `obsidian`, `delete`, etc.
- **modes** (recall/dream) further restrict the active set on top of the policy.

the effective toolset at any moment is:

```
effective = agent_policy ∩ mode_toolset
```

example: an agent whose policy excludes `web_search` will not have it even in dream mode, where it is otherwise exposed.

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
tools:
  allow: [list, read, search, query_memory, list_tags, find_notes_by_tag,
          write, edit, move, delete, recall_mode]
  deny:  [web_search, web_retrieve]
modes:
  dream: { every_hours: 6 }
  sleep: { every_hours: 3 }
```

## resolution order for a tool call

1. is the tool in the agent's `allow` set (and not in `deny`)?
2. is it exposed by the current mode?
3. does the target `source:path` resolve inside one of this agent's sources?

if any check fails, the call is rejected before touching data.
