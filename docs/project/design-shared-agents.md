# Design: agents shared between users

Status: planned. Steps are built one branch at a time off `feat/shared-agents`; each step lists what is done.

## Goal

Two or more users use the same agent: same SOUL, memory, mind palace, skills, tools, backends and chats.
Every member sees every chat of the agent, approves its cards and may change it, within the permissions of
the most restricted member. The agent is isolated from each member's private agents.

## Model: a space is a pseudo-user

A **space** is a user record of kind `space` with a list of `members` (real user ids). It holds one or more
shared agents. For the data plane it is an ordinary user: config in `.config/<space_id>/user.yaml` and
`agents/<agent_id>.yaml`, data in `data/<space_id>/<agent_id>/`, automations in `data/<space_id>/.automations/`.
So paths, index, conversations, memory, sources, automations, approvals and locks need no change: they are
already keyed by `(user_id, agent_id)`.

- Space id: `space-<8 hex>`, generated; it never logs in (no `password_hash`, refused by login).
- `members`: at least one real user. One space per group of members: sharing a second agent with the same
  group reuses that space.
- `max_level` of a space is derived: the minimum of the members' `max_level`, recomputed on every load. The
  existing ceiling (`src/config/ceiling.py`) then caps every agent of the space; what lies above the cap is
  disabled, not deleted, as today.
- Provider, models, Claude login and keys: the space's `user.yaml` is filled from the creator's at creation
  (same variable names in the secrets file, same Claude login folder, see "Paying"). Any member may change
  them later in the space's settings.
- Isolation: private agents of a member cannot @mention, delegate to or list a space's agents, and the other
  way round. Mentions and delegation inside the same space work as they do between agents of one user.

## Paying

The space uses the creator's providers and keys (the user who created it or sent the first invitation).
The UI says so on the shared agent ("uses the keys and Claude subscription of user-a").
Claude login: the login folder must lie inside the owner's config folder (`own_login_dir`). A space gets a
`billing_user` field; Claude login and usage are resolved against that user's folder, never against an
arbitrary path.

## Access from the web and API

- `current_user` stays the person. Every route taking an `agent_id` resolves the effective owner: the user
  itself, or a space the user is a member of that holds the agent (`resolve_owner(user_id, agent_id)`).
  A non-member gets 404, as for another user's agent today.
- Agent ids must be unique across a user's private agents and the spaces it is in; a clash is refused at
  share and accept time (the UI suggests a rename).
- `/api/chats` and the agents list include the spaces' agents; agents tab gets a "Shared" section (like
  "Hidden"); chat list items of a space agent get a "shared" tag. `hidden` is a property of the shared agent,
  the same for every member.
- Sync between members uses what already syncs two tabs of one user: chat list polling, `followReply` on a
  running reply, per-chat approvals. Approval cards appear in every member's view of the chat; the first
  decision wins.

## Who is speaking

- Every user message to a space agent is stored with `author` (user id) in the conversation JSONL and sent to
  the model with a prefix `[<display name>] `. Name = the user's `name`, upper-cased.
- `USER.md` of a space agent has one section per member (`## <user id>`). Its char limit is the normal limit
  times the number of members, capped at 5x. The `memory` tool with target `user` edits the speaker's section;
  dreams keep the sections.

## Telegram and notifications

- Telegram: a member may pick a space agent in their own bot like any agent; the reply goes only to that
  member. The chat shows on the web for every member; it is not mirrored to other members' Telegram.
- Notifications (`notify_telegram`, automation results, bell): to every member by default. Later: a target
  list or a group chat id.

## Sharing, invitations and leaving

- Share an existing agent ("make shared"): from the agent settings, pick users to invite. On the first
  acceptance the agent moves from the owner to the space (see "Moving"). Invited users see the request
  in their agents tab (pending / accept / decline); the inviter can revoke it while pending.
- Invite to an already shared agent: any member may invite more users; the cap is recomputed on accept.
- Leave: a member leaves the space; nothing is deleted. The space lives while it holds shared agents and at
  least one member.
- Unshare an agent (any member): with one member left it moves back to that user; with more, each member gets a
  private copy and the agent is removed from the space. A space with no agents left is deleted.
- All members are equal: any of them may edit, unshare or delete the agent.

## Moving an agent between owners

One operation, used by share (user to space) and unshare (space to user, once per member):
- Precheck: no reply, task or process running for the agent; lock it for the move.
- Config `agents/<id>.yaml` and data `data/<owner>/<id>/` copied (unshare) or moved (share); avatar too.
- Automations of the agent rewritten to the new owner; copies made by unshare start paused.
- Index rebuilt (it stores absolute paths).
- Unshare copies: `USER.md` keeps only the receiving user's section; keys and MCP tokens are not copied (each
  copy uses its user's own provider config); level capped by the user's `max_level`; an id clash gets `-2`.
- Share: the owner's `USER.md` becomes the owner's section.
- Audit entry in each affected owner.

## Admin CLI and audit

- `python -m src.admin space list|show|create|add-member|remove-member|delete`.
- Audit entries of a space record the member who acted.
- Deleting a user removes them from their spaces (the space lives on while it has members).

## Steps

1. Space model and storage: `kind: space`, `members`, `billing_user`, derived `max_level`, login refused,
   `resolve_owner`, admin CLI. Tests.
2. Web access by membership, "Shared" section, "shared" tag, isolation of mentions and delegation.
3. Speaker: `author` in the JSONL, `[NAME]` prefix, `USER.md` sections and limit.
4. Move: share an existing agent into a space and unshare (copies), with prechecks and index rebuild.
5. Invitations: request, accept, decline, revoke, leave; UI.
6. Notifications to every member; Telegram per speaker.
7. Docs (`docs/architecture/agents-and-users.md`, permissions, user guide, built-in `recall` skill).

Before deploy: back up config and data, then test the release on a throwaway instance with two test users.
