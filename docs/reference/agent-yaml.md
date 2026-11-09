# agent.yaml reference

One file per agent: `.config/<user_id>/agents/<agent_id>.yaml`. The agent's personality is **not** here: it lives in
`data/<user_id>/<agent_id>/SOUL.md` (a missing file is an empty personality) with its memories and skills next to it, see
[dream mode](../user-guide/dream.md#per-agent-files). Agents have no `models` block (model ids belong to the user: see
[models and providers](../user-guide/models-and-providers.md)); unknown keys are rejected.

Defaults when a key is omitted: `stream: true`, `has_memory: true`, `reasoning: true`, `reasoning_effort: medium`,
`conversation_messages_retained: 30`, `tools: true`, `associative_memory: llm`, `associative_execution: parallel`,
`dream.enabled: false`, `recall_knowledge: true`. The values in the example below are illustrative.

## agent file

```yaml
# <user_id>/agents/<agent_id>.yaml
id: aria
name: Aria
description: Helps with notes and planning  # optional; shown in the web agent list, not sent to the model
chat_config:
  stream: true                        # stream tokens as they arrive
  has_memory: true                   # keep conversation history in context
  reasoning: true                    # request and separate thinking tokens
  reasoning_effort: medium           # minimal | low | medium | high | xhigh | max
  reasoning_tokens: 20               # optional reasoning budget
  model: null                        # optional default chat model, e.g. claude_subscription:sonnet (see models and providers)
  associative_memory: llm            # off | llm | question_only (see memory)
  associative_execution: parallel    # parallel | sequential
  keep_reasoning_in_memory: false    # store the thinking in context/history
  recall_knowledge: true             # the built-in, read-only `recall` skill (how RECALL works); false hides it from this agent
  conversation_messages_retained: 20 # at most N messages kept in context; cuts go below N (see prompt caching)
  tools: true                       # enable the selected tool groups
  tool_groups: [memory_read, memory_edit, self]  # memory_read, memory_edit, web, self, folders_read, folders_edit, terminal, delegate
  mcp:                               # MCP servers enabled for this agent (see mcp.md)
    google_calendar: {tools: null, read_only: false, instructions: ""}
dream:                               # background self-review sessions (see dream.md)
  enabled: false                     # runs the schedule; /dream start works either way
  every_hours: null                  # run every N hours, or
  daily_at: "04:00"                  # once a day at this local time; setting both is invalid
  model: null                        # null = the user's chat model
  reasoning_effort: medium
  max_steps: 60                      # model calls per session
  max_tokens: 500000                 # input + output tokens per session
  max_minutes: 5                     # wall-clock limit (1-120); wrap-up warning 1 minute before
  tools: [folders]                   # optional read groups for dreams: folders (default; also needs a folder read group), web (opt-in); `terminal` is rejected
  prompt: ""                         # empty = built-in default
  notify_telegram: false
folders:                             # user folders, each exposed as the source `<name>:` (see user folders)
  - name: notes                      # ^[a-z0-9_-]{1,32}$, unique, not a built-in source name
    path: /path/to/obsidian-vault    # must exist; never created
    indexed: true                    # feeds associative memory and query_memory (default true); un-indexing or removing the folder purges its chunks. A path that overlaps config, data, logs, the program folder, /proc, /sys, /dev, ~/.ssh or ~/.gnupg is rejected (see settings.yaml folders.allowed_roots)
    read_only: false                 # never writable, whatever the tool groups (default false)
terminal:                            # required by the `terminal` tool group (see the terminal guide)
  backends:                          # 1+ names from the user's `terminal_backends` in user.yaml, each with its own policy
    sandbox:
      workdir: ~/workspace           # default directory inside this backend (default ~ = its user's home)
      approvals: smart               # manual (asks for everything, default) | smart (asks for dangerous) | off; hardline always refused
      # timeout_seconds: 120         # optional per-backend override of the command timeout
    pc:
      workdir: ~
      approvals: manual
  default_backend: sandbox           # one of the keys above; required when more than one, used when a call names no backend
  timeout_seconds: 60                # default command timeout, 1-600 (capped by settings.yaml terminal.max_timeout_seconds)
  max_output_chars: 20000            # output returned to the model, 500-200000 (capped by settings.yaml)
delegation:                          # the `delegate` tool group (see the delegation guide); optional, defaults apply when the group is selected
  enabled: true                      # false removes `task_start` without removing the group
  allowed_groups: [web]              # groups a task may be granted; only `web` is grantable (never memory, folders, terminal, mcp, delegate)
  max_parallel: null                 # running tasks per chat, 1-10; can only lower settings.yaml subagents.max_parallel_per_parent (3)
  timeout_seconds: null              # wall clock per task, 10-3600; can only lower subagents.timeout_seconds (600)
  max_tokens: null                   # input + output tokens per task, 1000-5000000; can only lower subagents.max_tokens (200000)
  targets: []                        # other agents of this user a task may be handed to: [{agent: scout, remember: false}]; empty = fresh workers only
```

`recall_knowledge` (default `true`, Basic > Advanced in the Agents tab) adds one line to the skills index and lets `skill_view` read the
built-in `recall` skill that ships with the code; see [RECALL knowledge](../user-guide/recall-knowledge.md). It applies to new chats.

The `terminal` block is only used when `terminal` is in `tool_groups`. The model picks the backend per call (the five tools
take an optional `backend` argument limited to these names); approvals, working directory and timeout follow the backend it
picks. A backend that does not resolve (unknown name, `local` while `terminal.allow_local` is off) is disabled with a warning
in the log and the agent keeps the others; when none resolves, or the block is missing, the agent still loads with the
terminal tools not built. `terminal.backend`, `workdir` and `approvals` at the top level of the block no longer exist. Dreams have no terminal: `dream.tools: [terminal]` and `terminal.allow_in_dream` are rejected with a validation error. See the
[terminal guide](../user-guide/terminal.md).

<a id="delegation"></a>The `delegation` block belongs to the `delegate` tool group, which exposes `task_start` (phase 1: a synchronous worker in a hidden
chat). The group is opt-in; selecting it without the block saves the defaults above. Unknown keys are rejected, and `allowed_groups` entries other
than `web` are refused. The numbers can only lower the application's `subagents:` limits ([settings.yaml](settings-yaml.md#subagents)). See the
[delegation guide](../user-guide/delegation.md).

`delegation.targets` is a list of `{agent: <id>, remember: <bool>}` entries (default `[]`, at most 20, no duplicates, never the agent itself). Each `agent` must be
another agent of the same user: saving through the web app, the API or the admin CLI refuses unknown ones, `agent check` warns about them,
and renaming or deleting an agent rewrites (renames) or drops it in the other agents' lists. `task_start(agent=...)` only accepts a listed agent; the task runs
as that agent (its own SOUL and settings, its tool groups intersected with the grant) and the worker chat is stored under it. `remember: false` (default) keeps the
task excluded from learning with memory writes blocked; `remember: true` makes it a hidden but ordinary chat the target's Dream may review, and the target keeps
its own `memory_read`/`memory_edit`/`self` tools (not `soul_edit`/`skill_manage`). A `remember` task is refused while the target is replying elsewhere.

The size limits of `SOUL.md`, `MEMORY.md` and `USER.md` are not agent settings: they are application-wide and set in the
`memory_limits` block of [settings.yaml](settings-yaml.md#memory-limits). A `memory_limits` key left over in an older agent
file is ignored when loading and is not written again when the agent is saved.

the agent's personality lives only in `data/<user_id>/<agent_id>/SOUL.md` (a
missing file is an empty personality; the agent YAML has no personality field),
with its memories and skills next to it; see [dream mode](../user-guide/dream.md#per-agent-files) for the
layout and the prompt assembly. when `tool_groups` is omitted, the default is
`[memory_read, memory_edit, web, self]`; folder groups are always explicit.

## loading

`load_agent(user_id, agent_id?)` reads `user.yaml` and the agent file, resolves the provider key from the environment, and returns a `ResolvedAgent` (used to build the `Agent` and its `SourceRegistry`). if `agent_id` is omitted, the user's first agent is used.

checks today: the agent belongs to the user, and the provider key env var (if configured) is set.

planned: a `defaults.yaml < user.yaml < agent.yaml` merge, resource/folder
references, and a `ConfigStore` with a channel→user index for routing incoming
messages.

## mutability

`settings.yaml` is read at start-up (restart to change it). `user.yaml` and agent files are re-read on every request, and the
cached agent runtime of a chat is rebuilt on its next message when either file (or `.config/secrets.env`) changed on disk, whoever
changed it: the web API, the [admin CLI](../user-guide/admin-cli.md) in another process, or a hand edit. A file left invalid by a
hand edit does not stop the services: they keep the last good configuration, log the error and show a notice in the chat; the
agent list keeps working. Model favorites, per-chat selections and agent edits made through
the APIs above are persisted immediately, without restarting.
