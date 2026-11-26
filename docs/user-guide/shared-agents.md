# Shared agents

A **shared agent** is one agent used by two or more people: the same SOUL, memory, mind palace, skills, tools, backends and chats. Everybody sees every chat of the agent. It stays apart from each member's private agents.

Design and internals: [shared agents design](../project/design-shared-agents.md), [agents and users](../architecture/agents-and-users.md#shared-agents-spaces).

## Sharing an agent

1. Open the agent's settings and go to **Share**.
2. Type the user id of the person to invite and press **Invite**. The agent does not move yet.
3. The invited person sees the request in the **Agents** tab (a dot marks the tab when something is waiting) with **Accept** and **Decline**. You can revoke it while it is pending.
4. On accept, the agent (config, memory, chats, avatar, automations) moves into a shared space. The space is created for the group, or reused if you already share another agent with the same people.

Any member can invite more people to an already shared agent. The agent must be idle (no reply running or queued) when it moves. An agent id that already exists in the invited person's private agents is refused.

## What members share

- Memory, mind palace, skills, chats and approvals (the first decision on an approval card wins).
- Tools, terminal backends, MCP servers and user folders, as set on the agent.
- All members are equal: any of them can edit the agent, invite, unshare or delete it.
- The permission **level** is the lowest `max_level` among the members. If a member's ceiling is lower than the agent's level, what is above it is disabled, not deleted. If a member can no longer be read (removed or broken account), the shared agent is unavailable until fixed.

## Who pays

The shared agent uses the **creator's** provider keys and Claude subscription (the user who first shared it). The agent shows "Shared agent · uses the keys and Claude subscription of user-a". Any member may change the provider settings of the shared agent.

## Who is speaking

Each message is stored with its author, and the model receives it with the speaker's name in capitals: `[USER-A] ...`. `USER.md` has one section per member (`## user-a`); the `memory` tool with target `user` edits the section of the person talking. Its size limit is the normal limit times the number of members, at most 5 times. Dreams keep the sections.

Private agents cannot @mention, delegate to or list a shared agent, and the shared agent cannot reach private ones. Mentions and delegation between agents of the same space work as usual.

## Leaving and unsharing

- **Leave**: you stop being a member; the agent stays with the others and nothing is deleted. The last member cannot leave: unshare instead.
- **Unshare** (any member): with one member left, the agent moves back to that person. With several, **every member gets a private copy** (chats, memory, automations) and the shared agent is removed. In a copy `USER.md` keeps only that person's section, keys and MCP tokens are not copied (each copy uses its owner's own provider settings), the level is capped by the owner's ceiling, and a clashing agent id gets `-2`.
- A space with no agents left is deleted.

## Automations

Automations move with the agent. Copies made by unshare start **paused**; resume them in the Automations dialog.

## Telegram

A member may pick a shared agent in their own Telegram bot like any agent. The reply goes only to the person talking; the chat also appears on the web for every member. Planned (in progress): notifications and automation results to every member, and Telegram per speaker.

## Admin

The admin commands for spaces and `agent share|unshare`: see the [admin CLI](admin-cli.md#shared-agents-spaces).
