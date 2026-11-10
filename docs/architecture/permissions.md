# Permission levels (design; steps 1 to 5 implemented)

Status: **steps 1 (schema, validation, API read side), 2 (ceiling enforcement), 3 (the approval rule engine), 4 (approvals with memory) and 5 (the `permissions` tool group and the permission-manager template) are implemented**,
except the limited dream delegate (see [what is enforced](#what-is-enforced-step-2) and [approval rules](#approval-rules)). The UI is design. The steps are listed under [implementation plan](#implementation-plan). Research behind it: Claude Code permission rules, the Codex
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
- **"Always" rules are as narrow as possible** (the exact command, or the exact path of a file operation), never a prefix or a
  wildcard. A blanket allow on a real machine can only be set by an admin.
- **Unattended runs** (dreams, scheduled tasks) deny whatever would ask.
- **Equivalence with today:** `manual` = ask always; `smart` = ask only the dangerous list; `off` = allow all except the floor.

The command parser stays best effort. The boundaries are the level, the backend kind and the approval, not the parser.

### Implemented (step 3)

Code: `src/terminal/rules.py` (pure: `RuleSet`, `evaluate`, `blanket_allows`), `src/terminal/policy.py` (`command_units`),
`TerminalSession.decide` and the file tools (callers), `src/config/levels.py` (`check_approval_rules`, the guardrail).

- **Order.** Protected paths and hardline first (fixed floor, nothing below can change it, and the audit records `layer: floor`);
  then the rules; then the person if the verdict is `ask`.
- **Lists.** `approval_rules: {default: allow|ask|deny, rules: [...]}` in `settings.yaml` (installation), `user.yaml` (user) and the
  agent file (agent). A rule is `{verdict, tool, id, command, path, backend, backend_kind, dangerous, reason}` with unknown keys
  refused. `tool` is `terminal`, `file_read` (`sandbox_read`, `sandbox_search`), `file_write` (`sandbox_write`, `sandbox_patch`) or `any`.
- **Resolution.** Every matching rule of every layer is collected; deny beats ask beats allow (on a tie the audit names the most
  specific layer, then the first rule). With no match: the backend's legacy `approvals` if the agent file sets it, else the `default` of
  the most specific layer that defines one (agent, user, installation), else `ask`.
- **Commands** are split with the policy parser into segments (`&&`, `||`, `;`, `|`, `&`, newlines, subshells, `$(...)`, backticks,
  `bash -c`, `eval`); every segment is judged and the worst verdict wins. A redirection is a segment of its own (`> target`), so an
  allow rule for `cat` does not cover `cat a > b`. A segment that starts with a wrapper (`sudo`, `env`, `timeout`) does not match
  `ls`. `command` without glob characters is a word prefix; with `*`, `?` or `[` it is a full `fnmatch`. `path` is an `fnmatch` on the
  resolved target (relative paths hang from the workdir, `~` is kept as typed). `dangerous` follows `policy.assess` for the whole action.
- **Legacy shorthand.** A backend's `approvals` is the most specific default for that backend, only when the agent file sets it
  (absent: the layer defaults decide): `manual` = ask, `smart` = allow and ask for dangerous, `off` = allow. Matching rules apply
  on top (deny and ask from any layer beat its allow; an allow rule lifts `manual`). The web form always writes the key.
- **Unattended** (dreams, tasks, no client): `allow` runs, every `ask` becomes `deny`. `off` additionally asks (so denies) dangerous actions
  and file writes, as before.
- **Backend kind.** ssh backends have `kind: sandbox | machine` (default `machine`); `local` backends are `local`.
- **Guardrail.** A rule that allows without a `command`, `path` or `dangerous` condition, a `default: allow` that is the effective default,
  or the shorthand `off`, on a `machine` or `local` backend the agent uses, is a validation error unless the agent's effective level is
  `admin` (`ApprovalRuleNotAllowed`, raised by `check_agent_level`, so on load, create, update and `agent check`). On a `sandbox`
  backend it is allowed from operator.
- **Audit** (`terminal_audit.jsonl`): `verdict` stays the policy assessment (`normal`, `dangerous`, `hardline`, `protected`); new keys
  `layer` (`installation`, `user`, `agent`, `builtin`, `floor`), `rule` (`agent:2`, `user:<id>`, `approvals:smart`; absent for a layer
  default), `rule_verdict` (`allow`, `ask`, `deny`) and `approval_id` (the card that was answered). The card's `reason` names the rule.
- **Workers** keep `approvals` pinned to `manual` (or `smart` when unattended), and the rules of the agent still apply to them.

Not routed through the engine yet (backlog): destructive folder operations (`folder_delete`, `folder_move`, memory delete/move on
user folders) and non-read-only MCP tools. Today they never ask; routing them needs per-call plumbing and cards for actions with
no command or backend.


### Implemented (step 4): approvals with memory

Code: `src/terminal/rules.py` (`rules_for_action`, `rule_for_action`, `offered_scopes`, `action_signature`), `src/terminal/approvals.py`
(`ApprovalBroker.answer`, session grants), `TerminalSession.decide`, `src/config/agents.py` (`AgentStore.add_approval_rules`,
`remove_approval_rule`, `clear_approval_rules`), `ChatService.resolve_approval` and `learn_approval_rules`, the web card and the
Telegram buttons.

- **Four answers.** The card (web and Telegram) offers *Approve once*, *Always in this session*, *Always for this agent* and *Deny*.
  `session` is in memory only: it is keyed by chat id and the exact action (tool kind, backend and the full command or path), is
  shared by nobody else and ends with the process or when the chat is deleted. `agent` appends a narrow `allow` rule to the
  agent file's `approval_rules`, through `AgentStore` (the user's lock, full validation and `check_agent_level` again).
- **The rule is exact.** A terminal command gets one rule per segment (`command_units`), each with `exact: true` (the text must
  equal the segment, no word prefix, no glob) and the backend's name; a file operation gets its exact path, its tool kind
  (`file_read` / `file_write`) and the backend. Identical rules are never added twice. A command that looks like it holds a
  secret, or whose segment is longer than a rule may be, gets no `agent` option.
- **When "always" is offered.** Never for an action the policy assesses as `dangerous`; never when the verdict is not a plain `ask`
  (a `deny` is never asked); `agent` only when appending the rule would really allow the action (an `ask` rule of any layer would
  still win, so only `session` is offered then); and only on a plain chat run: a subagent task asks in its parent's chat and gets
  `once`. The fixed floor is checked before the rules, so no appended rule can lift it.
- **Guardrail.** If saving fails (the agent would no longer satisfy the blanket-allow guardrail, the file cannot be written, the
  audit trail is not writable...), the action is still approved **once** and the answer carries `error`; nothing is written.
- **Live pickup.** The running agent's rule set is updated at once, so the next call of the same reply is not asked again; other
  chats of the agent rebuild from the file before their next reply.
- **Audit.** Every approval record in `terminal_audit.jsonl` has `scope` (`once`, `session`, `agent`, or `deny` for a refusal; an
  action allowed by a session grant is `decision: approved:session`). Writing or removing an agent rule is its own line:
  `event: approval_rule`, `action: add|remove`, `actor: user` (`admin` from the CLI), `user`, `agent`, `rule`, and `index` / `via: reset` / `chat` where they apply.
- **Reset.** The agent's rules can be listed, removed one by one or all at once from the web API, the agent form and
  `python -m src.admin agent rules`; the layer `default` stays.
- **Saving the agent form keeps the rules.** A save that does not send `approval_rules` keeps the stored list.

Not in this step: rules for folder operations and MCP calls.

### Implemented (step 5): the `permissions` tool group

Code: `src/agent/toolsets/permissions.py` (the eight tools and the card), `src/service/agent_permissions.py` (checks, plan, apply, audit),
`AgentStore.modify` (read-modify-write under the user's lock), `src/config/levels_help.py` (the text of `permission_levels_help`),
`src/agent/templates.py` (`permission-manager`).

- **Group.** `permissions`, minimum level `operator` (`SERVICE_MIN_LEVEL`, `GROUP_MIN_LEVEL`, the web catalog, Spanish text). An agent
  below operator cannot hold it (validation, like any group); it is never a default and a worker cannot be granted it.
- **Tools.** Reads: `agents_list` (id, name, effective, required and declared level, groups, number of learned rules),
  `agent_get_permissions(agent_id)` (also the user's `max_level`, terminal backends with kind, approval rules per layer) and
  `permission_levels_help(capability="")` (text built from `ceiling.py`/`levels.py`). Changes: `agent_set_level`, `agent_set_tool_groups`,
  `agent_add_rule`, `agent_remove_rule`, `agent_reset_rules`. Results are JSON (names and rule texts are data) or `error: ...`.
- **Scope.** The user is the caller's own (no user argument) and the target must be in that user's `agents`; an id of another user,
  a path-like id or an unknown id gives the same refusal. A change never targets the caller itself (level, groups or rules).
- **Level.** `agent_set_level` needs level <= the user's `max_level`, <= the caller's effective level (the lower of the running level,
  so a worker's, and the stored one) and >= what the target's configuration requires. A caller whose stored level fell below operator,
  or who lost the group, is refused.
- **Groups.** Every group must be allowed by the target's current level (so an undeclared agent is never raised implicitly), a group
  that is added must also be allowed by the caller's level, and `terminal` needs the target's `terminal` block.
- **Rules.** The rule is validated like the API validates it (unknown keys, `default` and the reason reserved for learned rules are
  refused). The blanket-allow guardrail runs with the target's level (through `AgentStore.modify`, as on a form save) and again with
  the caller's: a caller below admin cannot grant a blanket allow on a `machine` or `local` backend even to an admin target.
- **Human approval.** Every change asks through the approval broker with a card: caller (name, id, level), target, field and the
  before -> after (rules are listed). The card offers `once` and *Deny* only: `ApprovalBroker.answer` refuses `session` and `agent`
  for it (`InputError`), no learn hook exists and nothing is remembered. Denied, timed out, cancelled, shut down, no chat client
  (dream, scheduled task, worker task: the tools are not built there and refuse if called) and a missing audit trail all end with
  `error: ...` and no change. After the approval the plan is computed again under the lock and must equal what was approved, so a
  change made meanwhile (or a lowered `max_level`) cancels it.
- **Audit.** One line per attempt in `terminal_audit.jsonl` of the target and of the caller: `event: permission_change`, `actor`
  (caller id), `actor_kind: agent`, `user`, `agent` (file owner), `target`, `tool`, `field`, `action`, `before`, `after`,
  `decision` (`approved`, `denied`, or `refused` for an attempt the rules blocked before anyone was asked; that one only goes to the
  caller's file), `reason`, `approval_id`, `chat`, `applied`, and `rule` / `index` / `rules` for rules. Rule commands are redacted.
- **Running targets.** The file changes at once and the target's other chats rebuild before their next reply (as for rules learned
  from a card); a reply already running keeps its old configuration until it ends.
- **Template.** `agent create --from-template permission-manager`: level `operator`, groups `memory_read`, `memory_edit`, `self`,
  `permissions` (no terminal, no web), a short SOUL. It fails with a clear message for a user whose `max_level` is below operator.

Decisions: a mutation on the caller itself is refused for every field (an agent adding rules or groups to itself is the same
escalation as raising its level); removing an `ask` or `deny` rule, which loosens the target, is allowed with a warning on the card.

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

Status: **implemented** (step 2B): `src/agent/permissions_prompt.py`, used by chats, workers and dream sessions.

## Configuration shape (implemented in step 1)

```yaml
# user.yaml, set only by an administrator
max_level: operator          # minimal | reader | assistant | collaborator | operator | admin

# agent.yaml
level: collaborator          # must not exceed the owner's max_level
```

`max_level` is **required** in `user.yaml` (a missing value is a load error that names `user set --set max_level=<level>`), is set only
through the admin CLI and the web API never exposes a way to change it. An agent's `level` is optional; its effective value is the
declared one, else the lowest level covering its configuration (`required_level`). The **config rule** holds when an agent is
loaded, created or updated (web, service, admin CLI): `required_level(agent) <= effective level <= max_level`, else the agent is
invalid and the error names the agent, the service that needs the higher level and the level it needs. The minimum level of each
service is the table in `src/config/levels.py`, taken from the table above. Details:
[user.yaml](../reference/user-yaml.md#max_level-permission-levels), [agent.yaml](../reference/agent-yaml.md#level),
[admin CLI](../user-guide/admin-cli.md#permission-levels).

Resources stay where they are today (`terminal_backends`, `folders`, MCP servers); a backend gains a `kind` (`sandbox` or
`machine`) and a privilege field. Level data must never live under `chat_config`, which accepts unknown keys.

## What is enforced (step 2)

One module, `src/config/ceiling.py`, derives from an effective level what is allowed; nothing else compares levels.

| Where | Effect |
| --- | --- |
| Chat tools (`build_tools`) | groups above the level are dropped, terminal backends are limited to their class (ssh without root = operator, `local` or a root ssh user = admin), delegate tools need collaborator and `agent` targets operator, and a final per-tool gate removes anything the level does not cover. `skill_view` is never gated (it is how skills are used). |
| MCP (`build_mcp_tools`, refresh each turn) | no MCP tool and no note below collaborator. |
| Dispatch (chats and dreams) | a call to a tool the level does not cover is refused with a tool error even if a handler exists; a name that is neither built in nor an MCP tool is refused as well. |
| Delegation | a worker's level is `min(target's own level, launcher's level)` (stored in the task mark); launching needs collaborator, handing a task to another agent needs the launcher at operator; groups, backends and delegation targets of a worker above its level are dropped. |
| Dreams | follow the [dream table](#dream-mode): memory read from reader; `memory_edit` and `self` from assistant (and the chat group); folders read-only from reader (and `dream.tools: [folders]`); web from assistant (and `dream.tools: [web]`); never a terminal, tasks or permissions. A dream of a `minimal` agent does not run (the session fails with a message). |
| Templates | `guide` needs the `local` backend, so `agent create --from-template guide` fails for a user whose `max_level` is below admin; `permission-manager` needs operator. |

Not built yet: the limited dream delegate, MCP read-only tools and read-only sandbox tools in dreams (the ceiling already reports
them, the dream toolset does not use them) and the individually chosen tools of L0.

## Known gaps, out of scope for the level work

| Gap | Status |
| --- | --- |
| Outbound network control (domain allowlist, container egress) | Not present. `web` and the terminal are exfiltration channels. Planned as a separate branch. |
| Outbound messages to third parties (messages, email, posts) | No such service yet; when added it is its own service, never below L4. |
| Real isolation between users (per-user path limit) | Behavioural only today; levels reinforce but do not replace it. |
| Budgets (tokens and spend per agent, allowed models) | Partial for delegated tasks. |
| Session revocation on demotion, login throttle, CSRF token | Not present. |
| Audit of every write (files, memory, self) | Only the terminal and agent-made permission changes are audited today. |

## Implementation plan

| Step | Content |
| --- | --- |
| 0 | This specification (done with this page). |
| 1 (done) | Schema: `max_level` (required, admin only), agent `level`, validation, effective levels. No migration code: the project is in development. |
| 2 (done, except the dream limited delegate and the prompt block) | Ceiling enforcement in one module (`src/config/ceiling.py`): when tools are built (chats, workers, dreams), at dispatch, on config save and on load. Still to do: the `permissions` prompt block and skill page, and the limited dream delegate. |
| 3 (done) | Rule engine (`src/terminal/rules.py`): layers, deny/ask/allow, matching, the three legacy modes as a per-backend shorthand, the blanket-allow guardrail, audit fields. Terminal and file tools only; see [implemented](#implemented-step-3). |
| 4 (done) | Approvals with memory: once / session / always for this agent, web and Telegram, audit. |
| 5 (done) | `permissions` tool group and a permission-manager agent template; escalation tests (`tests/test_permissions_tool_*.py`). |
| 6 | Web UI and admin CLI: level selector capped by `max_level`, rule editor, admin screen. |
| 7 | Final documentation, changelog and remaining-gaps list. |
