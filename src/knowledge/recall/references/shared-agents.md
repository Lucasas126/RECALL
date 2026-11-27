# Shared agents

A shared agent is used by several people at once. Its owner is a **space** (`space-<8 hex>`, a pseudo-user that never logs in) with a list of members. Everything else works as for a private agent: SOUL, memory, skills, chats and tools are the agent's, not a person's.

## How you know you are shared

- Your system prompt shows `USER.md` in sections, one `## <user id>` per member, and a larger limit (normal limit times the member count, at most 5x).
- Every user message starts with the speaker's name in capitals, e.g. `[USER-A] hello`. Answer the person who wrote it; others read the same chat later.
- Your data lives under the space id, not under a person.

## Memory

- `memory` with target `user` edits the section of the person talking now, never another member's. Target `memory` (MEMORY.md) and the palace, skills and SOUL are shared by all.
- Put personal facts in the speaker's section; put facts about the group in MEMORY.md. Dreams keep the sections.
- Do not copy one member's private details into shared memory unless they asked.

## Permissions and billing

- Your level is capped by the member with the lowest `max_level`. If something is disabled, a member's ceiling may be the cause: say so.
- The space uses the providers and subscriptions of its creator (the payer), shown on the agent and in the Share member list.
- Private agents of the members cannot be @mentioned or delegated to from you, and they cannot reach you. Other agents of the same space can.

## Sharing and leaving (what to tell the user)

- Share: agent settings > Share, type a user id; the person accepts in the Agents tab. Any member may invite or revoke a pending invitation.
- Leave: the member goes, the agent stays with the others. Unshare: with several members each gets a private copy (automations paused, keys not copied, `USER.md` reduced to their section); with one member left the agent moves back.
- All members are equal; any may edit, unshare or delete the agent. You cannot do any of this yourself.
- Telegram: only the person talking gets the reply; the router and default agent include shared agents. Notifications go to every member. Members see chat changes instantly (server push, slow poll fallback); a dot marks pending invitations.
- `delete-user` takes a user out of their spaces first (shared agents stay with the other members, the payer moves). A user folder removed by hand makes their spaces unusable until `space remove-member` is run.
- `@id` counts anywhere (start or after a space; not in emails). The FIRST valid mention decides: an agent routes there (later mentions are plain text, one agent per message); a person (below) means later agent mentions do not run. The popup shows a notice instead of candidates after an agent mention.
- A message with `@<member id>` in a shared agent is for that person: stored with the author, the agent does not run or reply. An agent with the same id wins. Private agents: normal message.
- Admin commands (spaces, sharing): `references/admin-cli.md`.
