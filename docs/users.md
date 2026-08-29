# users

the platform is multi-user. each user owns one or more agents. users never see each other's agents, data or memories.

## relationships

- a **user** is a human owner, identified by `user_id`.
- a user owns **many agents**; each agent belongs to **exactly one** user.
- **channels** (telegram, cli) map an external identity to a `user_id`.

```
user ──owns──▶ agent ──scoped──▶ sources (mind_palace, conversations, obsidian, ...)
```

## user config (sketch)

```yaml
id: user-a
name: User A
channels:
  telegram: [123456789]        # telegram ids that map to this user
resources:                     # owned by the user, bound by their agents
  vaults:
    main: /home/user/vault
  folders:
    refs: /home/user/refs
agents: [aria, scout]
```

- **resources** are things the user owns (vaults, folders). agents bind to them by reference in their own config (see agents.md), so paths live in one place.
- **agents** lists the agent ids owned by this user.

## data layout

```
data/
  <user_id>/
    <agent_id>/
      mind_palace/
      conversations/
    <agent_id2>/
      ...
```

everything an agent produces lives under its user's folder. the vector index namespace is `<user_id>:<agent_id>`, so retrieval never crosses users or agents.

## identity resolution (incoming message)

1. read the channel identity (e.g. telegram user id).
2. map it to a `user_id`; unknown identities are rejected (or onboarded).
3. pick the target agent: explicit selection, else the user's first agent.
4. build that agent's `SourceRegistry` (scoped to user + agent) and handle the message.

## separation guarantees

- a vault/folder belongs to a **user**; several of that user's agents may bind the same vault, but each keeps its **own** safe-mode shadow (`ai-agent/` is per agent) to avoid collisions — consistent with overlay.md.
- resource paths are validated to belong to the requesting user before binding.
- no cross-user or cross-agent access is ever possible: enforced by the per-agent `SourceRegistry` and the `<user_id>:<agent_id>` index namespace.
