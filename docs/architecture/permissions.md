# Permission levels (design, planned)

Status: **design only**. Nothing on this page is implemented yet; each section says what exists today. The work is split into the
steps listed under [implementation plan](#implementation-plan). Research behind it: Claude Code permission rules, the Codex
approval/sandbox split, Meta's *Agents Rule of Two*, OWASP LLM06 (excessive agency) and an audit of RECALL's own code.

## Principles

1. **A level is a preset.** It fixes a *ceiling* over independent services; it is not a magic ladder.
2. **Resources are addresses the user configures** (terminal backends, folders by path, MCP servers). A level only says which
   *classes* of resource an agent may use. One user's laptop is that user's backend; no other user's agent sees it.
3. **Ceiling per user, level per agent.** A user may give their agents any level up to the user's own `max_level`. An operator
   cannot create an admin agent. Only an administrator changes a user's `max_level`.
4. **Approvals are independent of the level**: layered rule lists with central defaults that each agent can refine.
5. **Enforcement lives in code, never in the model** (complete mediation). SOUL text is not a control.
6. **Rule of Two.** A level that combines untrusted input (web, attachments, MCP output), sensitive data and the power to act
   outwards is only valid with human approval and never unattended.

## Levels and services

`yes` = allowed, `ask` = allowed with approval according to the rules, `no` = not allowed. `terminal` includes `sandbox_read`,
`sandbox_write`, `sandbox_patch` and `sandbox_search`.

| Service | L0 Minimal | L1 Reader | L2 Assistant | L3 Collaborator | L4 Operator | L5 Admin |
| --- | --- | --- | --- | --- | --- | --- |
| individually chosen tools | 1-2 | no | no | no | no | no |
| `memory_read` (own mind palace) | no | yes | yes | yes | yes | yes |
| `folders_read` (the user's folder list) | no | yes | yes | yes | yes | yes |
| `memory_edit` | no | no | yes | yes | yes | yes |
| `self` (MEMORY, USER, SOUL, skills) | no | no | yes | yes | yes | yes |
| `web` | no | no | yes | yes | yes | yes |
| `folders_edit` | no | no | no | ask | ask | ask |
| MCP (servers the user allows the agent) | no | no | no | ask | ask | ask |
| `delegate`: workers limited to `web` | no | no | no | yes | yes | yes |
| `terminal` on a disposable sandbox backend | no | no | no | no | ask | ask |
| `terminal` on a real machine, unprivileged | no | no | no | no | ask (always) | ask |
| `terminal` on a real machine with sudo/root, and the `local` backend | no | no | no | no | no | ask |
| `delegate`: hand tasks to the user's other agents | no | no | no | no | yes | yes |
| `permissions`: view and adjust levels and rules of the same user's agents, up to the user's `max_level` | no | no | no | no | yes | yes |
| act on other users' agents, data and settings; admin CLI | no | no | no | no | no | yes |
| dreams / unattended runs | no | read only | see [dream table](#dream-mode) | see table | see table | see table |

Notes:

- Every user can edit their own agents in the web UI; `permissions` lets an *agent* do it for them, so a user below admin can
  have a bot that tunes the permissions of their other bots.
- An agent can only restrict another agent to its own level or lower, never above the user's `max_level`.
- A permission change made by an agent always needs human approval (it cannot be set to "always allow"), is written to the audit
  log with a diff, and can be reset to the defaults.
- The `local` backend runs commands as the RECALL OS user on the RECALL host. It is a host shell and belongs to L5 only. The
  setup guide template therefore needs a non-shell mechanism (see the plan) before it can be used below L5.

## Approval rules

They replace the fixed per-backend modes `manual | smart | off`.

| Layer (least to most specific) | Defined by | Holds |
| --- | --- | --- |
| Fixed floor: hardline and protected paths | code | Always refused in every mode and level; nobody can change it. |
| Installation defaults | admin (`settings.yaml`) | Base list of actions to allow, ask or deny. |
| User defaults | the user | Adjustments over the base, for all their agents. |
| Agent rules | the user, or an agent with `permissions` | What accumulates through "always allow for this agent". |

- **Verdict:** deny beats ask, ask beats allow (as in Claude Code). An action that matches no rule uses the list's default.
- **Matching:** commands (existing parser), path globs, MCP tools, web domains and the backend kind (sandbox or real machine).
- **When an `ask` rule matches** the user chooses *approve once*, *approve for this session*, *approve always for this agent*
  (adds a narrow rule to that agent) or *deny*.
- **"Always" rules are as narrow as possible** (the exact command or its prefix), never a wildcard. A blanket allow on a real
  machine can only be set by an admin.
- **Unattended runs** (dreams, scheduled tasks) deny whatever would ask.
- **Equivalence with today:** `manual` = ask always; `smart` = ask only the dangerous list; `off` = allow all except the floor.

The command parser stays best effort. The boundaries are the level, the backend kind and the approval, not the parser.

## Dream mode

Rule: **read as far as the level allows, plus write the agent's own files**: mind palace, SOUL, memory, USER and skills.
Nobody answers approvals in a dream, so every `ask` becomes `deny`.

| Service | In a dream | Condition |
| --- | --- | --- |
| `memory_read` | read | level >= L1 |
| `memory_edit` (own mind palace) | write | level >= L2 |
| `self`: MEMORY.md, USER.md, SOUL.md, skills | write | level >= L2; SOUL keeps its last versions |
| `folders_read` | read only | level >= L1, only folders assigned to the agent |
| `folders_edit` | no | |
| `web` | read (ceiling) | level >= L2; off by default |
| MCP | read-only tools (ceiling) | level >= L3; off by default |
| `terminal`: exec, `sandbox_write`, `sandbox_patch` | no | |
| `terminal`: `sandbox_read`, `sandbox_search` | read only | level >= L4, disposable sandbox backends only |
| `delegate`: limited workers | configurable, off by default | level >= L3; read-only workers (`memory_read`, `folders_read`, `web` if the dream has it), no terminal, no writes, no handoff; lower limits than a chat; set in the agent's `dream` block; results return to the dream, which does the writing |
| `delegate`: hand tasks to other agents | no | |
| `permissions`, other users, admin | no | |

Risk: a dream that reads the web or an MCP server and then writes its own SOUL or memory can persist a prompt injection.
Conversation and worker text reaches dreams marked as untrusted, and web and MCP are a ceiling, not a default.

## The `permissions` block in the system prompt

Every agent gets, next to SOUL, memory and USER, a short `permissions` block generated from its level: level name, what it can and
cannot do in one line each, and an explicit reminder that **even if it finds a way around a limit it must not use it**; if it needs
more it tells the user which level would be required. The block links to an internal page of the built-in `recall` skill
(`references/permissions.md`) with every level, the service table, the dream table and the approval rules, read only when needed.

## Configuration shape (proposal)

```yaml
# user.yaml, set only by an administrator
max_level: operator          # minimal | reader | assistant | collaborator | operator | admin

# agent.yaml
level: collaborator          # must not exceed the owner's max_level
```

Resources stay where they are today (`terminal_backends`, `folders`, MCP servers); a backend gains a `kind` (`sandbox` or
`machine`) and a privilege field. Level data must never live under `chat_config`, which accepts unknown keys.

## Known gaps, out of scope for the level work

| Gap | Status |
| --- | --- |
| Outbound network control (domain allowlist, container egress) | Not present. `web` and the terminal are exfiltration channels. Planned as a separate branch. |
| Outbound messages to third parties (messages, email, posts) | No such service yet; when added it is its own service, never below L4. |
| Real isolation between users (per-user path limit) | Behavioural only today; levels reinforce but do not replace it. |
| Budgets (tokens and spend per agent, allowed models) | Partial for delegated tasks. |
| Session revocation on demotion, login throttle, CSRF token | Not present. |
| Audit of every write (files, memory, self, permission changes) | Only the terminal is audited today. |

## Implementation plan

| Step | Content |
| --- | --- |
| 0 | This specification (done with this page). |
| 1 | Schema and migration: `max_level` (admin only), agent `level`, validation, a migration that gives existing agents the lowest level covering their current groups. |
| 2 | Ceiling enforcement in one module: when tools are built (chats, workers, dreams), at dispatch, on config save and on load; `permissions` prompt block and skill page. |
| 3 | Rule engine: layers, deny/ask/allow, matching, the three legacy modes as presets. |
| 4 | Approvals with memory: once / session / always for this agent, web and Telegram, audit. |
| 5 | `permissions` tool group and a permission-manager agent template; escalation tests. |
| 6 | Web UI and admin CLI: level selector capped by `max_level`, rule editor, admin screen. |
| 7 | Final documentation, changelog and remaining-gaps list. |
